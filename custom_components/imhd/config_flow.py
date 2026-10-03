"""Config, import, reconfigure and options flows for IMHD.sk Departures."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    LocationSelector,
    LocationSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import slugify

from .api import (
    ImhdApi,
    ImhdConnectionError,
    ImhdError,
    ImhdInvalidStopError,
    ImhdStopNotFoundError,
    parse_stop_input,
)
from .const import (
    CONF_CITY,
    CONF_DEPARTURE_SENSORS,
    CONF_DIRECTION,
    CONF_EXCLUDE_LINES,
    CONF_LEAVE_WINDOW,
    CONF_LINES,
    CONF_LOCATION,
    CONF_MAX_DEPARTURES,
    CONF_METHOD,
    CONF_PLATFORM_LABELS,
    CONF_PLATFORMS,
    CONF_QUERY,
    CONF_SECTION,
    CONF_STOP,
    CONF_STOP_CITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_WALKING_TIME,
    DATA_YAML_IMPORTED,
    DEFAULT_OPTIONS,
    DEFAULT_SECTION,
    DOMAIN,
    MAX_DEPARTURE_SENSORS,
    MAX_DEPARTURES_LIMIT,
    MAX_LEAVE_WINDOW,
    MAX_WALKING_TIME,
    METHOD_NEAREST,
    METHODS,
    SECTIONS,
)
from .coordinator import as_list
from .models import StopInfo, board_url, natural_key, normalize_text

_LOGGER = logging.getLogger(__name__)

CONF_STOP_CHOICE = "stop_choice"

_NUMBER_FIELDS: dict[str, tuple[int, int]] = {
    CONF_MAX_DEPARTURES: (1, MAX_DEPARTURES_LIMIT),
    CONF_WALKING_TIME: (0, MAX_WALKING_TIME),
    CONF_LEAVE_WINDOW: (0, MAX_LEAVE_WINDOW),
    CONF_DEPARTURE_SENSORS: (0, MAX_DEPARTURE_SENSORS),
}


def make_unique_id(section: str, stop_id: int, name: str) -> str:
    """Return the config entry unique id of a configured stop."""
    return f"{section}_{stop_id}_{slugify(name)}"


def entry_data(stop: StopInfo, name: str) -> dict[str, Any]:
    """Return the config entry data for a stop (incl. a cached description)."""
    return {
        CONF_SECTION: stop.section,
        CONF_STOP_ID: stop.stop_id,
        CONF_NAME: name,
        CONF_STOP_NAME: stop.name,
        CONF_STOP_CITY: stop.city,
        CONF_PLATFORM_LABELS: dict(stop.platform_labels),
    }


def clean_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize settings coming from a form or YAML into entry options."""
    options: dict[str, Any] = {
        CONF_PLATFORMS: as_list(user_input.get(CONF_PLATFORMS)),
        CONF_LINES: as_list(user_input.get(CONF_LINES)),
        CONF_EXCLUDE_LINES: as_list(user_input.get(CONF_EXCLUDE_LINES)),
        CONF_DIRECTION: as_list(user_input.get(CONF_DIRECTION), split=False),
    }
    for key in _NUMBER_FIELDS:
        options[key] = int(user_input.get(key, DEFAULT_OPTIONS[key]))
    return options


