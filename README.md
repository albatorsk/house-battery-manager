# House Battery Manager

A Home Assistant custom integration that controls a house battery through its
power setpoint entity (for example a Modbus `number` entity). By default it
keeps net grid import/export as close to 0 W as possible, and it always keeps
every phase below the main fuse rating.

## Installation

**With HACS**

1. Open HACS in Home Assistant.
2. Open the menu (⋮) in the top right corner and select *Custom repositories*.
3. Add `https://github.com/albatorsk/house-battery-manager` with the type
   *Integration*.
4. Search for *House Battery Manager* in HACS and install it.
5. Restart Home Assistant.

**Manually**

1. Copy the `custom_components/house_battery_manager` folder into your Home
   Assistant `config/custom_components/` folder.
2. Restart Home Assistant.

**Add the integration**

1. Go to *Settings* and open *Devices & services*.
2. Select *Add integration*.
3. Search for *House Battery Manager* and select it.
4. Follow the setup steps described below.

## Setup

**Entities**

| Field | Notes |
|---|---|
| Battery setpoint | `number`/`input_number` in W. Positive = discharge/export, negative = charge/import. |
| Battery actual power | Optional but strongly recommended. Sensor in W, positive when discharging (tick *invert* if yours is positive when charging). |
| Battery SoC | Optional. Needed for the reserve and the force-charge/discharge SoC targets. |
| Energy meter | Must be a **power** sensor (W or kW), not an energy (kWh) sensor. |
| Meter type | *Grid connection*: net power at the house connection (preferred). *House consumption*: grid is calculated as load + EV (if not included) − PV − battery. |
| PV / EV charger | Optional power sensors. PV is required for the *consumption* meter type. |

**Fuses and power limits**

* Phase L1/L2/L3 sensors: per-phase current (A) or power (W) at the grid
  connection. **Strongly recommended.** Without them the integration assumes the
  load is evenly split over the three phases, which does not protect a single
  phase overloaded by, for example, a single-phase EV charger or sauna.
  Signed sensors (negative = export) are used as-is; unsigned sensors are given
  the sign of the total grid power.
* Voltage 230 V, fuse 20 A, safety margin 1 A (regulates to 19 A per phase).
* Maximum charge/discharge power of the battery.

**Tuning** — control interval (5 s), gain (0.7), deadband (30 W), minimum
setpoint change before writing over Modbus (20 W), minimum time between
battery commands (3 s, cannot be set lower) and the time after which an
unchanged meter is treated as unavailable (60 s, 0 disables).

Everything can be changed later under **Configure**.

## Entities created

| Entity | Purpose |
|---|---|
| `select.<name>_mode` | `Auto (zero grid)`, `Force charge`, `Force discharge`, `Idle` (0 W, fuse protection still active), `Off` (integration never writes). |
| `number.<name>_grid_target` | Grid target for Auto, default 0 W. Positive allows some import, negative aims for some export. |
| `number.<name>_force_charge_power` / `_force_discharge_power` | Power used by the forced modes. |
| `number.<name>_force_duration` | Minutes a forced mode lasts before returning to the previous mode (0 = until cancelled or SoC target reached). |
| `number.<name>_reserve_soc` | Auto mode never discharges below this SoC. |
| `number.<name>_force_charge_target_soc` / `_force_discharge_target_soc` | Forced mode ends at this SoC. |
| `switch.<name>_battery_may_power_ev` | Only if an EV charger sensor is configured. When off, Auto lets the EV charge from the grid rather than draining the battery. |
| `sensor.<name>_status`, `_battery_setpoint`, `_grid_power`, `_fuse_headroom`, `_force_ends` | What the controller is doing. `_battery_setpoint` is the last value actually sent. |
| `binary_sensor.<name>_fuse_overload` | On while any phase is above fuse − margin (19 A by default). See *EV charger without load balancing*. |

## Services

All target the mode select entity.

```yaml
action: house_battery_manager.force_charge
target:
  entity_id: select.house_battery_mode
data:
  power: 4000       # W, optional
  duration: 120     # minutes, optional (0 = until cancelled / target SoC)
  target_soc: 90    # optional
```

`house_battery_manager.force_discharge` takes the same fields, and
`house_battery_manager.cancel_force` returns to the previous mode.

