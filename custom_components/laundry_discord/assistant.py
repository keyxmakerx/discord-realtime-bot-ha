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

import asyncio
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
from . import trade as trade_mod
from .const import (
    CONF_LEARN_HABITS,
    CONF_TRADES,
    DEFAULT_LEARN_HABITS,
    DEFAULT_TRADES,
    GRID_BACK_CUSTOM_ID,
    GRID_DAY_CUSTOM_ID,
    GRID_RECUR_CUSTOM_ID,
    GRID_SLOT_CUSTOM_IDS,
    GUESS_BACK_CUSTOM_ID,
    GUESS_OFF_CUSTOM_ID,
    GUESS_RIGHT_CUSTOM_ID,
    GUESS_WRONG_CUSTOM_ID,
    NOTIFY_BACK_CUSTOM_ID,
    NOTIFY_KIND_CUSTOM_IDS,
    NOTIFY_QUIET_CUSTOM_ID,
    PANEL_CHANNEL_CUSTOM_ID,
    PANEL_DM_CUSTOM_ID,
    PANEL_GUESS_CUSTOM_ID,
    PANEL_MONITOR_CUSTOM_ID,
    PANEL_NOTIFY_CUSTOM_ID,
    PANEL_OFF_CUSTOM_ID,
    PANEL_WEEK_CUSTOM_ID,
    PLANNER_STORAGE_KEY,
    PLANNER_STORAGE_VERSION,
    TRADE_ACCEPT_CUSTOM_ID,
    TRADE_ASK_CUSTOM_ID,
    TRADE_BACK_CUSTOM_ID,
    TRADE_BLOCK_CUSTOM_ID,
    TRADE_OFFER_CUSTOM_ID,
    TRADE_PASS_CUSTOM_ID,
    TRADE_SEND_CUSTOM_ID,
)

if TYPE_CHECKING:
    from .discord_bot import DiscordBot

_LOGGER = logging.getLogger(__name__)

# One colour per panel. All five of these used to be blurple, which meant the
# stripe down the left of an ephemeral message carried no information at all:
# grid, ask-to-swap, welcome, settings and guess were indistinguishable at a
# glance, and these panels *replace each other in place*, so the stripe is the
# only thing that doesn't move between one and the next. Discord's own brand
# palette, so they still read as one bot rather than five.
_COLOR_PANEL = 0x5865F2  # blurple — 🤖 the hub everything comes back to
_COLOR_WELCOME = 0xFEE75C  # yellow — 👋 the one panel you see exactly once
_COLOR_GRID = 0x57F287  # green — 📅 the week
_COLOR_TRADE = 0xEB459E  # fuchsia — 🔁 the only panel that messages a person
_COLOR_GUESS = 0xC77DFF  # violet — 🔮 the model's opinion, not a fact
# Amber, and specifically *not* Discord's brand red, which is the one palette
# entry left. Red already means something in this integration — ``danger`` on a
# slot button is "somebody else has this" — and on an embed it reads as an
# error, which a settings panel is not. Amber is what a bell is drawn in.
_COLOR_NOTIFY = 0xFAA61A  # amber — 🔔 the only panel where every control mutes

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

# The four 🔔 switches: ``(emoji, button label, what the message actually is)``.
# Keyed by :data:`people.KINDS`, which is the one home for those names — see the
# comment there for why they live in a module that imports nothing. The third
# element is the panel's own sentence about that kind, kept beside its label so
# the button and the line explaining it cannot drift into describing two
# different messages.
#
# The check-in is "weekly" here and *not* "Sunday", which is what the design doc
# and the README both call it: the day is an integration option
# (``plan_dm_weekday``), read in :mod:`reminders`, and a panel that names a day
# the house has changed is a settings screen caught lying about something the
# reader can check in one tap. The heads-up line says "before" and not "an hour
# before" for exactly the same reason: its lead is ``nudge_lead``, an option
# running from five minutes to three hours, so the hour belongs to the house
# rather than to this panel.
_NOTIFY_KINDS = {
    people_mod.KIND_CHECKIN: (
        "📅",
        "Check-in",
        "the weekly DM about the week ahead",
    ),
    people_mod.KIND_SLOT: (
        "⏰",
        "Heads-up",
        "before a slot you booked opens, so it doesn't lapse unused",
    ),
    people_mod.KIND_OPPORTUNITY: (
        "💡",
        "Spare slot",
        "when the washer's clear and you're overdue by your own usual gap",
    ),
    people_mod.KIND_TRADES: (
        "🔁",
        "Swaps",
        "a housemate asking whether they can have one of your slots",
    ),
}

# The quiet-hours presets, as ``(start, end)`` local hours — start inclusive,
# end exclusive, wrapping midnight — with None for "no quiet hours" first,
# because the way *out* of a setting must never be harder to find than the way
# in (P7).
#
# Overnight only, and only five of them. That is not a limitation, it is the
# actual complaint: :data:`plan.SLOT_WINDOWS` opens AM at 06:00 and the heads-up
# runs ahead of the slot it is about, so the AM one is the earliest thing this
# integration schedules and the only one that can wake somebody — and *how much*
# earlier than 06:00 is the house's ``nudge_lead``, which is why no line in this
# panel names the hour it lands at. A midday window would be a setting nobody
# picks, and every unused setting is one more line a newcomer reads past to
# reach the one they came for.
_QUIET_PRESETS = (None, (22, 8), (23, 9), (0, 7), (21, 9))


def _quiet_label(window) -> str:
    """A quiet window as the panel says it out loud."""
    if window is None:
        return "No quiet hours"
    return f"{window[0]:02d}:00–{window[1]:02d}:00"


def _quiet_value(window) -> str:
    """A preset as a select value: ``"none"``, or ``"22-8"``.

    Deliberately the two hours and nothing else. A value carrying an index into
    :data:`_QUIET_PRESETS` would be read by a tap on a panel that was rendered
    before an upgrade reordered the list, and would then set a window nobody
    picked — the select is persistent, so its values outlive the render.
    """
    return "none" if window is None else f"{window[0]}-{window[1]}"


def _parse_quiet(value) -> tuple[int | None, int | None]:
    """A select value back to ``(start, end)`` hours; anything odd clears it.

    Clearing is the only safe direction for something unreadable. The failure
    mode of guessing is somebody silenced past a morning that never arrives,
    where the failure mode of clearing is one setting they can put back in a
    tap. :func:`people.set_quiet_hours` re-checks both hours anyway, so a pair
    out of range lands in the same place rather than storing a 25th hour.
    """
    parts = str(value).split("-")
    if len(parts) != 2:
        return (None, None)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (None, None)


# How long a swap request DM is allowed to take to leave the building. Same
# reasoning as :data:`reminders._SEND_TIMEOUT`: ``async_dm_user`` starts with
# ``wait_until_ready()``, so a send attempted while the gateway is down would
# otherwise park there until it reconnects and deliver an ask about a slot that
# has been and gone. A request that could not be sent is withdrawn, not queued.
_TRADE_SEND_TIMEOUT = 30

# What a tap on a swap DM says when the request behind it is gone — answered
# already, or lapsed. Deliberately says nothing about who or why: a DM sits in
# an inbox indefinitely, and the person tapping it a week late must not learn
# anything from the fact that it no longer works.
_TRADE_STALE = (
    "That one's lapsed — nothing's been changed, and nobody's been told "
    "anything either way."
)


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


