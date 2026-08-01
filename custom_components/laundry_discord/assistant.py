"""The 🤖 assistant: the private panel, per-person prefs and the DM plumbing.

One button on the card opens an **ephemeral** message — the only personal
surface in an otherwise shared channel, which is what lets an onboarding
explainer and per-person settings exist without costing the channel a single
line. Everything here is additive: with nobody opted in, this module sends
exactly the messages the bot already sent.

Four Discord facts shape this file (design doc §5.3 / §13):

* **An ephemeral message can only be sent as a response to an interaction.**
  The bot cannot start one. That single constraint is why reminders are DMs and
  why this panel only ever appears in reply to a tap.
* **An interaction token dies after 15 minutes.** Every tap is a *new*
  interaction with a fresh token, so a panel stays editable while somebody
  keeps tapping — but a tap on a panel opened an hour ago cannot edit that old
  message and has to answer with a fresh ephemeral instead. Both paths are
  implemented in :meth:`LaundryAssistant._async_respond`.
* **Ephemeral messages aren't durable** (they vanish on client restart), but
  their components dispatch through the persistent-view registry, so every
  ``custom_id`` here goes into the view handed to ``add_view``.
* **A DM to a known user id needs no privileged intent**, but raises
  ``discord.Forbidden`` (error 50007) when the recipient has DMs from server
  members turned off — and Discord never tells *them*. That is the one real
  failure mode (§10.5) and it is handled here: fall back to the channel so the
  message is never lost, then show them the fix the next time they open the
  panel.

Prefs live in their own ``Store`` key, separate from the session store, so a
bug in here cannot corrupt a live load. The same store also holds the habit
model's history and corrections (design doc §12) — one planner store, not two —
and this module is the only thing that writes to it. The dependency runs one
way: the coordinator reaches in for the panel, the ping routing and "a claim
just happened", and nothing in this module knows anything about the session
state machine.

Two rules govern every write here, and both are about what a shared house can
stand:

* **A store write only when something actually changed.** Claim, Unclaim and
  Reclaim are one load, so the second tap must cost nothing; the panel opening
  costs nothing; a DM going through when we already knew it would costs
  nothing.
* **Silence at log level.** Normal operation adds no lines at all. What debug
  lines exist mark a decision that was taken — a load recorded, a guess retired
  — never an evaluation that concluded "no".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import habit as habit_mod
from . import people as people_mod
from . import plan as plan_mod
from .const import (
    CONF_LEARN_HABITS,
    DEFAULT_LEARN_HABITS,
    GRID_BACK_CUSTOM_ID,
    GRID_DAY_CUSTOM_ID,
    GRID_SLOT_CUSTOM_IDS,
    GUESS_BACK_CUSTOM_ID,
    GUESS_OFF_CUSTOM_ID,
    GUESS_RIGHT_CUSTOM_ID,
    GUESS_WRONG_CUSTOM_ID,
    PANEL_CHANNEL_CUSTOM_ID,
    PANEL_DM_CUSTOM_ID,
    PANEL_GUESS_CUSTOM_ID,
    PANEL_MONITOR_CUSTOM_ID,
    PANEL_OFF_CUSTOM_ID,
    PANEL_WEEK_CUSTOM_ID,
    PLANNER_STORAGE_KEY,
    PLANNER_STORAGE_VERSION,
)

if TYPE_CHECKING:
    from .discord_bot import DiscordBot

_LOGGER = logging.getLogger(__name__)

_COLOR_PANEL = 0x5865F2  # Discord blurple — reads as "this is the bot talking"

# The §10.5 explainer, shown once at the top of the panel after a refused DM.
# Both settings are listed because either one can be the culprit and neither is
# discoverable: Discord tells the sender a DM bounced and never the recipient.
_DM_NOTICE = (
    "⚠️ **I couldn't DM you**\n"
    "Two settings control this:\n"
    "• Right-click the server icon → **Privacy Settings** → allow DMs from "
    "members\n"
    "• **User Settings → Privacy** → allow direct messages from server members\n"
    "Until then I'll ping you in the channel instead.\n\n"
)

# What each reminder mode actually does, in the panel's own words. "Off" still
# names you in the channel — it removes the *push*, not the information, the
# same trade the 🌙 Quiet button already makes on the card.
_MODE_LABELS = {
    people_mod.REMIND_DM: "📬 In a DM — just you, nothing in the channel",
    people_mod.REMIND_CHANNEL: "💬 In the channel, with an @mention",
    people_mod.REMIND_OFF: "🚫 No pushes — you're still named in the channel",
}


class _ReminderButton(discord.ui.Button):
    """One of the three "how should I reach you" choices.

    A single button per mode rather than a select menu: three fixed options fit
    in one row, and a button shows the current choice by its own style without
    the extra tap a dropdown costs.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        mode: str,
        custom_id: str,
        label: str,
        emoji: str,
        *,
        active: bool,
    ) -> None:
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if active
                else discord.ButtonStyle.secondary
            ),
            emoji=emoji,
            custom_id=custom_id,
            row=0,
        )
        self.assistant = assistant
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_choose_reminders(interaction, self.mode)
        except Exception:  # noqa: BLE001 - never let a callback reach HA
            _LOGGER.exception("Failed to handle an assistant reminder choice")
            await self.assistant.async_report_error(interaction)


