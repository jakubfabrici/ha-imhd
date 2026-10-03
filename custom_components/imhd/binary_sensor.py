"""Binary sensors for IMHD.sk Departures."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_LEAVE_WINDOW, DEFAULT_LEAVE_WINDOW
from .coordinator import ImhdConfigEntry, ImhdCoordinator
from .entity import ImhdEntity
from .models import Departure

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ImhdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of a stop."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ImhdTimeToLeaveSensor(coordinator),
            ImhdConnectedSensor(coordinator),
            ImhdDisruptionSensor(coordinator),
        ]
    )


class ImhdBinarySensor(ImhdEntity, BinarySensorEntity):
    """Base class of the binary sensors."""

    _entity_id_format = ENTITY_ID_FORMAT


class ImhdTimeToLeaveSensor(ImhdBinarySensor):
    """On when it is time to walk to the stop for the next catchable departure."""

    _attr_translation_key = "time_to_leave"

    def __init__(self, coordinator: ImhdCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "time_to_leave")
        self._window = int(
            coordinator.config_entry.options.get(CONF_LEAVE_WINDOW, DEFAULT_LEAVE_WINDOW)
        )

    @property
    def _departure(self) -> Departure | None:
        return next((dep for dep in self.coordinator.data.departures if dep.leave_in >= 0), None)

    @property
    def is_on(self) -> bool:
        """Return True inside the leave window."""
        dep = self._departure
        return dep is not None and 0 <= dep.leave_in <= self._window

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the departure the sensor is about."""
        dep = self._departure
        return {
            "line": dep.line if dep else None,
            "destination": dep.destination if dep else None,
            "departure": dep.departure.isoformat() if dep else None,
            "leave_in": dep.leave_in if dep else None,
        }


class ImhdConnectedSensor(ImhdBinarySensor):
    """Realtime feed connection state."""

    _attr_translation_key = "realtime_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ImhdCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "realtime_connected")

    @property
    def available(self) -> bool:
        """Always available: it reports the connection itself."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while connected to imhd.sk."""
        return self.coordinator.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection statistics."""
        last = self.coordinator.last_update
        return {
            "last_message": last.isoformat() if last else None,
            "reconnects": self.coordinator.reconnects,
        }


class ImhdDisruptionSensor(ImhdBinarySensor):
    """On while imhd.sk publishes service alerts."""

    _attr_translation_key = "disruption"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ImhdCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "disruption")

    @property
    def is_on(self) -> bool:
        """Return True when info texts exist."""
        return bool(self.coordinator.data.info)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the messages."""
        return {"messages": self.coordinator.data.info}
