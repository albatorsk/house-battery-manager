"""Setup suggestions for a battery managed by the APstorage Modbus integration.

See https://github.com/albatorsk/apstorage-ha
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_POWER_INVERT,
    CONF_BATTERY_SETPOINT,
    CONF_MAX_CHARGE,
    CONF_MAX_DISCHARGE,
    CONF_SOC,
)

APSTORAGE_DOMAIN = "apstorage"

# Config key -> (entity domain, Modbus register). APstorage uses the same sign
# convention as this integration: positive W = discharge.
_ENTITIES = {
    CONF_BATTERY_SETPOINT: ("number", 40183),  # Set Power
    CONF_BATTERY_POWER: ("sensor", 40117),  # Battery Power
    CONF_SOC: ("sensor", 40081),  # State of Charge
}
_LIMITS = {
    CONF_MAX_CHARGE: 40074,  # Max Charge Rate
    CONF_MAX_DISCHARGE: 40075,  # Max Discharge Rate
}


def suggested_config(hass: HomeAssistant) -> dict[str, Any]:
    """Return config values for the APstorage battery, if exactly one is set up."""
    entries = hass.config_entries.async_entries(APSTORAGE_DOMAIN)
    if len(entries) != 1:
        return {}
    entry_id = entries[0].entry_id
    registry = er.async_get(hass)

    def entity_id(domain: str, address: int) -> str | None:
        return registry.async_get_entity_id(
            domain, APSTORAGE_DOMAIN, f"apstorage_{entry_id}_{address}"
        )

    suggestions: dict[str, Any] = {}
    for key, (domain, address) in _ENTITIES.items():
        if found := entity_id(domain, address):
            suggestions[key] = found
    if not suggestions:
        return {}
    suggestions[CONF_BATTERY_POWER_INVERT] = False

    for key, address in _LIMITS.items():
        found = entity_id("sensor", address)
        state = hass.states.get(found) if found else None
        try:
            value = float(state.state)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            continue
        if value > 0:
            suggestions[key] = value
    return suggestions