class _PanelNotifyButton(discord.ui.Button):
    """Open the 🔔 "what I send you" sub-panel.

    A sub-panel rather than four more buttons on the main one, and that is a
    space decision with a reason behind it: row 1 had two free slots when this
    arrived, the controls need six, and a settings panel you have to read past
    to find *how should I reach you* has stopped being the onboarding surface
    it exists to be. So the one button that fits opens the five that don't.

    Shown to anybody onboarded, including somebody whose **Pings** are set to
    the channel — where none of these switches currently change anything. That
    is deliberate: hiding a setting because another setting makes it inert is
    how somebody concludes the bot cannot be told to stop, and the panel behind
    this says plainly what is and isn't reaching them.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="What I send you",
            style=discord.ButtonStyle.secondary,
            emoji="🔔",
            custom_id=PANEL_NOTIFY_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_open_notify(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the notification panel")
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


class _NotifyKindButton(discord.ui.Button):
    """One kind of unprompted message, labelled with the state it is in.

    Labelled and tapped exactly like 👁 Monitoring, and for the same reason: the
    label *is* the setting, so reading the panel and changing it are one gesture
    and there is no separate save to forget. ``enabled=None`` is the
    registration template, which has no person and so no state to report.

    The labels are short — "Heads-up", not "Slot heads-up" — because four of
    these share one row and Discord truncates a label from the **right**, which
    is exactly where the ``: on`` lives. A switch that has lost its state is a
    switch you have to tap to read, and tapping it is what changes it. The full
    sentence for every kind is in the embed above, where there is room.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        kind: str,
        label: str,
        emoji: str,
        *,
        enabled: bool | None,
    ) -> None:
        super().__init__(
            label=(
                label
                if enabled is None
                else f"{label}: {'on' if enabled else 'off'}"
            ),
            style=discord.ButtonStyle.secondary,
            emoji=emoji,
            custom_id=NOTIFY_KIND_CUSTOM_IDS[kind],
            row=0,
        )
        self.assistant = assistant
        self.kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_toggle_dm_kind(interaction, self.kind)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to toggle a notification kind")
            await self.assistant.async_report_error(interaction)


