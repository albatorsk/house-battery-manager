"""Binary sensor that reports when a phase is above the fuse limit."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HouseBatteryEntity
from .manager import BatteryManager, BatteryManagerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([FuseOverloadSensor(entry.runtime_data)])


class FuseOverloadSensor(HouseBatteryEntity, BinarySensorEntity):
    """On while any phase is above fuse rating minus margin.

    The battery corrects this within a few seconds when it can. If it stays on,
    the battery is at its limit (or empty) and loads such as the EV charger
    must be reduced, e.g. by an automation.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, manager: BatteryManager) -> None:
        super().__init__(manager, "fuse_overload")

    @property
    def is_on(self) -> bool | None:
        decision = self._manager.decision
        if decision is None or decision.phase_headroom is None:
            return None
        return decision.phase_headroom < 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        decision = self._manager.decision
        if decision is None or decision.phase_headroom is None:
            return None
        return {"headroom_a": round(decision.phase_headroom, 2)}