def settings_schema(platforms: list[str], *, with_name: bool) -> vol.Schema:
    """Return the schema of the settings / options form."""
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME)] = TextSelector()
    fields[vol.Optional(CONF_PLATFORMS)] = SelectSelector(
        SelectSelectorConfig(
            options=platforms,
            multiple=True,
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    for key in (CONF_LINES, CONF_EXCLUDE_LINES, CONF_DIRECTION):
        fields[vol.Optional(key)] = TextSelector(TextSelectorConfig(multiple=True))
    for key, (minimum, maximum) in _NUMBER_FIELDS.items():
        fields[vol.Required(key, default=DEFAULT_OPTIONS[key])] = NumberSelector(
            NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
        )
    return vol.Schema(fields)


def _city_options() -> list[SelectOptionDict]:
    return [
        SelectOptionDict(value=code, label=f"{city} ({code})")
        for code, city in sorted(SECTIONS.items(), key=lambda kv: normalize_text(kv[1]))
    ]


async def async_resolve_stop(api: ImhdApi, section: str, value: str | int) -> StopInfo:
    """Resolve a stop id, imhd.sk URL or exact stop name to a validated stop."""
    try:
        url_section, stop_id = parse_stop_input(value)
    except ImhdInvalidStopError:
        if isinstance(value, int) or str(value).startswith(("http:", "https:")):
            raise
        found = await api.async_find_stop_by_name(section, str(value))
        url_section, stop_id = found.section, found.stop_id
    return await api.async_get_stop(url_section or section, stop_id)


class ImhdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._section = DEFAULT_SECTION
        self._candidates: dict[str, StopInfo] = {}
        self._stop: StopInfo | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ImhdOptionsFlow:
        """Return the options flow."""
        return ImhdOptionsFlow()

    @property
    def _api(self) -> ImhdApi:
        return ImhdApi(async_get_clientsession(self.hass))

    # ------------------------------------------------------- choose stop

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a city and how to find the stop."""
        return await self._async_step_city("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the stop of an existing entry."""
        if user_input is None:
            self._section = self._get_reconfigure_entry().data[CONF_SECTION]
        return await self._async_step_city("reconfigure", user_input)

    async def _async_step_city(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._section = user_input[CONF_CITY]
            method = user_input[CONF_METHOD]
            if method != METHOD_NEAREST:
                return await getattr(self, f"async_step_{method}")()
            errors = await self._async_load_nearest(
                self.hass.config.latitude, self.hass.config.longitude
            )
            if not errors:
                return await self.async_step_pick_stop()
        schema = vol.Schema(
            {
                vol.Required(CONF_CITY, default=self._section): SelectSelector(
                    SelectSelectorConfig(options=_city_options(), mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_METHOD, default=METHOD_NEAREST): SelectSelector(
                    SelectSelectorConfig(
                        options=METHODS,
                        translation_key=CONF_METHOD,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def _async_load_nearest(self, latitude: float, longitude: float) -> dict[str, str]:
        try:
            stops = await self._api.async_nearest_stops(self._section, latitude, longitude)
        except ImhdError as err:
            _LOGGER.debug("Nearest stop lookup failed: %s", err)
            return {"base": "cannot_connect"}
        return self._set_candidates(stops)

    def _set_candidates(self, stops: list[StopInfo]) -> dict[str, str]:
        self._candidates = {str(stop.stop_id): stop for stop in stops}
        return {} if stops else {"base": "no_stops_found"}

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find stops near a location picked on a map."""
        errors: dict[str, str] = {}
        if user_input is not None:
            location = user_input[CONF_LOCATION]
            errors = await self._async_load_nearest(
                location[CONF_LATITUDE], location[CONF_LONGITUDE]
            )
            if not errors:
                return await self.async_step_pick_stop()
        default = {
            CONF_LATITUDE: self.hass.config.latitude,
            CONF_LONGITUDE: self.hass.config.longitude,
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_LOCATION, default=default): LocationSelector(
                    LocationSelectorConfig(radius=False)
                )
            }
        )
        return self.async_show_form(step_id="location", data_schema=schema, errors=errors)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Search stops by name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                stops = await self._api.async_search_stops(self._section, user_input[CONF_QUERY])
            except ImhdError as err:
                _LOGGER.debug("Stop search failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                errors = self._set_candidates(stops)
                if not errors:
                    return await self.async_step_pick_stop()
        schema = vol.Schema({vol.Required(CONF_QUERY): TextSelector()})
        return self.async_show_form(
            step_id="search",
            data_schema=schema,
            errors=errors,
            description_placeholders={"example": "Hodžovo nám."},
        )

    async def async_step_stop_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter a stop id or an imhd.sk departure board URL."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                section, stop_id = parse_stop_input(user_input[CONF_STOP])
                stop = await self._api.async_get_stop(section or self._section, stop_id)
            except ImhdError as err:
                errors["base"] = _error_key(err)
            else:
                return await self._async_stop_selected(stop)
        schema = vol.Schema({vol.Required(CONF_STOP): TextSelector()})
        return self.async_show_form(
            step_id="stop_id",
            data_schema=schema,
            errors=errors,
            description_placeholders={"example_url": board_url("ba", 83)},
        )

    async def async_step_pick_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the found stops."""
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = self._candidates[user_input[CONF_STOP_CHOICE]]
            try:
                stop = await self._api.async_get_stop(choice.section, choice.stop_id)
            except ImhdError as err:
                errors["base"] = _error_key(err)
            else:
                return await self._async_stop_selected(stop)
        options = [
            SelectOptionDict(value=key, label=stop.describe())
            for key, stop in self._candidates.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_STOP_CHOICE, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id="pick_stop", data_schema=schema, errors=errors)

    async def _async_stop_selected(self, stop: StopInfo) -> ConfigFlowResult:
        self._stop = stop
        if self.source == SOURCE_RECONFIGURE:
            return await self._async_finish_reconfigure(stop)
        return await self.async_step_settings()

    # ------------------------------------------------------- settings

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the stop and set the filters."""
        if (stop := self._stop) is None:
            return await self.async_step_user()
        if user_input is not None:
            name = str(user_input.get(CONF_NAME) or "").strip() or stop.name
            await self.async_set_unique_id(make_unique_id(stop.section, stop.stop_id, name))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=name, data=entry_data(stop, name), options=clean_options(user_input)
            )
        schema = self.add_suggested_values_to_schema(
            settings_schema(stop.platforms, with_name=True), {CONF_NAME: stop.name}
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            description_placeholders={"stop": stop.describe(), "url": stop.board_url},
        )

    # ------------------------------------------------------- reconfigure

    async def _async_finish_reconfigure(self, stop: StopInfo) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        name = entry.data.get(CONF_NAME) or entry.title
        unique_id = make_unique_id(stop.section, stop.stop_id, name)
        if unique_id != entry.unique_id:
            if any(
                other.unique_id == unique_id
                for other in self._async_current_entries(include_ignore=True)
                if other.entry_id != entry.entry_id
            ):
                return self.async_abort(reason="already_configured")
            await _async_migrate_entity_unique_ids(self.hass, entry, entry.unique_id, unique_id)
        options = dict(entry.options)
        if stop.stop_id != entry.data.get(CONF_STOP_ID):
            # Platform labels are stop specific.
            options[CONF_PLATFORMS] = []
        self.hass.config_entries.async_update_entry(
            entry,
            unique_id=unique_id,
            data={**entry.data, **entry_data(stop, name)},
            options=options,
        )
        return self.async_abort(reason="reconfigure_successful")

    # ------------------------------------------------------- YAML import

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create or update an entry from a YAML item."""
        section = import_data[CONF_CITY]
        options = clean_options(import_data)
        if (known := self._known_import(import_data)) is not None:
            # Unchanged stop with an explicit name: no need to ask imhd.sk again.
            entry, name = known
            return await self._async_finish_import(
                entry.unique_id or "", name, {**entry.data, CONF_NAME: name}, options
            )
        try:
            stop = await async_resolve_stop(self._api, section, import_data[CONF_STOP])
        except ImhdError as err:
            reason = _error_key(err)
            _LOGGER.error(
                "Cannot import imhd YAML stop %r (%s): %s - %s",
                import_data[CONF_STOP],
                section,
                reason,
                err,
            )
            return self.async_abort(reason=reason)
        name = str(import_data.get(CONF_NAME) or "").strip() or stop.name
        return await self._async_finish_import(
            make_unique_id(stop.section, stop.stop_id, name), name, entry_data(stop, name), options
        )

    def _known_import(self, import_data: dict[str, Any]) -> tuple[ConfigEntry, str] | None:
        """Return the existing entry of a YAML item given by id/URL and name."""
        if not (name := str(import_data.get(CONF_NAME) or "").strip()):
            return None
        try:
            section, stop_id = parse_stop_input(import_data[CONF_STOP])
        except ImhdInvalidStopError:
            return None
        unique_id = make_unique_id(section or import_data[CONF_CITY], stop_id, name)
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
        return (entry, name) if entry is not None else None

    async def _async_finish_import(
        self, unique_id: str, name: str, data: dict[str, Any], options: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the entry, or update the existing one (reloaded by its listener)."""
        existing = await self.async_set_unique_id(unique_id)
        self.hass.data.setdefault(DATA_YAML_IMPORTED, set()).add(unique_id)
        if existing is not None:
            if self.hass.config_entries.async_update_entry(
                existing, title=name, data=data, options=options
            ):
                _LOGGER.debug("Updated imhd entry %s from YAML", unique_id)
            return self.async_abort(reason="already_configured")
        _LOGGER.debug("Importing imhd stop %s from YAML", unique_id)
        return self.async_create_entry(title=name, data=data, options=options)


class ImhdOptionsFlow(OptionsFlow):
    """Edit the filters of a stop."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show / save the filter settings."""
        if user_input is not None:
            return self.async_create_entry(data=clean_options(user_input))
        labels = self.config_entry.data.get(CONF_PLATFORM_LABELS) or {}
        if (coordinator := getattr(self.config_entry, "runtime_data", None)) is not None:
            labels = coordinator.stop.platform_labels
        platforms = sorted(set(labels.values()), key=natural_key)
        schema = self.add_suggested_values_to_schema(
            settings_schema(platforms, with_name=False),
            {**DEFAULT_OPTIONS, **self.config_entry.options},
        )
        return self.async_show_form(step_id="init", data_schema=schema)


async def _async_migrate_entity_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, old: str | None, new: str
) -> None:
    """Keep entity ids when the entry unique id changes."""
    if not old:
        return

    @callback
    def _migrate(entity: er.RegistryEntry) -> dict[str, Any] | None:
        if entity.unique_id.startswith(f"{old}_"):
            return {"new_unique_id": f"{new}_{entity.unique_id[len(old) + 1 :]}"}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


def _error_key(err: ImhdError) -> str:
    """Map an API error to a translation key."""
    if isinstance(err, ImhdInvalidStopError):
        return "invalid_stop"
    if isinstance(err, ImhdStopNotFoundError):
        return "stop_not_found"
    if isinstance(err, ImhdConnectionError):
        return "cannot_connect"
    return "unknown"
