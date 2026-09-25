"""The bot's house-wide feature toggles as switch entities.

Writing a value updates the config entry options, which reloads the
integration like the options flow does. Per-person settings (DMs, monitoring,
guessing) are deliberately not here: they belong to each person, in Discord.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
)
from .coordinator import LaundryConfigEntry
from .entity import LaundryEntity

# Option key (also the translation key), default.
_SWITCHES = (
    (CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE),
    (CONF_SHOW_ASSISTANT, DEFAULT_SHOW_ASSISTANT),
    (CONF_LEARN_HABITS, DEFAULT_LEARN_HABITS),
    (CONF_REMIND_DMS, DEFAULT_REMIND_DMS),
    (CONF_TRADES, DEFAULT_TRADES),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LaundryConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the option switches."""
    async_add_entities(
        LaundryOptionSwitch(entry.runtime_data, entry, *row) for row in _SWITCHES
    )


class LaundryOptionSwitch(LaundryEntity, SwitchEntity):
    """One boolean option, read from the entry and written back to it."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key, default) -> None:
        super().__init__(coordinator, entry)
        self._key = key
        self._default = default
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_switch_{key}"

    @property
    def is_on(self) -> bool:
        merged = {**self._entry.data, **self._entry.options}
        value = merged.get(self._key, self._default)
        # A stored string "false" is truthy; don't let it switch a feature on.
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
