"""Integration tests running the component inside Home Assistant.

Requires: pip install pytest-homeassistant-custom-component
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
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


async def test_custom_command_interval(hass: HomeAssistant, clock) -> None:
    await _setup(hass, min_command_interval=5)
    assert _setpoint(hass) == 1200

    hass.states.async_set(METER, "2200", {"unit_of_measurement": "W"})
    await _advance(hass, clock, 3)
    # The default 3 s has passed, but this entry is set to 5 s.
    assert _setpoint(hass) == 1200
    await _advance(hass, clock, 2)
    assert _setpoint(hass) == 3400


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


async def test_battery_power_read_before_last_write_is_ignored(
    hass: HomeAssistant, clock
) -> None:
    # A slowly polled battery power sensor (APstorage polls every 60 s).
    hass.states.async_set("sensor.battery_power", "0", {"unit_of_measurement": "W"})
    await _setup(hass, battery_power_entity="sensor.battery_power")
    assert _setpoint(hass) == 1200

    # The meter has not caught up yet; the battery sensor still says 0 W.
    hass.states.async_set(METER, "1300", {"unit_of_measurement": "W"})
    await _advance(hass, clock, 3)
    # Based on the 1200 W just sent, not on the outdated 0 W reading.
    assert _setpoint(hass) == 2500

    # A fresh battery reading, a second after the write, is used again.
    await _advance(hass, clock, 1)
    hass.states.async_set("sensor.battery_power", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set(METER, "100", {"unit_of_measurement": "W"})
    await _advance(hass, clock, 2)
    assert _setpoint(hass) == 2100


async def test_setpoint_rounded_to_entity_step(hass: HomeAssistant, clock) -> None:
    hass.states.async_set(METER, "1234.4", {"unit_of_measurement": "W"})
    assert await async_setup_component(
        hass,
        "input_number",
        {"input_number": {"battery_setpoint": {"min": -10000, "max": 10000, "step": 10}}},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BATTERY_SETPOINT: SETPOINT, CONF_METER: METER, "gain": 1.0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _setpoint(hass) == 1230


APS_SETPOINT = "number.aps_123_set_power"


class FakeAPstorageCoordinator:
    """Stands in for the APstorage coordinator API (version 1)."""

    api_version = 1

    def __init__(self) -> None:
        self.data: dict[int, dict] = {
            40086: {"value": "HOLDING"},
            40096: {"value": 0},
            40100: {"value": 0},
        }
        self.heartbeat_stalled = False
        self.fail = False
        self.writes: list[tuple[int, float]] = []

    async def async_write_value(self, address: int, value: float) -> bool:
        if self.fail:
            raise HomeAssistantError("Modbus timeout")
        self.writes.append((address, value))
        return True


def _add_apstorage(hass: HomeAssistant, coordinator=None) -> str:
    """Register an APstorage battery device with its entities; return the device ID."""
    apstorage = MockConfigEntry(domain="apstorage", entry_id="aps1")
    apstorage.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id="aps1", identifiers={("apstorage", "aps1")}
    )
    registry = er.async_get(hass)
    for domain, address, object_id in (
        ("number", 40183, "aps_123_set_power"),
        ("sensor", 40117, "aps_123_battery_power"),
        ("sensor", 40081, "aps_123_state_of_charge_soc"),
        ("sensor", 40074, "aps_123_max_charge_rate"),
    ):
        registry.async_get_or_create(
            domain,
            "apstorage",
            f"apstorage_aps1_{address}",
            suggested_object_id=object_id,
            config_entry=apstorage,
            device_id=device.id,
        )
    hass.states.async_set("sensor.aps_123_max_charge_rate", "6000")
    hass.states.async_set(APS_SETPOINT, "0", {"min": -6000, "max": 6000, "step": 1})
    if coordinator is not None:
        hass.data.setdefault("apstorage", {})["aps1"] = {"coordinator": coordinator}
    return device.id


async def _setup_apstorage(
    hass: HomeAssistant, coordinator: FakeAPstorageCoordinator, **extra
) -> MockConfigEntry:
    device_id = _add_apstorage(hass, coordinator)
    hass.states.async_set(METER, "1200", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="House Battery",
        data={
            "name": "House Battery",
            CONF_BATTERY_SETPOINT: APS_SETPOINT,
            CONF_METER: METER,
            "gain": 1.0,
            "apstorage_device": device_id,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _sensor(hass: HomeAssistant, name: str) -> str:
    return hass.states.get(f"sensor.house_battery_{name}").state


async def test_config_flow_suggests_apstorage_entities(hass: HomeAssistant) -> None:
    device_id = _add_apstorage(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Battery"}
    )
    assert result["step_id"] == "battery"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    # The only APstorage battery is pre-selected.
    assert suggested["apstorage_device"] == device_id
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"apstorage_device": device_id}
    )

    assert result["step_id"] == "entities"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested[CONF_BATTERY_SETPOINT] == APS_SETPOINT
    assert suggested["battery_power_entity"] == "sensor.aps_123_battery_power"
    assert suggested["soc_entity"] == "sensor.aps_123_state_of_charge_soc"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BATTERY_SETPOINT: APS_SETPOINT, CONF_METER: METER},
    )
    limits = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert limits["max_charge_power"] == 6000
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"]["apstorage_device"] == device_id
    assert result["data"]["sync_reserve_soc"] is False


async def test_config_flow_without_apstorage_battery(hass: HomeAssistant) -> None:
    _add_apstorage(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Battery"}
    )
    # Clearing the battery means no APstorage entities are suggested.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "entities"
    assert not any(
        key.description and "suggested_value" in key.description
        for key in result["data_schema"].schema
        if str(key) == CONF_BATTERY_SETPOINT
    )


async def test_apstorage_setpoint_written_directly(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    await _setup_apstorage(hass, coordinator)
    assert coordinator.writes == [(40183, 1200)]
    assert _sensor(hass, "battery_setpoint") == "1200.0"
    status = hass.states.get("sensor.house_battery_status")
    assert status.attributes["battery_charge_status"] == "HOLDING"
    assert status.attributes["battery_alarms"] == []


async def test_apstorage_failed_write_is_not_counted(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    coordinator.fail = True
    await _setup_apstorage(hass, coordinator)
    # The battery never got 1200 W, so it is not treated as its power.
    assert _sensor(hass, "battery_setpoint") == "0.0"

    coordinator.fail = False
    await _advance(hass, clock, 5)
    assert coordinator.writes == [(40183, 1200)]
    assert _sensor(hass, "battery_setpoint") == "1200.0"


async def test_apstorage_alarm_sets_zero(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    await _setup_apstorage(hass, coordinator)
    assert coordinator.writes == [(40183, 1200)]

    coordinator.data[40096] = {"value": 1 << 1}  # over temperature
    await _advance(hass, clock, 5)
    assert coordinator.writes[-1] == (40183, 0)
    status = hass.states.get("sensor.house_battery_status")
    assert "over temperature" in status.state
    assert status.attributes["battery_alarms"] == ["over temperature"]
    # The selected mode is kept, so regulation resumes when the alarm clears.
    assert hass.states.get("select.house_battery_mode").state == "auto"

    coordinator.data[40096] = {"value": 0}
    await _advance(hass, clock, 5)
    assert coordinator.writes[-1] == (40183, 1200)


async def test_apstorage_grid_voltage_warning_is_not_a_fault(
    hass: HomeAssistant, clock
) -> None:
    coordinator = FakeAPstorageCoordinator()
    coordinator.data[40100] = {"value": 1 << 3}  # phase B voltage out of range
    await _setup_apstorage(hass, coordinator)
    assert coordinator.writes == [(40183, 1200)]
    status = hass.states.get("sensor.house_battery_status")
    assert status.attributes["battery_alarms"] == ["PCS alarm bit 3"]


async def test_apstorage_stalled_heartbeat_sets_zero(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    coordinator.heartbeat_stalled = True
    await _setup_apstorage(hass, coordinator)
    assert coordinator.writes == [(40183, 0)]
    assert "heartbeat" in _sensor(hass, "status")


async def test_apstorage_reserve_sync(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    await _setup_apstorage(hass, coordinator, sync_reserve_soc=True)
    assert coordinator.writes == [(40183, 1200)]

    # Sent on the next run that has no setpoint to send.
    hass.states.async_set(METER, "0", {"unit_of_measurement": "W"})
    await _advance(hass, clock, 5)
    assert coordinator.writes[-1] == (40080, 10.0)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.house_battery_reserve_soc", "value": 20},
        blocking=True,
    )
    # Commands stay at least 3 s apart, reserve writes included.
    assert coordinator.writes[-1] == (40080, 10.0)
    await _advance(hass, clock, 3)
    assert coordinator.writes[-1] == (40080, 20.0)


async def test_reserve_not_synced_by_default(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    await _setup_apstorage(hass, coordinator)
    await _advance(hass, clock, 5)
    assert all(address == 40183 for address, _ in coordinator.writes)


async def test_unavailable_setpoint_entity_is_not_counted(
    hass: HomeAssistant, clock
) -> None:
    sent: list[float] = []

    def _record(event: Event) -> None:
        if event.data.get("domain") == "input_number":
            sent.append(clock.now)

    hass.bus.async_listen(EVENT_CALL_SERVICE, _record)
    await _setup(hass)
    assert len(sent) == 1

    hass.states.async_set(SETPOINT, "unavailable")
    hass.states.async_set(METER, "500", {"unit_of_measurement": "W"})
    await _advance(hass, clock, 3)
    assert len(sent) == 1
    assert _sensor(hass, "battery_setpoint") == "1200.0"


async def test_options_flow_keeps_apstorage_battery(hass: HomeAssistant, clock) -> None:
    coordinator = FakeAPstorageCoordinator()
    entry = await _setup_apstorage(hass, coordinator)
    device_id = entry.data["apstorage_device"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "battery"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"apstorage_device": device_id, "sync_reserve_soc": True}
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BATTERY_SETPOINT: APS_SETPOINT, CONF_METER: METER}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options["apstorage_device"] == device_id
    assert entry.options["sync_reserve_soc"] is True
