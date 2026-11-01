"""Runtime that connects the control logic to Home Assistant entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_POWER_INVERT,
    CONF_BATTERY_SETPOINT,
    CONF_COMMAND_INTERVAL,
    CONF_DEADBAND,
    CONF_EV,
    CONF_EV_IN_METER,
    CONF_FUSE_CURRENT,
    CONF_FUSE_MARGIN,
    CONF_GAIN,
    CONF_MAX_CHARGE,
    CONF_MAX_DISCHARGE,
    CONF_METER,
    CONF_METER_INVERT,
    CONF_METER_TYPE,
    CONF_MIN_WRITE_CHANGE,
    CONF_PHASE_L1,
    CONF_PHASE_L2,
    CONF_PHASE_L3,
    CONF_PHASE_UNIT,
    CONF_PV,
    CONF_SOC,
    CONF_STALE_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    CONF_VOLTAGE,
    DEFAULT_COMMAND_INTERVAL,
    DEFAULT_DEADBAND,
    DEFAULT_FUSE_CURRENT,
    DEFAULT_FUSE_MARGIN,
    DEFAULT_GAIN,
    DEFAULT_MAX_CHARGE,
    DEFAULT_MAX_DISCHARGE,
    DEFAULT_MIN_WRITE_CHANGE,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VOLTAGE,
    METER_TYPE_CONSUMPTION,
    PHASE_UNIT_POWER,
)
from .controller import Decision, Measurements, Mode, Settings, compute

_LOGGER = logging.getLogger(__name__)

# Re-send the setpoint at least this often, in case the battery has a
# communication watchdog or something else changed the value.
KEEPALIVE_SECONDS = 60
# Minimum time between two control runs triggered by sensor updates.
MIN_RUN_SPACING = 1.0
# When a phase is above the fuse rating, commands may be sent this soon after
# the previous one instead of waiting for the normal command interval.
EMERGENCY_COMMAND_SPACING = 1.0

FORCE_MODES = (Mode.FORCE_CHARGE, Mode.FORCE_DISCHARGE)

type BatteryManagerConfigEntry = ConfigEntry[BatteryManager]


class BatteryManager:
    """Reads sensors, runs the controller and writes the battery setpoint."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # The options flow stores a complete configuration, so it replaces data.
        self.config: dict[str, Any] = dict(entry.options or entry.data)
        cfg = self.config
        self.settings = Settings(
            max_charge_power=float(cfg.get(CONF_MAX_CHARGE, DEFAULT_MAX_CHARGE)),
            max_discharge_power=float(cfg.get(CONF_MAX_DISCHARGE, DEFAULT_MAX_DISCHARGE)),
            voltage=float(cfg.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)),
            fuse_current=float(cfg.get(CONF_FUSE_CURRENT, DEFAULT_FUSE_CURRENT)),
            fuse_margin=float(cfg.get(CONF_FUSE_MARGIN, DEFAULT_FUSE_MARGIN)),
            gain=float(cfg.get(CONF_GAIN, DEFAULT_GAIN)),
            deadband=float(cfg.get(CONF_DEADBAND, DEFAULT_DEADBAND)),
        )
        self.force_duration: float = 60.0  # minutes, 0 = until cancelled
        self.force_end: datetime | None = None
        self.mode_before_force: Mode = Mode.AUTO

        self.last_setpoint: float = 0.0
        self.last_written: float | None = None
        self.last_write_time: float = float("-inf")
        # The battery's Modbus server is easily overloaded: never send commands
        # more often than this.
        self._command_interval = float(
            cfg.get(CONF_COMMAND_INTERVAL, DEFAULT_COMMAND_INTERVAL)
        )
        self.grid_power: float | None = None
        self.decision: Decision | None = None

        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._force_timer: CALLBACK_TYPE | None = None
        self._deferred_run: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        self._rerun = False
        self._last_run = 0.0

    # ------------------------------------------------------------------
    # Lifecycle

    async def async_start(self) -> None:
        """Start the control loop."""
        interval = timedelta(
            seconds=float(self.config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_interval, interval)
        )
        # React quickly to meter and EV charger changes, which matters for fuse
        # protection: the EV charger has no load balancing of its own.
        fast_entities = [
            e
            for e in (
                self.config.get(CONF_METER),
                self.config.get(CONF_EV),
                self.config.get(CONF_PHASE_L1),
                self.config.get(CONF_PHASE_L2),
                self.config.get(CONF_PHASE_L3),
            )
            if e
        ]
        self._unsubs.append(
            async_track_state_change_event(self.hass, fast_entities, self._async_state_changed)
        )
        await self.async_run()

    async def async_stop(self) -> None:
        """Stop the loop and leave the battery in a neutral state."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_force_timer()
        if self._deferred_run:
            self._deferred_run()
            self._deferred_run = None
        if self.settings.mode is not Mode.OFF:
            await self._async_write(0.0, force=True)

    # ------------------------------------------------------------------
    # Listener handling for entities

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register an entity update callback."""
        self._listeners.append(update_callback)

        def remove() -> None:
            self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------------
    # Settings API used by entities and services

    async def async_set_setting(self, name: str, value: Any) -> None:
        """Change a controller setting and re-run the controller."""
        if name == "force_duration":
            self.force_duration = float(value)
        else:
            setattr(self.settings, name, value)
        self._notify()
        await self.async_run()

    async def async_set_mode(
        self, mode: Mode, duration: float | None = None, run: bool = True
    ) -> None:
        """Change mode. Forced modes get an end time from ``duration`` minutes."""
        previous = self.settings.mode
        self._cancel_force_timer()
        self.force_end = None
        if mode in FORCE_MODES:
            if previous not in FORCE_MODES:
                self.mode_before_force = previous
            minutes = self.force_duration if duration is None else duration
            if minutes and minutes > 0:
                self._schedule_force_end(dt_util.utcnow() + timedelta(minutes=minutes))
        self.settings.mode = mode
        _LOGGER.debug("Mode changed from %s to %s (ends %s)", previous, mode, self.force_end)
        self._notify()
        if run:
            await self.async_run()

    @callback
    def restore_force(self, mode: Mode, end: datetime | None, before: Mode) -> None:
        """Restore a forced mode after a restart, if it has not expired."""
        self.mode_before_force = before
        if end is None or end <= dt_util.utcnow():
            # Never resume an open-ended or expired force after a restart.
            self.settings.mode = before
            return
        self.settings.mode = mode
        self._schedule_force_end(end)

    async def async_end_force(self) -> None:
        """Leave a forced mode and return to the mode used before it."""
        if self.settings.mode in FORCE_MODES:
            await self.async_set_mode(self.mode_before_force)

    def _schedule_force_end(self, end: datetime) -> None:
        self.force_end = end

        @callback
        def _expired(_now: datetime) -> None:
            self._force_timer = None
            self.hass.async_create_task(self.async_end_force())

        delay = max((end - dt_util.utcnow()).total_seconds(), 0)
        self._force_timer = async_call_later(self.hass, delay, _expired)

    def _cancel_force_timer(self) -> None:
        if self._force_timer:
            self._force_timer()
            self._force_timer = None

    # ------------------------------------------------------------------
    # Control loop

    async def _async_interval(self, _now: datetime) -> None:
        await self.async_run()

    async def _async_state_changed(self, _event: Event) -> None:
        wait = MIN_RUN_SPACING - (monotonic() - self._last_run)
        if wait <= 0:
            await self.async_run()
        else:
            # Too soon after the last run: run once shortly instead of dropping it.
            self._schedule_run(wait)

    @callback
    def _schedule_run(self, delay: float) -> None:
        """Run the controller once after ``delay`` seconds (coalesced)."""
        if self._deferred_run is None:
            self._deferred_run = async_call_later(self.hass, delay, self._async_deferred)

    async def _async_deferred(self, _now: datetime) -> None:
        self._deferred_run = None
        await self.async_run()

    async def async_run(self) -> None:
        """Run one control step."""
        if self._lock.locked():
            # A run is in progress; make it run once more with fresh settings.
            self._rerun = True
            return
        async with self._lock:
            self._rerun = True
            while self._rerun:
                self._rerun = False
                await self._async_run_once()

    async def _async_run_once(self) -> None:
        self._last_run = monotonic()
        meas = self._read_measurements()
        self.grid_power = meas.grid_power
        decision = compute(meas, self.settings, self.last_setpoint)
        self.decision = decision

        if decision.setpoint is not None:
            await self._async_write(decision.setpoint, urgent=decision.emergency)

        if decision.force_finished:
            _LOGGER.info("%s, returning to %s", decision.status, self.mode_before_force)
            await self.async_set_mode(self.mode_before_force, run=False)

        self._notify()

    async def _async_write(
        self, value: float, force: bool = False, urgent: bool = False
    ) -> None:
        entity_id = self.config[CONF_BATTERY_SETPOINT]
        state = self.hass.states.get(entity_id)
        if state is not None:
            # Respect the min/max of the target number entity.
            try:
                value = max(value, float(state.attributes["min"]))
                value = min(value, float(state.attributes["max"]))
            except (KeyError, TypeError, ValueError):
                pass

        min_change = float(self.config.get(CONF_MIN_WRITE_CHANGE, DEFAULT_MIN_WRITE_CHANGE))
        now = monotonic()
        if (
            not force
            and self.last_written is not None
            and abs(value - self.last_written) < min_change
            and not (value == 0 and self.last_written != 0)
            and now - self.last_write_time < KEEPALIVE_SECONDS
        ):
            return

        spacing = EMERGENCY_COMMAND_SPACING if urgent else self._command_interval
        wait = spacing - (now - self.last_write_time)
        if wait > 0:
            if not force:
                # Too soon for the Modbus server. Re-run the controller once it
                # is allowed, so the value sent is based on fresh measurements.
                self._schedule_run(wait)
                return
            await asyncio.sleep(wait)

        if urgent and now - self.last_write_time < self._command_interval:
            _LOGGER.warning(
                "Fuse overloaded: sending %s W before the normal command interval", value
            )
        # Count failed attempts too, so a struggling server is not hammered.
        self.last_write_time = monotonic()
        domain = entity_id.split(".", 1)[0]
        try:
            await self.hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - keep the loop alive on any failure
            _LOGGER.warning("Failed to write %s W to %s: %s", value, entity_id, err)
            return
        self.last_written = value
        # Only what was actually sent counts as the battery's commanded power.
        self.last_setpoint = value

    # ------------------------------------------------------------------
    # Sensor reading

    def _state(self, entity_id: str | None, check_stale: bool = False) -> State | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        if check_stale:
            timeout = float(self.config.get(CONF_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT))
            reported = getattr(state, "last_reported", None) or state.last_updated
            if timeout > 0 and (dt_util.utcnow() - reported).total_seconds() > timeout:
                _LOGGER.debug("%s is stale (last reported %s)", entity_id, reported)
                return None
        return state

    def _read_number(self, entity_id: str | None, check_stale: bool = False) -> float | None:
        state = self._state(entity_id, check_stale)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _read_power(self, entity_id: str | None, check_stale: bool = False) -> float | None:
        """Read a power sensor and convert it to W."""
        value = self._read_number(entity_id, check_stale)
        if value is None:
            return None
        unit = self.hass.states.get(entity_id).attributes.get("unit_of_measurement")
        if unit == "kW":
            value *= 1000
        elif unit == "MW":
            value *= 1_000_000
        return value

    def _read_current(self, entity_id: str | None) -> float | None:
        """Read a phase sensor and convert it to A."""
        if self.config.get(CONF_PHASE_UNIT) == PHASE_UNIT_POWER:
            power = self._read_power(entity_id, check_stale=True)
            return None if power is None else power / self.settings.voltage
        value = self._read_number(entity_id, check_stale=True)
        if value is None:
            return None
        if self.hass.states.get(entity_id).attributes.get("unit_of_measurement") == "mA":
            value /= 1000
        return value

    def _read_measurements(self) -> Measurements:
        cfg = self.config
        voltage = self.settings.voltage

        battery_power = self._read_power(cfg.get(CONF_BATTERY_POWER))
        if battery_power is not None and cfg.get(CONF_BATTERY_POWER_INVERT):
            battery_power = -battery_power
        battery_now = battery_power if battery_power is not None else self.last_setpoint

        ev_power = self._read_power(cfg.get(CONF_EV))
        ev_power = abs(ev_power) if ev_power is not None else None
        soc = self._read_number(cfg.get(CONF_SOC))
        meter = self._read_power(cfg.get(CONF_METER), check_stale=True)

        consumption_mode = cfg.get(CONF_METER_TYPE) == METER_TYPE_CONSUMPTION
        pv_power = 0.0
        ev_extra = 0.0
        if consumption_mode:
            pv = self._read_power(cfg.get(CONF_PV))
            pv_power = abs(pv) if pv is not None else 0.0
            if not cfg.get(CONF_EV_IN_METER, True) and ev_power:
                ev_extra = ev_power

        grid_power: float | None
        if meter is None:
            grid_power = None
        elif consumption_mode:
            # Meter measures the house load: grid = load + EV - PV - battery.
            grid_power = abs(meter) + ev_extra - pv_power - battery_now
        else:
            grid_power = -meter if cfg.get(CONF_METER_INVERT) else meter

        phase_ids = [cfg.get(CONF_PHASE_L1), cfg.get(CONF_PHASE_L2), cfg.get(CONF_PHASE_L3)]
        phase_currents: tuple[float, float, float] | None = None
        if grid_power is not None and all(phase_ids):
            values = [self._read_current(e) for e in phase_ids]
            if any(v is None for v in values):
                # Fuse protection cannot be guaranteed: treat as unavailable.
                grid_power = None
            elif consumption_mode:
                # Phase sensors measure the load; the balanced inverter
                # (PV + battery) and an external EV charger shift every phase.
                shift = (ev_extra - pv_power - battery_now) / (3 * voltage)
                phase_currents = tuple(abs(v) + shift for v in values)  # type: ignore[assignment]
            else:
                if any(v < 0 for v in values):
                    # Signed per-phase values: same convention as the meter.
                    if cfg.get(CONF_METER_INVERT):
                        values = [-v for v in values]
                elif grid_power < 0:
                    # Unsigned meter while exporting: assume all phases export.
                    values = [-v for v in values]
                phase_currents = tuple(values)  # type: ignore[assignment]

        return Measurements(
            grid_power=grid_power,
            battery_power=battery_power,
            phase_currents=phase_currents,
            soc=soc,
            ev_power=ev_power,
        )
