"""Thin discord.py wrapper for the Laundry Discord Bot integration.

The client is started *inside* Home Assistant's event loop (never ``client.run()``)
as a background task tied to the config entry, and closed on unload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.utils import MISSING

from homeassistant.core import HomeAssistant

from .const import CLAIM_CUSTOM_ID, QUIET_CUSTOM_ID, UNCLAIM_CUSTOM_ID

if TYPE_CHECKING:
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)


async def _safe_interaction_error(interaction: discord.Interaction) -> None:
    """Best-effort ephemeral error reply; never raises."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong — try again in a moment.", ephemeral=True
            )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not send interaction error response", exc_info=True)


class _ClaimButton(discord.ui.Button):
    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(
            label="Claim this load",
            style=discord.ButtonStyle.primary,
            emoji="🧺",
            custom_id=CLAIM_CUSTOM_ID,
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        who = interaction.user.display_name
        user_id = interaction.user.id
        try:
            if await self.coordinator.handle_claim(who, user_id):
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=ClaimView(self.coordinator, show="unclaim"),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
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
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if await self.coordinator.handle_unclaim():
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=ClaimView(self.coordinator, show="claim"),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
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
        )
        self.coordinator = coordinator

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if await self.coordinator.handle_toggle_quiet():
                await interaction.response.edit_message(
                    embed=self.coordinator.build_embed(),
                    view=ClaimView(self.coordinator, show="unclaim"),
                )
            else:
                await interaction.response.send_message(
                    "This load is no longer active.", ephemeral=True
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to handle Quiet interaction")
            await _safe_interaction_error(interaction)


class ClaimView(discord.ui.View):
    """Persistent view holding the Claim / Unclaim buttons.

    Persistent views need ``timeout=None`` and fixed ``custom_id``s so the
    buttons keep working after an HA/bot restart (re-registered via
    ``client.add_view`` in :meth:`LaundryDiscordClient.on_ready`).

    ``show`` controls which buttons are presented on a given message:
      - ``"claim"``   — a finished, unclaimed load (Claim only)
      - ``"unclaim"`` — a claimed load (Unclaim + the 🌙 Quiet toggle)
      - ``"both"``    — registration template so every custom_id stays live
        after a restart regardless of the message's current state.
    """

    def __init__(
        self, coordinator: "LaundryCoordinator", *, show: str = "both"
    ) -> None:
        super().__init__(timeout=None)
        if show in ("claim", "both"):
            self.add_item(_ClaimButton(coordinator))
        if show in ("unclaim", "both"):
            self.add_item(_UnclaimButton(coordinator))
            self.add_item(_QuietButton(coordinator))


class LaundryDiscordClient(discord.Client):
    """discord.Client subclass that wires the persistent view on startup."""

    def __init__(self, coordinator: "LaundryCoordinator", **kwargs) -> None:
        super().__init__(**kwargs)
        self.coordinator = coordinator
        self._view_registered = False

    async def on_ready(self) -> None:
        """Register the persistent Claim view and let the coordinator restore."""
        if not self._view_registered:
            try:
                self.add_view(ClaimView(self.coordinator))
                self._view_registered = True
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to register persistent Claim view")
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
        self._message: discord.Message | None = None

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

    async def _get_channel(self):
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self._channel_id)
        return channel

    async def _ensure_message(self, message_id: int) -> discord.Message:
        """Return the cached Message, or fetch it by ID (e.g. after restart)."""
        if self._message is not None and self._message.id == message_id:
            return self._message
        channel = await self._get_channel()
        self._message = await channel.fetch_message(message_id)
        return self._message

    async def async_post(
        self,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
        content: str | None = None,
        silent: bool = True,
    ) -> int:
        """Post a new message and remember it. Returns the message ID."""
        await self._client.wait_until_ready()
        channel = await self._get_channel()
        allowed = (
            discord.AllowedMentions(roles=True)
            if content
            else discord.AllowedMentions.none()
        )
        self._message = await channel.send(
            content=content,
            embed=embed,
            view=view,
            silent=silent,
            allowed_mentions=allowed,
        )
        return self._message.id

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
        await message.edit(embed=embed, view=view)

    async def async_send_ping(self, content: str) -> None:
        """Send a small standalone message that actually notifies a user.

        Used for the completion ping to whoever claimed the load, since editing
        an embed never triggers a push notification. Only user mentions are
        allowed (no @everyone / role pings).
        """
        await self._client.wait_until_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )

    async def async_announce_done(self, content: str) -> None:
        """Post a fresh, push-silent 'done' nudge as plain text (no embed).

        Used when an unclaimed load finishes: the original card is edited in place
        (keeping the embed + Claim button) but stays buried in the channel
        history, so we drop a short text line at the bottom where people will
        actually see it. Plain text — not a second embed — so it doesn't look
        like a duplicate of the card. ``silent=True`` keeps it visible without a
        push, and mentions are disabled so nobody is pinged.
        """
        await self._client.wait_until_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            silent=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
