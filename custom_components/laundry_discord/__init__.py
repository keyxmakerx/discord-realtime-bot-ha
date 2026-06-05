"""The Laundry Discord Bot integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, PLATFORMS, SERVICE_TEST_POST
from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Laundry Discord Bot from a config entry."""
    coordinator = LaundryCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Run the gateway as a background task tied to this entry; it is cancelled
    # automatically on unload. async_run_bot swallows errors so HA stays up.
    entry.async_create_background_task(
        hass, coordinator.async_run_bot(), f"{DOMAIN}_bot"
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: LaundryCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_shutdown()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
            if hass.services.has_service(DOMAIN, SERVICE_TEST_POST):
                hass.services.async_remove(DOMAIN, SERVICE_TEST_POST)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the debug test_post service (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_TEST_POST):
        return

    async def _handle_test_post(call: ServiceCall) -> None:
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            _LOGGER.warning("test_post called but no Laundry Discord entry is loaded")
            return
        for coordinator in coordinators:
            await coordinator.async_test_post()

    hass.services.async_register(DOMAIN, SERVICE_TEST_POST, _handle_test_post)
