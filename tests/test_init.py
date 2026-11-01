"""Integration tests running the component inside Home Assistant.

Requires: pip install pytest-homeassistant-custom-component
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.house_battery_manager.const import (
    CONF_BATTERY_SETPOINT,
    CONF_METER,
    CONF_METER_TYPE,
    CONF_PHASE_L1,
    CONF_PHASE_L2,
    CONF_PHASE_L3,
    DOMAIN,
    METER_TYPE_GRID,
)

SETPOINT = "input_number.battery_setpoint"
METER = "sensor.grid_power"
PHASES = {CONF_PHASE_L1: "sensor.l1", CONF_PHASE_L2: "sensor.l2", CONF_PHASE_L3: "sensor.l3"}


class FakeClock:
    """Replaces the manager's monotonic clock so throttling can be tested."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(freezer):
    fake = FakeClock()
    fake.freezer = freezer
    with patch("custom_components.house_battery_manager.manager.monotonic", fake):
        yield fake


async def _advance(hass: HomeAssistant, clock: FakeClock, seconds: float) -> None:
    """Move both clocks forward and fire the timers that are now due."""
    clock.now += seconds
    clock.freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _set_phases(hass: HomeAssistant, *amps: float) -> None:
    for i, value in enumerate(amps, start=1):
        hass.states.async_set(f"sensor.l{i}", str(value), {"unit_of_measurement": "A"})


def _setpoint(hass: HomeAssistant) -> float:
    return float(hass.states.get(SETPOINT).state)


async def _setup(hass: HomeAssistant, **extra) -> MockConfigEntry:
    assert await async_setup_component(
        hass,
        "input_number",
        {"input_number": {"battery_setpoint": {"min": -10000, "max": 10000, "step": 1}}},
    )
    hass.states.async_set(METER, "1200", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="House Battery",
        data={
            "name": "House Battery",
            CONF_BATTERY_SETPOINT: SETPOINT,
            CONF_METER: METER,
            CONF_METER_TYPE: METER_TYPE_GRID,
            "gain": 1.0,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_auto_mode_discharges_to_cover_import(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert _setpoint(hass) == 1200
    assert hass.states.get("select.house_battery_mode").state == "auto"
    assert hass.states.get("sensor.house_battery_grid_power").state == "1200"


async def test_force_charge_service_respects_fuse(hass: HomeAssistant, clock) -> None:
    _set_phases(hass, 16, 5, 5)
    await _setup(hass, **PHASES)
    # The first auto run discharged 1200 W; pretend the meter has caught up.
    hass.states.async_set(METER, "0", {"unit_of_measurement": "W"})
    await hass.services.async_call(
        DOMAIN,
        "force_charge",
        {"entity_id": "select.house_battery_mode", "power": 5000, "duration": 30},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("select.house_battery_mode").state == "force_charge"
    # Less than 3 s since the last command: nothing sent yet.
    assert _setpoint(hass) == 1200

    await _advance(hass, clock, 3)
    # 3 A headroom on L1 relative to the assumed 1200 W discharge: 1200 - 3*230*3.
    assert _setpoint(hass) == 1200 - 2070
    assert hass.states.get("sensor.house_battery_force_ends").state != "unknown"

    await hass.services.async_call(
        DOMAIN, "cancel_force", {"entity_id": "select.house_battery_mode"}, blocking=True
    )
    assert hass.states.get("select.house_battery_mode").state == "auto"


async def test_commands_at_least_3_seconds_apart(hass: HomeAssistant, clock) -> None:
    sent: list[float] = []

    def _record(event: Event) -> None:
        if event.data.get("domain") == "input_number":
            sent.append(clock.now)

    hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    await _setup(hass)

    # A rapidly changing meter, updated every 0.5 s for 15 s.
    for step in range(30):
        hass.states.async_set(METER, str(500 + (step % 2) * 1000), {"unit_of_measurement": "W"})
        await _advance(hass, clock, 0.5)

    assert len(sent) >= 4
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert min(gaps) >= 3


async def test_overload_sensor_and_discharge(hass: HomeAssistant, clock) -> None:
    _set_phases(hass, 5, 5, 5)
    await _setup(hass, **PHASES)
    assert hass.states.get("binary_sensor.house_battery_fuse_overload").state == "off"

    # EV starts on L1 (no load balancing in the charger): 21 A, limit is 19 A.
    hass.states.async_set(METER, "7000", {"unit_of_measurement": "W"})
    _set_phases(hass, 21, 5, 5)
    await _advance(hass, clock, 3)
    assert hass.states.get("binary_sensor.house_battery_fuse_overload").state == "on"
    # Battery discharges at least enough to bring L1 down to 19 A (2 A * 690 V).
    assert _setpoint(hass) >= 1200 + 1380


async def test_fuse_overload_bypasses_command_interval(hass: HomeAssistant, clock) -> None:
    _set_phases(hass, 5, 5, 5)
    await _setup(hass, **PHASES, gain=0.1)
    first = _setpoint(hass)

    # 1 s later the EV starts on L1: 22 A is above the 20 A fuse rating.
    _set_phases(hass, 22, 5, 5)
    await _advance(hass, clock, 1)
    # Sent immediately, without waiting for the 3 s interval: >= 3 A * 690 V more.
    assert _setpoint(hass) >= first + 2070


async def test_margin_overload_still_waits(hass: HomeAssistant, clock) -> None:
    _set_phases(hass, 5, 5, 5)
    await _setup(hass, **PHASES, gain=0.1)
    first = _setpoint(hass)

    # 19.5 A: above the 19 A limit but below the fuse rating, so no rush.
    _set_phases(hass, 19.5, 5, 5)
    await _advance(hass, clock, 1)
    assert _setpoint(hass) == first
    await _advance(hass, clock, 2)
    assert _setpoint(hass) > first


async def test_unavailable_meter_sets_zero(hass: HomeAssistant, clock) -> None:
    await _setup(hass)
    hass.states.async_set(METER, "unavailable")
    await _advance(hass, clock, 3)
    assert _setpoint(hass) == 0


async def test_unload_sets_zero(hass: HomeAssistant, clock) -> None:
    entry = await _setup(hass)
    clock.now += 3
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _setpoint(hass) == 0


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Battery"}
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BATTERY_SETPOINT: "number.battery", CONF_METER: METER},
    )
    assert result["step_id"] == "limits"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHASE_L1: "sensor.l1"}
    )
    assert result["errors"] == {"base": "phases_incomplete"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "tuning"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"]["fuse_current"] == 20.0
    assert result["data"]["min_command_interval"] == 3
