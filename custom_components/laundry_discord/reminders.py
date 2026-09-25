"""The reminder DM loop: the only part of this integration that starts a
conversation instead of answering one. Off by default (see `enabled`).

Every send is decided and budget-claimed by `nudge.claim_plan_dm` /
`nudge.claim_select`, persisted via `assistant.async_store_budgets` before
`assistant.async_send_dm` sends — so a bounced DM still counts. Fires on
the washer being handed over or a slot's own heads-up, whichever comes
first; never polled or replayed after a restart. Reads coordinator state
one-way.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change

from . import habit as habit_mod
from . import nudge as nudge_mod
from . import people as people_mod
from . import plan as plan_mod
from . import queue as queue_mod
from .const import (
    CONF_NUDGE_LEAD,
    CONF_PLAN_DM_TIME,
    CONF_PLAN_DM_WEEKDAY,
    CONF_REMIND_DMS,
    DATA_REMINDER_OWNER,
    DEFAULT_NUDGE_LEAD,
    DEFAULT_PLAN_DM_TIME,
    DEFAULT_PLAN_DM_WEEKDAY,
    DEFAULT_REMIND_DMS,
    NUDGE_FREE_CUSTOM_ID,
    NUDGE_ON_IT_CUSTOM_ID,
    NUDGE_PUSH_CUSTOM_ID,
    NUDGE_SKIP_CUSTOM_ID,
    PLAN_CHANGE_CUSTOM_ID,
    PLAN_STOP_CUSTOM_ID,
    PLAN_YES_CUSTOM_ID,
    SIGNAL_LOAD_CLAIMED,
    SIGNAL_WASHER_FREE,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_IDLE,
    STAGE_SELF_CLEAN,
    STAGE_WASHING,
    TAKEN_NEXT_CUSTOM_ID,
    TAKEN_OK_CUSTOM_ID,
    TAKEN_PUSH_CUSTOM_ID,
    UNCLAIMED,
)

if TYPE_CHECKING:
    from .assistant import LaundryAssistant
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)

# Stages in which the machine is unambiguously occupied.
_BUSY_STAGES = (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN)

# Max seconds to wait for a send. ``async_dm_user`` awaits gateway readiness,
# so an undelivered reminder is dropped rather than left to arrive stale.
_SEND_TIMEOUT = 30


# --------------------------------------------------------------- the DM buttons
class _ReminderButton(discord.ui.Button):
    """One reply to a reminder DM: answer, change one stored thing, and
    remove the buttons so a stale tap (unopened for days) can't act twice.
    """

    reply = ""

    def __init__(
        self, assistant: "LaundryAssistant", label: str, emoji: str, custom_id: str
    ) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            emoji=emoji,
            custom_id=custom_id,
            row=0,
        )
        self.assistant = assistant

    async def act(self, interaction: discord.Interaction) -> str:
        """Do the thing, and return the line the DM should end up saying."""
        raise NotImplementedError

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            note = await self.act(interaction)
        except Exception:  # noqa: BLE001 - never let a callback reach HA
            _LOGGER.exception("Failed to handle a reminder reply")
            await _report_error(interaction)
            return
        await _close_dm(interaction, note)


class _PlanYesButton(_ReminderButton):
    """✅ Yep — acknowledged; stores nothing. A confirmation isn't a training
    signal: the loads behind the guess are already counted.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Yep", "✅", PLAN_YES_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        return (
            "✅ Good — nothing to change. (Nothing stored either: the loads "
            "behind that guess were already counted.)"
        )


class _PlanChangeButton(_ReminderButton):
    """📅 Change — opens the same week grid the 🤖 panel uses, in place of the
    DM.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Change", "📅", PLAN_CHANGE_CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_open_grid(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the week grid from a reminder")
            await _report_error(interaction)


class _PlanStopButton(_ReminderButton):
    """🔕 Stop asking — turns off `predict`, which `nudge.eligible` then
    refuses outright (no plan DM, no nudges, no grid guess). Reversible via 🔮.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Stop asking", "🔕", PLAN_STOP_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        await self.assistant.async_set_predict(interaction.user.id, False)
        return (
            "🔕 Done — I won't guess your days or message you about them again. "
            "Everything else (your load finishing, the washer coming free) is "
            "unchanged. **Start guessing** in 🤖 → 🔮 puts it back."
        )


