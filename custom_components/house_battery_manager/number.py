"""Adjustable settings exposed as number entities."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HouseBatteryEntity
from .manager import BatteryManager, BatteryManagerConfigEntry


@dataclass(frozen=True, kw_only=True)
class HouseBatteryNumberDescription(NumberEntityDescription):
    """Number description with the name of the manager setting it controls."""

    setting: str


def _descriptions(manager: BatteryManager) -> list[HouseBatteryNumberDescription]:
    s = manager.settings
    power = {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": UnitOfPower.WATT,
        "mode": NumberMode.BOX,
    }
    soc = {
        "native_unit_of_measurement": PERCENTAGE,
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
    }
    fuse_max = 3 * s.voltage * s.fuse_current
    return [
        HouseBatteryNumberDescription(
            key="grid_target",
            setting="grid_target",
            native_min_value=-fuse_max,
            native_max_value=fuse_max,
            native_step=10,
            **power,
        ),
        HouseBatteryNumberDescription(
            key="force_charge_power",
            setting="force_charge_power",
            native_min_value=0,
            native_max_value=s.max_charge_power,
            native_step=50,
            **power,
        ),
        HouseBatteryNumberDescription(
            key="force_discharge_power",
            setting="force_discharge_power",
            native_min_value=0,
            native_max_value=s.max_discharge_power,
            native_step=50,
            **power,
        ),
        HouseBatteryNumberDescription(
            key="force_duration",
            setting="force_duration",
            native_min_value=0,
            native_max_value=1440,
            native_step=5,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.BOX,
        ),
        HouseBatteryNumberDescription(
            key="min_soc",
            setting="min_soc",
            entity_category=EntityCategory.CONFIG,
            **soc,
        ),
        HouseBatteryNumberDescription(
            key="charge_target_soc", setting="charge_target_soc", **soc
        ),
        HouseBatteryNumberDescription(
            key="discharge_target_soc", setting="discharge_target_soc", **soc
        ),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    async_add_entities(
        HouseBatteryNumber(manager, description) for description in _descriptions(manager)
    )


class HouseBatteryNumber(HouseBatteryEntity, RestoreNumber):
    """A setting of the battery manager, restored across restarts."""

    entity_description: HouseBatteryNumberDescription

    def __init__(self, manager: BatteryManager, description: HouseBatteryNumberDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    def _get(self) -> float:
        if self.entity_description.setting == "force_duration":
            return self._manager.force_duration
        return getattr(self._manager.settings, self.entity_description.setting)

    def _set(self, value: float) -> None:
        if self.entity_description.setting == "force_duration":
            self._manager.force_duration = value
        else:
            setattr(self._manager.settings, self.entity_description.setting, value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            desc = self.entity_description
            value = min(max(last.native_value, desc.native_min_value), desc.native_max_value)
            self._set(value)

    @property
    def native_value(self) -> float:
        return self._get()

    async def async_set_native_value(self, value: float) -> None:
        await self._manager.async_set_setting(self.entity_description.setting, value)
