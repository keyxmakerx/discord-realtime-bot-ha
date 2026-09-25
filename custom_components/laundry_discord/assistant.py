"""The assistant: private, ephemeral settings panel, per-person prefs, and DM plumbing.

An ephemeral message only exists as a reply to an interaction and expires
with its 15-minute token. Every `custom_id` must be registered on the
persistent view. A DM to someone with DMs off raises `discord.Forbidden`
and falls back to the channel.
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
from . import nudge as nudge_mod
from . import people as people_mod
from . import plan as plan_mod
from . import queue as queue_mod
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

# One colour per panel, so the embed's left stripe identifies which panel it is.
_COLOR_PANEL = 0x5865F2  # blurple — hub
_COLOR_WELCOME = 0xFEE75C  # yellow — onboarding, shown once
_COLOR_GRID = 0x57F287  # green — week grid
_COLOR_TRADE = 0xEB459E  # fuchsia — swap request
_COLOR_GUESS = 0xC77DFF  # violet — model's guess
# Not red: red already means "danger" (someone else has this slot) on a button.
_COLOR_NOTIFY = 0xFAA61A  # amber — notification settings

# Shown once after a refused DM; both settings are listed since either could be the cause.
_DM_NOTICE = (
    "⚠️ **I couldn't DM you**\n"
    "Two settings control this:\n"
    "• Right-click the server icon → **Privacy Settings** → allow DMs from "
    "members\n"
    "• **User Settings → Privacy** → allow direct messages from server members\n"
    "Until then I'll ping you in the channel instead.\n\n"
)

# "Off" still names you in the channel; it removes the push, not the mention.
_MODE_LABELS = {
    people_mod.REMIND_DM: "📬 In a DM — just you, nothing in the channel",
    people_mod.REMIND_CHANNEL: "💬 In the channel, with an @mention",
    people_mod.REMIND_OFF: "🚫 No pushes — you're still named in the channel",
}

# (emoji, button label, sentence describing that kind), keyed by people.KINDS.
# Avoids specifics like "Sunday" or "an hour before": the day/lead are options.
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
    people_mod.KIND_EMPTY: (
        "🧺",
        "Empty-it",
        "a reminder when your finished load is still in the washer",
    ),
    people_mod.KIND_TAKEN: (
        "🏃",
        "Slot taken",
        "when someone else is using the washer in a slot you booked",
    ),
}

# (start, end) local hours, wraps midnight. None ("no quiet hours") is first
# so turning the setting off is never harder to find than turning it on.
_QUIET_PRESETS = (None, (22, 8), (23, 9), (0, 7), (21, 9))


def _quiet_label(window) -> str:
    """A quiet window as the panel says it out loud."""
    if window is None:
        return "No quiet hours"
    return f"{window[0]:02d}:00–{window[1]:02d}:00"


def _quiet_value(window) -> str:
    """A preset as a select value: ``"none"``, or ``"22-8"``.

    Encodes the hours, not a list index — the select is persistent, so a stale
    panel's value must still mean the same thing after the preset list changes.
    """
    return "none" if window is None else f"{window[0]}-{window[1]}"


def _parse_quiet(value) -> tuple[int | None, int | None]:
    """A select value back to ``(start, end)`` hours; anything odd clears it.

    Fails safe to "no quiet hours" rather than guessing — a wrong guess could
    silence someone past a morning that never arrives.
    """
    parts = str(value).split("-")
    if len(parts) != 2:
        return (None, None)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (None, None)


def _message_key(message_id) -> str | None:
    """A Discord message id in its stored form, or None for anything unusable.

    Ids arrive as ``int`` but round-trip through JSON storage as ``str``; both
    must normalise to the same key. ``bool`` is excluded because ``True`` is an
    ``int``.
    """
    if message_id is None or isinstance(message_id, bool):
        return None
    key = str(message_id).strip()
    return key or None


def _normalise_nudge_cells(raw) -> dict[str, dict]:
    """The stored "which DM was about which cell" map, rebuilt off disk.

    Both cell and message id are required; a row missing either is dropped.
    """
    rows: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return rows
    for user_id, record in raw.items():
        if not isinstance(record, dict):
            continue
        cell = plan_mod.normalise_cell(record.get("cell"))
        ident = _message_key(record.get("message"))
        key = _message_key(user_id)
        if cell is None or ident is None or key is None:
            continue
        rows[key] = {"cell": cell, "message": ident}
    return rows


# Bounds how long a swap DM can wait on wait_until_ready() before withdrawing.
_TRADE_SEND_TIMEOUT = 30

# Says nothing about who or why: a DM tapped weeks later must not leak info.
_TRADE_STALE = (
    "That one's lapsed — nothing's been changed, and nobody's been told "
    "anything either way."
)


class _ReminderButton(discord.ui.Button):
    """One of the three "how should I reach you" choices."""

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
    """Consent toggle for logging this person's loads."""

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
    """Open the "here's what I think" guess panel."""

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

    Shown even when Pings is set to the channel, where these switches are
    inert — hiding it then would look like the bot can't be told to stop.
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

    Stores nothing: the loads behind the guess are already counted, so writing
    a row here would let the guess feed itself its own evidence.
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

    The label follows the state, so the same button turns it off and back on.
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

    Its own ``custom_id`` rather than the grid's: ``add_view`` keys the
    registry by id, so two views sharing one id would collide.
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
    """The 🔮 panel's controls: right/wrong/off, plus back.

    ``That's right``/``Wrong`` are added only when there's a guess to answer,
    but the registration template carries every id — an unregistered
    custom_id silently stops dispatching after a restart.
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

    The label is the setting: reading it and changing it are the same tap.
    Labels stay short (e.g. "Heads-up") because Discord truncates from the
    right, which is where the ``: on``/``: off`` suffix lives.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        kind: str,
        label: str,
        emoji: str,
        *,
        enabled: bool | None,
        row: int = 0,
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
            row=row,
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

    A select, not a time picker — Discord has none. The placeholder always
    names the stored window (not just the ``default`` option), so a
    hand-edited or dropped window still shows correctly.
    """

    def __init__(
        self, assistant: "LaundryAssistant", window: tuple[int, int] | None
    ) -> None:
        super().__init__(
            placeholder=f"Quiet hours: {_quiet_label(window)}",
            custom_id=NOTIFY_QUIET_CUSTOM_ID,
            min_values=1,
            max_values=1,
            row=2,
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
    """Back to the settings panel (its own ``custom_id``; see :class:`_GuessBackButton`)."""

    def __init__(self, assistant: "LaundryAssistant") -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=NOTIFY_BACK_CUSTOM_ID,
            row=3,
        )
        self.assistant = assistant

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.assistant.async_back_to_panel(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to return to the panel")
            await self.assistant.async_report_error(interaction)


class NotifyView(discord.ui.View):
    """The 🔔 panel: a toggle per kind (four to a row), quiet hours, and back.

    ``person=None`` is the registration template. A kind missing from
    :data:`_NOTIFY_KINDS` or :data:`const.NOTIFY_KIND_CUSTOM_IDS` raises
    ``KeyError`` at startup, not when someone opens the panel.
    """

    def __init__(
        self, assistant: "LaundryAssistant", *, person: dict | None = None
    ) -> None:
        super().__init__(timeout=None)
        for index, kind in enumerate(people_mod.KINDS):
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
                    row=index // 4,
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

    A select, not per-cell buttons: 7 days x 4 slots = 28 cells, and a message
    holds at most 25 components.
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


# Four button styles for six cell states: danger covers both "taken" states
# (not a veto), primary the model's guess, success yours, secondary free/running.
_SLOT_BUTTON_STYLES = {
    plan_mod.STATE_MINE: discord.ButtonStyle.success,
    plan_mod.STATE_TAKEN: discord.ButtonStyle.danger,
    plan_mod.STATE_TAKEN_EVERY_WEEK: discord.ButtonStyle.danger,
    plan_mod.STATE_EXPECTED: discord.ButtonStyle.primary,
}


class _GridSlotButton(discord.ui.Button):
    """Book or free one slot on the selected day.

    Not disabled when someone else holds the cell — the grid shows contention,
    it doesn't arbitrate it. Red means "already claimed", not "you may not".
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
    """🔁 — offer to ask whoever is down for the slot you just tapped.

    An extra option, not a replacement: tapping a taken slot still books
    you in alongside its holder.
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
    """♻ — promote the cell you just tapped to every week, or demote it.

    Targets the last-tapped cell, like 🔁. Labelled by what tapping it will
    do, not what's true now — "Every week" on an already-standing cell would
    read as a statement to confirm, and silently cancel it instead.
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

    Passing no ``occupancy``/``day`` builds the registration template, which
    must carry every ``custom_id`` — so 🔁 and ♻ are added to it too.
    ``recur`` decides ♻'s label, not whether it appears.
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

    Lists only the viewer's own cells for this week, minus the one being asked
    for. Options are described by day and slot only — never identify anyone.
    """

    def __init__(
        self,
        assistant: "LaundryAssistant",
        offers: list[str] | None,
        selected: str | None,
    ) -> None:
        # Discord requires at least one option; the template has none, hence the placeholder.
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

    The registration template carries all three ``custom_id``s, so the
    select falls back to a placeholder option rather than being left out.
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
    """One of the three answers on an incoming swap DM."""

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

    ``custom_id``s carry nothing per-request — resolved instead from the
    recipient and the DM's timestamp (:func:`trade.match_request`), so only
    one ask can be pending per person.
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

    One view class for everybody — no ``custom_id`` is per-person (that
    would leak who a message belongs to). ``person=None`` is the
    registration template, which must carry every ``custom_id``.
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
        # Same buttons/ids; wording differs (a first-timer is asked, a returning user told).
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
        # Hidden on the first-time panel (asks one question), but the template still needs the id.
        if onboarded or template:
            self.add_item(
                _MonitorButton(
                    assistant, enabled=person["monitor"] if person else None
                )
            )
            self.add_item(_WeekButton(assistant))
            # 🔔 especially: muting notifications means nothing before they've chosen how to be reached.
            self.add_item(_PanelNotifyButton(assistant))
        # 🔮 only shows with day-learning on; the template still registers its id either way.
        if template or (learning and onboarded):
            self.add_item(_GuessButton(assistant))


class LaundryAssistant:
    """Per-person preferences, the private panel and the DM plumbing.

    Owns its own ``Store``; never reaches back into the coordinator.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        bot: "DiscordBot",
        entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.bot = bot
        # Reads the entry directly, not the coordinator, to keep the dependency one-way.
        self._entry = entry
        self._store: Store = Store(
            hass, PLANNER_STORAGE_VERSION, PLANNER_STORAGE_KEY
        )
        self._people: dict[str, dict] = {}
        # Per-ISO-week booking overrides: {"2026-W32": {"3-eve": [ids]}}.
        self._overrides: dict[str, dict[str, list[str]]] = {}
        self._grid_day: dict[str, int] = {}  # which day each grid shows; memory only
        # Which of someone else's cells was tapped to ask about, and the offer. Memory only.
        self._ask_cell: dict[str, str] = {}
        self._ask_offer: dict[str, str] = {}
        # Last cell tapped, whoever holds it (unlike ``_ask_cell``, someone else's only).
        self._last_cell: dict[str, str] = {}
        # Which cell (and message id) each person's last reminder DM was about. Persisted.
        self._nudge_cell: dict[str, dict] = {}
        # Last slot-taken DM per person, as "week:cell": one per slot.
        self._taken_sent: dict[str, str] = {}
        # The live load's window, pushed by the coordinator; memory only.
        self._running_from: float | None = None
        self._running_until: float | None = None
        # The habit model's two stores: rows with an id and a timestamp, never a name.
        self._history: list[dict] = []
        self._corrections: list[dict] = []
        self._budgets: dict[str, dict] = {}  # per-person nudge budget, kept apart from prefs
        self._trades: list[dict] = []  # trade broker requests, pruned to the current week

    # ------------------------------------------------------------------ config
    @property
    def trades_enabled(self) -> bool:
        """Whether one housemate may ask another for a slot at all. Off by default."""
        if self._entry is None:
            return DEFAULT_TRADES
        merged = {**self._entry.data, **self._entry.options}
        return bool(merged.get(CONF_TRADES, DEFAULT_TRADES))

    @property
    def learn_habits(self) -> bool:
        """Whether the house has day-learning switched on at all.

        Outer gate on every history write and guess; inner gate is Monitoring.
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
        # Normalise once, here, so every later read works on a known shape.
        self._people = people_mod.normalise_people(source)
        # .get with a default: older stores have no overrides key; an upgrade must not break.
        raw = data.get("overrides") if isinstance(data, dict) else None
        self._overrides = plan_mod.prune_overrides(
            plan_mod.normalise_overrides(raw), self._current_week()
        )
        # Aged in memory only; retention is enforced on the write path instead.
        now = self._now()
        raw_history = data.get("history") if isinstance(data, dict) else None
        self._history = habit_mod.prune_history(raw_history, now)
        raw_corrections = data.get("corrections") if isinstance(data, dict) else None
        self._corrections = habit_mod.prune_corrections(raw_corrections, now)
        # Never cleared: a restart resetting these would turn "1 DM a day" into "1/restart".
        raw_budgets = data.get("budgets") if isinstance(data, dict) else None
        self._budgets = habit_mod.normalise_budgets(raw_budgets)
        # Pruned in memory like history/corrections; a past week's request is dead weight.
        raw_trades = data.get("trades") if isinstance(data, dict) else None
        self._trades = trade_mod.prune_requests(raw_trades, self._current_week())
        # Which DM was about which cell; a row missing either half is dropped.
        raw_nudges = data.get("nudges") if isinstance(data, dict) else None
        self._nudge_cell = _normalise_nudge_cells(raw_nudges)
        raw_taken = data.get("taken") if isinstance(data, dict) else None
        self._taken_sent = (
            {str(k): v for k, v in raw_taken.items() if isinstance(v, str)}
            if isinstance(raw_taken, dict)
            else {}
        )

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
                    "nudges": self._nudge_cell,
                    "taken": self._taken_sent,
                }
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to save assistant prefs")

    # -------------------------------------------------------------- the clock
    def _now(self):
        """Local time, per HA's configured timezone.

        Must agree with the household's wall clock, not UTC — a Sunday
        23:00 local booking is not Monday.
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

        Pushed rather than pulled, so the planner keeps working if detection
        is having a bad day.
        """
        self._running_from = started_ts if isinstance(started_ts, (int, float)) else None
        self._running_until = eta_ts if isinstance(eta_ts, (int, float)) else None

    def running_cells(self) -> list[str]:
        """The cells the live load occupies right now (see `_running_cells`)."""
        return self._running_cells()

    def _running_cells(self) -> list[str]:
        """The cells the washer is mid-load in, worked out fresh right now.

        Never cached or stored: ``*`` claims something about this moment, and
        caching it would let that claim outlive the load.
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
        """One real load happened, and this person claimed it.

        Gated by ``learn_habits`` and Monitoring: off means no row is
        written, not one filtered out later. Never raises.
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
            _LOGGER.debug("Logged a load for %s", user_id)
        except Exception:  # noqa: BLE001 - never raise into a card callback
            _LOGGER.exception("Failed to log a load for the habit model")

    async def async_forget_load(self, user_id, since_ts, until_ts) -> None:
        """That load was stopped on the machine, so it was never a wash.

        Removes the row :meth:`async_note_claim` wrote, bounded to this
        load's own session by ``since_ts``/``until_ts``. Never raises.
        """
        try:
            updated = habit_mod.forget_load(self._history, user_id, since_ts, until_ts)
            if updated == self._history:
                return
            self._history = updated
            await self._async_save()
            _LOGGER.debug("Dropped a stopped load from history for %s", user_id)
        except Exception:  # noqa: BLE001 - never raise into the session machine
            _LOGGER.exception("Failed to drop a stopped load from the habit model")

    def _predicts_for(self, user_id) -> bool:
        """Whether this person's guesses may be computed or shown at all.

        Monitoring gates reads too, not just writes: turning it off must stop
        new ``?`` guesses immediately, without deleting stored history.
        """
        if not self.learn_habits:
            return False
        person = people_mod.get_person(self._people, user_id)
        return bool(person["predict"] and person["monitor"])

    def _prediction(self, user_id) -> dict | None:
        """This viewer's own top prediction, or None — usually None."""
        if not self._predicts_for(user_id):
            return None
        return habit_mod.predict(
            self._history, user_id, self._now(), self._corrections
        )

    def _predicted_cells(self, user_id) -> list[str]:
        """The cells to draw as ``?`` — only ever the viewer's own.

        Only the top guess: the panel's Wrong button can only retire one,
        so showing more would put a ``?`` on the grid nothing can address.
        """
        prediction = self._prediction(user_id)
        return [prediction["cell"]] if prediction else []

    # ------------------------------------------------ the reminder loop's window
    # reminders owns *when*; every writer below saves only when a value changes.

    def now(self):
        """The clock the whole planner shares. See :meth:`_now`."""
        return self._now()

    @property
    def people_map(self) -> dict[str, dict]:
        """The prefs mapping, for :func:`nudge.eligible`. A copy, not the store."""
        return dict(self._people)

    @property
    def budgets(self) -> dict[str, dict]:
        """The nudge accounting, for the claim. A copy, not the store."""
        return dict(self._budgets)

    def prediction_for(self, user_id) -> dict | None:
        """This person's own top guess, or None — gated exactly like the 🔮 panel."""
        return self._prediction(user_id)

    def load_times(self, user_id) -> list[float]:
        """When this person's own retained loads happened, as timestamps."""
        return [
            row["ts"]
            for row in habit_mod.history_for(self._history, user_id, self._now())
        ]

    def typical_gap(self, user_id) -> float | None:
        """How many days this person usually leaves between loads, or None."""
        if not self._predicts_for(user_id):
            return None
        return habit_mod.typical_gap(self._history, user_id, self._now())

    def is_due(self, user_id) -> bool:
        """Whether this person is past their own usual gap between washes.

        Gates the opportunity nudge. False when the cadence isn't known yet.
        """
        if not self._predicts_for(user_id):
            return False
        return habit_mod.is_due(self._history, user_id, self._now())

    def occupancy(self) -> dict:
        """This week's reconciled occupancy — what the grid draws from."""
        return plan_mod.effective_week(
            self._people, self._overrides, self._current_week()
        )

    def booked_cells(self, user_id, week=None) -> list[str]:
        """The cells this person has actually booked in a week.

        ``week`` defaults to the current one; overridable because Push to
        tomorrow on a Sunday lands in the next ISO week.
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

        Called before the DM goes out, never after: a Forbidden send has
        still spent the nudge, and refunding it would retry forever.
        """
        updated = habit_mod.normalise_budgets(budgets)
        if updated == self._budgets:
            return  # a denied claim changes nothing, so it costs no write
        self._budgets = updated
        await self._async_save()

    async def async_set_predict(self, user_id, enabled: bool) -> None:
        """🔕 Stop asking — flips the same ``predict`` preference the 🔮 panel shows."""
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
        """👍 On it — mark the slot taken on the anonymous board.

        Idempotent, unlike :meth:`async_toggle_cell`. Shows only that a cell
        is taken, never by whom. ``week`` overridable, see :meth:`booked_cells`.
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

        Idempotent. Returns False if there was nothing to release, and
        removes only this person. Doesn't touch the standing (♻) slot.
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

    async def async_note_nudge_cell(self, user_id, cell, message_id) -> None:
        """Record which cell this DM was about, keyed by the message id.

        Keyed by message, not timestamp: the heads-up fires before its slot
        opens, so an old unanswered one must stay tied to its own message.
        Written after the send, so a tap in the brief window before is
        treated as stale — the safe direction to be wrong.
        """
        key = plan_mod.normalise_cell(cell)
        ident = _message_key(message_id)
        if key is None or ident is None:
            return
        row = {"cell": key, "message": ident}
        if self._nudge_cell.get(str(user_id)) == row:
            return
        self._nudge_cell[str(user_id)] = row
        await self._async_save()

    async def async_claim_taken_notice(self, user_id, cell) -> bool:
        """Record a slot-taken DM for this person's cell this week.

        False when one already went out for the same slot, so it's sent once.
        """
        key = f"{self._current_week()}:{plan_mod.normalise_cell(cell)}"
        if self._taken_sent.get(str(user_id)) == key:
            return False
        self._taken_sent[str(user_id)] = key
        await self._async_save()
        return True

    def nudge_cell(self, user_id, message_id) -> str | None:
        """The cell this particular DM was about, or None if unrecognised.

        None (a stale or overwritten record) means the caller must not act
        on the grid.
        """
        row = self._nudge_cell.get(str(user_id))
        ident = _message_key(message_id)
        if not isinstance(row, dict) or ident is None:
            return None
        return row.get("cell") if row.get("message") == ident else None

    async def async_record_push(self, user_id, cell) -> None:
        """⏭ Push to tomorrow — a correction that is not a wrong guess.

        The day was right; they're just not doing it tonight. Goes through
        :func:`habit.mark_nudge_pushed`, not ``mark_prediction_wrong``.
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
    ) -> "discord.Message | None":
        """DM one person. Returns the sent message, or None if it didn't go out.

        ``discord.Forbidden`` means DMs from members are off for them — a
        user setting, not a bug, logged at debug. Returns the message, not
        ``True``, so a reminder DM stays identifiable by its id later
        (:meth:`async_note_nudge_cell`).
        """
        if user_id is None:
            return None
        try:
            message = await self.bot.async_dm_user(user_id, content, view=view)
        except discord.Forbidden:
            _LOGGER.debug(
                "DM to %s refused (DMs from server members are off)", user_id
            )
            self._people = people_mod.mark_dm_failed(self._people, user_id)
            await self._async_save()
            return None
        except Exception:  # noqa: BLE001 - never raise into HA
            _LOGGER.exception("Failed to DM %s", user_id)
            return None
        # Only write on a change: a store write per load for no new info is pure churn.
        if people_mod.get_person(self._people, user_id)["dm_ok"] is not True:
            self._people = people_mod.mark_dm_ok(self._people, user_id)
            await self._async_save()
        return message

    async def async_route_ping(
        self, user_id, *, dm_text: str, channel_text: str
    ) -> str | None:
        """Deliver one personal message the way this person asked for it.

        Defaults to the channel. Returns the route actually used (a
        ``people.REMIND_*`` mode), or None if nothing was delivered. dm falls
        back to the channel on failure; off posts without the mention rather
        than dropping the message. A ``None`` user id still goes to the
        channel, since ``select_handoff`` already popped that entry.
        """
        mode = (
            people_mod.REMIND_CHANNEL
            if user_id is None
            else people_mod.delivery(self._people, user_id)
        )
        if mode == people_mod.REMIND_DM:
            if await self.async_send_dm(user_id, dm_text) is not None:
                return people_mod.REMIND_DM
            mode = people_mod.REMIND_CHANNEL
        try:
            if mode == people_mod.REMIND_OFF:
                await self.bot.async_announce_done(channel_text)
            else:
                await self.bot.async_send_ping(channel_text)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to deliver a laundry ping to %s", user_id)
            return None
        return mode

    async def async_remind_empty(
        self, user_id, *, name: str, waiting: bool, quiet: bool
    ) -> str | None:
        """Remind a claimant that their finished load is still in the washer.

        Blockable per person (🔔 Empty-it) and dropped inside their quiet
        hours. Follows their Pings route; 🌙 Quiet on the card names them
        without a push. Returns the route used, or None if not sent.
        """
        person = people_mod.get_person(self._people, user_id)
        if not people_mod.wants_kind(person, people_mod.KIND_EMPTY):
            return None
        if nudge_mod.in_quiet_hours(person, self._now()):
            return None
        if quiet:
            await self.bot.async_announce_done(
                queue_mod.empty_reminder_text(waiting=waiting, name=name)
            )
            return people_mod.REMIND_OFF
        body = queue_mod.empty_reminder_text(waiting=waiting)
        return await self.async_route_ping(
            user_id, dm_text=body, channel_text=f"<@{user_id}> {body}"
        )

    # ------------------------------------------------------------------- panel
    async def async_open_panel(self, interaction: discord.Interaction) -> None:
        """Answer a 🤖 tap with this person's own private panel.

        Works on any card, live load or not — it's the onboarding surface.
        """
        user_id = interaction.user.id
        name = interaction.user.display_name
        notice = people_mod.get_person(self._people, user_id)["dm_notice_pending"]
        # Refresh the name only for an existing record — looking shouldn't enrol a guest.
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

        Reads the current value at tap time rather than trusting the
        button's render, so "toggle" still works after a restart.
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

        Checks before writing, unlike the buttons: Discord pre-selects the
        current value, so re-picking it is an easy accidental tap.
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
        """"That's right" — acknowledged, and nothing is stored (see :class:`_GuessRightButton`)."""
        await self._async_render_guess(
            interaction,
            note="👍 Good — nothing to change, and nothing stored: the loads "
            "behind this guess were already counted.",
        )

    async def async_reject_guess(self, interaction: discord.Interaction) -> None:
        """"Wrong" — retire the guess for this cell (``mark_prediction_wrong``).

        Re-reads the cell at tap time rather than trusting the render, so it
        survives a restart between opening the panel and tapping.
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

        Always opens on today (an assignment, not ``setdefault``): remembering
        the last day viewed would make 📅 open somewhere unpredictable.
        """
        user_id = interaction.user.id
        self._grid_day[str(user_id)] = self._today()
        self._forget_tapped_cell(user_id)
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
        # 🔁/♻ target the last-tapped cell, so changing day must retire both.
        self._forget_tapped_cell(interaction.user.id)
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
        # Booking a slot someone else holds doesn't block them — it also arms 🔁.
        self._note_ask(user_id, cell, week)
        # ♻ targets this, whoever holds it — see ``_last_cell``.
        self._last_cell[str(user_id)] = cell
        # Booking enrols them (unlike opening the panel): an unambiguous "I use this bot".
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

        Writes ``person["slots"]`` only; this week's override is untouched
        either way, so demoting leaves this week's booking standing on its own.
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
        # Write the holder list back so ♻ never silently cancels a cell the override doesn't mention.
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

    def _forget_tapped_cell(self, user_id) -> None:
        """Retire both buttons that point at a cell rather than name one.

        Neither carries its cell in the label, so the arming tap is the only
        link to a slot, and it goes stale once the grid moves on.
        """
        self._clear_ask(user_id)
        self._last_cell.pop(str(user_id), None)

    def _note_ask(self, user_id, cell, week) -> None:
        """Arm 🔁 if this tap landed on a cell somebody else holds. View state only."""
        self._clear_ask(user_id)
        if not self.trades_enabled or not week:
            return
        occupancy = plan_mod.effective_week(self._people, self._overrides, week)
        if plan_mod.is_taken_by_other(occupancy, cell, user_id):
            self._ask_cell[str(user_id)] = cell

    def _offer_cells(self, user_id, want, week) -> list[str]:
        """The viewer's own cells they could put up in return, ordered."""
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
        # ♻ is offered only for a cell of your own, never someone else's or a free one.
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

        Fenced so Discord renders it monospace; the legend stays outside,
        since an emoji inside would break the alignment. Whether it explains
        a ``?`` comes from :func:`plan.render_week`, not string-scanning.
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
            # Only when a ║ is actually on the block — explaining an absent glyph is noise.
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
            # No name/count, like the grid — the cell named is one the viewer just tapped.
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
    # Decisions live in :mod:`trade`; before an accept, nothing built here may
    # contain a name or an id (the two reveal messages after ✅ Trade excepted).

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

        Order matters:

        1. Decide and claim atomically (:func:`trade.claim_request`).
        2. Persist before sending — a bounced DM was still attempted.
        3. Answer the interaction before the network call (3s ack window).
        4. Withdraw silently what couldn't be delivered — a failure would
           itself identify the holder.
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
            # Holder-side no, recorded as lapsed; asker is told what a delivered ask is told.
            self._trades = requests
            await self._async_save()
            self._clear_ask(user_id)
            await self._async_render_grid(
                interaction, edit=True, note=trade_mod.sent_text(want)
            )
            _LOGGER.debug("Swap request recorded without a send")
            return
        if reason != trade_mod.REASON_OK or request is None:
            # Every holder-side reason shares one sentence, so no reason leaks who holds it.
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
            # No ids in the log either — "who asked whom" is the one fact to keep.
            _LOGGER.debug("Swap request sent")
            return
        # Lapses silently: saying more would leak that this holder's DMs are closed.
        self._trades = trade_mod.withdraw(
            self._trades, request["id"], self._now()
        )
        await self._async_save()
        _LOGGER.debug("Swap request withdrawn: it could not be delivered")

    async def _async_deliver_request(self, request: dict) -> bool:
        """Send the anonymous ask. Returns whether it actually went out."""
        text = trade_mod.request_dm_text(request["want"], request["offer"])
        if text is None:
            return False
        try:
            async with asyncio.timeout(_TRADE_SEND_TIMEOUT):
                return await self.async_send_dm(
                    request["to"], text, TradeRequestView(self)
                ) is not None
        except TimeoutError:
            # Only the timeout; a CancelledError means HA is shutting down and must propagate.
            _LOGGER.debug("Swap request not delivered in time")
            return False

    def _dm_sent_ts(self, interaction: discord.Interaction, now) -> float | None:
        """When the tapped DM was sent, expressed on this box's clock.

        Comparing Discord's timestamp directly to ours would make every trade
        depend on the HA host's clock matching Discord's — an unsynced clock
        would answer "lapsed" to everything. So age is measured on Discord's
        clock alone and rebased onto ours (:func:`trade.dm_sent_ts`).
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

        A block outlives the 48-hour window: a standing decision, not an
        answer, so it leaves the lapsed request as-is. Requester never told.
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
        """✅ Trade / ❌ Pass / 🚫 Don't ask me again, from the request DM (see :class:`TradeRequestView`)."""
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
            # Permanent, per pair, stored on the holder's record so it outlives any one week.
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
            # Not budgeted: answers a question asked, not one the bot initiated.
            await self.async_send_dm(answered["from"], reply)

    def _trade_replies(
        self, action: str, answered: dict, holder_id
    ) -> tuple[str, str | None]:
        """What each side is told: ``(to the holder, to the asker)``.

        The only place here where an identity reaches a string (only on
        accept). Pass and Block tell the asker the same sentence.
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

        Quotes the DM verbatim so people can trust it's anonymous. Names
        nobody — who holds the cell is resolved at send time.
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

        A DM sits in an inbox indefinitely, so a second tap must not act
        again. Never raises — the state change already happened.
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
        # Re-checked here, not just on open: a tap can arrive on a panel opened before a DM was refused.
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

        Never re-armed once cleared, so clearing it before delivery is
        confirmed would lose it permanently — and a panel send can fail.
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

        Returns whether it reached the user (see :meth:`_async_clear_notice`).
        A stale token can't edit its old message — normal, not an error, so
        it falls through to a fresh ephemeral rather than "interaction failed".
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
            # Nothing left to try: the interaction itself is gone.
            _LOGGER.debug("Could not deliver the assistant panel", exc_info=True)
            return False
        return True

    async def async_followup_dm_notice(
        self, interaction: discord.Interaction
    ) -> None:
        """Tell somebody their DMs bounced, on any button they tap.

        Not just on 🤖: someone who already fixed their DMs would never
        reopen the panel to see it.
        """
        user_id = interaction.user.id
        if not people_mod.get_person(self._people, user_id)["dm_notice_pending"]:
            return
        try:
            await interaction.followup.send(_DM_NOTICE.strip(), ephemeral=True)
        except Exception:  # noqa: BLE001 - never raise into a card callback
            # The tap already succeeded; the notice just waits for the next one.
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
        """The 👋 first-time panel: explains the card's buttons before asking anything, in private."""
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

        Every line must describe something actually true right now — e.g.
        Guessing only appears when there's guessing to have an opinion about.
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
        """The 🔔 settings in one line: names what's off, not what's on."""
        off = [
            f"{_NOTIFY_KINDS[kind][0]} {_NOTIFY_KINDS[kind][1]}"
            for kind in people_mod.KINDS
            if not people_mod.wants_kind(person, kind)
        ]
        window = people_mod.quiet_hours(person)
        kinds = "🔔 all on" if not off else "🔕 off: " + ", ".join(off)
        quiet = (
            "no quiet hours"
            if window is None
            else f"quiet {_quiet_label(window)}"
        )
        return f"{kinds} · {quiet}"

    def _notify_embed(self, person: dict) -> discord.Embed:
        """The 🔔 panel — everything the bot starts, and the switch for each.

        Governs only what the bot/a housemate starts, not a reply to
        something you did (those stay under Pings). Doesn't claim delivery
        is guaranteed — the house's reminder option gates all three.
        """
        # Prepended, not a field: this caveat must be read first, and fields render after it.
        if person["reminders"] != people_mod.REMIND_DM:
            route = (
                "⚠️ Most of these can't reach you at the moment — **Pings** is "
                "set to something other than 📬 **DM me**, and they are DMs. "
                "(🧺 Empty-it follows **Pings**, so it still reaches you.) Your "
                "answers here are kept for when you change it back.\n\n"
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
                + "These are the messages **I** start — on a schedule, when your "
                "load needs you, or "
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
            # The only house-level switch that can make this line untrue —
            # already read in this module, so no duplicate rule elsewhere.
            if kind == people_mod.KIND_TRADES and not self.trades_enabled:
                line += "\n(swaps are off for this channel, so nobody can ask)"
            lines.append(line)
        embed.add_field(name="Messages", value="\n".join(lines), inline=False)
        window = people_mod.quiet_hours(person)
        # Names no clock time: nudge_lead is configurable, so a hardcoded
        # hour here could go stale.
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
        """The 🔮 panel — what the model thinks, in plain terms.

        The no-guess case (common for the first month) says so plainly.
        Wording comes from :func:`habit.describe_prediction`/``explain``,
        so the panel and any future DM can't drift apart.
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

        Shows their own numbers against the bar, so "nothing yet" reads as
        "give it time" rather than "broken".
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