class _NudgeOnItButton(_ReminderButton):
    """👍 On it — books the slot on the anonymous grid (nobody's name shown).
    Idempotent: a DM can be tapped twice.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "On it", "👍", NUDGE_ON_IT_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        cell = _dm_cell(self.assistant, interaction)
        if cell is None:
            return _STALE_TAP
        if not await self.assistant.async_book_cell(interaction.user.id, cell):
            return "👍 Nice one."
        return (
            "👍 Nice one — I've marked the slot taken on the week grid, so "
            "nobody else plans on top of you. No names, just a full cell."
        )


class _NudgePushButton(_ReminderButton):
    """⏭ Push to tomorrow — books tomorrow's cell instead. Not treated as a
    wrong guess (the day was right, just not tonight): goes through
    `habit.mark_nudge_pushed`, never `mark_prediction_wrong`.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Push to tomorrow", "⏭", NUDGE_PUSH_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        cell = _dm_cell(self.assistant, interaction)
        if cell is None:
            return _STALE_TAP
        user_id = interaction.user.id
        await self.assistant.async_record_push(user_id, cell)
        tomorrow = habit_mod.next_day_cell(cell)
        # Week from tomorrow's date, not today's: a Sunday push lands on
        # Monday, which is next ISO week.
        week = plan_mod.iso_week_key(self.assistant.now() + timedelta(days=1))
        if tomorrow is None or not await self.assistant.async_book_cell(
            user_id, tomorrow, week
        ):
            return "⏭ Fair enough — noted, and not counted against the guess."
        return (
            "⏭ Moved — you're down for the same slot tomorrow instead. That's "
            "not the guess being wrong, just you being busy, so it won't change "
            "what I think your usual days are."
        )


class _NudgeSkipButton(_ReminderButton):
    """🚫 Skip this week — sets `paused_until` to next Monday; expires on its
    own.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Skip this week", "🚫", NUDGE_SKIP_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        now = self.assistant.now()
        # Next Monday 00:00 local, not a fixed 7 days — skipping Saturday
        # shouldn't cost next Thursday too.
        monday = (now + timedelta(days=7 - now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await self.assistant.async_pause_until(
            interaction.user.id, monday.timestamp()
        )
        return "🚫 Right you are — nothing more from me until next week."


class _NudgeFreeButton(_ReminderButton):
    """🆓 Free it up — releases a booked slot back to the grid for others.
    Only offered on the slot heads-up; there's nothing to free about a guess.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Free it up", "🆓", NUDGE_FREE_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        cell = _dm_cell(self.assistant, interaction)
        if cell is None:
            return _STALE_TAP
        if not await self.assistant.async_free_cell(interaction.user.id, cell):
            return "🆓 Nothing to free up there — that slot wasn't yours."
        return (
            "🆓 Released — the slot's back on the grid for anyone. Thanks, "
            "that's genuinely useful to whoever was eyeing it. If it's a "
            "standing slot it'll be back next week as usual."
        )