class _NotifyQuietSelect(discord.ui.Select):
    """The overnight quiet window, as a short list of presets.

    A select rather than a time picker because Discord has no time input at
    all, and the alternative is parsing "10ish" out of a modal — a text box that
    can be wrong in more ways than it can be right, on a setting whose failure
    mode is silence.

    The stored window is named in the placeholder *as well as* marked ``default``
    on its own option, which is not belt-and-braces: Discord shows the
    placeholder only when no option is default, and that is exactly the case
    where somebody's record holds a window these presets don't contain — a
    hand-edited store, or a preset a later version dropped. The panel then still
    says what is actually in force, instead of showing an empty box that reads
    as "no quiet hours" about somebody who has some.
    """

    def __init__(
        self, assistant: "LaundryAssistant", window: tuple[int, int] | None
    ) -> None:
        super().__init__(
            placeholder=f"Quiet hours: {_quiet_label(window)}",
            custom_id=NOTIFY_QUIET_CUSTOM_ID,
            min_values=1,
            max_values=1,
            row=1,
            options=[
                discord.SelectOption(
                    label=_quiet_label(preset),
                    value=_quiet_value(preset),
                    default=preset == window,
                )
                for preset in _QUIET_PRESETS
            ],
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_set_quiet_hours(
                interaction, self.values[0]
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to set quiet hours")
            await self.assistant.async_report_error(interaction)


class _NotifyBackButton(discord.ui.Button):
    """Back to the settings panel.

    Its own ``custom_id`` rather than 🔮's or the grid's, though all three do the
    same thing: ``add_view`` keys the persistent registry by id, so two views
    sharing one means the second registration quietly wins for both.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=NOTIFY_BACK_CUSTOM_ID,
            row=2,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_back_to_panel(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to return to the panel")
            await self.assistant.async_report_error(interaction)


class NotifyView(discord.ui.View):
    """The 🔔 panel: four kind toggles, the quiet-hours select, and back.

    Three rows, six components. The four toggles share row 0 because they are
    one question asked four times; the select takes a whole row as Discord
    requires, and Back sits under both.

    ``person`` is the record being rendered for, used only to label the toggles
    and pre-select the window. ``None`` is the neutral registration template
    handed to ``add_view``. Unlike :class:`GuessView` there is nothing
    conditional to leave out here, so the template differs from a real render
    only in what the labels say — but it is still the template that must carry
    every id, because one that was never registered doesn't error, it silently
    stops dispatching.

    Iterating :data:`people.KINDS` rather than a list of its own is what keeps
    that honest: a kind added there without a row in :data:`_NOTIFY_KINDS` or an
    id in :data:`const.NOTIFY_KIND_CUSTOM_IDS` raises ``KeyError`` right here,
    and "here" is startup — ``on_ready`` builds this template — rather than the
    first time somebody opens the panel.
    """

    def __init__(
        self, assistant: "LaundryAssistant", *, person: dict | None = None
    ) -> None:
        super().__init__(timeout=None)
        for kind in people_mod.KINDS:
            emoji, label, _what = _NOTIFY_KINDS[kind]
            self.add_item(
                _NotifyKindButton(
                    assistant,
                    kind,
                    label,
                    emoji,
                    enabled=(
                        people_mod.wants_kind(person, kind)
                        if person is not None
                        else None
                    ),
                )
            )
        self.add_item(
            _NotifyQuietSelect(
                assistant,
                people_mod.quiet_hours(person) if person is not None else None,
            )
        )
        self.add_item(_NotifyBackButton(assistant))


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


# Which button style carries which cell state. The button row sits directly
# under the grid and until now said far less than it: it was binary — green for
# yours, grey for *everything else* — so free, taken and guessed were one
# appearance while the block above distinguished all three. Somebody reading the
# buttons rather than the grid could not see they were about to book themselves
# into a slot two other people already wanted.
#
# discord.py offers four styles and there are six states, so the mapping is not
# one to one and the two collisions are chosen rather than accidental:
#
# * **danger** — somebody else has this. One style for ▒ and ║ both, because
#   red already carries the fact that matters at the moment of tapping ("this
#   is contended"); *how often* they have it is what decides whether to ask for
#   a swap, and that is a decision you make reading the grid, not the row.
#   Red is not a veto — the button stays enabled, see below — it is the only
#   style left that reads as "not free", and this codebase had not used it.
# * **primary** — the model's guess. Blurple is the "suggested" accent, which
#   is exactly what a guess is.
# * **success** — yours. Unchanged.
# * **secondary** — free. Unchanged.
#
# ``*`` running has no style of its own: nothing produces it yet (Phase 4), and
# it falls back to secondary until something does.
_SLOT_BUTTON_STYLES = {
    plan_mod.STATE_MINE: discord.ButtonStyle.success,
    plan_mod.STATE_TAKEN: discord.ButtonStyle.danger,
    plan_mod.STATE_TAKEN_EVERY_WEEK: discord.ButtonStyle.danger,
    plan_mod.STATE_EXPECTED: discord.ButtonStyle.primary,
}


class _GridSlotButton(discord.ui.Button):
    """Book or free one slot on the selected day.

    Styled to say the same thing the cell above it says (see
    :data:`_SLOT_BUTTON_STYLES`). It is deliberately **not** disabled when
    somebody else holds the cell: the plan is information, not permission (§8),
    so two people can want the same slot and the grid's job is to make that
    visible rather than to arbitrate it. Red here means "somebody's already down
    for this", not "you may not".
    """

    def __init__(
        self, assistant: "LaundryAssistant", slot: str, *, state: str
    ) -> None:
        super().__init__(
            label=plan_mod.slot_label(slot),
            style=_SLOT_BUTTON_STYLES.get(state, discord.ButtonStyle.secondary),
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


class _TradeAskButton(discord.ui.Button):
    """🔁 — offer to ask whoever is down for the slot you just tapped (§9).

    Only ever added when the last tap landed on a cell somebody **else** holds,
    which is the moment §9 describes: *"That one's spoken for. Want me to
    ask?"*. It does not replace the booking — tapping a taken slot still books
    you in alongside its holder, because a plan is information, not permission
    (§8), and that stays true. This is the extra option, not a different one.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Ask to swap",
            style=discord.ButtonStyle.primary,
            emoji="🔁",
            custom_id=TRADE_ASK_CUSTOM_ID,
            row=2,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_open_ask(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the swap panel")
            await self.assistant.async_report_error(interaction)


class _GridRecurButton(discord.ui.Button):
    """♻ — promote the cell you just tapped to *every week*, or demote it.

    The writer ``person["slots"]`` never had. The field has been defaulted,
    normalised, read and reconciled since the planner shipped; nothing could
    set it, so a standing slot was a thing the store understood, the grid could
    draw and no human could create.

    It targets **the cell you last tapped**, the same convention 🔁 next to it
    uses, because that is the only cell on a seven-by-four grid the panel can
    know you mean. So the gesture is: tap Thu Eve, then tap ♻. It is added only
    when there is something to say — you hold the last-tapped cell — so it can
    never ask "every week?" about somebody else's booking or about nothing at
    all.

    Labelled by what it will *do* rather than by what is true now. "Every week"
    on a cell that is already standing reads as a statement and gets tapped by
    people meaning to confirm it, which would quietly cancel the thing they
    were agreeing with.
    """

    def __init__(self, assistant: "LaundryAssistant", *, standing: bool) -> None:
        super().__init__(
            label="Just this week" if standing else "Every week",
            style=discord.ButtonStyle.secondary
            if standing
            else discord.ButtonStyle.primary,
            emoji="♻️",
            custom_id=GRID_RECUR_CUSTOM_ID,
            row=2,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_toggle_recurring(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to change a recurring slot")
            await self.assistant.async_report_error(interaction)


class GridView(discord.ui.View):
    """The week grid's controls: day select, four slot toggles, back.

    Three rows, six to eight components — well inside the 5-row /
    25-component ceiling, with the select occupying a whole row of its own as
    Discord requires.

    ``occupancy`` and ``day`` are used only to label and colour the buttons.
    Passing neither builds the neutral registration template for ``add_view``,
    which must carry **every** ``custom_id`` — one that was never registered
    doesn't error, it silently stops dispatching, and this integration has
    already been bitten by that. That is why 🔁 and ♻ are both added for the
    template even though a template has no tapped cell to ask about or promote.

    ``expected`` and ``running`` are passed straight through to
    :func:`plan.cell_state` so a button and the cell above it can never disagree
    about what state that cell is in — one question, asked once, answered in the
    module that owns the precedence rule.

    ``recur`` is None when there is nothing of yours to promote, and otherwise
    says whether the last-tapped cell is already standing — which decides what
    ♻ is labelled, not whether it appears.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        *,
        occupancy: dict | None = None,
        day: int = 0,
        viewer_id=None,
        ask: bool = False,
        expected=None,
        running=None,
        recur: bool | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_GridDaySelect(assistant, day))
        for slot in plan_mod.SLOTS:
            cell = plan_mod.cell_key(day, slot)
            state = (
                plan_mod.cell_state(occupancy, cell, viewer_id, expected, running)
                if occupancy is not None and cell is not None
                else plan_mod.STATE_FREE
            )
            self.add_item(_GridSlotButton(assistant, slot, state=state))
        self.add_item(_GridBackButton(assistant))
        if recur is not None or occupancy is None:
            self.add_item(_GridRecurButton(assistant, standing=bool(recur)))
        if ask or occupancy is None:
            self.add_item(_TradeAskButton(assistant))


class _TradeOfferSelect(discord.ui.Select):
    """Which of *your own* slots to put up in return.

    Only your own cells for this week are listed, and the one you're asking for
    is left out — a swap needs two different slots and something real on both
    sides. Nothing in this list can identify anybody: every option is one of the
    viewer's own bookings, described by day and slot.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        offers: list[str] | None,
        selected: str | None,
    ) -> None:
        # A select must carry at least one option, and the registration
        # template (built with no offers) has none to carry — hence the inert
        # placeholder. The template is never rendered to anybody; it exists so
        # ``add_view`` learns this custom_id.
        options = [
            discord.SelectOption(
                label=trade_mod.describe_cell(cell) or cell,
                value=cell,
                default=cell == selected,
            )
            for cell in (offers or [])[:25]
        ] or [discord.SelectOption(label="—", value="none")]
        super().__init__(
            placeholder="What would you offer in return?",
            custom_id=TRADE_OFFER_CUSTOM_ID,
            min_values=1,
            max_values=1,
            row=0,
            options=options,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_pick_offer(interaction, self.values[0])
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to choose a swap offer")
            await self.assistant.async_report_error(interaction)


class _TradeSendButton(discord.ui.Button):
    """🔁 Ask — the one tap that puts a message on somebody else's phone."""

    def __init__(self, assistant: "LaundryAssistant", *, ready: bool) -> None:
        super().__init__(
            label="Ask",
            style=discord.ButtonStyle.primary,
            emoji="🔁",
            custom_id=TRADE_SEND_CUSTOM_ID,
            row=1,
            disabled=not ready,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_send_trade(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to send a swap request")
            await self.assistant.async_report_error(interaction)


class _TradeBackButton(discord.ui.Button):
    """Back to the grid, having asked nobody anything."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=TRADE_BACK_CUSTOM_ID,
            row=1,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_back_to_grid(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to return to the week grid")
            await self.assistant.async_report_error(interaction)


class TradeAskView(discord.ui.View):
    """The "shall I ask?" panel: pick an offer, then ask — or back out.

    Built with no arguments for ``add_view``; that template carries all three
    ``custom_id``s including the select's, which is why the select falls back to
    a placeholder option rather than being left out.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        *,
        offers: list[str] | None = None,
        selected: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(_TradeOfferSelect(assistant, offers, selected))
        self.add_item(_TradeSendButton(assistant, ready=bool(offers and selected)))
        self.add_item(_TradeBackButton(assistant))


class _TradeAnswerButton(discord.ui.Button):
    """One of the three answers on an incoming swap DM (§9 step 2)."""

    def __init__(
        self,
        assistant: "LaundryAssistant",
        action: str,
        label: str,
        emoji: str,
        custom_id: str,
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(
            label=label, style=style, emoji=emoji, custom_id=custom_id, row=0
        )
        self.assistant = assistant
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_answer_trade(interaction, self.action)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to answer a swap request")
            await self.assistant.async_report_error(interaction)


class TradeRequestView(discord.ui.View):
    """The anonymous request DM's three answers.

    Nothing per-request lives in these ``custom_id``s — a per-request id cannot
    be a persistent view, so the buttons would die at the next restart. Which
    request a tap answers is resolved from the recipient and the DM's own
    timestamp (:func:`trade.match_request`), which is why only one ask may ever
    be waiting on one person.
    """

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(timeout=None)
        self.add_item(
            _TradeAnswerButton(
                assistant,
                trade_mod.ACTION_ACCEPT,
                "Trade",
                "✅",
                TRADE_ACCEPT_CUSTOM_ID,
                discord.ButtonStyle.success,
            )
        )
        self.add_item(
            _TradeAnswerButton(
                assistant,
                trade_mod.ACTION_PASS,
                "Pass",
                "❌",
                TRADE_PASS_CUSTOM_ID,
                discord.ButtonStyle.secondary,
            )
        )
        self.add_item(
            _TradeAnswerButton(
                assistant,
                trade_mod.ACTION_BLOCK,
                "Don't ask me again",
                "🚫",
                TRADE_BLOCK_CUSTOM_ID,
                discord.ButtonStyle.secondary,
            )
        )


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

    Seven components at most, in two rows: the three reminder modes on row 0,
    and 👁 / 📅 / 🔔 / 🔮 on row 1. Discord allows 5 per row and 25 per message,
    so row 1 has one slot left before anything has to move — which is the whole
    reason 🔔 opens a sub-panel instead of putting four toggles and a select
    here, where they would not fit and would bury the one question this panel
    exists to ask.
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
            # 🔔 hides on the first-time panel for the same reason again, and
            # more sharply: it is a panel of four ways to hear *less*, shown to
            # somebody who has not yet agreed to hear anything. It answers a
            # question they haven't been asked yet, and the answer they'd give
            # to "how should I reach you" is the one that decides whether any
            # of it matters.
            self.add_item(_PanelNotifyButton(assistant))
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
        # Which cell each person's open grid last tapped that somebody *else*
        # holds, and what they've picked to offer for it. Memory only, like
        # ``_grid_day`` and for the same reason: it is view state, not a
        # preference, and losing it on a restart costs one tap. Nothing here is
        # a request — a request only exists once ``async_send_trade`` writes
        # one.
        self._ask_cell: dict[str, str] = {}
        self._ask_offer: dict[str, str] = {}
        # Which cell each person's open grid last tapped, whoever holds it.
        # ``_ask_cell`` above is *not* this: it holds a cell only when the tap
        # landed on somebody else's, because 🔁 has nothing to offer otherwise.
        # ♻ needs the opposite case — a cell of your own — so it needs its own
        # note. View state, memory only, like the two above.
        self._last_cell: dict[str, str] = {}
        # Which cell the last reminder DM to each person was about. See
        # :meth:`note_nudge_cell` — the heads-up lands before its slot opens, so
        # the message's own timestamp can no longer identify it unambiguously.
        self._nudge_cell: dict[str, str] = {}
        # The live load's window, pushed by the coordinator (never pulled: the
        # dependency runs one way, §14 rule 5). Two floats, memory only, and
        # deliberately *not* the cells themselves — those are worked out on
        # each render by :meth:`_running_cells`, so ``*`` cannot outlive the
        # load that justified it. A restart mid-load loses them, and the grid
        # simply stops claiming the washer is busy, which is the safe way round.
        self._running_from: float | None = None
        self._running_until: float | None = None
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
        # The trade broker's requests (design doc §9 / §12). In the planner
        # Store like everything else — a request is a fact about the week, and
        # a week is what this store is for. Bounded by the rules that write it
        # and pruned to the current week on load and on every write; expiry is
        # arithmetic at read time, so nothing has to run on a tick to age one
        # out and nothing is written to make an old request stop working.
        self._trades: list[dict] = []

    # ------------------------------------------------------------------ config
    @property
    def trades_enabled(self) -> bool:
        """Whether one housemate may ask another for a slot at all (§9).

        Off by default (§14 rule 7), and off means **inert**: no 🔁 button is
        rendered, ``async_open_ask`` refuses, and no request can be written — so
        a house that never turns it on never produces a byte of trade state or a
        single DM.
        """
        if self._entry is None:
            return DEFAULT_TRADES
        merged = {**self._entry.data, **self._entry.options}
        return bool(merged.get(CONF_TRADES, DEFAULT_TRADES))

    @property
    def learn_habits(self) -> bool:
        """Whether the house has day-learning switched on at all.

        The outer of the two gates on every history write and every ``?``; the
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
        # Pruned in memory on the way in, like history and corrections, and not
        # written back: startup is the definition of "nothing changed". A
        # request from a week that has already happened is dead weight — the
        # once-a-week rules it gates only ever ask about the week that is
        # running.
        raw_trades = data.get("trades") if isinstance(data, dict) else None
        self._trades = trade_mod.prune_requests(raw_trades, self._current_week())

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
                    "trades": self._trades,
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

    def note_running(self, started_ts, eta_ts) -> None:
        """The coordinator telling us a load's window, or that there isn't one.

        Pushed rather than pulled — the assistant must not reach back into the
        coordinator (§14 rule 5), so that the planner keeps working if the
        detection side is having a bad day. Two floats and no store write, so
        this is free to call on every tick that moves the ETA.
        """
        self._running_from = started_ts if isinstance(started_ts, (int, float)) else None
        self._running_until = eta_ts if isinstance(eta_ts, (int, float)) else None

    def _running_cells(self) -> list[str]:
        """The cells the washer is mid-load in, worked out fresh right now.

        Derived on every render and stored nowhere. ``*`` is the one glyph that
        claims something about *this moment*, so anything that could let it
        outlive its load — a cached list, a value in the Store — would make it a
        claim nobody can check. Recomputing is cheap and self-correcting: the
        moment the coordinator clears the window, the next render has no ``*``
        in it.
        """
        if self._running_from is None:
            return []
        try:
            start = dt_util.as_local(dt_util.utc_from_timestamp(self._running_from))
            end = (
                dt_util.as_local(dt_util.utc_from_timestamp(self._running_until))
                if self._running_until is not None
                else None
            )
        except (OSError, OverflowError, TypeError, ValueError):
            return []
        return plan_mod.cells_between(start, end)

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

    async def async_forget_load(self, user_id, since_ts, until_ts) -> None:
        """That load was stopped on the machine, so it was never a wash.

        The coordinator calls this from the cancel path and, as with
        :meth:`async_note_claim`, knows nothing else about the model. A claim
        made *during* the wash has already written its row by then, so "a
        cancelled load must not feed the habit model" cannot be a rule anybody
        remembers to apply at read time — it is this call, inside the same
        transition that ended the load, plus the coordinator refusing to log a
        claim tapped on a card it has already marked stopped. Between them there
        is no path by which a stopped cycle becomes evidence.

        ``since_ts``/``until_ts`` bound the retraction to that load's own
        session (see :func:`habit.forget_load`), so an earlier real wash of
        theirs cannot be caught by it.

        No consent gate here, deliberately: this only ever *removes*. Running it
        with day-learning off simply finds nothing, and the comparison below
        means finding nothing costs no store write.

        Never raises — it runs inside the completion transition, and failing to
        un-log a load must not leave the card unfinished.
        """
        try:
            updated = habit_mod.forget_load(self._history, user_id, since_ts, until_ts)
            if updated == self._history:
                return
            self._history = updated
            await self._async_save()
            # A decision actually taken (a row that existed is gone), not a
            # per-evaluation trace: the no-match path returns above.
            _LOGGER.debug("Dropped a stopped load from history for %s", user_id)
        except Exception:  # noqa: BLE001 - never raise into the session machine
            _LOGGER.exception("Failed to drop a stopped load from the habit model")

    def _predicts_for(self, user_id) -> bool:
        """Whether this person's guesses may be computed or shown at all.

        Both gates again, plus the person's ``predict`` preference. Monitoring
        is included on the *read* side deliberately: turning it off stops new
        rows, but rows already stored would otherwise keep producing ``?`` for
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
        """The cells to draw as ``?`` — **only ever the viewer's own** (§11).

        Every read in :mod:`habit` is scoped to one id, and the only id this is
        ever called with is ``interaction.user.id``: the person looking at the
        message. A prediction is a claim about one person's habits, and it is
        never rendered into anything more than one person can see.

        Deliberately the **top** guess alone, not every cell that clears the
        gate. :func:`habit.predictions` can return up to three (4/3/3 of ten
        loads all pass at 30%), but the 🔮 panel names exactly one and ❌ Wrong
        retires exactly that one. Drawing the other two would put a ``?`` on
        the grid that the only button for arguing with it cannot even mention,
        let alone remove — and tapping Wrong about the Monday ``?`` would
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

    def typical_gap(self, user_id) -> float | None:
        """How many days this person usually leaves between loads, or None.

        Gated the same way :meth:`prediction_for` is, and for the same reason:
        it is arithmetic about somebody's own history, so 👁 Monitoring off must
        stop it being *read* as well as written. Otherwise turning monitoring
        off would keep producing timing claims from rows already stored, which
        is precisely what somebody was switching off.
        """
        if not self._predicts_for(user_id):
            return None
        return habit_mod.typical_gap(self._history, user_id, self._now())

    def is_due(self, user_id) -> bool:
        """Whether this person is past **their own** usual gap between washes.

        The gate on the opportunity nudge. False whenever the cadence is not
        known yet, which is the common early answer and the quiet one.
        """
        if not self._predicts_for(user_id):
            return False
        return habit_mod.is_due(self._history, user_id, self._now())

    def occupancy(self) -> dict:
        """This week's reconciled occupancy — what the grid draws from.

        The reminder loop needs it to answer "has somebody else booked the slot
        this person usually uses", which is the fact that makes an opportunity
        nudge worth sending rather than a guess about their laundry.
        """
        return plan_mod.effective_week(
            self._people, self._overrides, self._current_week()
        )

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

    async def async_free_cell(self, user_id, cell, week=None) -> bool:
        """🆓 Free it up — give a booked slot back to the house.

        The mirror of :meth:`async_book_cell` and idempotent in the same way: a
        second tap must not re-book what the first released. Returns False when
        there was nothing of theirs to release, so the reply can say so rather
        than claiming to have freed a slot that was never taken.

        It removes **only this person** from the cell. Two people can hold one
        slot (§8), and releasing yours has no business evicting somebody else
        from a booking you never made.

        Whether the standing slot should go too is deliberately *not* asked
        here. This button answers "not this week"; ♻ on the grid is where "not
        every week" lives, and conflating them would turn one tap on one
        Thursday into a permanent change nobody asked for. Because
        :func:`plan.toggle_booking` writes the reconciled holder list into this
        week's override, a recurring slot released here is released for this
        week only and returns next week — which is exactly what a standing
        booking means.
        """
        key = plan_mod.normalise_cell(cell)
        target = week if isinstance(week, str) and week else self._current_week()
        if key is None or not target:
            return False
        if key not in self.booked_cells(user_id, target):
            return False  # not theirs — nothing to give back
        self._overrides, _booked = plan_mod.toggle_booking(
            self._people, self._overrides, target, key, user_id
        )
        await self._async_save()
        return True

    def note_nudge_cell(self, user_id, cell) -> None:
        """Remember which cell the DM we are about to send is about.

        The heads-up arrives *before* its slot opens, which breaks the trick the
        day-of nudge used to identify itself: at 19:00 both PM (16:00-20:00) is
        running and Eve starts within the hour, so a cell inferred from the
        message's own timestamp is genuinely ambiguous, and the wrong answer
        books a slot nobody chose on the whole household's grid.

        Memory only, so a restart loses it — and :func:`reminders._dm_cell`
        falls back to the timestamp reading rather than refusing the tap. That
        is strictly better than the old behaviour, not worse: exact whenever we
        know, and the previous best guess whenever we don't.
        """
        key = plan_mod.normalise_cell(cell)
        if key is not None:
            self._nudge_cell[str(user_id)] = key

    def nudge_cell(self, user_id) -> str | None:
        """The cell the last nudge DM to this person was about, if remembered."""
        return self._nudge_cell.get(str(user_id))

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

    # --------------------------------------------------------- what I send you
    async def async_open_notify(self, interaction: discord.Interaction) -> None:
        """Answer 🔔 with every message the bot starts, and its switch."""
        await self._async_render_notify(interaction)

    async def async_toggle_dm_kind(
        self, interaction: discord.Interaction, kind: str
    ) -> None:
        """🔔 button: flip one kind of unprompted message, then re-render.

        The current value is read at tap time rather than carried on the button
        from the render, for the reason :meth:`async_reject_guess` re-reads its
        cell: a panel opened before a restart is still tapped after one, and
        *toggle* has to mean the state as it is now rather than the state this
        process last drew. Getting that wrong on a switch is worse than on a
        correction — the tap would set the value the button already showed, so
        nothing would appear to happen, twice.
        """
        user_id = interaction.user.id
        person = people_mod.get_person(self._people, user_id)
        self._people = people_mod.set_dm_kind(
            self._people,
            user_id,
            kind,
            not people_mod.wants_kind(person, kind),
            name=interaction.user.display_name,
        )
        await self._async_save()
        await self._async_render_notify(interaction)

    async def async_set_quiet_hours(
        self, interaction: discord.Interaction, value
    ) -> None:
        """🔔 select: choose — or with "No quiet hours" clear — the window.

        The only control in this file where choosing the value that is already
        set is an ordinary gesture: Discord renders the current window as the
        selected option, so re-picking it is one stray tap away, where every
        button here flips something by definition. So this is also the only one
        that checks before writing, which is the panel's standing rule (a store
        write only when something actually changed) applied where it can finally
        bite. Comparing the rebuilt mapping is exact rather than approximate —
        every record in it has been through ``normalise_person``, so equal
        content compares equal and a re-pick costs nothing at all.
        """
        user_id = interaction.user.id
        start, end = _parse_quiet(value)
        updated = people_mod.set_quiet_hours(
            self._people,
            user_id,
            start,
            end,
            name=interaction.user.display_name,
        )
        if updated != self._people:
            self._people = updated
            await self._async_save()
        await self._async_render_notify(interaction)

    async def _async_render_notify(
        self, interaction: discord.Interaction
    ) -> None:
        """Draw the 🔔 panel for this viewer, in place where possible."""
        person = people_mod.get_person(self._people, interaction.user.id)
        await self._async_respond(
            interaction,
            self._notify_embed(person),
            NotifyView(self, person=person),
            edit=True,
        )

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
        self._clear_ask(user_id)
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
        # 🔁 always refers to the cell that was last *tapped*, so changing day
        # must retire it: a swap button pointing at a Thursday while the buttons
        # underneath say Monday is the one way this could ask about a slot
        # somebody didn't mean.
        self._clear_ask(interaction.user.id)
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
        # A tap that lands on somebody else's slot books you in alongside them —
        # unchanged, and §8 requires it — and *also* arms 🔁. That is the whole
        # of §9 step 1: the booking is the information, the offer to ask is the
        # extra option, and neither one is a veto over the other.
        self._note_ask(user_id, cell, week)
        # ♻ targets this, whoever holds it — see ``_last_cell``.
        self._last_cell[str(user_id)] = cell
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

    async def async_toggle_recurring(
        self, interaction: discord.Interaction
    ) -> None:
        """♻ — make the last-tapped cell a standing weekly slot, or stop it.

        Writes ``person["slots"]`` and nothing else. This week's override is
        deliberately left alone: you already hold the cell — that is the only
        reason ♻ was offered — so booking it again would be a no-op, and
        *freeing* it would make "every week" mean "every week starting next
        week", which is not what the button says.

        Demoting is the mirror image and the same restraint applies. Dropping
        the standing slot leaves this week's booking standing on its own, which
        is exactly right: "I don't do this every week any more" is not "cancel
        the one I have on Thursday".
        """
        user_id = interaction.user.id
        cell = self._last_cell.get(str(user_id))
        week = self._current_week()
        if cell is None or not week:
            await self._async_render_grid(interaction, edit=True)
            return
        person = people_mod.get_person(self._people, user_id)
        slots, standing = plan_mod.toggle_recurring(person.get("slots"), cell)
        self._people = people_mod.set_person(self._people, user_id, slots=slots)
        # Demoting must leave the cell booked for *this* week, and it would not
        # be if the standing slot was the only thing putting them on it — the
        # override for this week may simply not mention the cell. Writing the
        # reconciled holder list back pins what they can already see, so ♻ never
        # silently cancels a booking the grid was showing them a second ago.
        if not standing:
            held = plan_mod.holders(
                plan_mod.effective_week(self._people, self._overrides, week), cell
            )
            if str(user_id) not in held:
                self._overrides, _booked = plan_mod.toggle_booking(
                    self._people, self._overrides, week, cell, user_id
                )
        await self._async_save()
        parsed = plan_mod.parse_cell(cell)
        where = (
            f"{plan_mod.DAY_NAMES[parsed[0]]} {plan_mod.slot_label(parsed[1])}"
            if parsed
            else "That slot"
        )
        note = (
            f"♻️ **{where}** is yours every week now — it'll be on your grid "
            "before anyone else books it."
            if standing
            else f"♻️ **{where}** is just this week again. You still have it "
            "this week."
        )
        await self._async_render_grid(interaction, edit=True, note=note)

    async def async_back_to_panel(self, interaction: discord.Interaction) -> None:
        """Return from the grid to the settings panel."""
        await self._async_rerender(interaction)

    def _clear_ask(self, user_id) -> None:
        """Forget which cell 🔁 was pointing at, and what was offered for it."""
        key = str(user_id)
        self._ask_cell.pop(key, None)
        self._ask_offer.pop(key, None)

    def _note_ask(self, user_id, cell, week) -> None:
        """Arm 🔁 if this tap landed on a cell somebody else holds.

        Purely view state — nothing is written, nobody is contacted, and with
        trades switched off it never arms at all. It is the difference between
        "that one's spoken for" being a fact on the grid and being an offer to
        do something about it.
        """
        self._clear_ask(user_id)
        if not self.trades_enabled or not week:
            return
        occupancy = plan_mod.effective_week(self._people, self._overrides, week)
        if plan_mod.is_taken_by_other(occupancy, cell, user_id):
            self._ask_cell[str(user_id)] = cell

    def _offer_cells(self, user_id, want, week) -> list[str]:
        """The viewer's own cells they could put up in return, ordered.

        Their own bookings for this week, minus the one they're asking for. A
        list of the viewer's own slots leaks nothing: it is the same set the
        grid already draws back to them as ``█``.
        """
        return [
            cell
            for cell in sorted(
                self.booked_cells(user_id, week),
                key=lambda c: (plan_mod.parse_cell(c) or (9, "")),
            )
            if cell != want
        ]

    async def _async_render_grid(
        self, interaction: discord.Interaction, *, edit: bool, note: str | None = None
    ) -> None:
        """Draw the grid for this viewer, in place where possible."""
        user_id = interaction.user.id
        day = self._grid_day.get(str(user_id), self._today())
        week = self._current_week()
        occupancy = plan_mod.effective_week(self._people, self._overrides, week)
        ask_cell = self._ask_cell.get(str(user_id))
        expected = self._predicted_cells(user_id)
        running = self._running_cells()
        # ♻ is offered only for a cell of your own — there is nothing to promote
        # about somebody else's booking, and nothing at all about a free slot.
        last = self._last_cell.get(str(user_id))
        recur = (
            plan_mod.is_recurring_for_me(occupancy, last, user_id)
            if last is not None and plan_mod.is_mine(occupancy, last, user_id)
            else None
        )
        embed = self._grid_embed(
            occupancy,
            user_id,
            day,
            expected=expected,
            running=running,
            ask_cell=ask_cell,
            note=note,
        )
        view = GridView(
            self,
            occupancy=occupancy,
            day=day,
            viewer_id=user_id,
            ask=ask_cell is not None,
            expected=expected,
            running=running,
            recur=recur,
        )
        await self._async_respond(interaction, embed, view, edit=edit)

    def _grid_embed(
        self,
        occupancy,
        user_id,
        day: int,
        *,
        expected=None,
        running=None,
        ask_cell: str | None = None,
        note: str | None = None,
    ) -> discord.Embed:
        """The week as a monospace block, plus this person's own cells.

        The block is fenced so Discord renders it monospace — without that the
        columns pull apart into nonsense on a proportional font. Everything
        decorative (the legend, the slot windows) lives *outside* the fence,
        because an emoji inside a code block breaks the alignment the whole
        display depends on.

        ``expected`` is this viewer's own predicted cells and nobody else's.
        The legend and the explainer both key off whether a ``?`` is *actually
        on the block* rather than off whether a prediction exists, because those
        differ: a guess whose cells have all been booked by somebody else loses
        to those bookings and renders nothing. That answer now comes back from
        :func:`plan.render_week` alongside the grid. It used to be read out of
        the rendered string — ``CELL_EXPECTED in grid`` — which was fragile in a
        way that only looked harmless while the glyph was ``░``: the moment the
        guess became ``?``, any question mark anywhere in the block would have
        lit up an explainer for a guess nobody had made.

        ``running`` is the live load, and ``today`` is what turns the block from
        a shape into a calendar — without the marker, "is that free evening
        tonight or six days off?" needs counting from a header two lines up.
        """
        drawn = plan_mod.render_week(
            occupancy,
            viewer_id=user_id,
            expected=expected,
            running=running,
            today=self._today(),
        )
        embed = discord.Embed(
            title="📅 The week",
            description=(
                (f"{note}\n\n" if note else "")
                + f"```\n{drawn.grid}\n```\n"
                + drawn.legend
                + f"\n-# {plan_mod.render_windows()}"
            ),
            color=_COLOR_GRID,
        )
        now = self._now()
        mine = plan_mod.describe_cells(
            occupancy, user_id, today=self._today(), hour=getattr(now, "hour", None)
        )
        embed.add_field(
            name="Yours this week",
            value=mine or "nothing booked — tap a slot below",
            inline=False,
        )
        if drawn.running:
            embed.add_field(
                name=f"{plan_mod.CELL_RUNNING} The washer's going right now",
                value=(
                    "Worked out from the load actually running, not from "
                    "anybody's plans — it clears itself when the load ends. "
                    "It doesn't block the slot: whoever booked it still has it."
                ),
                inline=False,
            )
        if drawn.standing:
            # Only when a ║ is actually on the block, same rule as the guess
            # below: a note explaining a character nobody can see is noise.
            embed.add_field(
                name=(
                    f"{plan_mod.CELL_TAKEN_EVERY_WEEK} Somebody's down for that "
                    "every week"
                ),
                value=(
                    "A standing slot rather than a one-off, so it's the least "
                    "likely thing on the grid to move — worth knowing before "
                    "you ask. Still no name and no count: only that the cell is "
                    "spoken for, and how often."
                ),
                inline=False,
            )
        if drawn.guessed:
            embed.add_field(
                name=f"{plan_mod.CELL_EXPECTED} My guess at your usual days",
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
        if ask_cell is not None:
            # No name and no count, exactly like the grid itself: "spoken for"
            # is the whole of what anybody learns from this. The slot named here
            # is the one the viewer just tapped, so it tells them nothing they
            # did not already do.
            embed.add_field(
                name=f"🔁 {trade_mod.describe_cell(ask_cell)}",
                value=(
                    f"{trade_mod.ASK_PROMPT} You're down for it either way — "
                    "this would ask whoever else is, anonymously, whether "
                    "they'd swap."
                ),
                inline=False,
            )
        embed.set_footer(
            text="Nobody sees who booked what — only that a slot is taken."
        )
        return embed

    # ------------------------------------------------------- the trade broker
    # §9. Everything that *decides* lives in :mod:`trade`; this half delivers
    # and renders. The one rule to hold on to while reading it: **before an
    # accept, no string produced here may contain a name or an id.** That is
    # enforced structurally on the other side — the pre-accept text functions in
    # :mod:`trade` take cells and nothing else — so the job here is simply never
    # to add one, and the only two places an identity appears at all are the
    # two reveal messages after ✅ Trade.

    async def async_open_ask(self, interaction: discord.Interaction) -> None:
        """🔁 — open the "shall I ask?" panel for the cell just tapped."""
        if not self.trades_enabled:
            await self._async_render_grid(
                interaction,
                edit=True,
                note=trade_mod.refusal_text(trade_mod.REASON_TRADES_OFF),
            )
            return
        await self._async_render_ask(interaction)

    async def async_pick_offer(
        self, interaction: discord.Interaction, value
    ) -> None:
        """Choose which of your own slots to put up in return."""
        cell = plan_mod.normalise_cell(value)
        if cell is not None:
            self._ask_offer[str(interaction.user.id)] = cell
        await self._async_render_ask(interaction)

    async def async_back_to_grid(self, interaction: discord.Interaction) -> None:
        """Back out of the ask panel. 🔁 stays armed; nothing has been sent."""
        await self._async_render_grid(interaction, edit=True)

    async def async_send_trade(self, interaction: discord.Interaction) -> None:
        """The one tap that puts a message on another housemate's phone.

        The order here is deliberate and is the same discipline
        :mod:`reminders` uses:

        1. **Decide and claim in one call** (:func:`trade.claim_request`) — the
           verdict, the chosen holder, the DM budget and the new row all come
           back together, so there is no gap in which a check could pass and a
           send happen anyway.
        2. **Persist before sending.** A DM that bounces has still been
           attempted; refunding it would retry somebody with closed DMs at every
           tap forever.
        3. **Answer the interaction before the network call.** Discord's ack
           window is 3 seconds and a DM is a round trip — responding first is
           what stops a successfully-sent ask from showing the asker
           "interaction failed".
        4. **Withdraw what could not be delivered.** An open request nobody ever
           saw would block that slot and that person for the rest of the week
           (:func:`trade.withdraw` lapses it without recording a refusal, which
           would be a lie about the holder).
        """
        user_id = interaction.user.id
        key = str(user_id)
        want = self._ask_cell.get(key)
        week = self._current_week()
        if not self.trades_enabled or want is None or not week:
            await self._async_render_grid(interaction, edit=True)
            return
        occupancy = plan_mod.effective_week(self._people, self._overrides, week)
        reason, request, requests, budgets = trade_mod.claim_request(
            self._people,
            self._trades,
            self._budgets,
            user_id,
            plan_mod.holders(occupancy, want),
            want,
            self._ask_offer.get(key),
            week,
            self._now(),
            mine=self.booked_cells(user_id, week),
        )
        if reason == trade_mod.REASON_SILENT and request is not None:
            # The holder-side rules said no. The ask is still recorded (already
            # lapsed) and the asker is told exactly what a delivered ask is
            # told, because "did a DM go out?" is itself a fact about the
            # holder — see :func:`trade.claim_request`. Nothing is sent.
            self._trades = requests
            await self._async_save()
            self._clear_ask(user_id)
            await self._async_render_grid(
                interaction, edit=True, note=trade_mod.sent_text(want)
            )
            _LOGGER.debug("Swap request recorded without a send")
            return
        if reason != trade_mod.REASON_OK or request is None:
            # One sentence per reason, and every holder-side reason shares the
            # same one — the asker cannot see who holds the cell and must not be
            # able to work it out from how the bot says no.
            await self._async_render_ask(
                interaction, note=trade_mod.refusal_text(reason)
            )
            return
        self._trades = requests
        self._budgets = budgets
        await self._async_save()
        self._clear_ask(user_id)
        await self._async_render_grid(
            interaction, edit=True, note=trade_mod.sent_text(want)
        )
        if await self._async_deliver_request(request):
            # A decision taken, and deliberately with no ids in it: the log is
            # local, but "who asked whom" is the one fact this feature exists to
            # keep, and there is no reason for it to be recoverable from a debug
            # line either.
            _LOGGER.debug("Swap request sent")
            return
        self._trades = trade_mod.withdraw(
            self._trades, request["id"], self._now()
        )
        await self._async_save()
        try:
            await interaction.followup.send(
                trade_mod.refusal_text(trade_mod.REASON_UNDELIVERED),
                ephemeral=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not report an undelivered swap request",
                          exc_info=True)

    async def _async_deliver_request(self, request: dict) -> bool:
        """Send the anonymous ask. Returns whether it actually went out."""
        text = trade_mod.request_dm_text(request["want"], request["offer"])
        if text is None:
            return False
        try:
            async with asyncio.timeout(_TRADE_SEND_TIMEOUT):
                return await self.async_send_dm(
                    request["to"], text, TradeRequestView(self)
                )
        except TimeoutError:
            # Only the timeout. A CancelledError from outside is HA shutting
            # down and has to keep travelling.
            _LOGGER.debug("Swap request not delivered in time")
            return False

    def _dm_sent_ts(self, interaction: discord.Interaction, now) -> float | None:
        """When the tapped DM was sent, expressed on **this box's** clock.

        Discord stamps a message from its own clock (the snowflake) and a
        request row is stamped from ours, so comparing the two directly makes
        every trade in the house depend on the HA host's clock being within
        :data:`trade.MATCH_WINDOW_SECONDS` of Discord's. An RPi with no RTC that
        has not re-synced NTP would answer nothing, ever, and say "that one's
        lapsed" — 🚫 included. So the DM's *age* is measured on Discord's clock
        alone, from the tap's own timestamp, and :func:`trade.dm_sent_ts` puts
        it back on ours; any constant offset between the two cancels.
        """
        message = getattr(interaction, "message", None)
        try:
            sent = getattr(message, "created_at", None)
            tapped = getattr(interaction, "created_at", None)
            if sent is None or tapped is None:
                return None
            return trade_mod.dm_sent_ts(
                now, float(sent.timestamp()), float(tapped.timestamp())
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    async def _async_block_lapsed(
        self, interaction: discord.Interaction, holder_id, sent_ts
    ) -> bool:
        """Record 🚫 on a request that can no longer be answered. Did it stick?

        A block is not an answer to the ask — it is a standing decision about a
        person, and it has to outlive the 48-hour window (§9: *permanent, per
        requester-pair*). The lapsed request itself is left exactly as it is: an
        ask nobody answered must not become a decline, which would shut that
        slot to the whole house on the strength of a tap that said nothing about
        it. Nothing is sent to the requester either — they were never going to
        hear about a request that lapsed, and a block is not something they get
        told about (:func:`trade.block_ack_text`).
        """
        row = trade_mod.match_any_request(self._trades, holder_id, sent_ts)
        if row is None:
            return False
        self._people = people_mod.set_person(
            self._people,
            holder_id,
            no_trade_from=trade_mod.with_block(
                self._people, row["from"], holder_id
            ),
        )
        await self._async_save()
        _LOGGER.debug("Swap block recorded on a request that had lapsed")
        await self._async_close_dm(interaction, trade_mod.block_ack_text())
        return True

    async def async_answer_trade(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        """✅ Trade / ❌ Pass / 🚫 Don't ask me again, from the request DM.

        Which request this is resolved from the recipient plus **the DM's own
        timestamp**, never from the ``custom_id`` (a per-request id cannot be a
        persistent view, so the buttons would die at the next restart). A tap on
        a DM whose request has lapsed answers nothing at all — the state change
        and the reply both hang off :func:`trade.answer` returning OK.
        """
        holder_id = interaction.user.id
        now = self._now()
        sent_ts = self._dm_sent_ts(interaction, now)
        found = trade_mod.match_request(self._trades, holder_id, now, sent_ts)
        reason, answered, rows = trade_mod.answer(
            self._trades, found["id"] if found else None, action, now
        )
        if reason != trade_mod.REASON_OK or answered is None:
            if action == trade_mod.ACTION_BLOCK and await self._async_block_lapsed(
                interaction, holder_id, sent_ts
            ):
                return
            await self._async_close_dm(interaction, _TRADE_STALE)
            return
        self._trades = rows
        if action == trade_mod.ACTION_BLOCK:
            # Permanent, per pair, and stored on the *holder's* own record so it
            # outlives every request and every week.
            self._people = people_mod.set_person(
                self._people,
                holder_id,
                no_trade_from=trade_mod.with_block(
                    self._people, answered["from"], holder_id
                ),
            )
        if action == trade_mod.ACTION_ACCEPT:
            self._overrides = trade_mod.apply_swap(
                self._people, self._overrides, answered
            )
        await self._async_save()
        # The state only — no ids, for the reason in :meth:`async_send_trade`.
        _LOGGER.debug("Swap request answered: %s", answered["state"])
        note, reply = self._trade_replies(action, answered, holder_id)
        await self._async_close_dm(interaction, note)
        if reply:
            # Deliberately **not** budgeted. Every other unprompted DM in this
            # integration is the bot deciding to talk to somebody; this one is
            # the answer to a question that person asked, and it is the only way
            # they ever find out. Charging it could silently swallow the reply
            # to your own ask, which is both useless and unkind. It cannot be
            # used to pester either: exactly one arrives per ask, and the number
            # of asks is capped in several directions.
            await self.async_send_dm(answered["from"], reply)

    def _trade_replies(
        self, action: str, answered: dict, holder_id
    ) -> tuple[str, str | None]:
        """What each side is told: ``(to the holder, to the asker)``.

        The only place in this module where an identity reaches a string, and
        only down the accept branch — §9 step 3: an accept reveals both names to
        both parties, because from that moment they have to coordinate and they
        live together. ❌ Pass and 🚫 tell the asker exactly the same thing as
        each other, and it contains no name and no reason (§9 step 4), so a
        block is indistinguishable from an ordinary no.
        """
        want, offer = answered["want"], answered["offer"]
        if action == trade_mod.ACTION_ACCEPT:
            return (
                trade_mod.accepted_text_for_holder(
                    f"<@{answered['from']}>", want, offer
                )
                or _TRADE_STALE,
                trade_mod.accepted_text_for_requester(
                    f"<@{holder_id}>", want, offer
                ),
            )
        ack = (
            trade_mod.block_ack_text()
            if action == trade_mod.ACTION_BLOCK
            else trade_mod.pass_ack_text()
        )
        return (ack, trade_mod.passed_text(want))

    async def _async_render_ask(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """Draw the "shall I ask?" panel, in place where possible."""
        user_id = interaction.user.id
        key = str(user_id)
        want = self._ask_cell.get(key)
        week = self._current_week()
        if want is None or not week:
            await self._async_render_grid(interaction, edit=True)
            return
        offers = self._offer_cells(user_id, want, week)
        selected = self._ask_offer.get(key)
        if selected not in offers:
            selected = offers[0] if offers else None
            if selected is None:
                self._ask_offer.pop(key, None)
            else:
                self._ask_offer[key] = selected
        embed = self._ask_embed(want, selected, note=note)
        view = TradeAskView(self, offers=offers, selected=selected)
        await self._async_respond(interaction, embed, view, edit=True)

    def _ask_embed(
        self, want: str, offer: str | None, *, note: str | None
    ) -> discord.Embed:
        """The panel that shows exactly what would be sent, before it is sent.

        Quoting the DM back verbatim is the point: the whole feature rests on
        people believing the ask is anonymous, and the way to believe that is to
        read the words first. Nothing on this panel names anybody, because
        nothing on this side *knows* anybody — who holds the cell is resolved at
        send time and never rendered.
        """
        embed = discord.Embed(
            title="🔁 Ask to swap",
            description=(
                (f"{note}\n\n" if note else "")
                + (trade_mod.ask_panel_text(want, offer) or trade_mod.ASK_PROMPT)
            ),
            color=_COLOR_TRADE,
        )
        preview = trade_mod.request_dm_text(want, offer)
        if preview:
            embed.add_field(
                name="What they'll get, word for word",
                value=preview,
                inline=False,
            )
        embed.add_field(
            name="How this goes",
            value=(
                "✅ **they say yes** — the slots swap and you're both named to "
                "each other, because from there you have to sort it out "
                "between you.\n"
                "❌ **they pass** — I'll tell you they passed. No name, no "
                "reason, and that slot's shut for the rest of the week.\n"
                "🤐 **they ignore it** — it lapses on its own and nobody hears "
                "any more about it."
            ),
            inline=False,
        )
        embed.set_footer(
            text="One ask per slot per week, and never twice at the same person."
        )
        return embed

    async def _async_close_dm(
        self, interaction: discord.Interaction, note: str
    ) -> None:
        """Answer a swap DM by rewriting it and dropping its buttons.

        Taking the buttons away matters more here than anywhere else: a DM sits
        in an inbox indefinitely, and a second ✅ tapped a week later must not
        try to swap anything. Never raises — the state change already happened
        before we got here, so a failed edit is cosmetic.
        """
        try:
            await interaction.response.edit_message(content=note, view=None)
            return
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not edit a swap DM in place", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(note)
            else:
                await interaction.response.send_message(note)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not acknowledge a swap reply", exc_info=True)

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
            color=_COLOR_WELCOME,
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
                + "**Pings** is how I reach you about **your own** laundry — "
                "your load finishing, or the washer coming free after you "
                "tapped 🔜. 🔔 **What I send you** is everything else: the "
                "messages I start on my own, one switch each. The card itself "
                "is unaffected by either."
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
        embed.add_field(
            name="What I send you",
            value=self._notify_summary(person),
            inline=False,
        )
        if learning and person["monitor"]:
            embed.add_field(
                name="Guessing",
                value=(
                    "🔮 on — I'll mark the days I think you usually wash with "
                    f"{plan_mod.CELL_EXPECTED} on **your** week, and nowhere "
                    "else"
                    if person["predict"]
                    else "🚫 off — I won't guess your days"
                ),
                inline=False,
            )
        embed.set_footer(text="No stats about you are ever shown to the house.")
        return embed

    def _notify_summary(self, person: dict) -> str:
        """The 🔔 settings in one line, for the main panel.

        Names what is **off** rather than what is on, which is the shorter list
        in every case that matters and the only one worth a glance: four kinds
        all on is the default and needs no reading, whereas somebody who
        switched the heads-up off a month ago and is wondering why nothing
        arrives before their slot needs to see it without opening anything.
        """
        off = [
            f"{_NOTIFY_KINDS[kind][0]} {_NOTIFY_KINDS[kind][1]}"
            for kind in people_mod.KINDS
            if not people_mod.wants_kind(person, kind)
        ]
        window = people_mod.quiet_hours(person)
        kinds = "🔔 all four on" if not off else "🔕 off: " + ", ".join(off)
        quiet = (
            "no quiet hours"
            if window is None
            else f"quiet {_quiet_label(window)}"
        )
        return f"{kinds} · {quiet}"

    def _notify_embed(self, person: dict) -> discord.Embed:
        """The 🔔 panel — everything the bot starts, and the switch for each.

        The second paragraph is what stops this reading as a mute button for the
        whole integration, and it is the design's line rather than a hedge:
        these switches govern messages the **bot or a housemate** starts, never
        a reply to something you did. "Your load is done" answers 🧺 Claim and
        "the washer's yours" answers 🔜, and both are time-critical — a handoff
        held until 08:00 tells somebody the washer was free eight hours ago,
        which is worse than not sending it at all. Those stay under **Pings**,
        where "how do you want to be reached about your own laundry" lives.

        What this deliberately does **not** say is that anything switched on
        here will definitely arrive: the house's own reminder option gates all
        three of the bot's messages, and that rule lives in :mod:`reminders`.
        Restating it here would be a second copy to keep in step, and a settings
        screen that contradicts another screen is worse than one that is merely
        modest. So every line is worded as what the switch *allows*, which is
        true whatever the house has turned on — and the two facts this module
        does own, a delivery route that isn't a DM and swaps switched off for
        the channel, are stated outright, because both make a switch below inert
        and neither is discoverable from anywhere else.
        """
        # Prepended rather than added as a field, like the §10.5 notice: a
        # caveat that changes what everything under it means has to be read
        # first, and Discord renders fields *after* the description.
        if person["reminders"] != people_mod.REMIND_DM:
            route = (
                "⚠️ None of these can reach you at the moment — **Pings** is "
                "set to something other than 📬 **DM me**, and every message "
                "below is a DM. Your answers here are kept for when you change "
                "it back.\n\n"
            )
        elif person["dm_ok"] is False:
            route = (
                "⚠️ Your DMs are closed, so none of these are getting through. "
                "Open 🤖 for the two settings that fix it.\n\n"
            )
        else:
            route = ""
        embed = discord.Embed(
            title="🔔 What I send you",
            description=(
                route
                + "These are the messages **I** start — on a schedule, or "
                "because a housemate asked me to. Switching one off means I "
                "won't send it.\n\n"
                "Nothing here touches the messages that answer something *you* "
                "did: 🧺 **Claim** still tells you your load is done, and 🔜 "
                "still tells you when the washer is actually yours. Those are "
                "under **Pings** on the main panel."
            ),
            color=_COLOR_NOTIFY,
        )
        lines = []
        for kind in people_mod.KINDS:
            emoji, label, what = _NOTIFY_KINDS[kind]
            state = "on" if people_mod.wants_kind(person, kind) else "off"
            line = f"{emoji} **{label}: {state}** — {what}"
            # The house switch for swaps is the one that can make a line here
            # untrue on its own, and unlike the reminder option it is read in
            # this module already — so saying so costs no second copy of
            # anybody's rule. Somebody who leaves 🔁 on in a house that has
            # trades off should not conclude the switch did nothing.
            if kind == people_mod.KIND_TRADES and not self.trades_enabled:
                line += "\n(swaps are off for this channel, so nobody can ask)"
            lines.append(line)
        embed.add_field(name="Messages", value="\n".join(lines), inline=False)
        window = people_mod.quiet_hours(person)
        # The "no window" line names no clock time, though the obvious sentence
        # to write is "the earliest I'd reach you is 05:00". That one is only
        # true at the default lead: ``nudge_lead`` goes up to three hours, which
        # puts the AM heads-up at 03:00, and 💡 rides the washer coming free at
        # whatever hour that happens. Naming an hour the house has moved would
        # be this screen caught lying about the very thing the reader opened it
        # to decide — worse here than anywhere else, because the answer they
        # take away is "then I don't need a quiet window". What this module does
        # own is the slot table, and 06:00 is in it.
        opens = plan_mod.SLOT_WINDOWS[plan_mod.SLOT_AM][0]
        embed.add_field(
            name="Quiet hours",
            value=(
                f"🌙 **{_quiet_label(window)}** — anything above that would "
                "land inside it is **dropped**, not saved up for the morning. "
                "A heads-up delivered at 08:00 is about a slot that has gone."
                if window
                else "🌙 **None** — these arrive whenever they're due, small "
                "hours included: a heads-up runs ahead of the slot it's about "
                f"and the first slot of the day opens at {opens:02d}:00."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Nothing here can make me send you more than I already do."
        )
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
        embed = discord.Embed(title="🔮 What I think", color=_COLOR_GUESS)
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
                f"Guessing is off — no {plan_mod.CELL_EXPECTED} on your week, "
                "and I won't work out your days. **Start guessing** puts it "
                "back; the loads I already noted are still there."
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
