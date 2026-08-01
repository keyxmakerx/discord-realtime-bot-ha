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
    CONF_BOT_TOKEN,
    CONF_CHANNEL_ID,
    CONF_ETA_ENTITY,
    CONF_ETA_INTERVAL,
    CONF_AVAILABILITY_GRACE,
    CONF_CONFIRM_DELAY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_IDLE,
    CONF_ENERGY_LOAD_JUMP,
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
    CONF_WATER_ENTITY,
    CONF_WRINKLE_ENTITY,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_ETA_ENTITY,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_JOB_STATE_ENTITY,
    DEFAULT_MACHINE_STATE_ENTITY,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ENERGY_LOAD_JUMP,
    DEFAULT_HANDOFF_FALLBACK,
    DEFAULT_LEARN_HABITS,
    DEFAULT_NUDGE_LEAD,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    DEFAULT_PLAN_DM_TIME,
    DEFAULT_PLAN_DM_WEEKDAY,
    DEFAULT_QUEUE_EXPIRY,
    DEFAULT_REMIND_DMS,
    DEFAULT_RUNNING_ENTITY,
    DEFAULT_SHOW_ASSISTANT,
    DOMAIN,
    MAX_AVAILABILITY_GRACE,
    MAX_CONFIRM_DELAY,
    MAX_ENERGY_IDLE,
    MAX_ENERGY_LOAD_JUMP,
    MAX_ETA_INTERVAL,
    MAX_HANDOFF_FALLBACK,
    MAX_NUDGE_LEAD,
    MAX_QUEUE_EXPIRY,
    MIN_AVAILABILITY_GRACE,
    MIN_CONFIRM_DELAY,
    MIN_ENERGY_IDLE,
    MIN_ENERGY_LOAD_JUMP,
    MIN_ETA_INTERVAL,
    MIN_HANDOFF_FALLBACK,
    MIN_NUDGE_LEAD,
    MIN_QUEUE_EXPIRY,
)
from .plan import DAY_NAMES


