"""Thin discord.py wrapper for the Laundry Discord Bot integration.

The client is started *inside* Home Assistant's event loop (never ``client.run()``)
as a background task tied to the config entry, and closed on unload.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.utils import MISSING

from homeassistant.core import HomeAssistant

from .assistant import (
    AssistantView,
    GridView,
    GuessView,
    NotifyView,
    TradeAskView,
    TradeRequestView,
)
from .const import (
    ASSISTANT_CUSTOM_ID,
    CLAIM_CUSTOM_ID,
    EMPTIED_CUSTOM_ID,
    NEXT_CUSTOM_ID,
    QUIET_CUSTOM_ID,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_WASHING,
    UNCLAIM_CUSTOM_ID,
    UNCLAIMED,
)
from .queue import (
    QUEUE_CAP,
    TOGGLE_FULL,
    TOGGLE_STALE,
    position as queue_position,
    tap_notice,
)
from .reminders import NudgeView, PlanDMView

if TYPE_CHECKING:
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)

# Most Message objects to keep hot. The bot holds at most a couple of live
# messages (the load card, and later a board), so this only exists to stop the
# cache growing without bound across many loads.
_MESSAGE_CACHE_MAX = 8

# How long any send may wait for the gateway to be usable before giving up.
#
# ``wait_until_ready()`` waits on ``discord.Client._ready``, an ``asyncio.Event``
# that ``login()`` creates *before* the login HTTP call that can fail. So when
# the gateway task dies — a rotated or bad token, no network at boot, a Discord
# 5xx during login — that event is left unset with nothing alive to set it, and
# ``async_run_bot`` has already swallowed the exception and returned. Every send
# then blocks **forever**, and the coordinator awaits these inside its session
# lock: one wash at 09:00 would take the lock, set stage to washing, park here,
# and never come back. No card, no completion, and ``reset_session`` — the
# documented escape hatch — takes the same lock, so the recovery path is wedged
# too. ``close()`` makes it strictly worse by *clearing* ``_ready`` again, so a
# task parked here at unload can never be woken at all.
#
# Thirty seconds, the same number :data:`reminders._SEND_TIMEOUT` picked for the
# same hazard one layer up: comfortably longer than an ordinary reconnect, and
# short enough that a session transition cannot hold the lock across a real
# outage. Timing out **raises**, so each caller's existing ``except`` runs —
# ``_async_start_session`` puts the stage back to idle and resets the detector,
# which is exactly the recovery a failed post already had.
_READY_TIMEOUT = 30


async def _safe_interaction_error(interaction: discord.Interaction) -> None:
    """Best-effort ephemeral error reply; never raises."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong — try again in a moment.", ephemeral=True
            )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not send interaction error response", exc_info=True)


async def _dm_notice_followup(
    coordinator: "LaundryCoordinator", interaction: discord.Interaction
) -> None:
    """Piggyback the "I couldn't DM you" explainer on a card tap; never raises.

    Design doc §10.5 rule 3 says *any* button, not just 🤖: somebody who has
    already turned DMs on has no reason to open the panel again, so that is
    precisely the person whose reminders would go quiet forever with nothing
    telling them why. Runs after the callback's own response so it is a
    followup rather than a competing second response.
    """
    try:
        await coordinator.assistant.async_followup_dm_notice(interaction)
    except Exception:  # noqa: BLE001 - a notice must never break a real tap
        _LOGGER.debug("Could not follow up with the DM-failure notice", exc_info=True)


async def _ephemeral_followup(
    interaction: discord.Interaction, text: str | None
) -> None:
    """Say something privately to the tapper *after* the card's own edit.

    Discord allows exactly **one response** per interaction, and a card button
    spends it on ``edit_message`` — correctly, because the shared card is what
    the whole house reads. A ``followup`` is a legal second message on that
    same token, so the person who tapped gets a word addressed to them without
    the card losing its update. Same shape as :func:`_dm_notice_followup`,
    errors included: a confirmation must never be the reason a real tap fails,
    and ``None`` means nothing is owed and no Discord call is made at all.
    """
    if not text:
        return
    try:
        await interaction.followup.send(text, ephemeral=True)
    except Exception:  # noqa: BLE001 - a confirmation must never break a tap
        _LOGGER.debug("Could not send the tap confirmation", exc_info=True)


