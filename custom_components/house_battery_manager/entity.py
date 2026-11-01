"""Base entity for House Battery Manager."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .manager import BatteryManager


class HouseBatteryEntity(Entity):
    """Common behaviour: one device per config entry, push updates from the manager."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: BatteryManager, key: str) -> None:
        self._manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="House Battery Manager",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._manager.async_add_listener(self.async_write_ha_state))
