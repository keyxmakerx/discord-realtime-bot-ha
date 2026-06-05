"""Binary sensor for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the laundry binary sensor."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LaundryWaitingBinarySensor(coordinator, entry)])


class LaundryWaitingBinarySensor(LaundryEntity, BinarySensorEntity):
    """On when a finished load is unclaimed."""

    _attr_name = "Laundry Waiting"
    _attr_icon = "mdi:basket-unfill"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_waiting"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.waiting)
