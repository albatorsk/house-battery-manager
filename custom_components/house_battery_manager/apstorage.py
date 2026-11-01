"""Support for a battery managed by the APstorage Modbus integration.

See https://github.com/albatorsk/apstorage-ha

Entities are found through the APstorage device chosen during setup. When
the APstorage integration offers its coordinator API (version 1 or later),
setpoints are written straight to its Modbus client, which waits for the
write and reports failures. Older versions are used through their entities.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_POWER_INVERT,
    CONF_BATTERY_SETPOINT,
    CONF_MAX_CHARGE,
    CONF_MAX_DISCHARGE,
    CONF_SOC,
)

_LOGGER = logging.getLogger(__name__)

APSTORAGE_DOMAIN = "apstorage"

# Modbus registers (see the APstorage register table).
REG_MAX_CHARGE = 40074
REG_MAX_DISCHARGE = 40075
REG_SOC_RESERVE_MIN = 40080
REG_SOC = 40081
REG_CHARGE_STATUS = 40086
REG_BATTERY_EVENTS = 40096
REG_PCS_ALARMS = 40100
REG_BATTERY_POWER = 40117
REG_SET_POWER = 40183

# Config key -> (entity domain, register). APstorage uses the same sign
# convention as this integration: positive W = discharge.
_ENTITIES = {
    CONF_BATTERY_SETPOINT: ("number", REG_SET_POWER),
    CONF_BATTERY_POWER: ("sensor", REG_BATTERY_POWER),
    CONF_SOC: ("sensor", REG_SOC),
}
_LIMITS = {
    CONF_MAX_CHARGE: REG_MAX_CHARGE,
    CONF_MAX_DISCHARGE: REG_MAX_DISCHARGE,
}

# Alarm bits that mean the battery cannot be trusted to follow commands.
BATTERY_FAULT_BITS = {
    0: "communication error",
    1: "over temperature",
    3: "under temperature",
    5: "over charge current",
    7: "over discharge current",
    9: "over voltage",
    11: "under voltage",
    22: "ground fault",
}
PCS_FAULT_BITS = {0: "PCS communication error"}
# The other PCS bits (1-24) report grid voltage out of range. The PCS reacts
# to those itself, so they are shown but do not stop the battery.

# Minimum coordinator API version for direct writes and heartbeat checks.
_API_VERSION = 1


def apstorage_device_ids(hass: HomeAssistant) -> list[str]:
    """Return the device IDs of all APstorage batteries."""
    registry = dr.async_get(hass)
    devices: list[str] = []
    for entry in hass.config_entries.async_entries(APSTORAGE_DOMAIN):
        devices.extend(
            device.id for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        )
    return devices


def device_entities(hass: HomeAssistant, device_id: str | None) -> dict[tuple[str, int], str]:
    """Return {(entity domain, register): entity_id} for an APstorage device."""
    if not device_id:
        return {}
    found: dict[tuple[str, int], str] = {}
    for entity in er.async_entries_for_device(
        er.async_get(hass), device_id, include_disabled_entities=True
    ):
        if entity.platform != APSTORAGE_DOMAIN:
            continue
        # APstorage unique IDs end with the register address.
        suffix = entity.unique_id.rsplit("_", 1)[-1]
        if suffix.isdigit():
            found[(entity.domain, int(suffix))] = entity.entity_id
    return found


def suggested_config(hass: HomeAssistant, device_id: str | None = None) -> dict[str, Any]:
    """Return config values for an APstorage battery.

    Without ``device_id``, the battery is used if exactly one is set up.
    """
    if not device_id:
        devices = apstorage_device_ids(hass)
        if len(devices) != 1:
            return {}
        device_id = devices[0]
    entities = device_entities(hass, device_id)

    suggestions: dict[str, Any] = {}
    for key, register in _ENTITIES.items():
        if found := entities.get(register):
            suggestions[key] = found
    if not suggestions:
        return {}
    suggestions[CONF_BATTERY_POWER_INVERT] = False

    for key, address in _LIMITS.items():
        found = entities.get(("sensor", address))
        state = hass.states.get(found) if found else None
        try:
            value = float(state.state)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            continue
        if value > 0:
            suggestions[key] = value
    return suggestions


class APstorageLink:
    """Runtime access to the APstorage battery chosen during setup."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        self.hass = hass
        self.device_id = device_id

    def _entry_id(self) -> str | None:
        device = dr.async_get(self.hass).async_get(self.device_id)
        if device is None:
            return None
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == APSTORAGE_DOMAIN:
                return entry_id
        return None

    def coordinator(self) -> Any | None:
        """Return the APstorage coordinator, if the integration is loaded."""
        entry_id = self._entry_id()
        if entry_id is None:
            return None
        return self.hass.data.get(APSTORAGE_DOMAIN, {}).get(entry_id, {}).get("coordinator")

    def _api(self) -> Any | None:
        coordinator = self.coordinator()
        if coordinator is None or getattr(coordinator, "api_version", 0) < _API_VERSION:
            return None
        return coordinator

    def _value(self, address: int) -> Any:
        coordinator = self.coordinator()
        data = getattr(coordinator, "data", None) or {}
        return data.get(address, {}).get("value")

    def entity_id(self, domain: str, address: int) -> str | None:
        """Return the entity ID of one of the battery's registers."""
        return device_entities(self.hass, self.device_id).get((domain, address))

    # ------------------------------------------------------------------
    # Writing

    def writes_directly(self, setpoint_entity: str) -> bool:
        """True if setpoints for ``setpoint_entity`` can bypass the entity."""
        return (
            self._api() is not None
            and self.entity_id("number", REG_SET_POWER) == setpoint_entity
        )

    async def async_write_power(self, value: float) -> bool:
        """Write the setpoint and wait for the Modbus write.

        Returns False if a newer value (for example from the UI) replaced it.
        Raises HomeAssistantError if the write failed.
        """
        coordinator = self._api()
        if coordinator is None:
            raise RuntimeError("APstorage coordinator API is not available")
        return await coordinator.async_write_value(REG_SET_POWER, value)

    async def async_write_reserve(self, soc: float) -> bool:
        """Set the battery's own minimum SoC reserve (register 40080)."""
        coordinator = self._api()
        if coordinator is not None:
            return await coordinator.async_write_value(REG_SOC_RESERVE_MIN, soc)
        entity_id = self.entity_id("number", REG_SOC_RESERVE_MIN)
        if entity_id is None:
            raise RuntimeError(
                "This APstorage version has no writable SoC Reserve Min entity"
            )
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": soc}, blocking=True
        )
        return True

    # ------------------------------------------------------------------
    # Health

    def charge_status(self) -> str | None:
        """Return the battery's charge status (CHARGING, HOLDING, ...)."""
        value = self._value(REG_CHARGE_STATUS)
        return value if isinstance(value, str) else None

    def alarms(self) -> list[str]:
        """Return every active alarm bit, faults and warnings alike."""
        active: list[str] = []
        battery = self._value(REG_BATTERY_EVENTS)
        if isinstance(battery, int):
            active.extend(
                name for bit, name in BATTERY_FAULT_BITS.items() if battery & (1 << bit)
            )
        pcs = self._value(REG_PCS_ALARMS)
        if isinstance(pcs, int):
            active.extend(
                PCS_FAULT_BITS.get(bit, f"PCS alarm bit {bit}")
                for bit in range(25)
                if pcs & (1 << bit)
            )
        return active

    def fault(self) -> str | None:
        """Return why the battery cannot be trusted right now, or None."""
        coordinator = self.coordinator()
        if coordinator is None:
            return None
        faults: list[str] = []
        battery = self._value(REG_BATTERY_EVENTS)
        if isinstance(battery, int):
            faults.extend(
                name for bit, name in BATTERY_FAULT_BITS.items() if battery & (1 << bit)
            )
        pcs = self._value(REG_PCS_ALARMS)
        if isinstance(pcs, int):
            faults.extend(name for bit, name in PCS_FAULT_BITS.items() if pcs & (1 << bit))
        if getattr(coordinator, "heartbeat_stalled", False):
            faults.append("controller heartbeat stopped")
        return ", ".join(faults) or None
