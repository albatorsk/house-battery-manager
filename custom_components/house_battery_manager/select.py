"""Mode select and force charge/discharge services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DURATION,
    ATTR_POWER,
    ATTR_TARGET_SOC,
    SERVICE_CANCEL_FORCE,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
)
from .controller import Mode
from .entity import HouseBatteryEntity
from .manager import FORCE_MODES, BatteryManagerConfigEntry

FORCE_SCHEMA = {
    vol.Optional(ATTR_POWER): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional(ATTR_TARGET_SOC): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ModeSelect(entry.runtime_data)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_FORCE_CHARGE, FORCE_SCHEMA, "async_force_charge"
    )
    platform.async_register_entity_service(
        SERVICE_FORCE_DISCHARGE, FORCE_SCHEMA, "async_force_discharge"
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL_FORCE, cv.make_entity_service_schema({}), "async_cancel_force"
    )


class ModeSelect(HouseBatteryEntity, SelectEntity, RestoreEntity):
    """Selects how the battery is controlled."""

    _attr_options = [mode.value for mode in Mode]

    def __init__(self, manager) -> None:
        super().__init__(manager, "mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state not in self._attr_options:
            return
        mode = Mode(last.state)
        if mode in FORCE_MODES:
            end = dt_util.parse_datetime(last.attributes.get("force_end") or "")
            before = last.attributes.get("mode_before_force", Mode.AUTO)
            before = Mode(before) if before in self._attr_options else Mode.AUTO
            self._manager.restore_force(mode, end, before)
        else:
            self._manager.settings.mode = mode

    @property
    def current_option(self) -> str:
        return self._manager.settings.mode.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        return {
            "force_end": manager.force_end.isoformat() if manager.force_end else None,
            "mode_before_force": manager.mode_before_force.value,
        }

    async def async_select_option(self, option: str) -> None:
        await self._manager.async_set_mode(Mode(option))

    async def async_force_charge(
        self,
        power: float | None = None,
        duration: float | None = None,
        target_soc: float | None = None,
    ) -> None:
        settings = self._manager.settings
        if power is not None:
            settings.force_charge_power = min(power, settings.max_charge_power)
        if target_soc is not None:
            settings.charge_target_soc = target_soc
        await self._manager.async_set_mode(Mode.FORCE_CHARGE, duration)

    async def async_force_discharge(
        self,
        power: float | None = None,
        duration: float | None = None,
        target_soc: float | None = None,
    ) -> None:
        settings = self._manager.settings
        if power is not None:
            settings.force_discharge_power = min(power, settings.max_discharge_power)
        if target_soc is not None:
            settings.discharge_target_soc = target_soc
        await self._manager.async_set_mode(Mode.FORCE_DISCHARGE, duration)

    async def async_cancel_force(self) -> None:
        await self._manager.async_end_force()
