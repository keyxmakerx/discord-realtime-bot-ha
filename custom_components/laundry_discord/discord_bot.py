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
from .reminders import NudgeView, PlanDMView, SlotTakenView

if TYPE_CHECKING:
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)

# Cap on cached Message objects, so the cache can't grow unbounded across
# many loads (the bot only ever holds a couple live at once).
_MESSAGE_CACHE_MAX = 8

# How long a send may wait for the gateway before giving up. If the login
# task dies (bad token, no network, a Discord 5xx) the ready event never
# gets set and wait_until_ready() blocks forever — and callers hold the
# session lock while awaiting this, so a hang here wedges the whole
# integration, including the reset_session recovery path. Matches
# reminders._SEND_TIMEOUT.
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

    Fires on every button, not just 🤖, so someone with DMs already on for
    reminders learns why. Runs as a followup, after the callback's response.
    """
    try:
        await coordinator.assistant.async_followup_dm_notice(interaction)
    except Exception:  # noqa: BLE001 - a notice must never break a real tap
        _LOGGER.debug("Could not follow up with the DM-failure notice", exc_info=True)


async def _ephemeral_followup(
    interaction: discord.Interaction, text: str | None
) -> None:
    """Say something privately to the tapper, after the card's own edit.

    A card button spends its one Discord response on `edit_message`, so
    this sends a followup instead. Never raises. `None` means nothing is owed.
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

    Persistent views register by `custom_id`, not per message, so a tap on
    any card ever posted reaches this callback, not just the latest one.
    Without this check, an old-card tap would claim/edit the current load
    using a stale message. 🤖 is exempt (opens a personal panel, touches no
    load), and a card with no known message id also counts as stale.
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

    When on, completion names the claimant in plain text instead of
    @mentioning them (visible, no push). Label/emoji reflect the current
    state.
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
    """Join, or leave, the "I'm next" line (same button both ways).

    FIFO; a tap here is what earns the handoff ping once the washer frees up.
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
            # Read the place before the edit round-trip — another tap in that
            # window could move the line under us and misreport it.
            place = queue_position(self.coordinator.queue, user_id)
            # Exactly one response per path; a second one shows the user
            # "interaction failed".
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
                # Card edit shows the queue move to everyone.
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=view_for(self.coordinator),
                )
                # Followup: the edit above already used this interaction's
                # one response.
                await _ephemeral_followup(interaction, tap_notice(result, place))
            await _dm_notice_followup(self.coordinator, interaction)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle I'm next interaction")
            await _safe_interaction_error(interaction)


class _EmptiedButton(discord.ui.Button):
    """The claimant confirming they've actually cleared the drum.

    Completion alone doesn't free the machine — this tap does, and it's
    what releases the ping to whoever's next. Disappears once tapped.
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

    Added last so it renders rightmost — Discord has no right-align, only
    add order. Unlike the other buttons, it needs no live load: it opens
    even from an old card.
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

    Needs `timeout=None` and a fixed `custom_id` per button so they keep
    working after a restart (re-registered in `on_ready`).

    `show` picks the buttons for a message's current state:
      - `"claim"`   — a finished, unclaimed load
      - `"unclaim"` — a claimed load (+ the Quiet toggle)
      - `"both"`    — registration template: every custom_id present
        regardless of state

    `with_next` / `with_emptied` / `with_assistant` add the queue/assistant
    buttons, forced on by the `"both"` template — registered even when
    hidden, since an unregistered custom_id silently stops dispatching
    after a restart. Prefer `view_for` over instantiating this directly.
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

    Kept in one place so every callback that re-attaches a view agrees on it.

    - I'm next: shown for the whole life of a load (washing/drying/done).
    - Emptied it: only on a claimed, finished load not yet cleared.
    - Assistant: on every card unless the option hides it; inert until tapped.
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
                # All registered unconditionally, whatever the relevant option
                # says: each may already be live in a channel message or a DM,
                # and a custom_id not handed to add_view silently stops
                # dispatching after a restart. Built with no arguments, the
                # template form that carries every custom_id a render can use.
                self.add_view(ClaimView(self.coordinator))
                self.add_view(AssistantView(self.coordinator.assistant))
                self.add_view(GridView(self.coordinator.assistant))
                self.add_view(GuessView(self.coordinator.assistant))
                self.add_view(NotifyView(self.coordinator.assistant))
                self.add_view(PlanDMView(self.coordinator.assistant))
                self.add_view(NudgeView(self.coordinator.assistant))
                self.add_view(
                    SlotTakenView(self.coordinator.assistant, self.coordinator)
                )
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
        # Keyed by ID, insertion-ordered so the oldest can be evicted.
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
        """Wait for a usable gateway, or raise. Never waits forever.

        The only place `wait_until_ready()` is called, so `_READY_TIMEOUT`
        can't be forgotten. Raises `TimeoutError` rather than swallowing it.
        """
        try:
            async with asyncio.timeout(_READY_TIMEOUT):
                await self._client.wait_until_ready()
        except TimeoutError:
            # Not CancelledError: HA shutdown/unload cancellation must propagate.
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
            # Drop the entry; a deleted message must not wedge every later fetch.
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
        """Edit an existing message in place. Never sends a push.

        `view=MISSING` (default) leaves the existing view untouched; pass
        `None` to remove it or a view instance to set it.
        """
        message = await self._ensure_message(message_id)
        try:
            await message.edit(embed=embed, view=view)
        except Exception:  # noqa: BLE001 - re-raised; the caller logs it
            # A deleted message would fail every edit forever; drop it so the
            # next call refetches instead.
            self._messages.pop(message_id, None)
            raise

    async def async_send_ping(self, content: str) -> None:
        """Send a standalone message that actually pushes a notification.

        Editing an embed never pushes, hence a separate message for the
        completion ping. Only user mentions are allowed (no @everyone/role).
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

        Returned so a reminder DM stays identifiable, since its buttons are
        a persistent view that outlives this call. Exceptions propagate
        deliberately — callers need to distinguish `discord.Forbidden` (DMs
        closed) from a transient failure. `get_user` is tried first (no
        HTTP round trip); `fetch_user` covers a user the gateway hasn't
        sent us yet.
        """
        await self._wait_ready()
        uid = int(user_id)
        user = self._client.get_user(uid)
        if user is None:
            user = await self._client.fetch_user(uid)
        return await user.send(content=content, view=view)

    async def async_announce_done(self, content: str) -> None:
        """Post a push-silent 'done' nudge as plain text (no embed).

        The original card stays buried in channel history; this drops a
        visible line at the bottom instead, as plain text so it doesn't
        read as a duplicate. No mentions, so nobody is pinged.
        """
        await self._wait_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            silent=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
