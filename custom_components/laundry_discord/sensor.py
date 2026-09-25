"""Sensors for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import queue as queue_mod
from .const import STAGE_DONE_WAITING, STAGE_LABELS, UNCLAIMED
from .coordinator import LaundryConfigEntry
from .entity import LaundryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LaundryConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the laundry sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LaundryClaimedBySensor(coordinator, entry),
            LaundryStageSensor(coordinator, entry),
            LaundryConnectionHealthSensor(coordinator, entry),
            LaundryHealthSensor(coordinator, entry),
        ]
    )


class LaundryClaimedBySensor(LaundryEntity, SensorEntity):
    """Who currently has the load (or 'Unclaimed')."""

    _attr_translation_key = "claimed_by"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_claimed_by"

    @property
    def native_value(self) -> str:
        return self.coordinator.claimed_by or UNCLAIMED


class LaundryStageSensor(LaundryEntity, SensorEntity):
    """Current stage (Idle / Washing / Drying / Done — waiting / Done — claimed)."""

    _attr_translation_key = "stage"
    # The names of who is waiting stay out of recorder history, so the history
    # can't become a per-person tally. The count is fine to keep.
    _unrecorded_attributes = frozenset({"queue", "next_up"})

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

    @property
    def extra_state_attributes(self) -> dict:
        """The 🔜 line: ``queue_count``, ``queue`` and ``next_up``.

        Nothing clock-derived goes here: this entity is rewritten on every
        5-minute tick, and a changing attribute would write a recorder row each
        time. Read through :func:`queue.attributes` so expired entries are
        pruned.
        """
        coordinator = self.coordinator
        return queue_mod.attributes(
            coordinator.queue,
            dt_util.utcnow().timestamp(),
            float(coordinator.queue_expiry),
            coordinator.claimed_by_id,
        )


class LaundryConnectionHealthSensor(LaundryEntity, SensorEntity):
    """Number of times the washer's job-state sensor went unavailable in 24h."""

    _attr_translation_key = "connection_health"
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
        # Minutes are bucketed to 15 so the attribute doesn't change (and write
        # a recorder row) on every 5-minute tick.
        last = self.coordinator.last_flap
        minutes = self.coordinator.minutes_since_flap
        return {
            "last_drop": last.isoformat() if last is not None else None,
            "minutes_since_last_drop": (
                None if minutes is None else int(minutes // 15) * 15
            ),
        }


class LaundryHealthSensor(LaundryEntity, SensorEntity):
    """The diagnostics findings, refreshed every 5 minutes.

    State is the worst severity (``ok``/``note``/``warning``/``problem``); the
    summary and findings are attributes. ``findings`` carries ages in minutes,
    so it is kept out of recorder history.
    """

    _attr_translation_key = "health"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"findings"})

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_health"

    @property
    def native_value(self) -> str:
        return self.coordinator.health.get("severity", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        health = self.coordinator.health
        findings = health.get("findings") or []
        return {
            "summary": health.get("summary"),
            "problems": sum(1 for f in findings if f.get("severity") == "problem"),
            "warnings": sum(1 for f in findings if f.get("severity") == "warning"),
            "headlines": [f.get("headline") for f in findings],
            "findings": findings,
        }
