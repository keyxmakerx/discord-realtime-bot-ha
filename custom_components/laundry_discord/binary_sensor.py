"""Binary sensor for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LaundryConfigEntry
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LaundryConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the laundry binary sensor."""
    async_add_entities([LaundryWaitingBinarySensor(entry.runtime_data, entry)])


class LaundryWaitingBinarySensor(LaundryEntity, BinarySensorEntity):
    """On when a finished load is unclaimed."""

    _attr_translation_key = "waiting"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_waiting"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.waiting)
