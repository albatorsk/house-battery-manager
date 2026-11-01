"""Diagnostic sensors showing what the battery manager is doing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HouseBatteryEntity
from .manager import BatteryManager, BatteryManagerConfigEntry


@dataclass(frozen=True, kw_only=True)
class HouseBatterySensorDescription(SensorEntityDescription):
    """Sensor description with a value getter."""

    value_fn: Callable[[BatteryManager], float | str | datetime | None]


def _headroom(manager: BatteryManager) -> float | None:
    if manager.decision is None or manager.decision.phase_headroom is None:
        return None
    return round(manager.decision.phase_headroom, 2)


SENSORS: tuple[HouseBatterySensorDescription, ...] = (
    HouseBatterySensorDescription(
        key="status",
        value_fn=lambda m: m.decision.status if m.decision else None,
    ),
    HouseBatterySensorDescription(
        key="battery_setpoint",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda m: m.last_setpoint,
    ),
    HouseBatterySensorDescription(
        key="grid_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda m: None if m.grid_power is None else round(m.grid_power),
    ),
    HouseBatterySensorDescription(
        key="fuse_headroom",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=_headroom,
    ),
    HouseBatterySensorDescription(
        key="force_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m: m.force_end,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    async_add_entities(HouseBatterySensor(manager, description) for description in SENSORS)


class HouseBatterySensor(HouseBatteryEntity, SensorEntity):
    """A read-only view of the manager state."""

    entity_description: HouseBatterySensorDescription

    def __init__(self, manager: BatteryManager, description: HouseBatterySensorDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | str | datetime | None:
        return self.entity_description.value_fn(self._manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        decision = self._manager.decision
        if self.entity_description.key != "status" or decision is None:
            return None
        attributes: dict[str, Any] = {
            "fuse_limited": decision.fuse_limited,
            "fuse_min_setpoint": _round(decision.fuse_min_setpoint),
            "fuse_max_setpoint": _round(decision.fuse_max_setpoint),
            "battery_power_used": _round(decision.details.get("battery_now")),
        }
        if (apstorage := self._manager.apstorage) is not None:
            attributes["battery_charge_status"] = apstorage.charge_status()
            attributes["battery_alarms"] = apstorage.alarms()
        return attributes


def _round(value: float | None) -> float | None:
    return None if value is None else round(value)
