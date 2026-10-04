"""Actions (services) of the IMHD.sk Departures integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ImhdApi, ImhdError, resolve_section
from .const import (
    ATTR_CITY,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DIRECTION,
    ATTR_LIMIT,
    ATTR_LINE,
    ATTR_MIN_MINUTES,
    ATTR_QUERY,
    DOMAIN,
    MAX_DEPARTURES_LIMIT,
    SERVICE_FIND_STOPS,
    SERVICE_GET_DEPARTURES,
    SERVICE_REFRESH,
)
from .coordinator import ImhdConfigEntry, ImhdCoordinator, as_list
from .models import Departure, normalize_text

REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)

GET_DEPARTURES_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_LINE): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DIRECTION): cv.string,
        vol.Optional(ATTR_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_DEPARTURES_LIMIT)
        ),
        vol.Optional(ATTR_MIN_MINUTES, default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)

FIND_STOPS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CITY): cv.string,
        vol.Optional(CONF_LATITUDE): cv.latitude,
        vol.Optional(CONF_LONGITUDE): cv.longitude,
        vol.Optional(ATTR_QUERY): cv.string,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration actions."""

    async def _refresh(call: ServiceCall) -> None:
        for coordinator in _refresh_targets(hass, call):
            await coordinator.async_reconnect()

    async def _get_departures(call: ServiceCall) -> ServiceResponse:
        return _departures_response(_coordinator_for(hass, call), call)

    async def _find_stops(call: ServiceCall) -> ServiceResponse:
        return await _find_stops_response(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh, schema=REFRESH_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DEPARTURES,
        _get_departures,
        schema=GET_DEPARTURES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FIND_STOPS,
        _find_stops,
        schema=FIND_STOPS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _loaded_coordinator(hass: HomeAssistant, entry_id: str) -> ImhdCoordinator:
    """Return the coordinator of a loaded entry or raise a validation error."""
    entry: ImhdConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"name": entry.title},
        )
    return entry.runtime_data


def _entry_id_of_entity(hass: HomeAssistant, entity_id: str) -> str:
    """Return the config entry id owning an imhd entity."""
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None or reg_entry.platform != DOMAIN or not reg_entry.config_entry_id:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
            translation_placeholders={"entity_id": entity_id},
        )
    return reg_entry.config_entry_id


def _refresh_targets(hass: HomeAssistant, call: ServiceCall) -> list[ImhdCoordinator]:
    """Return the coordinators addressed by a refresh call (default: all)."""
    entry_ids = list(call.data.get(ATTR_CONFIG_ENTRY_ID, []))
    entry_ids += [_entry_id_of_entity(hass, e) for e in call.data.get(ATTR_ENTITY_ID, [])]
    if not entry_ids:
        return [
            entry.runtime_data
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
    return [_loaded_coordinator(hass, entry_id) for entry_id in dict.fromkeys(entry_ids)]


def _coordinator_for(hass: HomeAssistant, call: ServiceCall) -> ImhdCoordinator:
    """Return the coordinator addressed by `entity_id` or `config_entry_id`."""
    if entity_id := call.data.get(ATTR_ENTITY_ID):
        return _loaded_coordinator(hass, _entry_id_of_entity(hass, entity_id))
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        return _loaded_coordinator(hass, entry_id)
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="target_required")


def _departures_response(coordinator: ImhdCoordinator, call: ServiceCall) -> dict[str, Any]:
    """Build the get_departures response from fresh (filtered) departures."""
    lines = {line.casefold() for line in as_list(call.data.get(ATTR_LINE))}
    direction = normalize_text(call.data.get(ATTR_DIRECTION) or "")
    limit: int = call.data.get(ATTR_LIMIT) or coordinator.filter.max_departures

    def keep(departure: Departure) -> bool:
        return (not lines or departure.line.casefold() in lines) and (
            not direction or direction in departure.direction_text
        )

    # Only as many scheduled departures as the response holds are looked at.
    data = coordinator.build(keep=keep, min_minutes=call.data[ATTR_MIN_MINUTES], limit=limit)
    return {
        "stop": data.stop.as_dict(),
        "departures": [dep.as_dict() for dep in data.matching[:limit]],
    }


async def _find_stops_response(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Look up stops near a location or by name."""
    section = resolve_section(call.data[ATTR_CITY])
    if section is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_city",
            translation_placeholders={"city": call.data[ATTR_CITY]},
        )
    api = ImhdApi(async_get_clientsession(hass))
    try:
        if query := call.data.get(ATTR_QUERY):
            stops = await api.async_search_stops(section, query)
        else:
            stops = await api.async_nearest_stops(
                section,
                call.data.get(CONF_LATITUDE, hass.config.latitude),
                call.data.get(CONF_LONGITUDE, hass.config.longitude),
            )
    except ImhdError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="request_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    return {"stops": [stop.as_dict() for stop in stops]}
