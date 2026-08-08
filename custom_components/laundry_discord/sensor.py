"""Sensors for the Laundry Discord Bot integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import queue as queue_mod
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
    # The names ride on the live state (that is the point — a dashboard card),
    # but they are kept out of recorder history. Persisted, every 🔜 tap would
    # leave a timestamped row naming who joined and who left, and the retention
    # window would then answer "who queues most" — the tally design doc §11
    # bans. `queue_count` stays recorded: it is the same shape of fact the card
    # shows and names nobody.
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
        """The 🔜 line, so it can reach a dashboard without opening Discord.

        It rides on this entity rather than a new one because it is the same
        fact — what the machine is doing and who is waiting on it — and a
        second entity would need its own restore, its own availability and its
        own row in every dashboard that already shows this one.

        **Count and names only.** The recorder writes a history row whenever a
        state *or an attribute* changes, and this entity is refreshed by the
        5-minute health tick, so anything derived from the clock — how long
        somebody has waited, when they tapped — would differ on every single
        tick and write ~288 rows a day forever. That is exactly the bug the
        connection-health sensor had to be fixed for, and it is not worth
        reintroducing for a number nobody reads. These three change when
        somebody taps 🔜 (or an entry ages out), and at no other time.

        The selection rules live in :func:`queue.attributes` so the pure tests
        cover them: the stored line is only pruned when something happens to
        it, so read cold it can still name somebody the handoff would skip.
        """
        coordinator = self.coordinator
        return queue_mod.attributes(
            coordinator.queue,
            dt_util.utcnow().timestamp(),
            float(coordinator.queue_expiry),
            coordinator.claimed_by_id,
        )


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
        """Last drop, and roughly how long ago — deliberately *roughly*.

        The recorder writes a history row whenever a state or an attribute
        changes, and this entity is refreshed by the 5-minute health tick. A
        minutes-since value recomputed to one decimal therefore differed on
        every single tick, so this one diagnostic wrote ~288 rows a day
        forever — and on this washer, whose cloud drops roughly hourly, the
        flap list is never empty, so it never quiets down.

        Bucketing to a quarter of an hour keeps the attribute answering the
        only question anyone actually asks it ("recently, or ages ago?") while
        making it change 4 times an hour at most instead of 12, and not at all
        overnight once a drop is hours old. The precise value stays available
        as the timestamp above.
        """
        last = self.coordinator.last_flap
        minutes = self.coordinator.minutes_since_flap
        return {
            "last_drop": last.isoformat() if last is not None else None,
            "minutes_since_last_drop": (
                None if minutes is None else int(minutes // 15) * 15
            ),
        }