async def _is_live_card(
    coordinator: "LaundryCoordinator", interaction: discord.Interaction
) -> bool:
    """Whether this tap came from the card the bot is currently tracking.

    Persistent views are registered by ``custom_id``, not per message, so
    **every card the bot has ever posted stays live**: discord.py routes a tap
    on a three-week-old message into these callbacks exactly as it routes one
    on today's. Without this check, tapping 🧺 on an old card claims the
    *current* load for somebody who was looking at a finished one, and the
    ``edit_message`` that follows rewrites that historical card with today's
    embed — two wrong things at once, neither of them visible to the tapper.

    The one deliberate exception is 🤖, whose whole point is that it works from
    an old card (see :class:`_AssistantButton`); it opens a personal panel and
    touches no load.

    A card the bot cannot identify — ``message_id`` unset, or an interaction
    carrying no message — counts as stale. Refusing a tap costs one ephemeral
    line; acting on the wrong load costs a load.
    """
    message = getattr(interaction, "message", None)
    current = coordinator.message_id
    if current is not None and message is not None and message.id == current:
        return True
    await interaction.response.send_message(
        "That's an older laundry card. Scroll down to the newest one in this "
        "channel — this one is just history.",
        ephemeral=True,
    )
    return False


class _ClaimButton(discord.ui.Button):
    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            label="Claim this load",
            style=discord.ButtonStyle.primary,
            emoji="🧺",
            custom_id=CLAIM_CUSTOM_ID,
            row=0,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        who = interaction.user.display_name
        user_id = interaction.user.id
        try:
            if not await _is_live_card(self.coordinator, interaction):
                return
            if await self.coordinator.handle_claim(who, user_id):
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001 - never let a bot callback bubble into HA
            _LOGGER.exception("Failed to handle Claim interaction")
            await _safe_interaction_error(interaction)


class _UnclaimButton(discord.ui.Button):
    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            label="Unclaim",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            custom_id=UNCLAIM_CUSTOM_ID,
            row=0,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if not await _is_live_card(self.coordinator, interaction):
                return
            if await self.coordinator.handle_unclaim():
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle Unclaim interaction")
            await _safe_interaction_error(interaction)


