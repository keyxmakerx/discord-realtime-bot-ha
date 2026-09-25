"""Shared base entity for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DEVICE_NAME, DOMAIN, SIGNAL_UPDATE
from .coordinator import LaundryConfigEntry, LaundryCoordinator


class LaundryEntity(Entity):
    """Base entity that refreshes on the coordinator's dispatcher signal.

    Entity names come from translations and are prefixed with the device name
    "Laundry", so ids come out as ``sensor.laundry_stage`` and so on (plus an
    area prefix if the device is in one).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: LaundryCoordinator, entry: LaundryConfigEntry) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=DEVICE_NAME,
            manufacturer="Laundry Discord",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )
