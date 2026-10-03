"""Diagnostics for IMHD.sk Departures."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .api import iter_platform_elements
from .coordinator import ImhdConfigEntry

# Rows per platform kept from the last raw payload.
RAW_ROWS = 5


def _trim_raw(payload: Any) -> list[dict[str, Any]]:
    """Return the last `tabs` payload with at most RAW_ROWS rows per platform."""
    trimmed: list[dict[str, Any]] = []
    for element in iter_platform_elements(payload):
        rows = element.get("tab")
        if isinstance(rows, list):
            trimmed.append({**element, "tab": rows[:RAW_ROWS], "tab_rows_total": len(rows)})
        else:
            trimmed.append({**element, "tab": repr(rows)[:200], "tab_rows_total": None})
    return trimmed


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ImhdConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (nothing sensitive is stored)."""
    coordinator = entry.runtime_data
    data = coordinator.build()
    return {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "source": entry.source,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "stop": coordinator.stop.as_dict(),
        "connection": {
            "connected": coordinator.connected,
            "available": coordinator.available,
            "has_data": coordinator.has_data,
            "rejected": coordinator.rejected,
            "sessions": coordinator.feed.sessions,
            "reconnects": coordinator.reconnects,
            "last_update": data.last_update.isoformat() if data.last_update else None,
            "last_message": (
                coordinator.last_message.isoformat() if coordinator.last_message else None
            ),
            "server_time_offset_s": coordinator.server_time_offset,
        },
        "departures": {
            "total_after_filters": len(data.matching),
            "published": [dep.as_dict() for dep in data.departures],
            "info": data.info,
            "known_vehicles": len(coordinator.vehicles),
        },
        "last_raw_payload": _trim_raw(coordinator.last_raw),
    }