class _MonitorButton(discord.ui.Button):
    """Consent toggle for logging this person's loads (design doc §11)."""

    def __init__(
        self, assistant: "LaundryAssistant", *, enabled: bool | None
    ) -> None:
        super().__init__(
            label=(
                "Monitoring"
                if enabled is None
                else f"Monitoring: {'on' if enabled else 'off'}"
            ),
            style=discord.ButtonStyle.secondary,
            emoji="👁",
            custom_id=PANEL_MONITOR_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_toggle_monitor(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle the assistant monitor toggle")
            await self.assistant.async_report_error(interaction)


class _WeekButton(discord.ui.Button):
    """Open the 📅 week grid from the settings panel."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="My week",
            style=discord.ButtonStyle.primary,
            emoji="📅",
            custom_id=PANEL_WEEK_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_open_grid(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the week grid")
            await self.assistant.async_report_error(interaction)


class _GuessButton(discord.ui.Button):
    """Open the 🔮 "here's what I think" panel (design doc §7.3)."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Fix a guess",
            style=discord.ButtonStyle.secondary,
            emoji="🔮",
            custom_id=PANEL_GUESS_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_open_guess(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the guess panel")
            await self.assistant.async_report_error(interaction)


class _GuessRightButton(discord.ui.Button):
    """"That's right" — an acknowledgement, and deliberately nothing more.

    §7.3 is exact that the model learns from **actual claims and explicit
    corrections only**. A confirmation is neither: the loads that produced the
    guess are already in history and already counted, so writing a row here
    would be the guess feeding itself evidence with a human tap laundering it —
    the precise drift the section exists to prevent. So this button stores
    nothing, and the panel says so rather than implying a reward.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="That's right",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=GUESS_RIGHT_CUSTOM_ID,
            row=0,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_confirm_guess(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to acknowledge a guess")
            await self.assistant.async_report_error(interaction)


class _GuessWrongButton(discord.ui.Button):
    """"Wrong" — retire this guess (``habit.mark_prediction_wrong``)."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Wrong",
            style=discord.ButtonStyle.secondary,
            emoji="❌",
            custom_id=GUESS_WRONG_CUSTOM_ID,
            row=0,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_reject_guess(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to record a correction")
            await self.assistant.async_report_error(interaction)


class _GuessOffButton(discord.ui.Button):
    """"Stop guessing" — the person's ``predict`` preference, both ways.

    §7.3 names only the off direction, but an opt-out with no way back is a
    setting somebody has to edit a JSON store to undo (P7 — additive *and
    reversible*). The label follows the state, so the same button is the way
    out and the way back in.
    """

    def __init__(self, assistant: "LaundryAssistant", *, predicting: bool) -> None:
        super().__init__(
            label="Stop guessing" if predicting else "Start guessing",
            style=discord.ButtonStyle.secondary,
            emoji="🚫" if predicting else "🔮",
            custom_id=GUESS_OFF_CUSTOM_ID,
            row=0,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_toggle_predict(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to toggle predictions")
            await self.assistant.async_report_error(interaction)


class _GuessBackButton(discord.ui.Button):
    """Back to the settings panel.

    Its own ``custom_id`` rather than the grid's, even though it does the same
    thing: ``add_view`` keys the persistent registry by id, so two views sharing
    one id means the second registration quietly wins for both.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=GUESS_BACK_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_back_to_panel(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to return to the panel")
            await self.assistant.async_report_error(interaction)


class GuessView(discord.ui.View):
    """The 🔮 panel's controls: the §7.3 three, plus back.

    Two rows, at most four components. ``That's right`` and ``Wrong`` are only
    added when there is a guess on screen to answer — a button that argues with
    a sentence saying "I don't have a guess for you yet" is worse than no
    button — but the registration template (built with no arguments) carries
    every id, because an unregistered ``custom_id`` doesn't error, it silently
    stops dispatching.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        *,
        has_guess: bool = True,
        predicting: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        if has_guess:
            self.add_item(_GuessRightButton(assistant))
            self.add_item(_GuessWrongButton(assistant))
        self.add_item(_GuessOffButton(assistant, predicting=predicting))
        self.add_item(_GuessBackButton(assistant))


class _GridDaySelect(discord.ui.Select):
    """Which day the four slot buttons act on.

    A select rather than seven buttons because the grid is 7 x 4 = 28 cells and
    a message holds at most 25 components — a button per cell is impossible
    before it is even unreadable (design doc §6.4). So the grid is a *display*
    and this is how you point at a column of it.
    """

    def __init__(self, assistant: "LaundryAssistant", day: int) -> None:
        super().__init__(
            placeholder="Pick a day",
            custom_id=GRID_DAY_CUSTOM_ID,
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(
                    label=name,
                    value=str(index),
                    default=index == day,
                )
                for index, name in enumerate(plan_mod.DAY_NAMES)
            ],
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_pick_day(interaction, self.values[0])
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to change the grid's day")
            await self.assistant.async_report_error(interaction)


class _GridSlotButton(discord.ui.Button):
    """Book or free one slot on the selected day.

    Green when it's yours, grey otherwise. It is deliberately **not** disabled
    when somebody else holds the cell: the plan is information, not permission
    (§8), so two people can want the same slot and the grid's job is to make
    that visible rather than to arbitrate it.
    """

    def __init__(
        self, assistant: "LaundryAssistant", slot: str, *, mine: bool
    ) -> None:
        super().__init__(
            label=plan_mod.slot_label(slot),
            style=(
                discord.ButtonStyle.success if mine else discord.ButtonStyle.secondary
            ),
            custom_id=GRID_SLOT_CUSTOM_IDS[slot],
            row=1,
        )
        self.assistant = assistant
        self.slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_toggle_cell(interaction, self.slot)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to toggle a grid slot")
            await self.assistant.async_report_error(interaction)


class _GridBackButton(discord.ui.Button):
    """Back to the settings panel."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=GRID_BACK_CUSTOM_ID,
            row=2,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_back_to_panel(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to return to the panel")
            await self.assistant.async_report_error(interaction)


class GridView(discord.ui.View):
    """The week grid's controls: day select, four slot toggles, back.

    Three rows, six components — well inside the 5-row / 25-component ceiling,
    with the select occupying a whole row of its own as Discord requires.

    ``occupancy`` and ``day`` are used only to label and colour the buttons.
    Passing neither builds the neutral registration template for ``add_view``,
    which must carry **every** ``custom_id`` — one that was never registered
    doesn't error, it silently stops dispatching, and this integration has
    already been bitten by that.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        *,
        occupancy: dict | None = None,
        day: int = 0,
        viewer_id=None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_GridDaySelect(assistant, day))
        for slot in plan_mod.SLOTS:
            cell = plan_mod.cell_key(day, slot)
            mine = bool(
                occupancy is not None
                and cell is not None
                and plan_mod.is_mine(occupancy, cell, viewer_id)
            )
            self.add_item(_GridSlotButton(assistant, slot, mine=mine))
        self.add_item(_GridBackButton(assistant))


class AssistantView(discord.ui.View):
    """Persistent view for the ephemeral panel.

    One view class serves everybody — the callbacks read
    ``interaction.user.id``, so there is nothing per-person baked into a
    ``custom_id`` (which would be unregisterable, and would leak who a message
    belongs to).

    ``person`` is the record the panel is being rendered for, used only to
    label and highlight the buttons. ``None`` builds the neutral registration
    template handed to ``add_view`` on startup: it must contain **every**
    ``custom_id``, because one that was never registered doesn't error, it
    silently stops dispatching — a dead button, and a bug class this
    integration has already been bitten by.

    Five components at most, in two rows: the three reminder modes on row 0,
    and 👁 / 📅 / 🔮 on row 1. Discord allows 5 per row and 25 per message, so
    row 1 has two slots left before anything has to move.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        *,
        person: dict | None = None,
        learning: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        template = person is None
        mode = person["reminders"] if person else None
        onboarded = bool(person and person["onboarded"])
        # First-timer wording ("Yes, DM me") vs settings wording ("DM me"):
        # same buttons, same ids, but the first-time panel is a question and
        # the settings panel is a status.
        self.add_item(
            _ReminderButton(
                assistant,
                people_mod.REMIND_DM,
                PANEL_DM_CUSTOM_ID,
                "DM me" if onboarded else "Yes, DM me",
                "📬",
                active=mode == people_mod.REMIND_DM,
            )
        )
        self.add_item(
            _ReminderButton(
                assistant,
                people_mod.REMIND_CHANNEL,
                PANEL_CHANNEL_CUSTOM_ID,
                "In the channel",
                "💬",
                active=mode == people_mod.REMIND_CHANNEL,
            )
        )
        self.add_item(
            _ReminderButton(
                assistant,
                people_mod.REMIND_OFF,
                PANEL_OFF_CUSTOM_ID,
                "No pings" if onboarded else "No thanks",
                "🚫",
                active=mode == people_mod.REMIND_OFF,
            )
        )
        # The monitoring toggle is noise on the first-time panel — that panel
        # asks one question — but the template still needs its id registered.
        if onboarded or template:
            self.add_item(
                _MonitorButton(
                    assistant, enabled=person["monitor"] if person else None
                )
            )
            # Same reasoning for the grid: a first-timer is being asked how to
            # be reached, not invited to plan their week. Row 1 keeps it clear
            # of the three reminder-mode buttons on row 0.
            self.add_item(_WeekButton(assistant))
        # 🔮 only exists while the house has day-learning on: with the option
        # off there is no history, so the panel behind it could only ever say
        # "nothing to show", and a button that can't do anything is worse than
        # no button. The template still registers its id, so switching the
        # option on doesn't leave a dead button until the next restart.
        if template or (learning and onboarded):
            self.add_item(_GuessButton(assistant))


class LaundryAssistant:
    """Per-person preferences, the private panel and the DM plumbing.

    Owns its own ``Store``; holds a reference to the bot purely to send things.
    Nothing here reaches back into the coordinator.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        bot: "DiscordBot",
        entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.bot = bot
        # The config entry, purely to read the one option this module owns.
        # Deliberately the *entry* and not the coordinator: the dependency has
        # to keep running one way (§14 rule 5), and options change by reloading
        # the entry, which rebuilds this object anyway — so reading them once
        # here can never go stale.
        self._entry = entry
        self._store: Store = Store(
            hass, PLANNER_STORAGE_VERSION, PLANNER_STORAGE_KEY
        )
        self._people: dict[str, dict] = {}
        # Per-ISO-week booking overrides: {"2026-W32": {"3-eve": [ids]}}.
        # Recurring slots live on the person; this is "what actually happens
        # this week", which is the only thing a tap on the grid can mean.
        self._overrides: dict[str, dict[str, list[str]]] = {}
        # Which day each person's open grid is pointed at. Deliberately memory
        # only: it's view state, not a preference, and losing it on a restart
        # just means the grid reopens on today — which is where it should start
        # anyway. Keyed by the string form of the id, like everything else here.
        self._grid_day: dict[str, int] = {}
        # The habit model's two stores (design doc §12), both lists of rows
        # carrying an id and a timestamp and nowhere to put a name. They live
        # in *this* Store alongside people and overrides — one planner store,
        # separate from the session, exactly as §12 draws it.
        self._history: list[dict] = []
        self._corrections: list[dict] = []
        # The nudge budget (P2), per person: {"<id>": {"last_nudge_ts", ...}}.
        # §12 puts ``last_nudge_ts`` / ``nudges_this_week`` on the person; they
        # live in their own mapping here because they are *accounting*, not a
        # preference — nothing in the panel reads or writes them, and keeping
        # them apart means a nudge can never be one merge away from rewriting
        # somebody's settings. On disk they are in the planner Store like
        # everything else, which is what makes a restart not refill anybody's
        # allowance: the counters are loaded, not reset.
        self._budgets: dict[str, dict] = {}

    # ------------------------------------------------------------------ config
    @property
    def learn_habits(self) -> bool:
        """Whether the house has day-learning switched on at all.

        The outer of the two gates on every history write and every ``░``; the
        inner one is the person's own 👁 Monitoring consent. Off by default
        (§14 rule 7), and off means nothing is written *and* nothing is drawn.
        """
        if self._entry is None:
            return DEFAULT_LEARN_HABITS
        merged = {**self._entry.data, **self._entry.options}
        return bool(merged.get(CONF_LEARN_HABITS, DEFAULT_LEARN_HABITS))

    # ------------------------------------------------------------- persistence
    async def async_load(self) -> None:
        """Load per-person prefs. Never raises — no prefs is a working state."""
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to load assistant prefs; starting empty")
            data = None
        source = data.get("people") if isinstance(data, dict) else None
        # Normalise once, here, so every later read works on a known shape —
        # including the string-keyed mapping JSON always hands back.
        self._people = people_mod.normalise_people(source)
        # .get with a default: a store written by v0.17.0 has no overrides at
        # all, and an upgrade must not be the thing that breaks the panel.
        raw = data.get("overrides") if isinstance(data, dict) else None
        self._overrides = plan_mod.prune_overrides(
            plan_mod.normalise_overrides(raw), self._current_week()
        )
        # History and corrections are normalised and aged **in memory** on the
        # way in, and not written back: a load that changes nothing must not
        # cost a store write, and startup is the definition of "nothing
        # changed". The retention that matters is applied on the write path
        # (habit.record_load / record_correction both prune), so the file on
        # disk is bounded by the act of adding to it.
        now = self._now()
        raw_history = data.get("history") if isinstance(data, dict) else None
        self._history = habit_mod.prune_history(raw_history, now)
        raw_corrections = data.get("corrections") if isinstance(data, dict) else None
        self._corrections = habit_mod.prune_corrections(raw_corrections, now)
        # Normalised, never cleared: a restart that handed everybody a fresh
        # allowance would turn "1 DM a day" into "1 DM per restart", and the
        # windows the counters belong to are stored alongside them so the reset
        # is a comparison rather than something anybody has to remember to do.
        raw_budgets = data.get("budgets") if isinstance(data, dict) else None
        self._budgets = habit_mod.normalise_budgets(raw_budgets)

    async def _async_save(self) -> None:
        """Persist prefs. A failed save must not break the button that caused it."""
        try:
            await self._store.async_save(
                {
                    "people": self._people,
                    "overrides": self._overrides,
                    "history": self._history,
                    "corrections": self._corrections,
                    "budgets": self._budgets,
                }
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to save assistant prefs")

    # -------------------------------------------------------------- the clock
    def _now(self):
        """Local time, per HA's configured timezone.

        The grid is the only part of this integration that cares what day it
        is, and it has to agree with the household's wall clock rather than
        UTC — a Sunday-evening booking made at 23:00 local is not Monday.
        """
        return dt_util.now()

    def _current_week(self) -> str:
        """This week's ISO key, or "" if the clock is somehow unreadable."""
        return plan_mod.iso_week_key(self._now()) or ""

    def _today(self) -> int:
        """Today's Monday-based weekday index, defaulting to Monday."""
        day = plan_mod.weekday_of(self._now())
        return day if day is not None else 0

    # ------------------------------------------------------------- the model
    async def async_note_claim(self, user_id) -> None:
        """One real load happened, and this person claimed it (design doc §7.1).

        The coordinator calls exactly this on a Claim tap and knows nothing
        else about the model — not what a prediction is, not that history
        exists. Everything that makes the tap safe to record lives here:

        * **Two consent gates.** The house's ``learn_habits`` option, and then
          this person's own 👁 Monitoring preference, passed to
          :func:`habit.record_load` as ``monitor`` so the refusal happens at
          the write itself. Monitoring off means *no row is written at all*
          (§11) — not a row that later gets filtered out, because the one
          mistake here that can't be undone by a later fix is having stored it.
        * **A store write only when something changed.** ``record_load`` dedupes
          within the hour, so Claim → Unclaim → Reclaim is one load and the
          second tap returns the list unchanged; comparing before saving is
          what keeps that from costing a disk write anyway. The same comparison
          covers the monitoring-off case, which returns the pruned list and
          therefore usually writes nothing at all.
        * **Retention on the write path.** ``record_load`` prunes to 90 days and
          caps the row count in the same call, so history is bounded by the act
          of adding to it and never needs a sweeper.

        Never raises. It is called from inside the Claim callback, and a
        failure to log a load must not turn a successful claim into
        "interaction failed".
        """
        try:
            if not self.learn_habits:
                return
            monitor = people_mod.get_person(self._people, user_id)["monitor"]
            updated = habit_mod.record_load(
                self._history, user_id, self._now(), monitor=monitor
            )
            if updated == self._history:
                return
            self._history = updated
            await self._async_save()
            # A decision actually taken (a row exists now that didn't before),
            # not a per-evaluation trace: the dedupe and consent paths above
            # return before reaching this.
            _LOGGER.debug("Logged a load for %s", user_id)
        except Exception:  # noqa: BLE001 - never raise into a card callback
            _LOGGER.exception("Failed to log a load for the habit model")

    def _predicts_for(self, user_id) -> bool:
        """Whether this person's guesses may be computed or shown at all.

        Both gates again, plus the person's ``predict`` preference. Monitoring
        is included on the *read* side deliberately: turning it off stops new
        rows, but rows already stored would otherwise keep producing ``░`` for
        somebody who just said stop watching me. Nothing is deleted — a toggle
        somebody flips to see what it does must not destroy three months of
        history — it simply stops being read.
        """
        if not self.learn_habits:
            return False
        person = people_mod.get_person(self._people, user_id)
        return bool(person["predict"] and person["monitor"])

    def _prediction(self, user_id) -> dict | None:
        """This viewer's own top prediction, or None — usually None (P6)."""
        if not self._predicts_for(user_id):
            return None
        return habit_mod.predict(
            self._history, user_id, self._now(), self._corrections
        )

    def _predicted_cells(self, user_id) -> list[str]:
        """The cells to draw as ``░`` — **only ever the viewer's own** (§11).

        Every read in :mod:`habit` is scoped to one id, and the only id this is
        ever called with is ``interaction.user.id``: the person looking at the
        message. A prediction is a claim about one person's habits, and it is
        never rendered into anything more than one person can see.

        Deliberately the **top** guess alone, not every cell that clears the
        gate. :func:`habit.predictions` can return up to three (4/3/3 of ten
        loads all pass at 30%), but the 🔮 panel names exactly one and ❌ Wrong
        retires exactly that one. Drawing the other two would put a ``░`` on
        the grid that the only button for arguing with it cannot even mention,
        let alone remove — and tapping Wrong about the Monday ``░`` would
        silently discard the Thursday guess instead. P4 says the guesses are
        visible *and correctable*; until the panel grows the doc's "📅 Wrong —
        pick" step (§7.3), what is rendered is held to what can be corrected.
        """
        prediction = self._prediction(user_id)
        return [prediction["cell"]] if prediction else []

    # ------------------------------------------------ the reminder loop's window
    # :mod:`reminders` owns *when* somebody is contacted; this store owns
    # everything it needs to decide and everything a reply changes. The split is
    # the same one the coordinator already gets: it asks for a delivery or a
    # record, and never touches the Store itself. Every writer below saves only
    # when the value actually changed — a trigger that decides "no" must not
    # cost a disk write, and with nobody opted in that is every trigger.

    def now(self):
        """The clock the whole planner shares. See :meth:`_now`."""
        return self._now()

    @property
    def people_map(self) -> dict[str, dict]:
        """The prefs mapping, for :func:`nudge.eligible`. A copy, not the store."""
        return dict(self._people)

    @property
    def budgets(self) -> dict[str, dict]:
        """The nudge accounting (P2), for the claim. A copy, not the store."""
        return dict(self._budgets)

    def prediction_for(self, user_id) -> dict | None:
        """This person's own top guess, or None — the same one 🔮 shows.

        Deliberately :meth:`_prediction`, so the house's ``learn_habits`` option
        and the person's own 👁 / 🔮 toggles gate the DM exactly as they gate the
        panel. A reminder can never be sent about a guess the person cannot see.
        """
        return self._prediction(user_id)

    def load_times(self, user_id) -> list[float]:
        """When this person's own retained loads happened, as timestamps.

        Only theirs — like every read in :mod:`habit` — and only so the reminder
        loop can tell that somebody has already done the laundry it is about to
        suggest. Timestamps rather than rows, because that is the entire
        question and a row has a slot in it that nothing here needs.
        """
        return [
            row["ts"]
            for row in habit_mod.history_for(self._history, user_id, self._now())
        ]

    def booked_cells(self, user_id, week=None) -> list[str]:
        """The cells this person has actually booked in a week (§6.4).

        Defaults to the week that is running, which is what every reader wants;
        ``week`` exists for the one writer that does not — ⏭ Push to tomorrow
        on a Sunday, where "tomorrow" is the *next* ISO week.
        """
        target = week if isinstance(week, str) and week else self._current_week()
        occupancy = plan_mod.effective_week(self._people, self._overrides, target)
        return [
            cell
            for cell in occupancy
            if plan_mod.is_mine(occupancy, cell, user_id)
        ]

    async def async_store_budgets(self, budgets) -> None:
        """Persist the accounting a claim came back with.

        Called **before** the DM goes out, never after: a send that raises
        ``Forbidden`` has still spent the nudge, and refunding it would mean
        retrying somebody with closed DMs at every trigger forever.
        """
        updated = habit_mod.normalise_budgets(budgets)
        if updated == self._budgets:
            return  # a denied claim changes nothing, so it costs no write
        self._budgets = updated
        await self._async_save()

    async def async_set_predict(self, user_id, enabled: bool) -> None:
        """🔕 Stop asking — the permanent opt-out from the reminder DMs.

        The same ``predict`` preference the 🔮 panel's own button flips, on
        purpose: one switch, one meaning, and the panel is the way back (P7 —
        additive *and* reversible). Nothing re-prompts somebody who has turned
        it off, because :func:`nudge.eligible` refuses them outright.
        """
        if people_mod.get_person(self._people, user_id)["predict"] == bool(enabled):
            return
        self._people = people_mod.set_person(
            self._people, user_id, predict=bool(enabled)
        )
        await self._async_save()
        _LOGGER.debug("Predictions %s for %s", "on" if enabled else "off", user_id)

    async def async_pause_until(self, user_id, until_ts: float) -> None:
        """⏭ Skip this week — quiet until a timestamp, then back to normal."""
        self._people = people_mod.set_person(
            self._people, user_id, paused_until=float(until_ts)
        )
        await self._async_save()

    async def async_book_cell(self, user_id, cell, week=None) -> bool:
        """👍 On it — mark the slot taken on the anonymous board (§10.3).

        Idempotent, unlike :meth:`async_toggle_cell`: a second tap on a nudge
        must not *un*book the slot the first tap booked. The board still shows
        only that a cell is taken, never by whom (P5).

        ``week`` defaults to the week that is running. It has to be overridable
        because a cell key carries a weekday and no date: ⏭ Push to tomorrow on
        a **Sunday** lands on Monday, and Monday belongs to the *next* ISO week.
        Booking it under the current one writes the Monday that is six days
        past — a booking nobody made, on a day nobody can use, which the day-of
        trigger then never finds.
        """
        key = plan_mod.normalise_cell(cell)
        target = week if isinstance(week, str) and week else self._current_week()
        if key is None or not target:
            return False
        if key in self.booked_cells(user_id, target):
            return True  # already theirs — nothing changed, nothing written
        self._overrides, _booked = plan_mod.toggle_booking(
            self._people, self._overrides, target, key, user_id
        )
        await self._async_save()
        return True

    async def async_record_push(self, user_id, cell) -> None:
        """⏭ Push to tomorrow — a correction that is **not** a wrong guess.

        §7.3 is exact about this: the day was right, the person just isn't doing
        it tonight. Counting it as a miss would train the model out of every
        correct guess anybody was ever too busy to act on, so it goes through
        :func:`habit.mark_nudge_pushed` and nowhere near
        :func:`habit.mark_prediction_wrong`.
        """
        updated = habit_mod.mark_nudge_pushed(
            self._corrections, user_id, cell, self._now()
        )
        if updated == self._corrections:
            return
        self._corrections = updated
        await self._async_save()

    # --------------------------------------------------------------------- DMs
    async def async_send_dm(
        self, user_id, content: str, view: discord.ui.View | None = None
    ) -> bool:
        """DM one person. Returns True only if it actually went out.

        ``discord.Forbidden`` (50007) means their privacy settings refuse DMs
        from server members. That is a *user setting*, not a bug, so it logs at
        debug — the caller falls back to the channel and the panel explains it
        to the one person who can fix it.
        """
        if user_id is None:
            return False
        try:
            await self.bot.async_dm_user(user_id, content, view=view)
        except discord.Forbidden:
            _LOGGER.debug(
                "DM to %s refused (DMs from server members are off)", user_id
            )
            self._people = people_mod.mark_dm_failed(self._people, user_id)
            await self._async_save()
            return False
        except Exception:  # noqa: BLE001 - never raise into HA
            _LOGGER.exception("Failed to DM %s", user_id)
            return False
        # Only write on a change: the completion ping runs once per load, and a
        # store write per load for no new information is pure churn.
        if people_mod.get_person(self._people, user_id)["dm_ok"] is not True:
            self._people = people_mod.mark_dm_ok(self._people, user_id)
            await self._async_save()
        return True

    async def async_route_ping(
        self, user_id, *, dm_text: str, channel_text: str
    ) -> bool:
        """Deliver one personal message the way this person asked for it.

        The default is the **channel**, so anybody who has never opened the
        panel gets exactly what they got before this feature existed. Returns
        True if something was delivered.

        - **dm** — DM them; on any failure fall through to the channel, because
          a handoff that nobody hears is worse than a line in the channel.
        - **channel** — today's behaviour: a real @mention, which is the only
          thing that makes a phone buzz (an embed edit never does).
        - **off** — post the same line push-silently with mentions suppressed.
          They are still *named*, so the information isn't lost; only the push
          is. Same trade the 🌙 Quiet button already makes on the card.

        A ``None`` user id still goes to the channel. ``queue.py`` explicitly
        contemplates an entry whose id failed to persist (``{"id": None}``),
        and by the time we're called ``select_handoff`` has already popped that
        entry off the line — so returning early here would consume a handoff
        and post nothing at all. An ugly ``<@None>`` line is what the bot did
        before this module existed, and it at least tells the house the washer
        is free.
        """
        mode = (
            people_mod.REMIND_CHANNEL
            if user_id is None
            else people_mod.delivery(self._people, user_id)
        )
        if mode == people_mod.REMIND_DM and await self.async_send_dm(
            user_id, dm_text
        ):
            return True
        try:
            if mode == people_mod.REMIND_OFF:
                await self.bot.async_announce_done(channel_text)
            else:
                await self.bot.async_send_ping(channel_text)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to deliver a laundry ping to %s", user_id)
            return False
        return True

    # ------------------------------------------------------------------- panel
    async def async_open_panel(self, interaction: discord.Interaction) -> None:
        """Answer a 🤖 tap with this person's own private panel.

        Deliberately works on any card, live load or not: it is the onboarding
        surface, and somebody scrolling up through the channel is exactly the
        person who most needs to know what the buttons do.
        """
        user_id = interaction.user.id
        name = interaction.user.display_name
        notice = people_mod.get_person(self._people, user_id)["dm_notice_pending"]
        # Refresh the last-seen display name, but only for somebody who already
        # has a record — merely looking at the panel shouldn't enrol a guest.
        if people_mod.is_known(self._people, user_id) and (
            people_mod.get_person(self._people, user_id)["name"] != name
        ):
            self._people = people_mod.set_person(self._people, user_id, name=name)
            await self._async_save()
        embed, view = self._build_panel(user_id, name, notice=notice)
        delivered = await self._async_respond(interaction, embed, view, edit=False)
        await self._async_clear_notice(user_id, notice=notice, delivered=delivered)

    async def async_choose_reminders(
        self, interaction: discord.Interaction, mode: str
    ) -> None:
        """Panel button: record how they want to be reached, then re-render."""
        self._people = people_mod.set_reminders(
            self._people,
            interaction.user.id,
            mode,
            name=interaction.user.display_name,
        )
        await self._async_save()
        await self._async_rerender(interaction)

    async def async_toggle_monitor(self, interaction: discord.Interaction) -> None:
        """Panel button: flip per-person load logging, then re-render."""
        user_id = interaction.user.id
        current = people_mod.get_person(self._people, user_id)["monitor"]
        self._people = people_mod.set_monitor(
            self._people,
            user_id,
            not current,
            name=interaction.user.display_name,
        )
        await self._async_save()
        await self._async_rerender(interaction)

    # ------------------------------------------------------------- the guess
    async def async_open_guess(self, interaction: discord.Interaction) -> None:
        """Answer 🔮 with what the model thinks, and the three ways to reply."""
        await self._async_render_guess(interaction)

    async def async_confirm_guess(self, interaction: discord.Interaction) -> None:
        """"That's right" — acknowledged, and nothing is stored.

        See :class:`_GuessRightButton`: confirming a guess is not one of the two
        training signals §7.3 allows, and the loads behind it are already
        counted. So this costs no store write and the panel says as much,
        rather than implying the model was rewarded.
        """
        await self._async_render_guess(
            interaction,
            note="👍 Good — nothing to change, and nothing stored: the loads "
            "behind this guess were already counted.",
        )

    async def async_reject_guess(self, interaction: discord.Interaction) -> None:
        """"Wrong" — retire the guess for this cell (``mark_prediction_wrong``).

        The cell is re-read at tap time rather than remembered from the render.
        It is the same answer in every realistic case (nothing can change it
        but a fresh claim), it survives a restart between opening the panel and
        tapping, and "wrong" then always means the guess currently on screen
        rather than one this process happens to still remember.
        """
        user_id = interaction.user.id
        prediction = self._prediction(user_id)
        cell = prediction["cell"] if prediction else None
        if cell is None:
            # Nothing to correct — most likely a second tap on a stale panel.
            await self._async_render_guess(interaction)
            return
        self._corrections = habit_mod.mark_prediction_wrong(
            self._corrections, user_id, cell, self._now()
        )
        await self._async_save()
        _LOGGER.debug("Retired a prediction for %s", user_id)
        await self._async_render_guess(
            interaction,
            note="✅ Dropped. I'll only put that slot back if you actually "
            "wash then again — arguing from the same loads you just told me "
            "were wrong would be me learning from myself.",
        )

    async def async_toggle_predict(self, interaction: discord.Interaction) -> None:
        """"Stop guessing" / "Start guessing" — the ``predict`` preference."""
        user_id = interaction.user.id
        current = people_mod.get_person(self._people, user_id)["predict"]
        self._people = people_mod.set_person(
            self._people,
            user_id,
            predict=not current,
            name=interaction.user.display_name,
        )
        await self._async_save()
        _LOGGER.debug("Predictions %s for %s", "off" if current else "on", user_id)
        await self._async_render_guess(interaction)

    async def _async_render_guess(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """Draw the 🔮 panel for this viewer, in place where possible."""
        user_id = interaction.user.id
        person = people_mod.get_person(self._people, user_id)
        prediction = self._prediction(user_id)
        embed = self._guess_embed(user_id, person, prediction, note=note)
        view = GuessView(
            self,
            has_guess=prediction is not None,
            predicting=bool(person["predict"]),
        )
        await self._async_respond(interaction, embed, view, edit=True)

    # -------------------------------------------------------------- the grid
    async def async_open_grid(self, interaction: discord.Interaction) -> None:
        """Answer 📅 with this person's own view of the week.

        Always opens on today, because the overwhelmingly common reason to look
        is "can I wash tonight". Deliberately an assignment rather than a
        ``setdefault``: remembering the last day somebody happened to be
        looking at makes 📅 open somewhere different depending on history they
        can't see, and "it opens on today" is a promise worth keeping.
        """
        user_id = interaction.user.id
        self._grid_day[str(user_id)] = self._today()
        await self._async_render_grid(interaction, edit=True)

    async def async_pick_day(
        self, interaction: discord.Interaction, value
    ) -> None:
        """Point the four slot buttons at a different day."""
        try:
            day = int(value)
        except (TypeError, ValueError):
            day = self._today()
        if not plan_mod.is_weekday(day):
            day = self._today()
        self._grid_day[str(interaction.user.id)] = day
        await self._async_render_grid(interaction, edit=True)

    async def async_toggle_cell(
        self, interaction: discord.Interaction, slot: str
    ) -> None:
        """Book or free one cell for the selected day, this week."""
        user_id = interaction.user.id
        day = self._grid_day.get(str(user_id), self._today())
        cell = plan_mod.cell_key(day, slot)
        week = self._current_week()
        if cell is None or not week:
            await self._async_render_grid(interaction, edit=True)
            return
        self._overrides, _booked = plan_mod.toggle_booking(
            self._people, self._overrides, week, cell, user_id
        )
        # Booking a slot enrols them. Not because the grid needs it — a
        # booking stores the raw id in the override, so it renders as theirs
        # whether or not a prefs record exists — but because deliberately
        # planning a wash is an unambiguous "I use this bot", which merely
        # *looking* at the panel is not (see async_open_panel, which pointedly
        # doesn't enrol a guest). It gives them a record to hold a display name
        # and, from Phase 4, reminder settings.
        if not people_mod.is_known(self._people, user_id):
            self._people = people_mod.set_person(
                self._people, user_id, name=interaction.user.display_name
            )
        await self._async_save()
        await self._async_render_grid(interaction, edit=True)

    async def async_back_to_panel(self, interaction: discord.Interaction) -> None:
        """Return from the grid to the settings panel."""
        await self._async_rerender(interaction)

    async def _async_render_grid(
        self, interaction: discord.Interaction, *, edit: bool
    ) -> None:
        """Draw the grid for this viewer, in place where possible."""
        user_id = interaction.user.id
        day = self._grid_day.get(str(user_id), self._today())
        week = self._current_week()
        occupancy = plan_mod.effective_week(self._people, self._overrides, week)
        embed = self._grid_embed(occupancy, user_id, day)
        view = GridView(self, occupancy=occupancy, day=day, viewer_id=user_id)
        await self._async_respond(interaction, embed, view, edit=edit)

    def _grid_embed(self, occupancy, user_id, day: int) -> discord.Embed:
        """The week as a monospace block, plus this person's own cells.

        The block is fenced so Discord renders it monospace — without that the
        columns pull apart into nonsense on a proportional font. Everything
        decorative (the legend, the slot windows) lives *outside* the fence,
        because an emoji inside a code block breaks the alignment the whole
        display depends on.

        ``expected`` is this viewer's own predicted cells and nobody else's.
        The legend and the explainer both key off whether a ``░`` is *actually
        on the block* rather than off whether a prediction exists, because
        those differ: a guess whose cells have all been booked by somebody else
        loses to those bookings and renders nothing. Asking the rendered string
        is the one test that cannot drift from the renderer.
        """
        expected = self._predicted_cells(user_id)
        grid = plan_mod.render_grid(occupancy, viewer_id=user_id, expected=expected)
        guessed = plan_mod.CELL_EXPECTED in grid
        embed = discord.Embed(
            title="📅 The week",
            description=(
                f"```\n{grid}\n```\n"
                + plan_mod.render_legend(expected=guessed)
                + f"\n-# {plan_mod.render_windows()}"
            ),
            color=_COLOR_PANEL,
        )
        mine = plan_mod.describe_cells(occupancy, user_id)
        embed.add_field(
            name="Yours this week",
            value=mine or "nothing booked — tap a slot below",
            inline=False,
        )
        if guessed:
            embed.add_field(
                name="░ My guess at your usual days",
                value=(
                    "Worked out from your own loads, shown **only to you**, and "
                    "never on a cell somebody has actually booked. Tap 🔮 on the "
                    "panel to argue with it."
                ),
                inline=False,
            )
        embed.add_field(
            name=f"Tap a slot for {plan_mod.DAY_NAMES[day]}",
            value=(
                "Booking says *I'm planning to wash then* — it doesn't reserve "
                "the machine, and it never stops anyone else using it."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Nobody sees who booked what — only that a slot is taken."
        )
        return embed

    async def _async_rerender(self, interaction: discord.Interaction) -> None:
        """Redraw the panel in place after a setting changed."""
        user_id = interaction.user.id
        # Taps can arrive on a panel opened before a DM was refused, so the
        # notice is re-checked here rather than only on open.
        notice = people_mod.get_person(self._people, user_id)["dm_notice_pending"]
        embed, view = self._build_panel(
            user_id, interaction.user.display_name, notice=notice
        )
        delivered = await self._async_respond(interaction, embed, view, edit=True)
        await self._async_clear_notice(user_id, notice=notice, delivered=delivered)

    async def _async_clear_notice(
        self, user_id, *, notice: bool, delivered: bool
    ) -> None:
        """Retire the "I couldn't DM you" explainer, but only once they saw it.

        The flag is the *only* record that we owe somebody the §10.5 fix, and
        it can never be re-armed on its own: once ``dm_ok`` is False,
        :func:`people.delivery` routes them to the channel, so no further DM is
        attempted and ``mark_dm_failed`` never fires again. Clearing it before
        the panel is known to have landed would therefore lose the explainer
        permanently — and the panel genuinely can fail to land (the 3-second
        ack window elapsing on a busy event loop, or a token dying mid-flight),
        which is exactly why :meth:`_async_respond` reports whether it did.
        """
        if not (notice and delivered):
            return
        _, self._people = people_mod.take_pending_dm_notice(self._people, user_id)
        await self._async_save()

    async def _async_respond(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        view: discord.ui.View,
        *,
        edit: bool,
    ) -> bool:
        """Show the panel: edit in place if we can, otherwise send a fresh one.

        Returns whether the panel actually reached the user. Callers need that
        to know when it is safe to retire a one-shot notice — see
        :meth:`_async_clear_notice`.

        The 15-minute rule lives here. Each tap carries a fresh interaction
        token, so a panel somebody is actively using keeps editing in place —
        but a tap on a panel opened long ago can't touch that old message, and
        Discord rejects the edit. That is a normal, expected outcome, not an
        error, so it falls through to a brand new ephemeral rather than leaving
        the user staring at "interaction failed".
        """
        if edit:
            try:
                await interaction.response.edit_message(embed=embed, view=view)
                return True
            except discord.HTTPException:
                _LOGGER.debug(
                    "Assistant panel edit rejected (stale interaction) — "
                    "sending a fresh one",
                    exc_info=True,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Assistant panel edit failed", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=embed, view=view, ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    embed=embed, view=view, ephemeral=True
                )
        except Exception:  # noqa: BLE001
            # Nothing left to try: the interaction itself is gone (its 3-second
            # ack window elapsed, or the token expired mid-flight).
            _LOGGER.debug("Could not deliver the assistant panel", exc_info=True)
            return False
        return True

    async def async_followup_dm_notice(
        self, interaction: discord.Interaction
    ) -> None:
        """Tell somebody their DMs bounced, on **any** button they tap (§10.5).

        Rule 3 of §10.5 is "the next time they tap any button", not "the next
        time they open 🤖" — and the difference is the whole point. Somebody
        who already set up DMs has no reason to ever open the panel again, so
        hanging the explainer off 🤖 alone means the one person who can fix the
        setting is the one person who never sees it.

        Sent as a ``followup`` because every card button has already answered
        its interaction by the time we get here; a followup is a legal second
        message on that same fresh token and leaves the card's own edit alone.
        The common case is nothing owed, which costs one dict read and no
        Discord call at all.
        """
        user_id = interaction.user.id
        if not people_mod.get_person(self._people, user_id)["dm_notice_pending"]:
            return
        try:
            await interaction.followup.send(_DM_NOTICE.strip(), ephemeral=True)
        except Exception:  # noqa: BLE001 - never raise into a card callback
            # The tap itself already succeeded; the notice keeps waiting for
            # the next one rather than being lost here.
            _LOGGER.debug("Could not deliver the DM-failure notice", exc_info=True)
            return
        _, self._people = people_mod.take_pending_dm_notice(self._people, user_id)
        await self._async_save()

    async def async_report_error(self, interaction: discord.Interaction) -> None:
        """Best-effort ephemeral error reply from a panel button; never raises."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong — try again in a moment.",
                    ephemeral=True,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not send panel error response", exc_info=True)

    # ------------------------------------------------------------------ embeds
    def _build_panel(
        self, user_id, name: str, *, notice: bool
    ) -> tuple[discord.Embed, AssistantView]:
        """The panel for this person: first-time explainer or settings."""
        person = people_mod.get_person(self._people, user_id)
        if person["onboarded"]:
            embed = self._settings_embed(person, notice=notice)
        else:
            embed = self._welcome_embed(name, notice=notice)
        return embed, AssistantView(
            self, person=person, learning=self.learn_habits
        )

    def _welcome_embed(self, name: str, *, notice: bool) -> discord.Embed:
        """The 👋 first-time panel — the entire onboarding story, in private.

        Written for somebody who has never seen this channel (a new housemate,
        a guest): it says what the buttons on the card do before it asks them
        anything, because "want reminders?" is a meaningless question if you
        don't yet know what the bot is for.
        """
        embed = discord.Embed(
            title="👋 First time?",
            description=(
                (_DM_NOTICE if notice else "")
                + "This channel watches the washer and posts **one message per "
                "load**, updated in place as it goes. Here's what you can tap "
                "on it:\n\n"
                "🧺 **Claim** — call dibs on a running load, and I'll tell you "
                "when it's done.\n"
                "🔜 **I'm next** — get told when the washer is *actually* free. "
                "That's not the moment it finishes: the last person's clothes "
                "are still in it.\n"
                "🌙 **Quiet** — claim without the ping, for when you're asleep.\n"
                "✅ **Emptied it** — you've cleared the drum; whoever's waiting "
                "gets told the machine is theirs.\n\n"
                "**How should I reach you** when something's actually for you — "
                "your load finishing, or the washer coming free?"
            ),
            color=_COLOR_PANEL,
        )
        embed.set_footer(text="You can change this any time from 🤖.")
        return embed

    def _settings_embed(self, person: dict, *, notice: bool) -> discord.Embed:
        """The 🤖 settings panel — current prefs, and the controls we honour.

        Every line here describes something that is actually true right now: a
        panel claiming a setting that nothing reads is how a settings screen
        stops being believed. So Monitoring reads differently depending on
        whether the house has day-learning on, and the Guessing line only
        appears when there is guessing to have an opinion about.
        """
        learning = self.learn_habits
        embed = discord.Embed(
            title="🤖 Your laundry assistant",
            description=(
                (_DM_NOTICE if notice else "")
                + "Settings for the messages that are **about you** — your load "
                "finishing, or the washer coming free after you tapped 🔜. The "
                "card itself is unaffected."
            ),
            color=_COLOR_PANEL,
        )
        pings = _MODE_LABELS.get(
            person["reminders"], _MODE_LABELS[people_mod.REMIND_CHANNEL]
        )
        if person["reminders"] == people_mod.REMIND_DM and person["dm_ok"] is False:
            # Never claim a delivery route that is currently failing.
            pings += "\n(your DMs are closed, so I'm using the channel)"
        embed.add_field(name="Pings", value=pings, inline=False)
        if person["monitor"]:
            monitoring = (
                "👁 on — when you tap 🧺 Claim I note the day and time, so I can "
                "work out the days you usually wash"
                if learning
                else "👁 on — your loads can be logged, so I can learn the days "
                "you usually wash\n(day-learning is off for this channel, so "
                "nothing is being logged — this is your answer for if it's "
                "turned on)"
            )
        else:
            monitoring = "🚫 off — I won't log your loads at all"
        embed.add_field(name="Monitoring", value=monitoring, inline=False)
        if learning and person["monitor"]:
            embed.add_field(
                name="Guessing",
                value=(
                    "🔮 on — I'll mark the days I think you usually wash as ░ on "
                    "**your** week, and nowhere else"
                    if person["predict"]
                    else "🚫 off — I won't guess your days"
                ),
                inline=False,
            )
        embed.set_footer(text="No stats about you are ever shown to the house.")
        return embed

    def _guess_embed(
        self, user_id, person: dict, prediction: dict | None, *, note: str | None
    ) -> discord.Embed:
        """The 🔮 panel — what the model thinks, in the §7.3 wording.

        The important case is the one with **no guess**, because for the first
        month or so that is every case (P6). It says so plainly and shows the
        arithmetic it is short of, rather than hedging its way into a sentence
        that sounds like a prediction: "I think you *might* wash Thursdays" is
        exactly the confident nonsense the gate exists to prevent, and somebody
        who reads it once stops believing the ones that clear the bar.

        The prose is :func:`habit.describe_prediction` and :func:`habit.explain`
        rather than anything invented here — one place decides how a bucket is
        said out loud, so the DM this becomes in the next phase can't drift
        from the panel.
        """
        embed = discord.Embed(title="🔮 What I think", color=_COLOR_PANEL)
        if prediction is not None:
            where = habit_mod.describe_prediction(prediction)
            why = habit_mod.explain(prediction)
            body = f"I think you wash **{where}**"
            body += f" — {why}." if why else "."
            body += (
                "\n\nThat's a guess from your own claims, and it never leaves "
                "this message: nobody else sees it, on the week grid or "
                "anywhere else."
            )
        elif not self.learn_habits:
            body = (
                "Day-learning is switched off for this channel, so I'm not "
                "keeping any history and I've nothing to guess from."
            )
        elif not person["monitor"]:
            body = (
                "👁 Monitoring is off, so I'm not logging your loads and I "
                "won't guess your days. Turn it back on from the panel if you "
                "want me to."
            )
        elif not person["predict"]:
            body = (
                "Guessing is off — no ░ on your week, and I won't work out "
                "your days. **Start guessing** puts it back; the loads I "
                "already noted are still there."
            )
        else:
            body = self._thin_data_text(user_id)
        if note:
            body = f"{note}\n\n{body}"
        embed.description = body
        embed.set_footer(
            text="I only ever learn from real Claim taps and from this panel."
        )
        return embed

    def _thin_data_text(self, user_id) -> str:
        """Why there is no guess yet, in this person's own numbers.

        Somebody who taps a button called *Fix a guess* and is told "nothing"
        deserves to know whether that means *broken* or *give it a fortnight*,
        and the three gates fail for genuinely different reasons (§7.2). Their
        own two numbers — loads seen, weeks watched — plus the bar, is enough
        to tell those apart without the panel pretending to a guess it doesn't
        have. Both numbers are read scoped to this one id, like every read in
        :mod:`habit`, so there is nothing here about anybody else.
        """
        now = self._now()
        loads = habit_mod.load_count(self._history, user_id, now)
        weeks = habit_mod.history_weeks(self._history, user_id, now)
        if loads == 0:
            seen = "I haven't seen you claim a load yet"
        else:
            seen = (
                f"So far I've noted **{loads} "
                f"{'load' if loads == 1 else 'loads'}** of yours over "
                f"**{weeks:.0f} {'week' if round(weeks) == 1 else 'weeks'}**"
            )
        return (
            "Nothing yet — and for the first few weeks that's the normal "
            f"answer, not a fault.\n\n{seen}. Before I'll say anything I need "
            f"**{habit_mod.MIN_OBSERVATIONS} loads in the same slot**, that "
            f"slot to be at least **{habit_mod.MIN_SHARE_PERCENT}%** of your "
            f"loads, and **{habit_mod.MIN_WEEKS} weeks** of history. Miss one "
            "of those and I'd rather say nothing than guess at you."
        )
