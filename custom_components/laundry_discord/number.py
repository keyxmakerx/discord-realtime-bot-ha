"""The bot's timing knobs, as adjustable entities.

Everything here was already configurable through the options flow; this makes
the same values readable and settable from a dashboard, which matters because
the ones worth touching are the ones you want to touch *while something is
going wrong* — and the options flow is four clicks and a modal away from the
card telling you something is wrong.

**Changing one reloads the integration.** Writing to ``entry.options`` fires the
update listener, exactly as the options flow does, and the reload is what makes
the new value reach the detector (which is built with its thresholds) rather
than only the config dict. That means a brief Discord reconnect per change, so
these are for tuning, not for automating against.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_AVAILABILITY_GRACE,
    CONF_CONFIRM_DELAY,
    CONF_ENERGY_IDLE,
    CONF_ENERGY_LOAD_JUMP,
    CONF_HANDOFF_FALLBACK,
    CONF_QUEUE_EXPIRY,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ENERGY_LOAD_JUMP,
    DEFAULT_HANDOFF_FALLBACK,
    DEFAULT_QUEUE_EXPIRY,
    DOMAIN,
)
from .entity import LaundryEntity

# key, default, name, icon, min, max, step, unit, whole numbers only
_NUMBERS = (
    (
        CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE,
        "Laundry Flat Meter Timeout", "mdi:timer-sand",
        5, 240, 5, "min", True,
    ),
    (
        CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY,
        "Laundry Confirm Delay", "mdi:timer-outline",
        5, 300, 5, "s", True,
    ),
    (
        CONF_ENERGY_LOAD_JUMP, DEFAULT_ENERGY_LOAD_JUMP,
        "Laundry Load Jump Threshold", "mdi:flash",
        0.05, 2.0, 0.05, "kWh", False,
    ),
    (
        CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK,
        "Laundry Handoff Fallback", "mdi:account-clock",
        5, 120, 5, "min", True,
    ),
    (
        CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY,
        "Laundry Queue Expiry", "mdi:account-multiple-remove",
        1, 48, 1, "h", True,
    ),
    (
        CONF_AVAILABILITY_GRACE, DEFAULT_AVAILABILITY_GRACE,
        "Laundry Availability Grace", "mdi:cloud-question",
        1, 60, 1, "min", True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        LaundryOptionNumber(coordinator, entry, *row) for row in _NUMBERS
    )


class LaundryOptionNumber(LaundryEntity, NumberEntity):
    """One numeric option, read from the entry and written back to it."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator, entry, key, default, name, icon,
        minimum, maximum, step, unit, whole,
    ) -> None:
        super().__init__(coordinator, entry)
        self._key = key
        self._default = default
        self._whole = whole
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{entry.entry_id}_number_{key}"

    @property
    def native_value(self) -> float:
        # entry.data is the original setup; options override it. Same merge the
        # coordinator does, read live so the value survives a reload without
        # this entity needing its own copy of the config.
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
