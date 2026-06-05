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

from .const import CLAIM_CUSTOM_ID

if TYPE_CHECKING:
    from .coordinator import LaundryCoordinator

_LOGGER = logging.getLogger(__name__)


class ClaimView(discord.ui.View):
    """Persistent view holding the single 'Claim' button.

    Persistent views need ``timeout=None`` and a fixed ``custom_id`` so the
    button keeps working after an HA/bot restart (re-registered via
    ``client.add_view`` in :meth:`LaundryDiscordClient.on_ready`).
    """

    def __init__(self, coordinator: "LaundryCoordinator") -> None:
        super().__init__(timeout=None)
        self.coordinator = coordinator

    @discord.ui.button(
        label="Claim this load",
        style=discord.ButtonStyle.primary,
        emoji="🧺",
        custom_id=CLAIM_CUSTOM_ID,
    )
    async def claim(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Handle a tap on the Claim button."""
        who = interaction.user.display_name
        try:
            await self.coordinator.handle_claim(who)
            embed = self.coordinator.build_embed(claimed_by=who)
            # Editing the message removes the button (view=None) and shows the
            # claimant. This is the interaction response, so no extra push fires.
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception:  # noqa: BLE001 - never let a bot callback bubble into HA
            _LOGGER.exception("Failed to handle Claim interaction")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Something went wrong claiming this load.", ephemeral=True
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not send claim error response", exc_info=True)


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
        """Send a small standalone message that actually notifies (role mention).

        Used only for the optional drying alert, since editing an embed never
        triggers a push notification.
        """
        await self._client.wait_until_ready()
        channel = await self._get_channel()
        await channel.send(
            content=content,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
