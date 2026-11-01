"""House Battery Manager: keeps grid exchange near zero within the fuse limits."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .manager import BatteryManager, BatteryManagerConfigEntry

PLATFORMS = [
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: BatteryManagerConfigEntry) -> bool:
    """Set up House Battery Manager from a config entry."""
    manager = BatteryManager(hass, entry)
    entry.runtime_data = manager
    # Entities restore their previous values while being added, so start the
    # control loop only after all platforms are set up.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BatteryManagerConfigEntry) -> bool:
    """Unload a config entry and leave the battery at 0 W."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: BatteryManagerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
