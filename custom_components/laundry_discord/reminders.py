"""The reminder DM loop (design doc §10) — the one thing here that starts a
conversation.

Everything else in this integration answers something: a button was tapped, a
meter moved, a load finished. This module decides, on its own schedule, to put a
message on somebody's phone. That single difference is why it lives in its own
file, behind its own option, defaulting **off**, and why every send in it is
routed through the same three chokepoints:

* :func:`nudge.claim_plan_dm` / :func:`nudge.claim_select` — the decision
  *and* the budget claim, in one call (P2). Nothing sends without one.
* :meth:`assistant.LaundryAssistant.async_store_budgets` — the claim is
  persisted **before** the send, so a DM that bounces still costs a nudge.
* :meth:`assistant.LaundryAssistant.async_send_dm` — the existing plumbing,
  which arms the §10.5 self-heal notice when Discord refuses.

Two things shape the scheduling:

* **Event-driven, not clock-driven (§10.4).** A fixed 18:00 reminder is a guess;
  the washer actually being free is a fact. So a message fires on whichever
  comes first — the coordinator handing the machine over, or the heads-up an
  hour before a booked slot opens. The first of those reuses the *existing*
  handoff moments via :data:`const.SIGNAL_WASHER_FREE`; there is deliberately no
  second definition of "free" anywhere in this file.

  The heads-up replaced a day-of nudge that fired near the slot's *end*, which
  was too late to act on and made a booking the weakest signal in the system
  rather than the strongest. :func:`nudge.select` then decides which single
  message, if any, is worth sending — so the two triggers cannot produce two
  DMs about one evening.
* **Triggers, not polling.** Everything here is either
  ``async_track_time_change`` (fires once, at a wall-clock time) or a dispatcher
  callback. Nothing runs on a tick, so nothing is evaluated — and nothing is
  written — on a household where nobody has opted in. A restart re-registers the
  triggers for their *next* occurrence, so a trigger missed while HA was down is
  simply missed: a nudge about a slot that has already passed is worse than no
  nudge, and a burst of catch-up DMs at boot is how this feature would get
  muted on day one.

The dependency runs one way, as §14 rule 5 requires: this module reads
coordinator state and subscribes to a signal, and the coordinator neither
imports it nor knows it exists.
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
from . import plan as plan_mod
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
    SIGNAL_WASHER_FREE,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_IDLE,
    STAGE_SELF_CLEAN,
    STAGE_WASHING,
    UNCLAIMED,
)

if TYPE_CHECKING:
    from .assistant import LaundryAssistant
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)

# Stages in which the machine is unambiguously occupied.
_BUSY_STAGES = (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN)

# How long a reminder is allowed to take to leave the building before it is
# dropped instead. ``DiscordBot.async_dm_user`` starts with
# ``wait_until_ready()``, so a nudge decided while the gateway is down would
# otherwise sit in that await until it reconnects and then arrive hours later,
# announcing a slot that ended at midnight and a washer whose state is long
# since unknown — and hold up everybody after it in the same pass. Over budget
# is dropped rather than queued (P2); undeliverable is the same thing.
_SEND_TIMEOUT = 30


# --------------------------------------------------------------- the DM buttons
class _ReminderButton(discord.ui.Button):
    """One reply to a reminder DM.

    Every subclass does the same three things — answer the interaction, change
    one stored thing, and take the buttons away — so the shared parts live here.
    Taking the buttons away matters: a DM sits in the inbox indefinitely, and
    "On it" tapped twice, a week apart, must not book a second slot.
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
    """✅ Yep — acknowledged, and deliberately nothing stored.

    §7.3 allows the model exactly two training signals: real claims and explicit
    corrections. A confirmation is neither — the loads behind the guess are
    already in history and already counted — so writing anything here would be
    the guess feeding itself evidence with a human tap laundering it. The reply
    says so rather than implying a reward.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Yep", "✅", PLAN_YES_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        return (
            "✅ Good — nothing to change. (Nothing stored either: the loads "
            "behind that guess were already counted.)"
        )


class _PlanChangeButton(_ReminderButton):
    """📅 Change — open the week grid, right here in the DM.

    The same ephemeral grid the 🤖 panel opens, because "change my days" already
    has a surface and a second one would be a second thing to keep in step. The
    DM message becomes the grid; its own ↩️ Back button leads to the panel.
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
    """🔕 Stop asking — permanent, and never re-prompted.

    Sets the person's ``predict`` preference off, which :func:`nudge.eligible`
    refuses outright: no Sunday DM, no day-of nudge, and no ? on their grid
    either. The 🔮 panel's own button is the way back, so this is an opt-out
    rather than a one-way door (P7).
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
    """👍 On it — mark the slot taken on the anonymous board (§10.3).

    So the house knows without anybody announcing it: the board shows a cell is
    taken and never by whom (P5). Idempotent, because a DM can be tapped twice.
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
    """⏭ Push to tomorrow — moves the plan, and is **not** a wrong guess.

    §7.3 is exact: the day was right, the person just isn't doing it tonight, so
    this goes through :func:`habit.mark_nudge_pushed` and never near
    ``mark_prediction_wrong``. Tomorrow's cell is then booked, which is what
    actually *moves* the nudge — the day-of trigger reads bookings first, so
    tomorrow's slot is what they're down for tomorrow.
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
        # The week comes from tomorrow's own **date**, not from today's: a cell
        # key carries a weekday and no date, so a Sunday push lands on Monday —
        # which belongs to the next ISO week. Booking it under this one writes
        # the Monday that is already six days gone, and the push silently
        # doesn't move anything.
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
    """🚫 Skip this week — silent for the rest of the week (§10.3).

    Implemented as ``paused_until``, the pause :mod:`people` has carried since
    the panel shipped, set to next Monday. It expires on its own, so "skip" is a
    week off rather than another permanent setting to remember having set.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(assistant, "Skip this week", "🚫", NUDGE_SKIP_CUSTOM_ID)

    async def act(self, interaction: discord.Interaction) -> str:
        now = self.assistant.now()
        # Next Monday, 00:00 local — "the rest of this week", however much of it
        # is left. Not a fixed seven days: skipping on a Saturday should not
        # cost the following Thursday too.
        monday = (now + timedelta(days=7 - now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await self.assistant.async_pause_until(
            interaction.user.id, monday.timestamp()
        )
        return "🚫 Right you are — nothing more from me until next week."


class _NudgeFreeButton(_ReminderButton):
    """🆓 Free it up — hand a booked slot back to the house.

    The one button here that serves somebody *other* than the person tapping
    it, and the reason the heads-up is worded as a question rather than a
    reminder. A reservation about to lapse unused is exactly the capacity the
    grid exists to reclaim, so the reply somebody gives when the answer is "no,
    not tonight" still does something useful — instead of the message being
    ignored and the slot sitting there blocking a housemate all evening.

    Only ever offered on the slot heads-up, because it is the only message
    about a cell they actually booked. There is nothing to free about a guess.
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
    """The Sunday DM's three answers (§10.2)."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(timeout=None)
        self.add_item(_PlanYesButton(assistant))
        self.add_item(_PlanChangeButton(assistant))
        self.add_item(_PlanStopButton(assistant))


class NudgeView(discord.ui.View):
    """The replies to a reminder DM, which differ by what it was about.

    One view class rather than two, for the reason :class:`GridView` is one
    class: ``add_view`` keys the persistent registry by ``custom_id``, so two
    view classes sharing 👍 On it would mean the second registration quietly
    wins for both. Built with no ``kind`` this is the registration template and
    carries **every** button; a real DM carries the subset that makes sense.

    * **The slot heads-up** — you booked this and it starts within the hour, so
      all three answers are live: do it, give it back, or move it.
    * **The opportunity** — nothing was booked, so there is nothing to free and
      nothing to push. Just "yes" and "leave me alone this week".
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


# What a reminder reply says when its own slot is over, or when the message is
# too old for us to still know which slot it meant. Nothing is written in either
# case, so the reply must not imply anything was: a person who taps 👍 at
# breakfast and is told the house has been informed is worse off than one who is
# told the truth. Worded to cover both, because from the reader's side they are
# the same thing — an old message that no longer does anything.
_STALE_TAP = (
    "That one's out of date, so I've left the week grid alone — 🤖 → 📅 if you "
    "want to put yourself down for a slot."
)


def _dm_cell(assistant: "LaundryAssistant", interaction: discord.Interaction):
    """Which cell **this** DM was about, or None if it can't act on the grid.

    Looked up by the tapped message's own id, against the note written when it
    was sent (:meth:`assistant.LaundryAssistant.async_note_nudge_cell`). Baking
    the cell into the ``custom_id`` is not available — a per-person, per-cell id
    cannot be a persistent view, so the button would die at the next restart —
    so the id is the handle, and the store holds the meaning.

    Two earlier readings of this question are gone, and both were wrong in the
    same direction: they answered confidently about the wrong slot.

    * **The message's timestamp.** A DM sits in an inbox indefinitely, so
      "whatever slot is running when they finally look" is a different slot in
      the ordinary case. Worse after v0.26.0: the heads-up now goes out
      ``nudge_lead`` minutes *before* its slot opens, so the send always lands
      inside the **previous** slot's window — 19:00 for a 20:00 booking reads as
      PM — and the reading is not merely stale but systematically one slot
      early.
    * **The last cell noted for that person.** One note per person, overwritten
      by every DM and tied to no message, so a heads-up from Monday that nobody
      answered — still live, because :class:`NudgeView` is persistent and its
      buttons never expire — dispatched *Tuesday's* cell when it was finally
      tapped. 🆓 Free it up then released a booking they still wanted, on a
      reply that names no slot, so they could not even tell.

    None means "we cannot say which slot this message meant", which covers a DM
    older than the note, a DM from before the note existed, and a tap that beat
    the note being written. There is nothing useful left to write in any of them
    — only something honest left to say (:data:`_STALE_TAP`).

    A tap after the slot has ended is also None, and separately: booking a
    window that has closed tells the house nothing and blocks a slot nobody can
    use.
    """
    cell = assistant.nudge_cell(
        interaction.user.id, getattr(getattr(interaction, "message", None), "id", None)
    )
    if cell is None or nudge_mod.slot_ended(cell, assistant.now()):
        return None
    return cell


async def _close_dm(interaction: discord.Interaction, note: str) -> None:
    """Answer the tap by rewriting the DM and dropping its buttons.

    Never raises, and never leaves the user staring at "interaction failed":
    the state change already happened before we got here, so a failed edit is
    cosmetic. A followup is the fallback because the original message may be old
    enough that Discord refuses the edit.
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
    """Schedules the two reminder DMs, and sends neither unless asked to.

    Constructed for every config entry, but :meth:`async_setup` returns
    immediately when the option is off — before a single trigger is registered
    or listener connected. With the flag off this object holds three references
    and does nothing else for the life of the entry.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: "LaundryCoordinator",
    ) -> None:
        self.hass = hass
        self._entry = entry
        # Read-only, and only ever for "is the washer free" (§14 rule 5). The
        # coordinator does not know this object exists.
        self._coordinator = coordinator
        self._assistant = coordinator.assistant
        self._unsubs: list = []
        # The sending passes currently in flight; see :meth:`_create_task`.
        self._tasks: set[asyncio.Task] = set()

    def _create_task(self, coro) -> None:
        """Schedule one sending pass, and keep hold of it.

        The same reasoning as :meth:`coordinator.LaundryCoordinator._create_task`
        and the same failure it prevents: a pass created against ``hass`` alone
        outlives :meth:`shutdown`, so unloading the entry — or reloading it on an
        options change — left a half-finished round of DMs still going out under
        the settings the user had just changed, against an assistant and a client
        the new entry has replaced.

        It also closes the ordinary asyncio hazard that the loop holds only a
        weak reference to a running task; each one drops itself here when it
        finishes.
        """
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    @callback
    def _task_done(self, task) -> None:
        """Drop a finished pass, and answer for whatever it raised.

        The same reasoning as :meth:`coordinator.LaundryCoordinator._task_done`,
        and it matters slightly more here: a sending pass is scheduled from a
        clock trigger nobody is watching, so an unretrieved ``TimeoutError``
        would be a stack trace at ERROR every time the gateway happened to be
        down at 05:00. That is a log line about the bot working as intended.
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
        """Whether the bot may contact anybody unprompted at all. Default off.

        Two options, both of which have to be on — which is what the option's
        own description already promises the person switching it on. Day
        learning is not decoration here: it is what writes the load history,
        and that history is the only record of "they have already washed today"
        that stops the washer freeing at 21:40 from nudging the person who just
        emptied it. Letting the nudge run while its own guard was dead is how a
        reminder becomes the thing everybody mutes.
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
        """Minutes before a slot **starts** that its heads-up fires.

        The same option as before, with its meaning corrected rather than
        replaced: it used to be a lead before the slot *ended*, which is the one
        line that made a reservation worthless — booking Thursday Eve bought
        nothing until you were already standing inside it. The default was, and
        still is, 60, so nobody's configuration changes meaning under them by
        more than the fix they wanted.
        """
        try:
            return int(self._option(CONF_NUDGE_LEAD, DEFAULT_NUDGE_LEAD))
        except (TypeError, ValueError):
            return DEFAULT_NUDGE_LEAD

    # ------------------------------------------------------------------- setup
    async def async_setup(self) -> None:
        """Register the triggers — or, with the option off, do nothing at all.

        The early return is the whole of the kill switch, and it is first: no
        trigger, no listener, no read of the store, no clock call. There is
        nothing below it that a disabled entry can reach.
        """
        if not self.enabled:
            return
        # Exactly one entry runs this loop. The planner Store key is global —
        # one household, one set of people, one nudge budget — but the config
        # flow keys entries on the *channel*, so a house with two washers has
        # two entries, two LaundryAssistants over that one file and two
        # in-memory copies of the budget. Both would claim Sunday's DM against
        # their own copy, both would be allowed, and every reminder would go out
        # twice at double the intended cap. First entry loaded wins; the others
        # register nothing.
        owner = self.hass.data.get(DATA_REMINDER_OWNER)
        if owner is not None and owner != self._entry.entry_id:
            return
        self.hass.data[DATA_REMINDER_OWNER] = self._entry.entry_id
        hour, minute = self.plan_clock
        # Fires once a day at the configured time; the handler drops everything
        # but the configured weekday. HA's time trigger has no weekday of its
        # own, and one comparison a day is not a poll.
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_plan_time, hour=hour, minute=minute, second=0
            )
        )
        # One heads-up per slot, an hour before that slot's own **start**. This
        # replaces the retired day-of nudge, whose lead was measured from the
        # slot's *end* — which is why booking Thursday Eve used to do nothing
        # until you were already inside it.
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
        # §10.4 trigger (a): the coordinator's own handoff moments. Not a second
        # notion of "free" — the same one the queue is handed off on.
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_WASHER_FREE, self._on_washer_free
            )
        )

    async def shutdown(self) -> None:
        """Drop every trigger and listener, and stop any pass in flight.

        Safe to call twice. Releases the single-owner claim too, and only if it
        is ours — so reloading the entry that owns the loop hands it back rather
        than leaving the household with no reminder loop at all, and unloading
        one of the others leaves the owner alone.

        Awaitable rather than a plain ``@callback`` because dropping the
        triggers only stops *new* passes. A pass already running holds the
        people map, the budgets and the assistant of the entry being unloaded,
        and can sit for the full :data:`_SEND_TIMEOUT` on each person — so an
        options change (which reloads the entry) could have DMs going out under
        the settings somebody had just replaced. ``ConfigEntry.async_on_unload``
        takes a coroutine function and waits for it, so this costs nothing on
        the ordinary path where there is nothing to cancel.
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
        # The weekday comes from the planner's own clock rather than the one HA
        # hands in, so there is exactly one definition of what day it is here.
        if self._assistant.now().weekday() != self.plan_weekday:
            return
        self._create_task(self._async_send_plan_dms())

    @callback
    def _on_fallback_time(self, _now) -> None:
        self._create_task(self._async_send_nudges(released=False))

    @callback
    def _on_washer_free(self, payload=None) -> None:
        """The washer just changed hands. Scheduled, never awaited inline.

        The signal is sent while the coordinator holds the session lock, so
        this returns immediately and the work happens on its own task — nothing
        in this module ever takes that lock, and nothing it does may delay a
        handoff ping.

        Three things come with the signal, and each of them can stop this dead:

        * **It was handed to the 🔜 line.** Then it is not free, it is theirs.
          Telling a second person the same machine is available is the queue and
          the reminders disagreeing about reality in front of two people, and
          whoever walks down first wins. Nothing is scheduled at all.
        * **It was hedged** — the backstop fired and nobody confirmed anything.
          "Probably free, worth a look" is not the fact the DM asserts, so the
          decision falls back to the same stricter test a clock trigger gets:
          identical drum contents must not produce opposite verdicts 45 minutes
          apart.
        * **Whose load it was**, so the one person guaranteed not to need this
          message is the one person who cannot receive it.
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

    # ------------------------------------------------------------- the sending
    async def _async_send_plan_dms(self) -> None:
        """The Sunday plan DM, one per opted-in person (§10.2).

        Somebody with no confident prediction gets **nothing** — not a DM saying
        the bot doesn't know their days yet. Silence is the default (P6), and
        for the first month it is every person.
        """
        now = self._assistant.now()
        for user_id in self._assistant.people_map:
            try:
                people_map = self._assistant.people_map
                # The cheap gate first, so a house where nobody has opted in
                # does no work at all: reading a prediction means scanning that
                # person's history, and there is no point doing it for somebody
                # who cannot be messaged whatever it says.
                #
                # Deliberately **no kind**, even though this loop sends exactly
                # one and knows which. A pre-filter is allowed to be broader
                # than the decision it precedes and never narrower: 📅 and the
                # quiet window are re-asked inside :func:`nudge.claim_plan_dm`,
                # which is where the budget and the reason string come from, so
                # asking here as well would only mean two places to keep in step
                # for a check that costs a dict read.
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
                # Persisted first, always: the claim is spent whether or not
                # the send lands (see the module docstring).
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
            except Exception:  # noqa: BLE001 - one bad record isn't the house
                _LOGGER.exception("Failed to send a plan DM to %s", user_id)

    async def _async_send_nudges(
        self, *, released: bool, just_washed_id=None
    ) -> None:
        """At most **one** message per person, chosen for them right now.

        Both triggers run this same method, because they are the same question
        asked at two different moments — an hour before a slot opens, and when
        the washer changes hands. Which one got here first is not recorded and
        does not matter: the budget claim inside :func:`nudge.claim_select` is
        what makes the second a no-op.

        The choice itself lives in :func:`nudge.select`, so the rule that there
        is only ever one message is enforced in a pure function with tests
        rather than by four triggers agreeing to take turns.
        """
        now = self._assistant.now()
        free = self._washer_free(released=released)
        # str() on both sides: ids come off the session store as ints and out of
        # the planner store as strings, and a mismatch here is silent — it just
        # sends the one DM this whole guard exists to stop.
        just_washed = None if just_washed_id is None else str(just_washed_id)
        occupancy = self._assistant.occupancy()
        for user_id in self._assistant.people_map:
            try:
                people_map = self._assistant.people_map
                # Same reasoning as the plan DM: the preference check is a dict
                # read, and everything after it is not.
                #
                # Kind-agnostic, and here it is load-bearing rather than merely
                # tidy. This pass does not yet know which message it would send
                # — :func:`nudge.select` decides that from a booking, a
                # prediction and the occupancy, none of which have been read
                # yet — so there is no kind to pass. Naming one anyway would
                # mean picking ⏰ or 💡 before the branch that chooses between
                # them, and somebody who switched off only the one we guessed
                # would be dropped here, never reaching the branch that would
                # have sent them the other.
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
                    # Their own learned cadence, not a fixed number of days:
                    # the twice-a-week washer and the fortnightly one are both
                    # left alone until *they* are overdue.
                    due=self._assistant.is_due(user_id),
                    # The washer coming free is very often this person's own
                    # load finishing, and being told to do the laundry you have
                    # just done is the fastest way to get a bot muted.
                    loads=self._assistant.load_times(user_id),
                    # ...and the history is not always there to say so: it is
                    # written only for a load somebody tapped Claim on. The
                    # coordinator knows whose load just ended, so it is asked
                    # rather than inferred.
                    just_washed=just_washed is not None
                    and str(user_id) == just_washed,
                    lead_minutes=self.nudge_lead,
                )
                # Persisted first, always: the claim is spent whether or not
                # the send lands (see the module docstring).
                await self._assistant.async_store_budgets(budgets)
                if reason != nudge_mod.REASON_OK:
                    self._log_drop(user_id, reason)
                    continue
                if kind == nudge_mod.MSG_SLOT:
                    # The *measured* distance to the slot, not the option that
                    # decided when to look for one. Two triggers send this
                    # message and only one of them fires at slot-start-minus-
                    # lead; the washer coming free at 19:55 must not tell
                    # somebody their 20:00 slot starts in an hour.
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
                # After the send, because the note is keyed by the message id
                # and there isn't one before it. That is a change of ordering
                # from the per-person note this replaced, and the trade is worth
                # stating: the window it opens is the tail of one HTTP round
                # trip, and a tap inside it finds no record and is answered as
                # stale — where the old ordering could not be *unsure*, only
                # confidently wrong about a slot on everybody's grid.
                await self._assistant.async_note_nudge_cell(
                    user_id, cell, getattr(message, "id", None)
                )
                _LOGGER.debug("Sent the %s DM to %s", kind, user_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to send a nudge to %s", user_id)

    async def _async_deliver(self, user_id, text: str, view):
        """Send one reminder, or give up on it. Never waits for a reconnect.

        Returns the sent message (so the caller can record what it was about)
        or None.

        The one thing a reminder must not do is arrive late. Both of these
        messages are about *right now* — a slot that is running, a washer that
        is free — and ``async_dm_user`` begins with ``wait_until_ready()``, so a
        send attempted while the gateway is down parks in that await until it
        comes back. At 07:40 the next morning it would deliver "you're down for
        tonight, and the washer's free right now" about a slot that ended at
        midnight, and everybody after this person in the pass would have waited
        those eight hours to be evaluated at all.

        The budget was claimed and persisted before we got here, and it stays
        spent: over budget is dropped rather than queued (P2), and a nudge that
        could not leave inside its own slot is the same thing.
        """
        try:
            async with asyncio.timeout(_SEND_TIMEOUT):
                return await self._assistant.async_send_dm(user_id, text, view)
        except TimeoutError:
            # Only the timeout. A CancelledError from outside is HA shutting
            # down and has to keep travelling.
            _LOGGER.debug(
                "Reminder for %s dropped: not delivered within %ss",
                user_id,
                _SEND_TIMEOUT,
            )
            return None

    def _log_drop(self, user_id, reason: str) -> None:
        """One debug line, and **only** for a message the budget ate.

        Every other reason is an evaluation that concluded "no" — not their day,
        not opted in, paused — and there are four triggers a day times everybody
        the bot has ever seen. Logging those would put a wall of text in the log
        of a household that has opted into nothing, which is how the debug log
        stops being readable at the moment somebody needs it.
        """
        if reason in nudge_mod.BUDGET_REASONS:
            _LOGGER.debug("Reminder for %s dropped: over the %s budget", user_id, reason)

    def _washer_free(self, *, released: bool) -> bool:
        """Whether the washer counts as free, for a message that says it is.

        ``released=True`` is the dispatcher path **for a release somebody
        confirmed** — the ✅ tap or an unclaimed completion, where the
        coordinator has already decided the machine has changed hands. The only
        thing that can have changed between the signal and this task running is
        a new load starting, so that is all this re-checks.

        A *hedged* release is deliberately not one of those: the backstop timer
        firing means nobody has confirmed anything, which is why the queue is
        told "probably free, worth a look". It arrives here with
        ``released=False`` and gets the same test a clock trigger gets, because
        it is the same state — otherwise a finished load with its owner's
        clothes still in the drum would be suppressed at 19:00 and announced as
        free at 19:45.

        At a *clock* trigger nobody has decided anything either, so the stricter
        reading applies — and "done" is not "empty" (§4.1): a finished load with
        its owner's clothes still in the drum is not a machine anybody can use.
        A load nobody claimed is treated as free, exactly as the coordinator
        treats it when it hands that one straight to the queue.
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
