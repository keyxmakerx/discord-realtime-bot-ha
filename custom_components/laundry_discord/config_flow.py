"""Config and options flow for the Laundry Discord Bot integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ANNOUNCE_FREE,
    CONF_AVAILABILITY_GRACE,
    CONF_BOT_TOKEN,
    CONF_CHANNEL_ID,
    CONF_CONFIRM_DELAY,
    CONF_EMPTY_REMINDER,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_IDLE,
    CONF_ENERGY_LOAD_JUMP,
    CONF_ETA_ENTITY,
    CONF_ETA_INTERVAL,
    CONF_HANDOFF_FALLBACK,
    CONF_JOB_STATE_ENTITY,
    CONF_LEARN_HABITS,
    CONF_MACHINE_STATE_ENTITY,
    CONF_NUDGE_LEAD,
    CONF_PING_CLAIMANT_ON_COMPLETE,
    CONF_PLAN_DM_TIME,
    CONF_PLAN_DM_WEEKDAY,
    CONF_QUEUE_EXPIRY,
    CONF_REMIND_DMS,
    CONF_RUNNING_ENTITY,
    CONF_SHOW_ASSISTANT,
    CONF_TRADES,
    CONF_WATER_ENTITY,
    CONF_WRINKLE_ENTITY,
    DEFAULT_ANNOUNCE_FREE,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_EMPTY_REMINDER,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ENERGY_LOAD_JUMP,
    DEFAULT_ETA_ENTITY,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_HANDOFF_FALLBACK,
    DEFAULT_JOB_STATE_ENTITY,
    DEFAULT_LEARN_HABITS,
    DEFAULT_MACHINE_STATE_ENTITY,
    DEFAULT_NUDGE_LEAD,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    DEFAULT_PLAN_DM_TIME,
    DEFAULT_PLAN_DM_WEEKDAY,
    DEFAULT_QUEUE_EXPIRY,
    DEFAULT_REMIND_DMS,
    DEFAULT_RUNNING_ENTITY,
    DEFAULT_SHOW_ASSISTANT,
    DEFAULT_TRADES,
    DOMAIN,
    MAX_AVAILABILITY_GRACE,
    MAX_CONFIRM_DELAY,
    MAX_EMPTY_REMINDER,
    MAX_ENERGY_IDLE,
    MAX_ENERGY_LOAD_JUMP,
    MAX_ETA_INTERVAL,
    MAX_HANDOFF_FALLBACK,
    MAX_NUDGE_LEAD,
    MAX_QUEUE_EXPIRY,
    MIN_AVAILABILITY_GRACE,
    MIN_CONFIRM_DELAY,
    MIN_EMPTY_REMINDER,
    MIN_ENERGY_IDLE,
    MIN_ENERGY_LOAD_JUMP,
    MIN_ETA_INTERVAL,
    MIN_HANDOFF_FALLBACK,
    MIN_NUDGE_LEAD,
    MIN_QUEUE_EXPIRY,
)
from .plan import DAY_NAMES


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _sensor(domain: str = "sensor") -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _eta_interval() -> selector.NumberSelector:
    return _number(MIN_ETA_INTERVAL, MAX_ETA_INTERVAL, 5, "seconds")


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the options flow, defaulting to the current values."""

    def required(key: str, default: Any) -> vol.Required:
        return vol.Required(key, default=defaults.get(key, default))

    return vol.Schema(
        {
            required(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL): _eta_interval(),
            required(CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY): _number(
                MIN_CONFIRM_DELAY, MAX_CONFIRM_DELAY, 5, "seconds"
            ),
            required(CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE): _number(
                MIN_ENERGY_IDLE, MAX_ENERGY_IDLE, 5, "minutes"
            ),
            required(CONF_ENERGY_LOAD_JUMP, DEFAULT_ENERGY_LOAD_JUMP): _number(
                MIN_ENERGY_LOAD_JUMP, MAX_ENERGY_LOAD_JUMP, 0.1, "kWh"
            ),
            required(
                CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE
            ): selector.BooleanSelector(),
            required(CONF_ANNOUNCE_FREE, DEFAULT_ANNOUNCE_FREE): (
                selector.BooleanSelector()
            ),
            required(CONF_AVAILABILITY_GRACE, DEFAULT_AVAILABILITY_GRACE): _number(
                MIN_AVAILABILITY_GRACE, MAX_AVAILABILITY_GRACE, 1, "minutes"
            ),
            required(CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK): _number(
                MIN_HANDOFF_FALLBACK, MAX_HANDOFF_FALLBACK, 5, "minutes"
            ),
            required(CONF_EMPTY_REMINDER, DEFAULT_EMPTY_REMINDER): _number(
                MIN_EMPTY_REMINDER, MAX_EMPTY_REMINDER, 5, "minutes"
            ),
            required(CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY): _number(
                MIN_QUEUE_EXPIRY, MAX_QUEUE_EXPIRY, 1, "hours"
            ),
            required(CONF_SHOW_ASSISTANT, DEFAULT_SHOW_ASSISTANT): (
                selector.BooleanSelector()
            ),
            required(CONF_LEARN_HABITS, DEFAULT_LEARN_HABITS): (
                selector.BooleanSelector()
            ),
            required(CONF_REMIND_DMS, DEFAULT_REMIND_DMS): selector.BooleanSelector(),
            vol.Required(
                CONF_PLAN_DM_WEEKDAY,
                default=str(
                    defaults.get(CONF_PLAN_DM_WEEKDAY, DEFAULT_PLAN_DM_WEEKDAY)
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=str(index), label=name)
                        for index, name in enumerate(DAY_NAMES)
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            required(CONF_PLAN_DM_TIME, DEFAULT_PLAN_DM_TIME): (
                selector.TimeSelector()
            ),
            required(CONF_NUDGE_LEAD, DEFAULT_NUDGE_LEAD): _number(
                MIN_NUDGE_LEAD, MAX_NUDGE_LEAD, 5, "minutes"
            ),
            required(CONF_TRADES, DEFAULT_TRADES): selector.BooleanSelector(),
        }
    )


class LaundryDiscordConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the bot token, channel and washer entities."""
        errors: dict[str, str] = {}

        if user_input is not None:
            channel_id = str(user_input[CONF_CHANNEL_ID]).strip()
            if not channel_id.isdigit():
                errors[CONF_CHANNEL_ID] = "invalid_channel"
            else:
                await self.async_set_unique_id(channel_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_BOT_TOKEN: user_input[CONF_BOT_TOKEN],
                    CONF_CHANNEL_ID: channel_id,
                    CONF_RUNNING_ENTITY: user_input[CONF_RUNNING_ENTITY],
                    CONF_JOB_STATE_ENTITY: user_input[CONF_JOB_STATE_ENTITY],
                    CONF_ETA_ENTITY: user_input[CONF_ETA_ENTITY],
                    CONF_MACHINE_STATE_ENTITY: (
                        user_input.get(CONF_MACHINE_STATE_ENTITY) or ""
                    ),
                    CONF_ENERGY_ENTITY: user_input.get(CONF_ENERGY_ENTITY) or "",
                    CONF_WATER_ENTITY: user_input.get(CONF_WATER_ENTITY) or "",
                    CONF_WRINKLE_ENTITY: user_input.get(CONF_WRINKLE_ENTITY) or "",
                }
                options = {
                    CONF_ETA_INTERVAL: int(user_input[CONF_ETA_INTERVAL]),
                    CONF_PING_CLAIMANT_ON_COMPLETE: user_input[
                        CONF_PING_CLAIMANT_ON_COMPLETE
                    ],
                }
                return self.async_create_entry(
                    title="Laundry Discord Bot", data=data, options=options
                )

        defaults = user_input or {}

        def required(key: str, default: Any) -> vol.Required:
            return vol.Required(key, default=defaults.get(key, default))

        schema = vol.Schema(
            {
                required(CONF_BOT_TOKEN, ""): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD
                    )
                ),
                required(CONF_CHANNEL_ID, ""): selector.TextSelector(),
                required(CONF_RUNNING_ENTITY, DEFAULT_RUNNING_ENTITY): _sensor(
                    "binary_sensor"
                ),
                required(CONF_JOB_STATE_ENTITY, DEFAULT_JOB_STATE_ENTITY): _sensor(),
                required(CONF_ETA_ENTITY, DEFAULT_ETA_ENTITY): _sensor(),
                vol.Optional(
                    CONF_MACHINE_STATE_ENTITY,
                    default=defaults.get(
                        CONF_MACHINE_STATE_ENTITY, DEFAULT_MACHINE_STATE_ENTITY
                    ),
                ): _sensor(),
                vol.Optional(CONF_ENERGY_ENTITY): _sensor(),
                vol.Optional(CONF_WATER_ENTITY): _sensor(),
                vol.Optional(CONF_WRINKLE_ENTITY): _sensor("binary_sensor"),
                required(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL): _eta_interval(),
                required(
                    CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return LaundryDiscordOptionsFlow()


class LaundryDiscordOptionsFlow(OptionsFlow):
    """Handle the options flow (timings and house-wide features)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_ETA_INTERVAL: int(user_input[CONF_ETA_INTERVAL]),
                    CONF_CONFIRM_DELAY: int(user_input[CONF_CONFIRM_DELAY]),
                    CONF_ENERGY_IDLE: int(user_input[CONF_ENERGY_IDLE]),
                    CONF_ENERGY_LOAD_JUMP: float(user_input[CONF_ENERGY_LOAD_JUMP]),
                    CONF_PING_CLAIMANT_ON_COMPLETE: user_input[
                        CONF_PING_CLAIMANT_ON_COMPLETE
                    ],
                    CONF_ANNOUNCE_FREE: user_input[CONF_ANNOUNCE_FREE],
                    CONF_AVAILABILITY_GRACE: int(user_input[CONF_AVAILABILITY_GRACE]),
                    CONF_HANDOFF_FALLBACK: int(user_input[CONF_HANDOFF_FALLBACK]),
                    CONF_EMPTY_REMINDER: int(user_input[CONF_EMPTY_REMINDER]),
                    CONF_QUEUE_EXPIRY: int(user_input[CONF_QUEUE_EXPIRY]),
                    CONF_SHOW_ASSISTANT: user_input[CONF_SHOW_ASSISTANT],
                    CONF_LEARN_HABITS: user_input[CONF_LEARN_HABITS],
                    CONF_REMIND_DMS: user_input[CONF_REMIND_DMS],
                    # The select returns a string; store the weekday as an int.
                    CONF_PLAN_DM_WEEKDAY: int(user_input[CONF_PLAN_DM_WEEKDAY]),
                    CONF_PLAN_DM_TIME: str(user_input[CONF_PLAN_DM_TIME]),
                    CONF_NUDGE_LEAD: int(user_input[CONF_NUDGE_LEAD]),
                    CONF_TRADES: user_input[CONF_TRADES],
                }
            )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current)
        )
