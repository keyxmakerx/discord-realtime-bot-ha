"""Shared base entity for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import LaundryCoordinator


class LaundryEntity(Entity):
    """Base entity that refreshes on the coordinator dispatcher signal."""

    _attr_should_poll = False
    # has_entity_name is intentionally False so the entity_ids come out as
    # sensor.laundry_claimed_by / sensor.laundry_stage / binary_sensor.laundry_waiting
    # (matching the documented dashboard/automation names) rather than being
    # prefixed with the device name.
    _attr_has_entity_name = False

    def __init__(self, coordinator: LaundryCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Laundry Discord Bot",
            manufacturer="Laundry Discord",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self.async_write_ha_state
            )
        )
