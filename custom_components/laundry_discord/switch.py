"""The bot's feature toggles, as switches.

The same values the options flow sets, surfaced so a dashboard can show what
is on and turn it off without a modal. Like the numbers, writing one reloads
the integration — that is what the options flow already does, and it is what
makes a change reach the parts of the bot that read config at construction.

Everything here is house-wide. The per-person consents (📬 DM, 👁 monitoring,
🔮 guessing and the 🔔 kinds) are deliberately **not** here: they belong to the
person who set them, they are set in Discord where that person can see them,
and a housemate's notification consent is not a thing to be flipped from a
shared wall tablet.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LEARN_HABITS,
    CONF_PING_CLAIMANT_ON_COMPLETE,
    CONF_REMIND_DMS,
    CONF_SHOW_ASSISTANT,
    CONF_TRADES,
    DEFAULT_LEARN_HABITS,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    DEFAULT_REMIND_DMS,
    DEFAULT_SHOW_ASSISTANT,
    DEFAULT_TRADES,
    DOMAIN,
)
from .entity import LaundryEntity

# key, default, name, icon
_SWITCHES = (
    (
        CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE,
        "Laundry Ping On Complete", "mdi:bell-ring",
    ),
    (CONF_SHOW_ASSISTANT, DEFAULT_SHOW_ASSISTANT, "Laundry Show Assistant", "mdi:robot"),
    (CONF_LEARN_HABITS, DEFAULT_LEARN_HABITS, "Laundry Learn Habits", "mdi:brain"),
    (CONF_REMIND_DMS, DEFAULT_REMIND_DMS, "Laundry Reminder DMs", "mdi:email-outline"),
    (CONF_TRADES, DEFAULT_TRADES, "Laundry Slot Trades", "mdi:swap-horizontal"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        LaundryOptionSwitch(coordinator, entry, *row) for row in _SWITCHES
    )


class LaundryOptionSwitch(LaundryEntity, SwitchEntity):
    """One boolean option, read from the entry and written back to it."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key, default, name, icon) -> None:
        super().__init__(coordinator, entry)
        self._key = key
        self._default = default
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_switch_{key}"

    @property
    def is_on(self) -> bool:
        merged = {**self._entry.data, **self._entry.options}
        value = merged.get(self._key, self._default)
        # A value that came back from JSON as the *string* "false" is truthy,
        # and reading it as True would silently turn a feature on that nobody
        # asked for. people._flag exists for the same hazard on the person
        # records; this is the config-entry end of it.
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _write(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, self._key: value}
        )

    async def async_turn_on(self, **kwargs) -> None:
        self._write(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._write(False)
