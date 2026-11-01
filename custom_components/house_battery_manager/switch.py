"""Switch controlling whether the battery may supply the EV charger."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_EV
from .entity import HouseBatteryEntity
from .manager import BatteryManager, BatteryManagerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    if manager.config.get(CONF_EV):
        async_add_entities([BatteryPowersEvSwitch(manager)])


class BatteryPowersEvSwitch(HouseBatteryEntity, SwitchEntity, RestoreEntity):
    """When off, Auto mode lets the EV charge from the grid instead of the battery."""

    def __init__(self, manager: BatteryManager) -> None:
        super().__init__(manager, "battery_powers_ev")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._manager.settings.battery_powers_ev = last.state == "on"

    @property
    def is_on(self) -> bool:
        return self._manager.settings.battery_powers_ev

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._manager.async_set_setting("battery_powers_ev", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._manager.async_set_setting("battery_powers_ev", False)