class PlanDMView(discord.ui.View):
    """The Sunday plan DM's three answers."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(timeout=None)
        self.add_item(_PlanYesButton(assistant))
        self.add_item(_PlanChangeButton(assistant))
        self.add_item(_PlanStopButton(assistant))


class NudgeView(discord.ui.View):
    """The buttons on a reminder DM; which show depends on `kind`. One class,
    not two: `add_view` keys the persistent registry by `custom_id`, and two
    classes sharing "On it" would collide. `kind=None` is the template and
    carries every button.

    * slot heads-up: On it / Free it up / Push to tomorrow
    * opportunity: On it / Skip this week
    """

    def __init__(
        self, assistant: "LaundryAssistant", *, kind: str | None = None
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_NudgeOnItButton(assistant))
        if kind in (None, nudge_mod.MSG_SLOT):
            self.add_item(_NudgeFreeButton(assistant))
            self.add_item(_NudgePushButton(assistant))
        if kind in (None, nudge_mod.MSG_OPPORTUNITY):
            self.add_item(_NudgeSkipButton(assistant))


class _TakenNextButton(_ReminderButton):
    """🔜 Put me next — joins the line, so the normal handoff tells them when
    the washer is actually free. Never leaves the line, unlike the card's 🔜.
    """

    def __init__(
        self, assistant: "LaundryAssistant", coordinator: "LaundryCoordinator"
    ) -> None:
        super().__init__(assistant, "Put me next", "🔜", TAKEN_NEXT_CUSTOM_ID)
        self.coordinator = coordinator

    async def act(self, interaction: discord.Interaction) -> str:
        user = interaction.user
        result, place = await self.coordinator.handle_next_join(
            user.display_name, user.id
        )
        if result == queue_mod.TOGGLE_STALE:
            return "🧺 Nothing's running any more — the washer should be free."
        if result == queue_mod.TOGGLE_FULL:
            return "🔜 The line's full right now — try 🔜 on the card in a bit."
        if result == queue_mod.TOGGLE_ALREADY:
            where = f" ({queue_mod.ordinal(place)})" if place else ""
            return f"🔜 You're already in line{where} — I'll ping you when it's free."
        return queue_mod.tap_notice(result, place) or "🔜 You're in line."


class _TakenPushButton(_ReminderButton):
    """⏭ Move to tomorrow — books the same slot tomorrow. Not a habit-model
    correction: they didn't choose to skip, someone else got there first.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Move to tomorrow", "⏭", TAKEN_PUSH_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        user_id = interaction.user.id
        cell = self.assistant.nudge_cell(
            user_id, getattr(getattr(interaction, "message", None), "id", None)
        )
        tomorrow = habit_mod.next_day_cell(cell) if cell else None
        if tomorrow is None:
            return _STALE_TAP
        week = plan_mod.iso_week_key(self.assistant.now() + timedelta(days=1))
        if not await self.assistant.async_book_cell(user_id, tomorrow, week):
            return _STALE_TAP
        return "⏭ Done — you're down for the same slot tomorrow."


class _TakenOkButton(_ReminderButton):
    """👍 It's fine — acknowledged; changes nothing."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "It's fine", "👍", TAKEN_OK_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        return "👍 No worries — your booking's unchanged."


class SlotTakenView(discord.ui.View):
    """The slot-taken DM's three answers."""

    def __init__(
        self, assistant: "LaundryAssistant", coordinator: "LaundryCoordinator"
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_TakenNextButton(assistant, coordinator))
        self.add_item(_TakenPushButton(assistant))
        self.add_item(_TakenOkButton(assistant))


# Shown for a tap on an ended or unidentifiable slot. Nothing is written, so
# the wording must not imply that it was.
_STALE_TAP = (
    "That one's out of date, so I've left the week grid alone — 🤖 → 📅 if you "
    "want to put yourself down for a slot."
)


def _dm_cell(assistant: "LaundryAssistant", interaction: discord.Interaction):
    """Which cell this DM was about, or None if it can't act on the grid.
    Looked up by the tapped message's id against the note written when it
    was sent (`assistant.async_note_nudge_cell`), since a per-person cell
    can't be baked into a persistent view's `custom_id`. None if the message
    predates its note or the slot has ended — only `_STALE_TAP` to say so.
    """
    cell = assistant.nudge_cell(
        interaction.user.id, getattr(getattr(interaction, "message", None), "id", None)
    )
    if cell is None or nudge_mod.slot_ended(cell, assistant.now()):
        return None
    return cell


async def _close_dm(interaction: discord.Interaction, note: str) -> None:
    """Rewrite the DM with `note` and drop its buttons. Never raises: the
    state change already happened, so a failed edit is only cosmetic. Falls
    back to a followup if Discord refuses to edit an old message.
    """
    try:
        await interaction.response.edit_message(content=note, view=None)
        return
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not edit a reminder DM in place", exc_info=True)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(note)
        else:
            await interaction.response.send_message(note)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not acknowledge a reminder reply", exc_info=True)