def _eta_interval_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_ETA_INTERVAL,
            max=MAX_ETA_INTERVAL,
            step=5,
            unit_of_measurement="seconds",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the options (and the option portion of initial setup)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ETA_INTERVAL,
                default=defaults.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL),
            ): _eta_interval_selector(),
            vol.Required(
                CONF_CONFIRM_DELAY,
                default=defaults.get(CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_CONFIRM_DELAY,
                    max=MAX_CONFIRM_DELAY,
                    step=5,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_ENERGY_IDLE,
                default=defaults.get(CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_ENERGY_IDLE,
                    max=MAX_ENERGY_IDLE,
                    step=5,
                    unit_of_measurement="minutes",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_ENERGY_LOAD_JUMP,
                default=defaults.get(
                    CONF_ENERGY_LOAD_JUMP, DEFAULT_ENERGY_LOAD_JUMP
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_ENERGY_LOAD_JUMP,
                    max=MAX_ENERGY_LOAD_JUMP,
                    step=0.1,
                    unit_of_measurement="kWh",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_PING_CLAIMANT_ON_COMPLETE,
                default=defaults.get(
                    CONF_PING_CLAIMANT_ON_COMPLETE,
                    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_AVAILABILITY_GRACE,
                default=defaults.get(
                    CONF_AVAILABILITY_GRACE, DEFAULT_AVAILABILITY_GRACE
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_AVAILABILITY_GRACE,
                    max=MAX_AVAILABILITY_GRACE,
                    step=1,
                    unit_of_measurement="minutes",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_HANDOFF_FALLBACK,
                default=defaults.get(
                    CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_HANDOFF_FALLBACK,
                    max=MAX_HANDOFF_FALLBACK,
                    step=5,
                    unit_of_measurement="minutes",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_QUEUE_EXPIRY,
                default=defaults.get(CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_QUEUE_EXPIRY,
                    max=MAX_QUEUE_EXPIRY,
                    step=1,
                    unit_of_measurement="hours",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SHOW_ASSISTANT,
                default=defaults.get(CONF_SHOW_ASSISTANT, DEFAULT_SHOW_ASSISTANT),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_LEARN_HABITS,
                default=defaults.get(CONF_LEARN_HABITS, DEFAULT_LEARN_HABITS),
            ): selector.BooleanSelector(),
            # The one switch that lets the bot start a conversation. Off by
            # default, and the three settings under it do nothing at all while
            # it is off — they are here so somebody who turns it on isn't then
            # hunting for when it will fire.
            vol.Required(
                CONF_REMIND_DMS,
                default=defaults.get(CONF_REMIND_DMS, DEFAULT_REMIND_DMS),
            ): selector.BooleanSelector(),
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
            vol.Required(
                CONF_PLAN_DM_TIME,
                default=defaults.get(CONF_PLAN_DM_TIME, DEFAULT_PLAN_DM_TIME),
            ): selector.TimeSelector(),
            vol.Required(
                CONF_NUDGE_LEAD,
                default=defaults.get(CONF_NUDGE_LEAD, DEFAULT_NUDGE_LEAD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_NUDGE_LEAD,
                    max=MAX_NUDGE_LEAD,
                    step=5,
                    unit_of_measurement="minutes",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


class LaundryDiscordConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect connection + entity config, then the options."""
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
                    CONF_MACHINE_STATE_ENTITY: user_input.get(
                        CONF_MACHINE_STATE_ENTITY
                    )
                    or "",
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
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BOT_TOKEN, default=defaults.get(CONF_BOT_TOKEN, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD
                    )
                ),
                vol.Required(
                    CONF_CHANNEL_ID, default=defaults.get(CONF_CHANNEL_ID, "")
                ): selector.TextSelector(),
                vol.Required(
                    CONF_RUNNING_ENTITY,
                    default=defaults.get(CONF_RUNNING_ENTITY, DEFAULT_RUNNING_ENTITY),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Required(
                    CONF_JOB_STATE_ENTITY,
                    default=defaults.get(
                        CONF_JOB_STATE_ENTITY, DEFAULT_JOB_STATE_ENTITY
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_ETA_ENTITY,
                    default=defaults.get(CONF_ETA_ENTITY, DEFAULT_ETA_ENTITY),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_MACHINE_STATE_ENTITY,
                    default=defaults.get(
                        CONF_MACHINE_STATE_ENTITY, DEFAULT_MACHINE_STATE_ENTITY
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_ENERGY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_WATER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_WRINKLE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Required(
                    CONF_ETA_INTERVAL,
                    default=defaults.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL),
                ): _eta_interval_selector(),
                vol.Required(
                    CONF_PING_CLAIMANT_ON_COMPLETE,
                    default=defaults.get(
                        CONF_PING_CLAIMANT_ON_COMPLETE,
                        DEFAULT_PING_CLAIMANT_ON_COMPLETE,
                    ),
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LaundryDiscordOptionsFlow()


class LaundryDiscordOptionsFlow(OptionsFlow):
    """Handle the options flow (ETA interval, completion ping)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_ETA_INTERVAL: int(user_input[CONF_ETA_INTERVAL]),
                    CONF_CONFIRM_DELAY: int(user_input[CONF_CONFIRM_DELAY]),
                    CONF_ENERGY_IDLE: int(user_input[CONF_ENERGY_IDLE]),
                    CONF_ENERGY_LOAD_JUMP: float(
                        user_input[CONF_ENERGY_LOAD_JUMP]
                    ),
                    CONF_PING_CLAIMANT_ON_COMPLETE: user_input[
                        CONF_PING_CLAIMANT_ON_COMPLETE
                    ],
                    CONF_AVAILABILITY_GRACE: int(
                        user_input[CONF_AVAILABILITY_GRACE]
                    ),
                    CONF_HANDOFF_FALLBACK: int(
                        user_input[CONF_HANDOFF_FALLBACK]
                    ),
                    CONF_QUEUE_EXPIRY: int(user_input[CONF_QUEUE_EXPIRY]),
                    CONF_SHOW_ASSISTANT: user_input[CONF_SHOW_ASSISTANT],
                    CONF_LEARN_HABITS: user_input[CONF_LEARN_HABITS],
                    CONF_REMIND_DMS: user_input[CONF_REMIND_DMS],
                    # The select hands back a string; stored as the int the
                    # weekday actually is, so nothing downstream has to parse it.
                    CONF_PLAN_DM_WEEKDAY: int(user_input[CONF_PLAN_DM_WEEKDAY]),
                    CONF_PLAN_DM_TIME: str(user_input[CONF_PLAN_DM_TIME]),
                    CONF_NUDGE_LEAD: int(user_input[CONF_NUDGE_LEAD]),
                }
            )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current)
        )
