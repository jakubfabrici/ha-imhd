"""The IMHD.sk Departures integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ImhdApi, ImhdError, resolve_section
from .const import (
    CONF_CITY,
    CONF_DEPARTURE_SENSORS,
    CONF_DIRECTION,
    CONF_EXCLUDE_LINES,
    CONF_LEAVE_WINDOW,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORM_LABELS,
    CONF_PLATFORMS,
    CONF_SECTION,
    CONF_STOP,
    CONF_STOP_CITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_WALKING_TIME,
    DATA_YAML_IMPORTED,
    DEFAULT_DEPARTURE_SENSORS,
    DEFAULT_LEAVE_WINDOW,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_WALKING_TIME,
    DOMAIN,
    FIRST_DATA_TIMEOUT,
    ISSUE_REJECTED,
    ISSUE_YAML_REMOVED,
    MAX_DEPARTURE_SENSORS,
    MAX_DEPARTURES_LIMIT,
    MAX_LEAVE_WINDOW,
    MAX_WALKING_TIME,
)
from .coordinator import ImhdConfigEntry, ImhdCoordinator, as_list
from .models import StopInfo
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


def _section(value: Any) -> str:
    """Validate a section code or city name."""
    if section := resolve_section(str(value)):
        return section
    raise vol.Invalid(f"unknown imhd.sk city / section: {value}")


def _text_list(value: Any) -> list[str]:
    """Accept a list or a single string (commas are kept)."""
    return as_list(value, split=False)


def _bounded(minimum: int, maximum: int) -> vol.All:
    return vol.All(vol.Coerce(int), vol.Range(min=minimum, max=maximum))


STOP_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(CONF_CITY): _section,
        vol.Required(CONF_STOP): vol.Any(cv.positive_int, cv.string),
        vol.Optional(CONF_PLATFORMS, default=[]): as_list,
        vol.Optional(CONF_LINES, default=[]): as_list,
        vol.Optional(CONF_EXCLUDE_LINES, default=[]): as_list,
        vol.Optional(CONF_DIRECTION, default=[]): _text_list,
        vol.Optional(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _bounded(
            1, MAX_DEPARTURES_LIMIT
        ),
        vol.Optional(CONF_WALKING_TIME, default=DEFAULT_WALKING_TIME): _bounded(
            0, MAX_WALKING_TIME
        ),
        vol.Optional(CONF_LEAVE_WINDOW, default=DEFAULT_LEAVE_WINDOW): _bounded(
            0, MAX_LEAVE_WINDOW
        ),
        vol.Optional(CONF_DEPARTURE_SENSORS, default=DEFAULT_DEPARTURE_SENSORS): _bounded(
            0, MAX_DEPARTURE_SENSORS
        ),
    }
)

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.All(cv.ensure_list, [STOP_SCHEMA])}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services and import YAML configured stops."""
    async_setup_services(hass)
    items: list[dict[str, Any]] = config.get(DOMAIN, [])
    hass.async_create_task(_async_import_yaml(hass, items))
    return True


async def _async_import_yaml(hass: HomeAssistant, items: list[dict[str, Any]]) -> None:
    """Import YAML items and flag imported entries that left the YAML."""
    imported: set[str] = set()
    hass.data[DATA_YAML_IMPORTED] = imported
    results = await asyncio.gather(
        *(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(item)
            )
            for item in items
        )
    )
    if any(
        result["type"] is not FlowResultType.CREATE_ENTRY
        and result.get("reason") != "already_configured"
        for result in results
    ):
        _LOGGER.debug("Some YAML stops could not be imported; skipping cleanup check")
        return
    for entry in hass.config_entries.async_entries(DOMAIN):
        issue_id = f"{ISSUE_YAML_REMOVED}_{entry.entry_id}"
        if entry.source != SOURCE_IMPORT or entry.unique_id in imported:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_YAML_REMOVED,
            translation_placeholders={"name": entry.title},
        )


def _cached_stop(entry: ImhdConfigEntry) -> StopInfo | None:
    """Return the stop description saved in the entry (if any)."""
    if not (name := entry.data.get(CONF_STOP_NAME)):
        return None
    return StopInfo(
        stop_id=int(entry.data[CONF_STOP_ID]),
        name=name,
        section=entry.data[CONF_SECTION],
        city=entry.data.get(CONF_STOP_CITY),
        platform_labels=dict(entry.data.get(CONF_PLATFORM_LABELS) or {}),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ImhdConfigEntry) -> bool:
    """Set up one stop from a config entry."""
    # Registered first: a YAML import may update the entry while it sets up.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    api = ImhdApi(async_get_clientsession(hass))
    section, stop_id = entry.data[CONF_SECTION], int(entry.data[CONF_STOP_ID])
    try:
        stop = await api.async_get_stop(section, stop_id)
    except ImhdError as err:
        if (stop := _cached_stop(entry)) is None:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_fetch_stop",
                translation_placeholders={"stop_id": str(stop_id), "error": str(err)},
            ) from err
        _LOGGER.debug("Using cached stop info for %s/%s: %s", section, stop_id, err)

    coordinator = ImhdCoordinator(hass, entry, api, stop)
    entry.runtime_data = coordinator
    coordinator.async_start()
    # A refused connection does not fail the setup: the feed retries with a long
    # back-off and a repairs issue explains it (no HA retry loop hammering imhd.sk).
    await coordinator.async_wait_first_result(FIRST_DATA_TIMEOUT)

    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ImhdConfigEntry) -> None:
    """Reload the entry after options / data changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ImhdConfigEntry) -> bool:
    """Unload a config entry (the coordinator shuts the feed down)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ImhdConfigEntry) -> None:
    """Clean up repairs issues of a removed entry."""
    for issue in (ISSUE_REJECTED, ISSUE_YAML_REMOVED):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