async def _report_error(interaction: discord.Interaction) -> None:
    """Best-effort reply when a reminder button blew up; never raises."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong — try again in a moment."
            )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not send a reminder error response", exc_info=True)


# ------------------------------------------------------------------ the loop
class LaundryReminders:
    """Schedules the two reminder DMs. Constructed for every config entry, but
    `async_setup` returns immediately — no trigger registered, no listener
    connected — when `enabled` is False.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: "LaundryCoordinator",
    ) -> None:
        self.hass = hass
        self._entry = entry
        # Read-only, only ever for "is the washer free"; the coordinator
        # doesn't know this object exists.
        self._coordinator = coordinator
        self._assistant = coordinator.assistant
        self._unsubs: list = []
        # Sending passes currently in flight; see `_create_task`.
        self._tasks: set[asyncio.Task] = set()

    def _create_task(self, coro) -> None:
        """Schedule one sending pass and keep a strong reference to it, so it
        isn't GC'd mid-flight and `shutdown` can cancel it instead of it
        outliving a reload with stale settings.
        """
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    @callback
    def _task_done(self, task) -> None:
        """Drop a finished pass and re-raise whatever it raised, except a
        `TimeoutError` (gateway not ready — expected; logged at debug where
        it's raised).
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, TimeoutError):
            _LOGGER.debug("Reminder pass abandoned: gateway never became ready")
            return
        raise exc

    # ------------------------------------------------------------------ config
    def _option(self, key, default):
        merged = {**self._entry.data, **self._entry.options}
        return merged.get(key, default)

    @property
    def enabled(self) -> bool:
        """Whether the bot may DM anybody unprompted. Default off; requires
        both the reminders option and habit learning (which supplies the load
        history `washed_today` needs).
        """
        if not bool(self._option(CONF_REMIND_DMS, DEFAULT_REMIND_DMS)):
            return False
        return bool(self._assistant.learn_habits)

    @property
    def plan_weekday(self) -> int:
        """Which day the plan DM goes out — 0 Monday, 6 Sunday."""
        try:
            day = int(self._option(CONF_PLAN_DM_WEEKDAY, DEFAULT_PLAN_DM_WEEKDAY))
        except (TypeError, ValueError):
            return DEFAULT_PLAN_DM_WEEKDAY
        return day if plan_mod.is_weekday(day) else DEFAULT_PLAN_DM_WEEKDAY

    @property
    def plan_clock(self) -> tuple[int, int]:
        """What time it goes out, as ``(hour, minute)``."""
        clock = nudge_mod.parse_clock(
            self._option(CONF_PLAN_DM_TIME, DEFAULT_PLAN_DM_TIME)
        )
        return clock or nudge_mod.parse_clock(DEFAULT_PLAN_DM_TIME) or (18, 0)

    @property
    def nudge_lead(self) -> int:
        """Minutes before a slot starts that its heads-up fires. Default 60."""
        try:
            return int(self._option(CONF_NUDGE_LEAD, DEFAULT_NUDGE_LEAD))
        except (TypeError, ValueError):
            return DEFAULT_NUDGE_LEAD

    # ------------------------------------------------------------------- setup
    async def async_setup(self) -> None:
        """Register the triggers, or do nothing if `enabled` is False (checked
        first, before any trigger, listener, or store read).
        """
        if not self.enabled:
            return
        # Budget Store is global: only the first entry loaded runs this loop,
        # or two washers (two entries) could double the send cap.
        owner = self.hass.data.get(DATA_REMINDER_OWNER)
        if owner is not None and owner != self._entry.entry_id:
            return
        self.hass.data[DATA_REMINDER_OWNER] = self._entry.entry_id
        hour, minute = self.plan_clock
        # Fires daily; the handler filters to the configured weekday since
        # HA's time trigger has no weekday of its own.
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_plan_time, hour=hour, minute=minute, second=0
            )
        )
        # One heads-up per slot, `nudge_lead` minutes before that slot starts.
        for slot in plan_mod.SLOTS:
            clock = nudge_mod.heads_up_clock(slot, self.nudge_lead)
            if clock is None:  # unreachable for a real slot; belt and braces
                continue
            self._unsubs.append(
                async_track_time_change(
                    self.hass,
                    self._on_fallback_time,
                    hour=clock[0],
                    minute=clock[1],
                    second=0,
                )
            )
        # The coordinator's own handoff signal — the same "free", not a second
        # definition of it.
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_WASHER_FREE, self._on_washer_free
            )
        )
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_LOAD_CLAIMED, self._on_load_claimed
            )
        )

    async def shutdown(self) -> None:
        """Drop every trigger/listener and cancel any pass in flight (safe to
        call twice); releases the single-owner claim only if it's ours.
        Async because a pass already running can hold stale settings for up
        to `_SEND_TIMEOUT` per person, so in-flight ones are cancelled and
        awaited, not just dropped.
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.hass.data.get(DATA_REMINDER_OWNER) == self._entry.entry_id:
            self.hass.data.pop(DATA_REMINDER_OWNER, None)
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    # ---------------------------------------------------------------- triggers
    @callback
    def _on_plan_time(self, _now) -> None:
        # Use the planner's own clock, not the one HA hands in.
        if self._assistant.now().weekday() != self.plan_weekday:
            return
        self._create_task(self._async_send_plan_dms())

    @callback
    def _on_fallback_time(self, _now) -> None:
        self._create_task(self._async_send_nudges(released=False))

    @callback
    def _on_washer_free(self, payload=None) -> None:
        """The washer just changed hands. Scheduled onto its own task, never
        awaited inline, since the signal fires while the coordinator holds
        the session lock. Skips entirely if handed to the queue
        (`handed_off`); passes `hedged` and `claimant_id` through for the
        stricter free-test and to exclude the person whose own load this was.
        """
        data = payload if isinstance(payload, dict) else {}
        if data.get("handed_off"):
            return
        self._create_task(
            self._async_send_nudges(
                released=not data.get("hedged", False),
                just_washed_id=data.get("claimant_id"),
            )
        )

    @callback
    def _on_load_claimed(self, payload=None) -> None:
        """Someone claimed a load; tell anyone whose booked slot it's in."""
        data = payload if isinstance(payload, dict) else {}
        self._create_task(self._async_send_taken(data.get("claimant_id")))

    # ------------------------------------------------------------- the sending
    async def _async_send_taken(self, claimant_id) -> None:
        """The slot-taken DM: one per person per slot, never naming anyone.

        Not charged to the DM budget (it's about their own booking and would
        otherwise be blocked on any day they already had a heads-up), but
        gated like every reminder DM, including 🔔 Slot taken and quiet hours.
        """
        now = self._assistant.now()
        targets = nudge_mod.slot_taken_targets(
            self._assistant.running_cells(),
            self._assistant.occupancy(),
            claimant_id,
            waiting=[entry.get("id") for entry in self._coordinator.queue],
        )
        for user_id, cell in targets.items():
            try:
                if nudge_mod.eligible(
                    self._assistant.people_map,
                    user_id,
                    now,
                    kind=people_mod.KIND_TAKEN,
                ) != nudge_mod.REASON_OK:
                    continue
                text = nudge_mod.taken_text(cell)
                # Recorded before sending: a bounced DM mustn't be retried.
                if text is None or not await self._assistant.async_claim_taken_notice(
                    user_id, cell
                ):
                    continue
                message = await self._async_deliver(
                    user_id, text, SlotTakenView(self._assistant, self._coordinator)
                )
                if message is None:
                    continue
                await self._assistant.async_note_nudge_cell(
                    user_id, cell, getattr(message, "id", None)
                )
                _LOGGER.debug("Sent the slot-taken DM to %s", user_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to send a slot-taken DM to %s", user_id)

    async def _async_send_plan_dms(self) -> None:
        """The Sunday plan DM, one per opted-in person. Nobody with no
        confident prediction is sent anything — silence is the default, not a
        DM saying the bot doesn't know their days yet.
        """
        now = self._assistant.now()
        for user_id in self._assistant.people_map:
            try:
                people_map = self._assistant.people_map
                # Cheap gate first, so an all-opted-out house scans nobody's
                # history. No `kind` here: the per-kind/quiet checks are
                # re-asked inside `claim_plan_dm`, which is what actually
                # claims the budget.
                if nudge_mod.eligible(people_map, user_id, now) != (
                    nudge_mod.REASON_OK
                ):
                    continue
                prediction = self._assistant.prediction_for(user_id)
                reason, budgets = nudge_mod.claim_plan_dm(
                    people_map,
                    self._assistant.budgets,
                    user_id,
                    prediction,
                    now,
                )
                # Persist before sending: a bounced DM still costs the claim.
                await self._assistant.async_store_budgets(budgets)
                if reason != nudge_mod.REASON_OK:
                    self._log_drop(user_id, reason)
                    continue
                text = nudge_mod.plan_dm_text(prediction)
                if text is None:
                    continue
                if not await self._async_deliver(
                    user_id, text, PlanDMView(self._assistant)
                ):
                    continue
                _LOGGER.debug("Sent the plan DM to %s", user_id)
            except Exception:  # noqa: BLE001 - one bad record shouldn't stop the rest
                _LOGGER.exception("Failed to send a plan DM to %s", user_id)

    async def _async_send_nudges(
        self, *, released: bool, just_washed_id=None
    ) -> None:
        """At most one message per person, chosen by `nudge.select`. Both
        triggers (slot-soon clock, washer-free signal) call this; whichever
        runs first wins, and `claim_select`'s budget claim makes the second
        one a no-op.
        """
        now = self._assistant.now()
        free = self._washer_free(released=released)
        # str() both sides: session-store ids are ints, planner-store ids are
        # strings, and a mismatch here would silently send the DM this guards
        # against.
        just_washed = None if just_washed_id is None else str(just_washed_id)
        occupancy = self._assistant.occupancy()
        for user_id in self._assistant.people_map:
            try:
                people_map = self._assistant.people_map
                # No `kind` yet: `select` hasn't chosen slot-vs-opportunity, so
                # naming one here could wrongly drop someone who only muted
                # the other kind.
                if nudge_mod.eligible(people_map, user_id, now) != (
                    nudge_mod.REASON_OK
                ):
                    continue
                prediction = self._assistant.prediction_for(user_id)
                kind, cell, reason, budgets = nudge_mod.claim_select(
                    people_map,
                    self._assistant.budgets,
                    user_id,
                    now,
                    booked=self._assistant.booked_cells(user_id),
                    prediction=prediction,
                    occupancy=occupancy,
                    washer_free=free,
                    # Their own learned cadence, not a fixed number of days.
                    due=self._assistant.is_due(user_id),
                    # Avoids nudging someone about a load they just finished.
                    loads=self._assistant.load_times(user_id),
                    # Coordinator-confirmed; history only covers claimed loads.
                    just_washed=just_washed is not None
                    and str(user_id) == just_washed,
                    lead_minutes=self.nudge_lead,
                )
                # Persist before sending: a bounced DM still costs the claim.
                await self._assistant.async_store_budgets(budgets)
                if reason != nudge_mod.REASON_OK:
                    self._log_drop(user_id, reason)
                    continue
                if kind == nudge_mod.MSG_SLOT:
                    # Measured distance to the slot, not the configured lead:
                    # a washer-free trigger can fire earlier than
                    # slot-start-minus-lead.
                    text = nudge_mod.heads_up_text(
                        cell, nudge_mod.minutes_until_slot(cell, now)
                    )
                else:
                    text = nudge_mod.opportunity_text(
                        cell, prediction, self._assistant.typical_gap(user_id)
                    )
                if text is None:
                    continue
                message = await self._async_deliver(
                    user_id, text, NudgeView(self._assistant, kind=kind)
                )
                if message is None:
                    continue
                # Noted after the send since it's keyed by the message id; a
                # tap in the brief window before this runs reads as stale.
                await self._assistant.async_note_nudge_cell(
                    user_id, cell, getattr(message, "id", None)
                )
                _LOGGER.debug("Sent the %s DM to %s", kind, user_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to send a nudge to %s", user_id)

    async def _async_deliver(self, user_id, text: str, view):
        """Send one reminder within `_SEND_TIMEOUT`, or give up (returns the
        sent message, or None). `async_dm_user` awaits gateway readiness, so
        an undelivered attempt would otherwise park until reconnect and
        arrive hours late about a long-over slot. Not refunded on a drop,
        same as being over budget.
        """
        try:
            async with asyncio.timeout(_SEND_TIMEOUT):
                return await self._assistant.async_send_dm(user_id, text, view)
        except TimeoutError:
            # Not CancelledError: that's HA shutting down and must propagate.
            _LOGGER.debug(
                "Reminder for %s dropped: not delivered within %ss",
                user_id,
                _SEND_TIMEOUT,
            )
            return None

    def _log_drop(self, user_id, reason: str) -> None:
        """Logs a debug line, but only when the budget was what dropped the
        message — every other reason is routine, and logging those too would
        flood the log of a house that's opted into nothing.
        """
        if reason in nudge_mod.BUDGET_REASONS:
            _LOGGER.debug("Reminder for %s dropped: over the %s budget", user_id, reason)

    def _washer_free(self, *, released: bool) -> bool:
        """Whether the washer counts as free. `released=True` (a confirmed
        handoff) only re-checks whether a new load has started since. A
        *hedged* release (backstop fired, unconfirmed) is treated like a
        clock trigger: "done" isn't "empty", so a finished load whose owner
        hasn't emptied it isn't free unless nobody claimed it.
        """
        stage = self._coordinator.stage
        if stage in _BUSY_STAGES:
            return False
        if released or stage == STAGE_IDLE:
            return True
        if stage != STAGE_DONE_WAITING:
            return False
        return bool(
            self._coordinator.emptied or self._coordinator.claimed_by == UNCLAIMED
        )
