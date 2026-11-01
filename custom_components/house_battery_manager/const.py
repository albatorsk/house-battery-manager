"""Constants for the House Battery Manager integration."""

from __future__ import annotations

DOMAIN = "house_battery_manager"

# Entities
CONF_BATTERY_SETPOINT = "battery_setpoint_entity"
CONF_BATTERY_POWER = "battery_power_entity"
CONF_BATTERY_POWER_INVERT = "battery_power_invert"
CONF_SOC = "soc_entity"
CONF_METER = "meter_entity"
CONF_METER_TYPE = "meter_type"
CONF_METER_INVERT = "meter_invert"
CONF_PV = "pv_entity"
CONF_EV = "ev_entity"
CONF_EV_IN_METER = "ev_included_in_meter"
CONF_PHASE_L1 = "phase_l1_entity"
CONF_PHASE_L2 = "phase_l2_entity"
CONF_PHASE_L3 = "phase_l3_entity"
CONF_PHASE_UNIT = "phase_unit"

# Electrical
CONF_VOLTAGE = "voltage"
CONF_FUSE_CURRENT = "fuse_current"
CONF_FUSE_MARGIN = "fuse_margin"
CONF_MAX_CHARGE = "max_charge_power"
CONF_MAX_DISCHARGE = "max_discharge_power"

# Tuning
CONF_UPDATE_INTERVAL = "update_interval"
CONF_GAIN = "gain"
CONF_DEADBAND = "deadband"
CONF_MIN_WRITE_CHANGE = "min_write_change"
CONF_COMMAND_INTERVAL = "min_command_interval"
CONF_STALE_TIMEOUT = "stale_timeout"

METER_TYPE_GRID = "grid"
METER_TYPE_CONSUMPTION = "consumption"

PHASE_UNIT_CURRENT = "current"
PHASE_UNIT_POWER = "power"

DEFAULT_VOLTAGE = 230.0
DEFAULT_FUSE_CURRENT = 20.0
DEFAULT_FUSE_MARGIN = 1.0
DEFAULT_MAX_CHARGE = 5000
DEFAULT_MAX_DISCHARGE = 5000
DEFAULT_UPDATE_INTERVAL = 5
DEFAULT_GAIN = 0.7
DEFAULT_DEADBAND = 30
DEFAULT_MIN_WRITE_CHANGE = 20
DEFAULT_COMMAND_INTERVAL = 3
DEFAULT_STALE_TIMEOUT = 60

# Services
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_CANCEL_FORCE = "cancel_force"

ATTR_POWER = "power"
ATTR_DURATION = "duration"
ATTR_TARGET_SOC = "target_soc"
