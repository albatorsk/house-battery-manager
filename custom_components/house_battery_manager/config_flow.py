"""Config and options flow for House Battery Manager."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    DOMAIN,
    METER_TYPE_CONSUMPTION,
    METER_TYPE_GRID,
    PHASE_UNIT_CURRENT,
    PHASE_UNIT_POWER,
)

SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
BOOL = selector.BooleanSelector()


def _num(min_: float, max_: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=min_, max=max_, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _choice(options: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key=key,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


ENTITIES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BATTERY_SETPOINT): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["number", "input_number"])
        ),
        vol.Optional(CONF_BATTERY_POWER): SENSOR,
        vol.Required(CONF_BATTERY_POWER_INVERT, default=False): BOOL,
        vol.Optional(CONF_SOC): SENSOR,
        vol.Required(CONF_METER): SENSOR,
        vol.Required(CONF_METER_TYPE, default=METER_TYPE_GRID): _choice(
            [METER_TYPE_GRID, METER_TYPE_CONSUMPTION], CONF_METER_TYPE
        ),
        vol.Required(CONF_METER_INVERT, default=False): BOOL,
        vol.Optional(CONF_PV): SENSOR,
        vol.Optional(CONF_EV): SENSOR,
        vol.Required(CONF_EV_IN_METER, default=True): BOOL,
    }
)

LIMITS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PHASE_L1): SENSOR,
        vol.Optional(CONF_PHASE_L2): SENSOR,
        vol.Optional(CONF_PHASE_L3): SENSOR,
        vol.Required(CONF_PHASE_UNIT, default=PHASE_UNIT_CURRENT): _choice(
            [PHASE_UNIT_CURRENT, PHASE_UNIT_POWER], CONF_PHASE_UNIT
        ),
        vol.Required(CONF_VOLTAGE, default=DEFAULT_VOLTAGE): _num(100, 400, 1, "V"),
        vol.Required(CONF_FUSE_CURRENT, default=DEFAULT_FUSE_CURRENT): _num(1, 200, 1, "A"),
        vol.Required(CONF_FUSE_MARGIN, default=DEFAULT_FUSE_MARGIN): _num(0, 20, 0.1, "A"),
        vol.Required(CONF_MAX_CHARGE, default=DEFAULT_MAX_CHARGE): _num(0, 100000, 50, "W"),
        vol.Required(CONF_MAX_DISCHARGE, default=DEFAULT_MAX_DISCHARGE): _num(0, 100000, 50, "W"),
    }
)

TUNING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): _num(1, 60, 1, "s"),
        vol.Required(CONF_GAIN, default=DEFAULT_GAIN): _num(0.1, 1.0, 0.05),
        vol.Required(CONF_DEADBAND, default=DEFAULT_DEADBAND): _num(0, 1000, 5, "W"),
        vol.Required(CONF_MIN_WRITE_CHANGE, default=DEFAULT_MIN_WRITE_CHANGE): _num(0, 1000, 5, "W"),
        vol.Required(CONF_COMMAND_INTERVAL, default=DEFAULT_COMMAND_INTERVAL): _num(3, 60, 1, "s"),
        vol.Required(CONF_STALE_TIMEOUT, default=DEFAULT_STALE_TIMEOUT): _num(0, 3600, 5, "s"),
    }
)


def _validate_limits(user_input: dict[str, Any]) -> dict[str, str]:
    phases = [user_input.get(k) for k in (CONF_PHASE_L1, CONF_PHASE_L2, CONF_PHASE_L3)]
    if any(phases) and not all(phases):
        return {"base": "phases_incomplete"}
    return {}


class _FlowSteps:
    """Steps shared by the config flow and the options flow."""

    data: dict[str, Any]

    def _defaults(self) -> dict[str, Any]:
        return self.data

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_limits()
        return self.async_show_form(
            step_id="entities",
            data_schema=self.add_suggested_values_to_schema(ENTITIES_SCHEMA, self._defaults()),
        )

    async def async_step_limits(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_limits(user_input)
            if not errors:
                for key in (CONF_PHASE_L1, CONF_PHASE_L2, CONF_PHASE_L3):
                    self.data.pop(key, None)
                self.data.update(user_input)
                return await self.async_step_tuning()
        return self.async_show_form(
            step_id="limits",
            data_schema=self.add_suggested_values_to_schema(
                LIMITS_SCHEMA, user_input or self._defaults()
            ),
            errors=errors,
        )

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data.update(user_input)
            return self._finish()
        return self.async_show_form(
            step_id="tuning",
            data_schema=self.add_suggested_values_to_schema(TUNING_SCHEMA, self._defaults()),
        )


class HouseBatteryConfigFlow(_FlowSteps, ConfigFlow, domain=DOMAIN):
    """Initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        self.data = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data[CONF_NAME] = user_input[CONF_NAME]
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default="House Battery"): selector.TextSelector()}
            ),
        )

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self.data[CONF_NAME], data=self.data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HouseBatteryOptionsFlow()


class HouseBatteryOptionsFlow(_FlowSteps, OptionsFlow):
    """Reconfigure everything after setup."""

    def __init__(self) -> None:
        self.data = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.data = {CONF_NAME: self.config_entry.title}
        self._previous = dict(self.config_entry.options or self.config_entry.data)
        return await self.async_step_entities()

    def _defaults(self) -> dict[str, Any]:
        # Previous values are only suggestions; the stored options are built
        # solely from what is submitted, so cleared optional entities vanish.
        return {**self._previous, **self.data}

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self.data)
