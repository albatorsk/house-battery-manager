"""Pure control logic for the House Battery Manager.

This module has no Home Assistant dependencies so it can be unit tested in
isolation. All powers are in watts, all currents in amperes.

Sign conventions used throughout:
    grid power      positive = import from grid, negative = export to grid
    battery power   positive = discharge (export), negative = charge (import)

The battery convention matches the battery setpoint entity: positive values
make the battery deliver power, negative values make it charge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Mode(StrEnum):
    """Operating modes."""

    AUTO = "auto"  # Keep grid exchange at the target (default 0 W)
    FORCE_CHARGE = "force_charge"
    FORCE_DISCHARGE = "force_discharge"
    IDLE = "idle"  # Battery setpoint held at 0 W, fuse protection still active
    OFF = "off"  # Integration does not write to the battery at all


@dataclass(slots=True)
class Measurements:
    """A snapshot of the current state of the installation."""

    grid_power: float | None
    """Net grid power, + import / - export. None if unavailable."""

    battery_power: float | None = None
    """Measured battery power, + discharge / - charge. None if not available."""

    phase_currents: tuple[float, float, float] | None = None
    """Signed grid current per phase (+ import / - export). None if unknown."""

    soc: float | None = None
    """Battery state of charge in percent. None if not available."""

    ev_power: float | None = None
    """EV charger power, always >= 0. None if not available."""


@dataclass(slots=True)
class Settings:
    """User adjustable settings."""

    mode: Mode = Mode.AUTO
    grid_target: float = 0.0
    force_charge_power: float = 3000.0
    force_discharge_power: float = 3000.0
    max_charge_power: float = 5000.0
    max_discharge_power: float = 5000.0
    min_soc: float = 10.0
    """Reserve: AUTO mode never discharges below this SoC."""
    charge_target_soc: float = 100.0
    """FORCE_CHARGE stops once this SoC is reached."""
    discharge_target_soc: float = 10.0
    """FORCE_DISCHARGE stops once this SoC is reached."""
    battery_powers_ev: bool = True
    """If False, AUTO mode will not discharge the battery to feed the EV."""

    # Electrical limits
    voltage: float = 230.0
    fuse_current: float = 20.0
    fuse_margin: float = 1.0
    """Safety margin in A subtracted from the fuse rating."""

    # Regulation tuning
    gain: float = 0.7
    deadband: float = 30.0


@dataclass(slots=True)
class Decision:
    """Result of one control step."""

    setpoint: float | None
    """New battery setpoint in W, or None if nothing should be written."""

    status: str
    fuse_limited: bool = False
    force_finished: bool = False
    """True when a forced charge/discharge reached its SoC goal."""
    emergency: bool = False
    """A phase is above the fuse rating itself (not just the margin) and the
    setpoint was limited to relieve it: the fuse may blow if not acted on now."""
    fuse_min_setpoint: float | None = None
    fuse_max_setpoint: float | None = None
    phase_headroom: float | None = None
    """Smallest remaining current (A) on any phase before the fuse limit."""
    details: dict[str, float] = field(default_factory=dict)


def phase_currents_from_total(grid_power: float, voltage: float) -> tuple[float, float, float]:
    """Assume a perfectly balanced load when no per-phase data is available."""
    per_phase = grid_power / (3 * voltage)
    return (per_phase, per_phase, per_phase)


def fuse_setpoint_window(
    battery_now: float,
    phase_currents: tuple[float, float, float],
    settings: Settings,
) -> tuple[float, float, float]:
    """Return (min_setpoint, max_setpoint, headroom) that keeps every phase within the fuse.

    The battery is modelled as a balanced three phase inverter: changing the
    battery output by ``delta`` W changes each phase's grid current by
    ``-delta / (3 * V)``. For every phase k with signed current i_k we need
    ``|i_k - delta / (3V)| <= I_max`` which gives
    ``3V * (i_k - I_max) <= delta <= 3V * (i_k + I_max)``.
    """
    limit = max(settings.fuse_current - settings.fuse_margin, 0.0)
    three_v = 3 * settings.voltage
    delta_min = max(three_v * (i - limit) for i in phase_currents)
    delta_max = min(three_v * (i + limit) for i in phase_currents)
    headroom = limit - max(abs(i) for i in phase_currents)
    return battery_now + delta_min, battery_now + delta_max, headroom


def compute(
    meas: Measurements,
    settings: Settings,
    last_setpoint: float,
) -> Decision:
    """Compute the next battery setpoint."""
    mode = settings.mode
    if mode is Mode.OFF:
        return Decision(None, "Off: not controlling the battery")

    if meas.grid_power is None:
        # Without grid data we cannot regulate nor guarantee the fuse limit.
        return Decision(0.0, "Grid power unavailable: battery set to 0 W (fail-safe)")

    battery_now = meas.battery_power if meas.battery_power is not None else last_setpoint
    soc = meas.soc
    force_finished = False

    # 1. Desired setpoint for the selected mode.
    if mode is Mode.AUTO:
        target = settings.grid_target
        if not settings.battery_powers_ev and meas.ev_power:
            # Let the EV draw from the grid instead of from the battery.
            target += max(meas.ev_power, 0.0)
        error = meas.grid_power - target
        if abs(error) <= settings.deadband:
            desired = last_setpoint
            status = "Auto: within deadband"
        else:
            desired = battery_now + settings.gain * error
            status = "Auto: regulating grid to target"
        if soc is not None and soc <= settings.min_soc and desired > 0:
            desired = 0.0
            status = "Auto: at reserve SoC, discharge blocked"
    elif mode is Mode.FORCE_CHARGE:
        if soc is not None and soc >= settings.charge_target_soc:
            desired = 0.0
            force_finished = True
            status = "Force charge: target SoC reached"
        else:
            desired = -abs(settings.force_charge_power)
            status = "Force charge"
    elif mode is Mode.FORCE_DISCHARGE:
        if soc is not None and soc <= settings.discharge_target_soc:
            desired = 0.0
            force_finished = True
            status = "Force discharge: target SoC reached"
        else:
            desired = abs(settings.force_discharge_power)
            status = "Force discharge"
    else:  # IDLE
        desired = 0.0
        status = "Idle"

    # 2. Battery power limits.
    max_charge = abs(settings.max_charge_power)
    max_discharge = abs(settings.max_discharge_power)
    desired = min(max(desired, -max_charge), max_discharge)

    # 3. Fuse protection always wins (within what the battery can physically do).
    currents = meas.phase_currents or phase_currents_from_total(meas.grid_power, settings.voltage)
    fuse_min, fuse_max, headroom = fuse_setpoint_window(battery_now, currents, settings)
    fuse_limited = False
    if desired > fuse_max:
        desired = fuse_max
        fuse_limited = True
    if desired < fuse_min:
        # Import too high: the battery must charge less or discharge more.
        # This deliberately overrides the SoC reserve to protect the fuses.
        desired = fuse_min
        fuse_limited = True
    emergency = fuse_limited and max(abs(i) for i in currents) > settings.fuse_current
    if emergency:
        status = f"{status} (fuse overloaded)"
    elif fuse_limited:
        status = f"{status} (limited by fuse)"
    desired = min(max(desired, -max_charge), max_discharge)

    return Decision(
        setpoint=round(desired),
        status=status,
        fuse_limited=fuse_limited,
        force_finished=force_finished,
        emergency=emergency,
        fuse_min_setpoint=fuse_min,
        fuse_max_setpoint=fuse_max,
        phase_headroom=headroom,
        details={"battery_now": battery_now},
    )
