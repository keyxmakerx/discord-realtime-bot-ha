"""Sensors for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STAGE_DONE_WAITING, STAGE_LABELS, UNCLAIMED
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
            LaundryConnectionHealthSensor(coordinator, entry),
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
        coordinator = self.coordinator
        if (
            coordinator.stage == STAGE_DONE_WAITING
            and coordinator.claimed_by != UNCLAIMED
        ):
            return "Done — claimed"
        return STAGE_LABELS.get(coordinator.stage, coordinator.stage)


class LaundryConnectionHealthSensor(LaundryEntity, SensorEntity):
    """Diagnostic: how often the washer's cloud connection drops out.

    State is the number of `unavailable` blips on the job-state sensor in the
    last 24h. Useful for a dashboard chip and for judging whether a wifi/AP
    change actually helped. It never notifies — purely visibility.
    """

    _attr_name = "Laundry Connection Health"
    _attr_icon = "mdi:wifi-alert"
    _attr_native_unit_of_measurement = "drops/24h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_connection_health"

    @property
    def native_value(self) -> int:
        return self.coordinator.flap_count_24h

    @property
    def extra_state_attributes(self) -> dict:
        last = self.coordinator.last_flap
        return {
            "last_drop": last.isoformat() if last is not None else None,
            "minutes_since_last_drop": self.coordinator.minutes_since_flap,
        }
