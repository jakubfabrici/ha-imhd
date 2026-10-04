"""Binary sensors for IMHD.sk Departures."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData, RestoreEntity

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
            "departure": dep.expected.isoformat() if dep else None,
            # The integration's countdown: `departure` is cut to the minute.
            "minutes": dep.minutes if dep else None,
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
        """Return connection statistics (the last message time is in diagnostics)."""
        return {"reconnects": self.coordinator.reconnects}


class ImhdDisruptionSensor(ImhdBinarySensor, RestoreEntity):
    """On while imhd.sk publishes service alerts.

    After a restart or reload the info texts arrive after the departures: the
    last state of the same stop is kept until they are known, so an active alert
    does not turn off and on again (and notify twice).
    """

    _attr_translation_key = "disruption"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ImhdCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "disruption")
        self._restored_on: bool | None = None
        self._restored_messages: list[str] = []

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Remember the stop: a reconfigure (or a new entry) may reuse the entity id."""
        return RestoredExtraData({"stop_id": self.coordinator.stop.stop_id})

    async def async_added_to_hass(self) -> None:
        """Restore the last known state (used until the info texts are known)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        extra = await self.async_get_last_extra_data()
        same_stop = extra is not None and (
            extra.as_dict().get("stop_id") == self.coordinator.stop.stop_id
        )
        if same_stop and last is not None and last.state in (STATE_ON, STATE_OFF):
            self._restored_on = last.state == STATE_ON
            messages = last.attributes.get("messages")
            if isinstance(messages, list):
                self._restored_messages = [str(message) for message in messages]

    @property
    def is_on(self) -> bool | None:
        """Return True when info texts exist (None while unknown)."""
        data = self.coordinator.data
        return bool(data.info) if data.info_known else self._restored_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the messages."""
        data = self.coordinator.data
        return {"messages": data.info if data.info_known else self._restored_messages}