Example: charge cheaply at night.

```yaml
automation:
  - alias: Charge battery on cheap night power
    triggers:
      - trigger: time
        at: "02:00:00"
    actions:
      - action: house_battery_manager.force_charge
        target:
          entity_id: select.house_battery_mode
        data:
          power: 5000
          duration: 180
          target_soc: 80
```

## How it works

Every control interval, and shortly after every meter update:

1. **Auto:** `new setpoint = battery power + gain × (grid power − target)`.
   Import makes the battery discharge more, export makes it charge more.
   Forced modes use their fixed power, Idle uses 0 W.
2. The result is clamped to the battery's max charge/discharge power and the
   reserve SoC.
3. **Fuse protection** then clamps the setpoint so that no phase exceeds
   `fuse − margin`, in either direction. The battery is modelled as a balanced
   three-phase inverter, so changing its output by ΔW changes each phase's
   current by ΔW / (3 × 230 V). Fuse protection overrides the reserve SoC and
   applies to forced charging, so grid charging always stays within the fuse.

**Command rate limit:** the battery's Modbus server is never sent a command
more often than every 3 seconds (configurable, minimum 3 s). The controller
still reacts to every meter update. If a change is needed sooner, it waits
until 3 s have passed, recalculates with the latest measurements, and sends
that value, so it never sends an outdated one. Failed writes count too, so a
struggling server is not flooded with retries.

**Emergency exception:** if a phase is measured above the fuse rating itself
(above 20 A, not just above the 19 A regulation limit), and the new setpoint
relieves that phase, the command is sent at once instead of waiting for the
3 s window. Even then, commands are at least 1 s apart, and a warning is logged.
Between 19 A and 20 A the normal 3 s spacing still applies. Without a battery
power sensor, repeated emergency commands assume the previous one already took
effect and may discharge more than needed while the battery is still ramping
up. That is safe for the fuse, and normal regulation corrects it afterwards. Small changes (< 20 W) are not
sent at all, except for a keep-alive once a minute.

**Fail-safe:** if the meter (or any configured phase sensor) is unavailable or
stale, the battery is set to 0 W. When the integration is unloaded or
reconfigured it also sets the battery to 0 W (unless the mode is `Off`).
A forced mode is restored after a Home Assistant restart only if it had an end
time that has not yet passed.

## EV charger without load balancing

The EV charger cannot limit itself, so the battery handles the fuse:

* Updates from the meter, the phase sensors and the EV charger power sensor
  trigger a control run immediately. A run never waits for the 5 s interval.
* When the EV starts while the battery is charging, charging is reduced at the
  next allowed command, or turned into discharging if needed, so that the most
  loaded phase stays at 19 A or below.
* Worst-case reaction time is about: meter update delay + up to 3 s command
  spacing (about 1 s when a phase is above 20 A) + the battery's own ramp time. Main fuses tolerate short overloads
  (a 20 A fuse carrying 25 A takes minutes, not seconds, to blow), and the
  1 A margin adds to that. Check how often your meter actually updates. A meter
  that updates every 10 s or more makes everything slower. Consider raising the
  margin if the meter is slow.

If the house plus the EV need more than the fuse allows even with the battery
fully discharging (or the battery is empty), the battery cannot fix it.
`binary_sensor.<name>_fuse_overload` stays on in that case. If the charger can
be switched off or have its current lowered from Home Assistant, use an
automation as a last line of defence:

```yaml
automation:
  - alias: Pause EV charging on fuse overload
    triggers:
      - trigger: state
        entity_id: binary_sensor.house_battery_fuse_overload
        to: "on"
        for: "00:00:15"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.ev_charger   # your charger's entity
```

## Limitations — please read

* The integration only controls the battery. If the house draws more than the
  fuse allows, it discharges the battery up to its maximum power to help, but
  it cannot switch off loads itself (see the overload sensor above).
* Protection is only as fast as your meter plus the command spacing (3 s, or 1 s in an emergency).
* A battery inverter that is not three-phase balanced is not modelled.

## Development

```bash
python3 -m unittest tests/test_controller.py       # control logic, no HA needed
pip install pytest-homeassistant-custom-component
pytest                                             # full integration tests
```
