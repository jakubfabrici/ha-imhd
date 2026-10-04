"""Base entity for IMHD.sk Departures."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import ImhdCoordinator


def entity_unique_id(coordinator: ImhdCoordinator, key: str) -> str:
    """Return the unique id of an entity of a config entry."""
    entry = coordinator.config_entry
    return f"{entry.unique_id or entry.entry_id}_{key}"


class ImhdEntity(CoordinatorEntity[ImhdCoordinator]):
    """An entity belonging to one configured stop (device)."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    # "<domain>.{}", set by the platform classes.
    _entity_id_format: str

    def __init__(self, coordinator: ImhdCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        stop = coordinator.stop
        self._attr_unique_id = entity_unique_id(coordinator, key)
        # Language independent entity ids (<domain>.<name>_<key>) for templates;
        # the display name stays translated.
        self.entity_id = self._entity_id_format.format(f"{slugify(entry.title)}_{key}")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=stop.city_name,
            configuration_url=stop.board_url,
        )

    @property
    def available(self) -> bool:
        """Return True while the coordinator has trustworthy data."""
        return self.coordinator.available
