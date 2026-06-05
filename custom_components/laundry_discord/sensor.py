"""Sensors for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STAGE_LABELS, UNCLAIMED
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the laundry sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LaundryClaimedBySensor(coordinator, entry),
            LaundryStageSensor(coordinator, entry),
        ]
    )


class LaundryClaimedBySensor(LaundryEntity, SensorEntity):
    """Who currently has the finished load (or 'Unclaimed')."""

    _attr_name = "Laundry Claimed by"
    _attr_icon = "mdi:account-check"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_claimed_by"

    @property
    def native_value(self) -> str:
        return self.coordinator.claimed_by or UNCLAIMED


class LaundryStageSensor(LaundryEntity, SensorEntity):
    """Current laundry stage (Idle / Washing / Drying / Done — waiting)."""

    _attr_name = "Laundry Stage"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_stage"

    @property
    def native_value(self) -> str:
        return STAGE_LABELS.get(self.coordinator.stage, self.coordinator.stage)
