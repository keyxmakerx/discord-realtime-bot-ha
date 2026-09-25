"""The bot's timing options as number entities.

Writing a value updates the config entry options, which reloads the
integration (a brief Discord reconnect) exactly like the options flow does.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_AVAILABILITY_GRACE,
    CONF_CONFIRM_DELAY,
    CONF_EMPTY_REMINDER,
    CONF_ENERGY_IDLE,
    CONF_ENERGY_LOAD_JUMP,
    CONF_HANDOFF_FALLBACK,
    CONF_QUEUE_EXPIRY,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_EMPTY_REMINDER,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ENERGY_LOAD_JUMP,
    DEFAULT_HANDOFF_FALLBACK,
    DEFAULT_QUEUE_EXPIRY,
    MAX_AVAILABILITY_GRACE,
    MAX_CONFIRM_DELAY,
    MAX_EMPTY_REMINDER,
    MAX_ENERGY_IDLE,
    MAX_ENERGY_LOAD_JUMP,
    MAX_HANDOFF_FALLBACK,
    MAX_QUEUE_EXPIRY,
    MIN_AVAILABILITY_GRACE,
    MIN_CONFIRM_DELAY,
    MIN_EMPTY_REMINDER,
    MIN_ENERGY_IDLE,
    MIN_ENERGY_LOAD_JUMP,
    MIN_HANDOFF_FALLBACK,
    MIN_QUEUE_EXPIRY,
)
from .coordinator import LaundryConfigEntry
from .entity import LaundryEntity

# Option key (also the translation key), default, min, max, step, unit, device
# class, whole numbers only. The limits match the options flow.
_NUMBERS = (
    (
        CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE,
        MIN_ENERGY_IDLE, MAX_ENERGY_IDLE, 5,
        UnitOfTime.MINUTES, NumberDeviceClass.DURATION, True,
    ),
    (
        CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY,
        MIN_CONFIRM_DELAY, MAX_CONFIRM_DELAY, 5,
        UnitOfTime.SECONDS, NumberDeviceClass.DURATION, True,
    ),
    (
        CONF_ENERGY_LOAD_JUMP, DEFAULT_ENERGY_LOAD_JUMP,
        MIN_ENERGY_LOAD_JUMP, MAX_ENERGY_LOAD_JUMP, 0.1,
        UnitOfEnergy.KILO_WATT_HOUR, NumberDeviceClass.ENERGY, False,
    ),
    (
        CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK,
        MIN_HANDOFF_FALLBACK, MAX_HANDOFF_FALLBACK, 5,
        UnitOfTime.MINUTES, NumberDeviceClass.DURATION, True,
    ),
    (
        CONF_EMPTY_REMINDER, DEFAULT_EMPTY_REMINDER,
        MIN_EMPTY_REMINDER, MAX_EMPTY_REMINDER, 5,
        UnitOfTime.MINUTES, NumberDeviceClass.DURATION, True,
    ),
    (
        CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY,
        MIN_QUEUE_EXPIRY, MAX_QUEUE_EXPIRY, 1,
        UnitOfTime.HOURS, NumberDeviceClass.DURATION, True,
    ),
    (
        CONF_AVAILABILITY_GRACE, DEFAULT_AVAILABILITY_GRACE,
        MIN_AVAILABILITY_GRACE, MAX_AVAILABILITY_GRACE, 1,
        UnitOfTime.MINUTES, NumberDeviceClass.DURATION, True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LaundryConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the option numbers."""
    async_add_entities(
        LaundryOptionNumber(entry.runtime_data, entry, *row) for row in _NUMBERS
    )


class LaundryOptionNumber(LaundryEntity, NumberEntity):
    """One numeric option, read from the entry and written back to it."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator, entry, key, default,
        minimum, maximum, step, unit, device_class, whole,
    ) -> None:
        super().__init__(coordinator, entry)
        self._key = key
        self._default = default
        self._whole = whole
        self._attr_translation_key = key
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"{entry.entry_id}_number_{key}"

    @property
    def native_value(self) -> float:
        merged = {**self._entry.data, **self._entry.options}
        try:
            return float(merged.get(self._key, self._default))
        except (TypeError, ValueError):
            return float(self._default)

    async def async_set_native_value(self, value: float) -> None:
        stored = int(value) if self._whole else round(float(value), 2)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, self._key: stored}
        )
