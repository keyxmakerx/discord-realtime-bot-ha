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
bug in here cannot corrupt a live load. The dependency runs one way: the
coordinator reaches in for the panel and the ping routing, and nothing in this
module knows anything about the session state machine.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import people as people_mod
from . import plan as plan_mod
from .const import (
    GRID_BACK_CUSTOM_ID,
    GRID_DAY_CUSTOM_ID,
    GRID_SLOT_CUSTOM_IDS,
    PANEL_CHANNEL_CUSTOM_ID,
    PANEL_DM_CUSTOM_ID,
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
    """

    def __init__(
        self, assistant: "LaundryAssistant", *, person: dict | None = None
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


class LaundryAssistant:
    """Per-person preferences, the private panel and the DM plumbing.

    Owns its own ``Store``; holds a reference to the bot purely to send things.
    Nothing here reaches back into the coordinator.
    """

    def __init__(self, hass: HomeAssistant, bot: "DiscordBot") -> None:
        self.hass = hass
        self.bot = bot
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

    async def _async_save(self) -> None:
        """Persist prefs. A failed save must not break the button that caused it."""
        try:
            await self._store.async_save(
                {"people": self._people, "overrides": self._overrides}
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
        """
        embed = discord.Embed(
            title="📅 The week",
            description=(
                f"```\n{plan_mod.render_grid(occupancy, viewer_id=user_id)}\n```\n"
                f"{plan_mod.render_legend()}\n"
                f"-# {plan_mod.render_windows()}"
            ),
            color=_COLOR_PANEL,
        )
        mine = plan_mod.describe_cells(occupancy, user_id)
        embed.add_field(
            name="Yours this week",
            value=mine or "nothing booked — tap a slot below",
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
        return embed, AssistantView(self, person=person)

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
        """The 🤖 settings panel — current prefs, and the two controls we honour.

        Slots, predictions and the week grid are deliberately absent: they
        belong to later phases, and a button that does nothing yet is worse
        than no button (design doc §15).
        """
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
        embed.add_field(
            name="Monitoring",
            value=(
                "👁 on — your loads can be logged, so I can learn the days you "
                "usually wash"
                if person["monitor"]
                else "🚫 off — I won't log your loads at all"
            )
            + "\n(nothing is being logged yet — this is your answer for when it "
            "is)",
            inline=False,
        )
        embed.set_footer(text="No stats about you are ever shown to the house.")
        return embed
