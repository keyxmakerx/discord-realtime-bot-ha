"""Buttons for the Laundry Discord Bot integration.

Each button runs the action of the same name, so the manual escape hatches are
one tap on a dashboard.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LaundryConfigEntry
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LaundryConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the laundry buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LaundryTestPostButton(coordinator, entry),
            LaundryResetSessionButton(coordinator, entry),
            LaundryTrackLoadButton(coordinator, entry),
            LaundryDiagnosticsButton(coordinator, entry),
        ]
    )


class LaundryTestPostButton(LaundryEntity, ButtonEntity):
    """Post a sample card to prove the Discord path works."""

    _attr_translation_key = "test_post"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_test_post"

    async def async_press(self) -> None:
        await self.coordinator.async_test_post()


class LaundryResetSessionButton(LaundryEntity, ButtonEntity):
    """Force-close a stuck session. Announces nothing, pings nobody."""

    _attr_translation_key = "reset_session"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_reset_session"

    async def async_press(self) -> None:
        await self.coordinator.async_reset_session()


class LaundryTrackLoadButton(LaundryEntity, ButtonEntity):
    """Start tracking a load the bot missed. Posts a real card."""

    _attr_translation_key = "track_load"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_track_load"

    async def async_press(self) -> None:
        await self.coordinator.async_track_current_load()


class LaundryDiagnosticsButton(LaundryEntity, ButtonEntity):
    """Re-run the health checks now instead of waiting for the 5-minute tick."""

    _attr_translation_key = "run_diagnostics"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_diagnostics"

    async def async_press(self) -> None:
        self.coordinator.refresh_health()
