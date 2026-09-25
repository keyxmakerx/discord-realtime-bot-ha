"""The Laundry Discord Bot integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import diagnose as diagnose_mod
from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_DIAGNOSTICS,
    SERVICE_RESET_SESSION,
    SERVICE_TEST_POST,
    SERVICE_TRACK_LOAD,
)
from .coordinator import LaundryConfigEntry, LaundryCoordinator
from .reminders import LaundryReminders

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LaundryConfigEntry) -> bool:
    """Set up Laundry Discord Bot from a config entry."""
    coordinator = LaundryCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator

    # The reminder DMs. Registers nothing while the option is off.
    reminders = LaundryReminders(hass, entry, coordinator)
    await reminders.async_setup()
    entry.async_on_unload(reminders.shutdown)

    # The gateway runs as a background task tied to the entry; async_run_bot
    # swallows errors so a bot failure can't take HA down.
    entry.async_create_background_task(
        hass, coordinator.async_run_bot(), f"{DOMAIN}_bot"
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LaundryConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_shutdown()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: LaundryConfigEntry
) -> None:
    """Reload when options change (options flow, number or switch entities)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _loaded_entries(hass: HomeAssistant) -> list[LaundryConfigEntry]:
    """Every loaded entry, or a user-facing error if there are none."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    return entries


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the debug and escape-hatch actions. Each runs on every entry."""

    async def _handle_test_post(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.async_test_post()

    async def _handle_reset_session(call: ServiceCall) -> None:
        for entry in _loaded_entries(hass):
            await entry.runtime_data.async_reset_session()

    async def _handle_track_load(call: ServiceCall) -> None:
        # Does nothing if a load is already tracked; reset_session first.
        for entry in _loaded_entries(hass):
            await entry.runtime_data.async_track_current_load()

    async def _handle_diagnostics(call: ServiceCall) -> ServiceResponse:
        """Return the health findings as response data.

        A coordinator that can't be read is reported as an entry rather than
        failing the call: this runs when something is already wrong.
        """
        now = dt_util.utcnow().timestamp()
        results = []
        for entry in _loaded_entries(hass):
            try:
                snap = entry.runtime_data.diagnostic_snapshot()
                findings = diagnose_mod.check(
                    snap["session"],
                    now,
                    watched=snap["watched"],
                    max_session_minutes=snap["config"]["max_session_minutes"],
                )
                results.append({
                    "entry_id": entry.entry_id,
                    "summary": diagnose_mod.summarise(findings),
                    "findings": findings,
                    "state": snap,
                })
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Diagnostics failed for entry %s", entry.entry_id)
                results.append({
                    "entry_id": entry.entry_id,
                    "summary": f"could not be read: {type(err).__name__}",
                    "findings": [],
                    "state": {},
                })
        return {
            "summary": results[0]["summary"] if len(results) == 1 else (
                diagnose_mod.summarise_entries(results)
            ),
            "entries": results,
        }

    hass.services.async_register(DOMAIN, SERVICE_TEST_POST, _handle_test_post)
    hass.services.async_register(DOMAIN, SERVICE_RESET_SESSION, _handle_reset_session)
    hass.services.async_register(DOMAIN, SERVICE_TRACK_LOAD, _handle_track_load)
    hass.services.async_register(
        DOMAIN,
        SERVICE_DIAGNOSTICS,
        _handle_diagnostics,
        # OPTIONAL rather than ONLY, so a script can call it without capturing
        # the response.
        supports_response=SupportsResponse.OPTIONAL,
    )
