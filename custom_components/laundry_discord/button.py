"""Buttons for the Laundry Discord Bot integration.

The three service calls, as entities. They exist so the things somebody
actually needs mid-incident — "is it stuck", "unstick it", "does Discord still
work" — are one tap on a dashboard rather than Developer Tools, YAML mode and
a remembered action name. Nothing here is new behaviour; each button is the
service it names.

``reset_session`` is deliberately **not** given a confirmation dialog: HA's
button platform has no such thing, and the action it runs is already the safe
one (it announces nothing and pings nobody). The dangerous button would be one
that posts.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LaundryTestPostButton(coordinator, entry),
            LaundryResetSessionButton(coordinator, entry),
            LaundryDiagnosticsButton(coordinator, entry),
        ]
    )


class LaundryTestPostButton(LaundryEntity, ButtonEntity):
    """Post a sample card, to prove the whole Discord path still works."""

    _attr_name = "Laundry Test Post"
    _attr_icon = "mdi:message-text-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_test_post"

    async def async_press(self) -> None:
        await self.coordinator.async_test_post()


class LaundryResetSessionButton(LaundryEntity, ButtonEntity):
    """Force-close a wedged session. Announces nothing, pings nobody."""

    _attr_name = "Laundry Reset Session"
    _attr_icon = "mdi:restart-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_reset_session"

    async def async_press(self) -> None:
        await self.coordinator.async_reset_session()


class LaundryDiagnosticsButton(LaundryEntity, ButtonEntity):
    """Re-run the health checks now, rather than waiting for the 5-minute tick.

    The findings land on ``sensor.laundry_health``; this only refreshes them.
    Useful because the one finding that asks you to "run it again in a few
    minutes" is otherwise a five-minute wait.
    """

    _attr_name = "Laundry Run Diagnostics"
    _attr_icon = "mdi:stethoscope"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_button_diagnostics"

    async def async_press(self) -> None:
        self.coordinator.refresh_health()
