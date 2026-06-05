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
    CONF_JOB_STATE_ENTITY,
    CONF_PING_ON_DRYING,
    CONF_PING_ROLE_ID,
    CONF_RUNNING_ENTITY,
    DEFAULT_ETA_ENTITY,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_JOB_STATE_ENTITY,
    DEFAULT_PING_ON_DRYING,
    DEFAULT_PING_ROLE_ID,
    DEFAULT_RUNNING_ENTITY,
    DOMAIN,
    MAX_ETA_INTERVAL,
    MIN_ETA_INTERVAL,
)


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the options (and the option portion of initial setup)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ETA_INTERVAL,
                default=defaults.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_ETA_INTERVAL,
                    max=MAX_ETA_INTERVAL,
                    step=5,
                    unit_of_measurement="seconds",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_PING_ROLE_ID,
                default=defaults.get(CONF_PING_ROLE_ID, DEFAULT_PING_ROLE_ID),
            ): selector.TextSelector(),
            vol.Required(
                CONF_PING_ON_DRYING,
                default=defaults.get(CONF_PING_ON_DRYING, DEFAULT_PING_ON_DRYING),
            ): selector.BooleanSelector(),
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
            role_id = str(user_input.get(CONF_PING_ROLE_ID, "")).strip()
            if not channel_id.isdigit():
                errors[CONF_CHANNEL_ID] = "invalid_channel"
            elif role_id and not role_id.isdigit():
                errors[CONF_PING_ROLE_ID] = "invalid_role"
            else:
                await self.async_set_unique_id(channel_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_BOT_TOKEN: user_input[CONF_BOT_TOKEN],
                    CONF_CHANNEL_ID: channel_id,
                    CONF_RUNNING_ENTITY: user_input[CONF_RUNNING_ENTITY],
                    CONF_JOB_STATE_ENTITY: user_input[CONF_JOB_STATE_ENTITY],
                    CONF_ETA_ENTITY: user_input[CONF_ETA_ENTITY],
                }
                options = {
                    CONF_ETA_INTERVAL: int(user_input[CONF_ETA_INTERVAL]),
                    CONF_PING_ROLE_ID: role_id,
                    CONF_PING_ON_DRYING: user_input[CONF_PING_ON_DRYING],
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
                vol.Required(
                    CONF_ETA_INTERVAL,
                    default=defaults.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_ETA_INTERVAL,
                        max=MAX_ETA_INTERVAL,
                        step=5,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_PING_ROLE_ID,
                    default=defaults.get(CONF_PING_ROLE_ID, DEFAULT_PING_ROLE_ID),
                ): selector.TextSelector(),
                vol.Required(
                    CONF_PING_ON_DRYING,
                    default=defaults.get(
                        CONF_PING_ON_DRYING, DEFAULT_PING_ON_DRYING
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
    """Handle the options flow (interval, role mention, drying ping)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            role_id = str(user_input.get(CONF_PING_ROLE_ID, "")).strip()
            if role_id and not role_id.isdigit():
                errors[CONF_PING_ROLE_ID] = "invalid_role"
            else:
                return self.async_create_entry(
                    data={
                        CONF_ETA_INTERVAL: int(user_input[CONF_ETA_INTERVAL]),
                        CONF_PING_ROLE_ID: role_id,
                        CONF_PING_ON_DRYING: user_input[CONF_PING_ON_DRYING],
                    }
                )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current),
            errors=errors,
        )