class _QuietButton(discord.ui.Button):
    """Toggle 'quiet' for the claimed load.

    When quiet is on, completion names the claimant in plain text instead of
    @mentioning them — visible, but no push (for when they're asleep). The label
    /emoji reflect the current state so one tap flips it back.
    """

    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        quiet = coordinator.quiet
        super().__init__(
            label="Unmute" if quiet else "Quiet",
            style=discord.ButtonStyle.secondary,
            emoji="🔔" if quiet else "🌙",
            custom_id=QUIET_CUSTOM_ID,
            row=0,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if not await _is_live_card(self.coordinator, interaction):
                return
            if await self.coordinator.handle_toggle_quiet():
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle Quiet interaction")
            await _safe_interaction_error(interaction)


class _NextUpButton(discord.ui.Button):
    """Join (or leave) the "I'm next" line.

    Tapping it while already in the line removes you — the same button both
    ways, because a second "leave the line" button would be a fifth control on
    a card that already has enough. The line is FIFO and the tap is what earns
    the handoff ping once the washer is actually free.
    """

    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            label="I'm next",
            style=discord.ButtonStyle.secondary,
            emoji="🔜",
            custom_id=NEXT_CUSTOM_ID,
            row=1,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        who = interaction.user.display_name
        user_id = interaction.user.id
        try:
            if not await _is_live_card(self.coordinator, interaction):
                return
            result = await self.coordinator.handle_next_toggle(who, user_id)
            # Read the place now, not after the edit: `edit_message` is a round
            # trip, and anybody else's tap inside that window would move the
            # line under us and misreport where this person actually stands.
            place = queue_position(self.coordinator.queue, user_id)
            # Exactly one response per path — Discord rejects a second one, and
            # a swallowed tap shows the user "interaction failed".
            if result == TOGGLE_FULL:
                await interaction.response.send_message(
                    f"The line's full ({QUEUE_CAP} people waiting) — try again "
                    "once it's moved.",
                    ephemeral=True,
                )
            elif result == TOGGLE_STALE:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
            else:
                # Added or removed: the card's "Next up" line changed, so the
                # whole house sees the queue move without anyone announcing it.
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
                # ...and then, privately, what it did *for them*. The shared
                # card alone leaves joining and leaving indistinguishable to
                # the one person who needs to know which just happened — and
                # invisible entirely on a phone scrolled past the card. A
                # followup, not a response: the edit above already spent the
                # single response this interaction gets.
                await _ephemeral_followup(interaction, tap_notice(result, place))
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle I'm next interaction")
            await _safe_interaction_error(interaction)


class _EmptiedButton(discord.ui.Button):
    """The claimant confirming they've actually cleared the drum.

    A finished washer is not an empty washer — the claimant's clothes are still
    in it — so this tap, not completion, is what hands the machine over and
    releases the ping to whoever is next. It disappears once tapped.
    """

    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            label="Emptied it",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=EMPTIED_CUSTOM_ID,
            row=1,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if not await _is_live_card(self.coordinator, interaction):
                return
            if await self.coordinator.handle_emptied():
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle Emptied it interaction")
            await _safe_interaction_error(interaction)


class _AssistantButton(discord.ui.Button):
    """Open the private 🤖 panel (settings, or the first-time explainer).

    Emoji only and no label: it is the least important control on the card and
    shouldn't compete with the ones that do laundry. It is added **last** so it
    renders rightmost — Discord lays buttons out in add order and has no
    right-align, so position is the only lever there is.

    Unlike every other button here it does **not** need a live load: a tap on a
    week-old card still opens the panel, which matters because someone
    scrolling back through the channel is exactly the person who has never used
    it before.
    """

    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="🤖",
            custom_id=ASSISTANT_CUSTOM_ID,
            row=1,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self.coordinator.assistant.async_open_panel(interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to open the assistant panel")
            await _safe_interaction_error(interaction)


class ClaimView(discord.ui.View):
    """Persistent view holding the card's Claim / queue / assistant buttons.

    Persistent views need ``timeout=None`` and fixed ``custom_id``s so the
    buttons keep working after an HA/bot restart (re-registered via
    ``client.add_view`` in :meth:`LaundryDiscordClient.on_ready`).

    ``show`` controls which buttons are presented on a given message:
      - ``"claim"``   — a finished, unclaimed load (Claim only)
      - ``"unclaim"`` — a claimed load (Unclaim + the 🌙 Quiet toggle)
      - ``"both"``    — registration template so every custom_id stays live
        after a restart regardless of the message's current state.

    ``with_next`` / ``with_emptied`` / ``with_assistant`` add the queue and
    assistant buttons, and the ``"both"`` template forces them on: a
    ``custom_id`` that was never handed to ``add_view`` silently stops
    dispatching after a restart, which looks exactly like a dead button and is
    a bug class this integration has already been bitten by. The template
    includes 🤖 even when the option hides it, so flipping the option on
    doesn't leave a dead button until the next restart.

    Rows are explicit — claim/unclaim/quiet on row 0, next/emptied/🤖 on row 1
    (3 of the 5 a row allows) — rather than left to Discord's auto-flow, so the
    card people have learned doesn't reshuffle when a button comes or goes.

    Prefer :func:`view_for` over calling this directly: it is the one place
    that maps coordinator state onto a button set.
    """

    def __init__(
        self,
        coordinator: "LaundryCoordinator",
        *,
        show: str = "both",
        with_next: bool = False,
        with_emptied: bool = False,
        with_assistant: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        template = show == "both"
        if show in ("claim", "both"):
            self.add_item(_ClaimButton(coordinator))
        if show in ("unclaim", "both"):
            self.add_item(_UnclaimButton(coordinator))
            self.add_item(_QuietButton(coordinator))
        if with_next or template:
            self.add_item(_NextUpButton(coordinator))
        if with_emptied or template:
            self.add_item(_EmptiedButton(coordinator))
        # Last, always: rightmost is where it belongs (see _AssistantButton).
        if with_assistant or template:
            self.add_item(_AssistantButton(coordinator))


def view_for(coordinator: "LaundryCoordinator") -> ClaimView:
    """Build the button set that matches the coordinator's current state.

    Every callback re-attaches a view when it edits the card, so this lives in
    one place: if two call sites disagreed, a button would appear or vanish
    depending on which one last touched the message.

    - 🔜 **I'm next** rides along for the whole life of a load (washing, drying
      and done-waiting) — people queue up mid-cycle, not just at the end.
    - ✅ **Emptied it** only makes sense on a *claimed*, finished load that
      hasn't been cleared yet: nobody else can confirm it, and once it's been
      tapped there is nothing left to confirm.
    - 🤖 **Assistant** rides along on every card unless the option hides it. It
      is inert until tapped — no pings, no channel lines, nothing stored — and
      it is the only place a newcomer finds out what the rest of the row does.
    """
    claimed = (
        coordinator.claimed_by != UNCLAIMED and coordinator.claimed_by_id is not None
    )
    return ClaimView(
        coordinator,
        show="unclaim" if claimed else "claim",
        with_next=coordinator.stage
        in (STAGE_WASHING, STAGE_DRYING, STAGE_DONE_WAITING),
        with_emptied=(
            coordinator.stage == STAGE_DONE_WAITING
            and claimed
            and not coordinator.emptied
        ),
        with_assistant=coordinator.show_assistant,
    )


class LaundryDiscordClient(discord.Client):
    """discord.Client subclass that wires the persistent view on startup."""

    def __init__(self, coordinator: "LaundryCoordinator", **kwargs) -> None:
        super().__init__(**kwargs)
        self.coordinator = coordinator
        self._view_registered = False

    async def on_ready(self) -> None:
        """Register the persistent views and let the coordinator restore."""
        if not self._view_registered:
            try:
                self.add_view(ClaimView(self.coordinator))
                # The panel's own buttons dispatch through the same registry,
                # even though the message carrying them is ephemeral — without
                # this, every panel opened before a restart goes dead.
                self.add_view(AssistantView(self.coordinator.assistant))
                # Likewise the grid's day select and slot toggles, and the 🔮
                # panel's three answers. Both are built with no arguments,
                # which is the template form that carries every custom_id —
                # including the ones a given render leaves out.
                self.add_view(GridView(self.coordinator.assistant))
                self.add_view(GuessView(self.coordinator.assistant))
                # And the 🔔 sub-panel's four toggles, quiet-hours select and
                # back. Built with no person, which is the template form. It
                # matters most of all here: a toggle that never registered
                # still *looks* like it saved — the label only changes on the
                # re-render a dispatched tap would have caused, so the panel
                # goes on saying "on" about a message somebody switched off.
                self.add_view(NotifyView(self.coordinator.assistant))
                # The reminder DMs' replies. Registered whatever the reminder
                # option currently says: a DM already sitting in somebody's
                # inbox has to keep working, and "🔕 Stop asking" is the last
                # button that should ever answer "interaction failed".
                self.add_view(PlanDMView(self.coordinator.assistant))
                self.add_view(NudgeView(self.coordinator.assistant))
                # The trade broker's panel and its request DM. Registered
                # whatever the trades option currently says, for the same
                # reason as the reminder DMs: an ask already sitting in
                # somebody's inbox has to keep working, and "🚫 Don't ask me
                # again" is the last button here that should ever answer
                # "interaction failed".
                self.add_view(TradeAskView(self.coordinator.assistant))
                self.add_view(TradeRequestView(self.coordinator.assistant))
                self._view_registered = True
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to register persistent views")
        _LOGGER.debug("Discord bot connected as %s", self.user)
        try:
            await self.coordinator.async_on_bot_ready()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error during post-ready restore")


class DiscordBot:
    """High-level helper the coordinator uses to post/edit Discord messages."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "LaundryCoordinator",
        token: str,
        channel_id: str | int,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._token = token
        self._channel_id = int(channel_id)
        intents = discord.Intents.default()  # buttons need no privileged intents
        self._client = LaundryDiscordClient(coordinator, intents=intents)
        # Message objects keyed by ID, insertion-ordered so the oldest can be
        # evicted. It used to be a single slot, which meant every alternation
        # between two long-lived messages (the load card and, later, a pinned
        # board) cost a refetch — and a refetch is a round trip on the ETA tick.
        self._messages: dict[int, discord.Message] = {}

    async def async_start(self) -> None:
        """Connect to the Discord gateway (runs until closed)."""
        await self._client.start(self._token)

    async def async_close(self) -> None:
        """Close the gateway connection."""
        if not self._client.is_closed():
            await self._client.close()

    @property
    def is_ready(self) -> bool:
        return self._client.is_ready()

    async def _wait_ready(self) -> None:
        """Wait for a usable gateway, or raise. **Never waits forever.**

        The one place ``wait_until_ready()`` is allowed to be called, so the
        bound in :data:`_READY_TIMEOUT` cannot be forgotten by a send added
        later. Re-raised as ``TimeoutError`` rather than swallowed: a caller
        that thinks it posted a card when it did not is worse off than one whose
        ``except`` branch runs.
        """
        try:
            async with asyncio.timeout(_READY_TIMEOUT):
                await self._client.wait_until_ready()
        except TimeoutError:
            # Only the timeout. A CancelledError from outside is HA shutting
            # down (or an unload cancelling this task) and has to keep going.
            _LOGGER.debug("Discord gateway not ready within %ss", _READY_TIMEOUT)
            raise

    async def _get_channel(self):
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self._channel_id)
        return channel

    def _remember(self, message: discord.Message) -> None:
        """Cache a Message, evicting the oldest insertion past the cap."""
        self._messages.pop(message.id, None)  # re-insert so it counts as newest
        self._messages[message.id] = message
        while len(self._messages) > _MESSAGE_CACHE_MAX:
            self._messages.pop(next(iter(self._messages)))

    async def _ensure_message(self, message_id: int) -> discord.Message:
        """Return the cached Message, or fetch it by ID (e.g. after restart)."""
        cached = self._messages.get(message_id)
        if cached is not None:
            return cached
        channel = await self._get_channel()
        try:
            message = await channel.fetch_message(message_id)
        except Exception:  # noqa: BLE001 - re-raised; the caller logs it
            # Never leave a stale entry for a message we couldn't fetch — one
            # deleted message must not wedge every later call for that ID.
            self._messages.pop(message_id, None)
            raise
        self._remember(message)
        return message

    async def async_post(
        self,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
        content: str | None = None,
        silent: bool = True,
    ) -> int:
        """Post a new message and remember it. Returns the message ID."""
        await self._wait_ready()
        channel = await self._get_channel()
        allowed = (
            discord.AllowedMentions(roles=True)
            if content
            else discord.AllowedMentions.none()
        )
        message = await channel.send(
            content=content,
            embed=embed,
            view=view,
            silent=silent,
            allowed_mentions=allowed,
        )
        self._remember(message)
        return message.id

    async def async_edit(
        self,
        message_id: int,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = MISSING,
    ) -> None:
        """Edit an existing message in place. Edits never send a push.

        ``view`` defaults to ``MISSING`` so an existing view is left untouched;
        pass ``None`` to remove it or a view instance to set it.
        """
        message = await self._ensure_message(message_id)
        try:
            await message.edit(embed=embed, view=view)
        except Exception:  # noqa: BLE001 - re-raised; the caller logs it
            # A cached Message whose real message was deleted fails every edit
            # forever; drop it so the next call refetches (and fails loudly on
            # the fetch instead of silently on a phantom object).
            self._messages.pop(message_id, None)
            raise

    async def async_send_ping(self, content: str) -> None:
        """Send a small standalone message that actually notifies a user.

        Used for the completion ping to whoever claimed the load, since editing
        an embed never triggers a push notification. Only user mentions are
        allowed (no @everyone / role pings).
        """
        await self._wait_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )

    async def async_dm_user(
        self, user_id: int | str, content: str, *, view: discord.ui.View | None = None
    ) -> discord.Message:
        """Send one direct message to a known user ID. Returns the message.

        The message comes back because a reminder DM has to stay identifiable:
        its buttons are a persistent view and outlive both the slot and the
        process, so what the DM was *about* is recorded against its id.

        Deliberately lets exceptions out: the assistant has to tell
        ``discord.Forbidden`` (error 50007 — "this user has DMs from server
        members turned off", which Discord never reveals to *them*) apart from
        a transient failure, because only the first one is worth remembering.

        DMing a bare user ID needs no privileged intent. ``get_user`` is tried
        first so a cached user costs no HTTP round trip; ``fetch_user`` covers
        somebody the gateway hasn't sent us yet.
        """
        await self._wait_ready()
        uid = int(user_id)
        user = self._client.get_user(uid)
        if user is None:
            user = await self._client.fetch_user(uid)
        return await user.send(content=content, view=view)

    async def async_announce_done(self, content: str) -> None:
        """Post a fresh, push-silent 'done' nudge as plain text (no embed).

        Used when an unclaimed load finishes: the original card is edited in place
        (keeping the embed + Claim button) but stays buried in the channel
        history, so we drop a short text line at the bottom where people will
        actually see it. Plain text — not a second embed — so it doesn't look
        like a duplicate of the card. ``silent=True`` keeps it visible without a
        push, and mentions are disabled so nobody is pinged.
        """
        await self._wait_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            silent=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
