"""Sensors for IMHD.sk Departures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import Platform, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_DEPARTURE_SENSORS, DEFAULT_DEPARTURE_SENSORS, DOMAIN, MAX_DEPARTURE_SENSORS
from .coordinator import ImhdConfigEntry, ImhdCoordinator
from .entity import ImhdEntity, entity_unique_id
from .models import Departure, StopData

PARALLEL_UPDATES = 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _main_attributes(coordinator: ImhdCoordinator, data: StopData) -> dict[str, Any]:
    """Return the attributes of the main (template friendly) sensor."""
    stop = data.stop
    nxt = data.next
    return {
        "stop_name": stop.name,
        "stop_id": stop.stop_id,
        "city": stop.city_name,
        "section": stop.section,
        "platforms": coordinator.platforms,
        "departures": [dep.as_dict() for dep in data.departures],
        "next_line": nxt.line if nxt else None,
        "next_destination": nxt.destination if nxt else None,
        "next_time": nxt.as_dict()["time"] if nxt else None,
        "next_departure": _iso(nxt.departure) if nxt else None,
        "next_minutes": nxt.minutes if nxt else None,
        "next_delay": nxt.delay if nxt else None,
        "next_realtime": nxt.realtime if nxt else None,
        "next_platform": nxt.platform if nxt else None,
        "lines": data.lines,
        "departure_count": len(data.departures),
        "info": data.info,
        "connected": data.connected,
        "last_update": _iso(data.last_update),
    }


def _next_attributes(dep: Departure | None) -> dict[str, Any]:
    """Return the short attribute set of the next departure."""
    if dep is None:
        return {}
    full = dep.as_dict()
    keys = ("line", "destination", "time", "departure", "minutes", "delay", "platform")
    return {key: full[key] for key in keys}


@dataclass(frozen=True, kw_only=True)
class ImhdSensorDescription(SensorEntityDescription):
    """Describes an IMHD sensor."""

    value_fn: Callable[[StopData], StateType | datetime]
    attrs_fn: Callable[[ImhdCoordinator, StopData], dict[str, Any]] = lambda _c, data: (
        _next_attributes(data.next)
    )


SENSORS: tuple[ImhdSensorDescription, ...] = (
    ImhdSensorDescription(
        key="departures",
        translation_key="departures",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda data: data.next.minutes if data.next else None,
        attrs_fn=_main_attributes,
    ),
    ImhdSensorDescription(
        key="next_departure",
        translation_key="next_departure",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.next.departure if data.next else None,
    ),
    ImhdSensorDescription(
        key="next_line",
        translation_key="next_line",
        value_fn=lambda data: data.next.line if data.next else None,
    ),
    ImhdSensorDescription(
        key="delay",
        translation_key="delay",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda data: data.next.delay if data.next else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ImhdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of a stop."""
    coordinator = entry.runtime_data
    count = int(entry.options.get(CONF_DEPARTURE_SENSORS, DEFAULT_DEPARTURE_SENSORS))
    _remove_surplus_departure_sensors(hass, coordinator, count)
    entities: list[SensorEntity] = [ImhdSensor(coordinator, description) for description in SENSORS]
    entities.extend(ImhdDepartureSensor(coordinator, n) for n in range(1, count + 1))
    async_add_entities(entities)


@callback
def _remove_surplus_departure_sensors(
    hass: HomeAssistant, coordinator: ImhdCoordinator, count: int
) -> None:
    """Drop per-row sensors that are no longer configured."""
    registry = er.async_get(hass)
    for number in range(count + 1, MAX_DEPARTURE_SENSORS + 1):
        unique_id = entity_unique_id(coordinator, f"departure_{number}")
        if entity_id := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id):
            registry.async_remove(entity_id)


class ImhdSensor(ImhdEntity, SensorEntity):
    """A sensor describing the next departures of a stop."""

    entity_description: ImhdSensorDescription
    _entity_id_format = ENTITY_ID_FORMAT
    _unrecorded_attributes = frozenset({"departures", "info"})

    def __init__(self, coordinator: ImhdCoordinator, description: ImhdSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.entity_description.attrs_fn(self.coordinator, self.coordinator.data)


class ImhdDepartureSensor(ImhdEntity, SensorEntity):
    """Minutes until the N-th departure (attributes: the full departure)."""

    _attr_translation_key = "departure"
    _entity_id_format = ENTITY_ID_FORMAT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ImhdCoordinator, number: int) -> None:
        """Initialize the sensor for departure `number` (1-based)."""
        super().__init__(coordinator, f"departure_{number}")
        self._index = number - 1
        self._attr_translation_placeholders = {"number": str(number)}

    @property
    def _departure(self) -> Departure | None:
        # The full filtered list: departure_N may go beyond max_departures.
        departures = self.coordinator.data.matching
        return departures[self._index] if self._index < len(departures) else None

    @property
    def native_value(self) -> int | None:
        """Return minutes until the departure."""
        return dep.minutes if (dep := self._departure) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all fields of the departure."""
        return dep.as_dict() if (dep := self._departure) else {}
