"""Session state machine for the Laundry Discord Bot.

Watches the washer entities, drives the Discord bot (one embed per load), and
mirrors the lifecycle into HA entities. All Discord work is funnelled through a
lock so edits never overlap, and every bot call is wrapped so a failure logs
rather than taking HA down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOT_TOKEN,
    CONF_CHANNEL_ID,
    CONF_ETA_ENTITY,
    CONF_ETA_INTERVAL,
    CONF_JOB_STATE_ENTITY,
    CONF_PING_CLAIMANT_ON_COMPLETE,
    CONF_RUNNING_ENTITY,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    INVALID_OLD_STATES,
    PROGRESS_PHASES,
    JOB_STATE_DRYING,
    JOB_STATE_NONE,
    REAL_PHASES,
    SIGNAL_UPDATE,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_IDLE,
    STAGE_WASHING,
    STORAGE_KEY,
    STORAGE_VERSION,
    UNCLAIMED,
)
from .discord_bot import ClaimView, DiscordBot

_LOGGER = logging.getLogger(__name__)

# Colors per stage.
_COLOR_WASHING = 0x3498DB
_COLOR_DRYING = 0xE67E22
_COLOR_DONE = 0x2ECC71
_COLOR_CLAIMED = 0x95A5A6
_COLOR_TEST = 0x9B59B6

_FOOTER = "ETA is the washer's own estimate and may drift — treat it as approximate."


class LaundryCoordinator:
    """Owns the laundry-notification lifecycle for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._cfg = {**entry.data, **entry.options}
        self.bot = DiscordBot(
            hass, self, self._cfg[CONF_BOT_TOKEN], self._cfg[CONF_CHANNEL_ID]
        )

        # Session state (persisted).
        self.stage: str = STAGE_IDLE
        self.waiting: bool = False
        self.claimed_by: str = UNCLAIMED
        self.claimed_by_id: int | None = None
        self.message_id: int | None = None
        # True when the session was picked up mid-cycle (washer already running
        # at startup) rather than caught at its off->on start.
        self.catch_up: bool = False

        self._eta_unsub = None
        self._unsubs: list = []
        self._lock = asyncio.Lock()
        self._restored = False

    # ------------------------------------------------------------------ config
    @property
    def running_entity(self) -> str:
        return self._cfg[CONF_RUNNING_ENTITY]

    @property
    def job_state_entity(self) -> str:
        return self._cfg[CONF_JOB_STATE_ENTITY]

    @property
    def eta_entity(self) -> str:
        return self._cfg[CONF_ETA_ENTITY]

    @property
    def eta_interval(self) -> int:
        return int(self._cfg.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL))

    @property
    def ping_claimant_on_complete(self) -> bool:
        return bool(
            self._cfg.get(
                CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE
            )
        )

    # ------------------------------------------------------------------- setup
    async def async_setup(self) -> None:
        """Load persisted session and subscribe to the watched entities."""
        await self._async_load()
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.running_entity], self._on_running
            )
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.job_state_entity], self._on_job_state
            )
        )

    async def async_run_bot(self) -> None:
        """Background task body: run the gateway, never crash HA on failure."""
        try:
            await self.bot.async_start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Laundry Discord bot stopped unexpectedly")

    async def async_shutdown(self) -> None:
        """Tear down listeners, timers and the gateway connection."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._stop_eta_timer()
        try:
            await self.bot.async_close()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error closing Discord bot")

    async def async_on_bot_ready(self) -> None:
        """Restore an in-progress session once the gateway is connected."""
        if self._restored:
            return
        self._restored = True
        if self.stage in (STAGE_WASHING, STAGE_DRYING) and self.message_id:
            self._start_eta_timer()
            _LOGGER.debug("Restored active laundry session (stage=%s)", self.stage)
        elif self.stage == STAGE_IDLE:
            # Nothing was in progress when we last saved, but the washer may
            # already be mid-cycle (e.g. installed or restarted during a load).
            # A normal off->on is caught by the state listener; this catches a
            # load that was *already* running before the bot connected.
            running = self.hass.states.get(self.running_entity)
            if running is not None and running.state == "on":
                _LOGGER.debug("Catching an already-running laundry load on startup")
                self.hass.async_create_task(
                    self._async_start_session(catch_up=True)
                )
        # DONE_WAITING sessions keep their button working via the persistent
        # ClaimView re-registered in on_ready; nothing else to do here.
        self._notify_entities()

    # ------------------------------------------------------------- persistence
    async def _async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self.stage = data.get("stage", STAGE_IDLE)
        self.waiting = data.get("waiting", False)
        self.claimed_by = data.get("claimed_by", UNCLAIMED)
        self.claimed_by_id = data.get("claimed_by_id")
        self.message_id = data.get("message_id")
        self.catch_up = data.get("catch_up", False)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "stage": self.stage,
                "waiting": self.waiting,
                "claimed_by": self.claimed_by,
                "claimed_by_id": self.claimed_by_id,
                "message_id": self.message_id,
                "catch_up": self.catch_up,
            }
        )

    @callback
    def _notify_entities(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ---------------------------------------------------------- state handlers
    @callback
    def _on_running(self, event: Event) -> None:
        """Start a session when the washer turns on.

        A clean ``off -> on`` is a normal start. A transition INTO ``on`` from
        ``unavailable``/``unknown``/``None`` means the washer "appeared" already
        running — e.g. its cloud entity populated a few seconds after an HA
        restart, just after the bot's one-shot startup check already ran. We
        catch that up, but only when nothing is currently being tracked, so a
        mid-wash cloud flap can't spawn a duplicate session.
        """
        new = event.data.get("new_state")
        if new is None or new.state != "on":
            return
        old = event.data.get("old_state")
        old_s = old.state if old is not None else None
        if old_s == "off":
            self.hass.async_create_task(self._async_start_session())
        elif old_s in (None, "unavailable", "unknown") and self.stage == STAGE_IDLE:
            self.hass.async_create_task(self._async_start_session(catch_up=True))

    @callback
    def _on_job_state(self, event: Event) -> None:
        """Drive drying/finished transitions, immune to the ~51-min flap."""
        new = event.data.get("new_state")
        if new is None:
            return
        old = event.data.get("old_state")
        new_s = new.state
        old_s = old.state if old is not None else None

        # Drying: only from a real phase (never from unavailable/unknown/none).
        if (
            new_s == JOB_STATE_DRYING
            and old_s is not None
            and old_s not in INVALID_OLD_STATES
        ):
            self.hass.async_create_task(self._async_handle_drying())
        # Finished: into "none" from a real wash phase.
        elif new_s == JOB_STATE_NONE and old_s in REAL_PHASES:
            self.hass.async_create_task(self._async_handle_finished())

    # ------------------------------------------------------- lifecycle actions
    async def _async_start_session(self, catch_up: bool = False) -> None:
        async with self._lock:
            # A wash already in progress wins. A previous *finished* load (still
            # showing its claim/unclaim message) is simply superseded by this one.
            if self.stage in (STAGE_WASHING, STAGE_DRYING):
                _LOGGER.debug("Start ignored; wash already active (stage=%s)", self.stage)
                return
            self.stage = STAGE_WASHING
            self.catch_up = catch_up
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.message_id = None

            # The start post is a normal, visible message with the Claim button
            # so people can call dibs early. It never @mentions anyone — the only
            # ping is to the claimant when the load is done.
            embed = self.build_embed()
            try:
                self.message_id = await self.bot.async_post(
                    embed,
                    view=ClaimView(self, show="claim"),
                    content=None,
                    silent=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to post laundry start message")
                self.stage = STAGE_IDLE
                return

            self._start_eta_timer()
            await self._async_save()
            self._notify_entities()

    async def _async_handle_drying(self) -> None:
        async with self._lock:
            if self.stage == STAGE_IDLE:
                return
            self.stage = STAGE_DRYING
            # Silent edit; the button (claim/unclaim) is preserved.
            embed = self.build_embed()
            try:
                if self.message_id:
                    await self.bot.async_edit(self.message_id, embed)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update drying state")
            await self._async_save()
            self._notify_entities()

    async def _async_handle_finished(self) -> None:
        async with self._lock:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING):
                return
            self._stop_eta_timer()
            self.stage = STAGE_DONE_WAITING
            # A pre-claim made during the wash carries through to completion.
            claimed = self.claimed_by != UNCLAIMED and self.claimed_by_id is not None
            self.waiting = not claimed
            embed = self.build_embed()
            view = ClaimView(self, show="unclaim" if claimed else "claim")
            try:
                if self.message_id:
                    await self.bot.async_edit(self.message_id, embed, view=view)
                # The one push per load: @mention the claimant, if there is one.
                # If nobody claimed, the done message above is plain — no ping.
                if claimed and self.ping_claimant_on_complete:
                    await self.bot.async_send_ping(
                        f"<@{self.claimed_by_id}> 🧺 Your laundry's done — "
                        "don't forget the lint tray!"
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update finished state")
            await self._async_save()
            self._notify_entities()

    # Stages in which a claim/unclaim tap is meaningful (an active load exists).
    _CLAIMABLE_STAGES = (STAGE_WASHING, STAGE_DRYING, STAGE_DONE_WAITING)

    async def handle_claim(self, who: str, user_id: int) -> bool:
        """Record the claimant. Claims are allowed from the start of the wash and
        are reversible. Returns False on a stale tap (no active load).
        """
        if self.stage not in self._CLAIMABLE_STAGES:
            return False
        self.claimed_by = who
        self.claimed_by_id = user_id
        if self.stage == STAGE_DONE_WAITING:
            self.waiting = False
        await self._async_save()
        self._notify_entities()
        return True

    async def handle_unclaim(self) -> bool:
        """Undo a claim — the load is up for grabs again. Called from the button."""
        if self.stage not in self._CLAIMABLE_STAGES:
            return False
        self.claimed_by = UNCLAIMED
        self.claimed_by_id = None
        if self.stage == STAGE_DONE_WAITING:
            self.waiting = True
        await self._async_save()
        self._notify_entities()
        return True

    async def async_test_post(self) -> None:
        """Debug service: post a sample embed with a working Claim button."""
        async with self._lock:
            self.stage = STAGE_DONE_WAITING
            self.waiting = True
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            embed = self.build_embed(test=True)
            try:
                self.message_id = await self.bot.async_post(
                    embed, view=ClaimView(self, show="claim"), silent=True
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("test_post failed")
                return
            await self._async_save()
            self._notify_entities()

    # ----------------------------------------------------------- ETA timer
    def _start_eta_timer(self) -> None:
        self._stop_eta_timer()
        self._eta_unsub = async_track_time_interval(
            self.hass, self._async_eta_tick, timedelta(seconds=self.eta_interval)
        )

    def _stop_eta_timer(self) -> None:
        if self._eta_unsub is not None:
            self._eta_unsub()
            self._eta_unsub = None

    async def _async_eta_tick(self, now) -> None:
        if self.stage not in (STAGE_WASHING, STAGE_DRYING) or not self.message_id:
            return
        try:
            await self.bot.async_edit(self.message_id, self.build_embed())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ETA edit failed")

    # ------------------------------------------------------------------ embeds
    def _eta_text(self) -> str:
        state = self.hass.states.get(self.eta_entity)
        if state is None or state.state in ("", "unknown", "unavailable", None):
            return "ETA updating…"
        target = dt_util.parse_datetime(state.state)
        if target is None:
            return "ETA updating…"
        if target.tzinfo is None:
            target = dt_util.as_utc(target)
        local = dt_util.as_local(target)
        clock = local.strftime("%-I:%M %p")
        delta = (target - dt_util.utcnow()).total_seconds()
        if delta <= 0:
            return f"~{clock} (any moment now)"
        mins = int(delta // 60)
        hours, minutes = divmod(mins, 60)
        rel = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
        return f"~{clock}, about {rel} left"

    def _progress_bar(self) -> str | None:
        """Render a wash→dry stage bar from the live job_state, or None."""
        state = self.hass.states.get(self.job_state_entity)
        current = state.state if state is not None else None

        # Final wash phase: everything done.
        if current == "finish":
            return " → ".join(f"🟩 {label}" for label, _ in PROGRESS_PHASES)

        current_idx: int | None = None
        for i, (_label, values) in enumerate(PROGRESS_PHASES):
            if current in values:
                current_idx = i
                break

        parts: list[str] = []
        for i, (label, _values) in enumerate(PROGRESS_PHASES):
            if current_idx is not None and i < current_idx:
                marker = "🟩"  # completed
            elif current_idx is not None and i == current_idx:
                marker = "🟦"  # in progress
            else:
                marker = "⬜"  # upcoming / unknown
            parts.append(f"{marker} {label}")
        return " → ".join(parts)

    def build_embed(self, *, test: bool = False) -> discord.Embed:
        """Build the embed for the current stage (reads live state + claimant)."""
        if test:
            embed = discord.Embed(
                title="🧪 Laundry Bot test post",
                description=(
                    "This is a test. Tap **Claim this load** to verify the button "
                    "and that `sensor.laundry_claimed_by` updates in HA. Then tap "
                    "**Unclaim** to undo it."
                ),
                color=_COLOR_TEST,
            )
            embed.set_footer(text="Test post via laundry_discord.test_post")
            embed.timestamp = dt_util.utcnow()
            return embed

        if self.stage == STAGE_WASHING:
            embed = discord.Embed(
                title=(
                    "🫧 Laundry in progress" if self.catch_up else "🫧 Laundry started"
                ),
                description="The washer is running. Tap **Claim** to call dibs.",
                color=_COLOR_WASHING,
            )
            self._add_progress_and_eta(embed)
            self._add_claimant(embed)
        elif self.stage == STAGE_DRYING:
            embed = discord.Embed(
                title="🌀 Drying",
                description="Pull out anything you don't want dried!",
                color=_COLOR_DRYING,
            )
            self._add_progress_and_eta(embed)
            self._add_claimant(embed)
        elif self.stage == STAGE_DONE_WAITING:
            if self.claimed_by and self.claimed_by != UNCLAIMED:
                embed = discord.Embed(
                    title="🧺 Claimed",
                    description=(
                        f"Claimed by **{self.claimed_by}**.\n"
                        "Grabbed it by accident? Tap **Unclaim**."
                    ),
                    color=_COLOR_CLAIMED,
                )
            else:
                embed = discord.Embed(
                    title="✅ Laundry done!",
                    description=(
                        "Don't forget the **lint tray**.\n"
                        "Tap **Claim this load** to grab it."
                    ),
                    color=_COLOR_DONE,
                )
        else:
            embed = discord.Embed(
                title="Laundry", description="Idle.", color=_COLOR_CLAIMED
            )

        embed.set_footer(text=_FOOTER)
        embed.timestamp = dt_util.utcnow()
        return embed

    def _add_progress_and_eta(self, embed: discord.Embed) -> None:
        bar = self._progress_bar()
        if bar:
            embed.add_field(name="Progress", value=bar, inline=False)
        embed.add_field(name="Estimated finish", value=self._eta_text(), inline=False)

    def _add_claimant(self, embed: discord.Embed) -> None:
        if self.claimed_by and self.claimed_by != UNCLAIMED:
            embed.add_field(
                name="Claimed by", value=f"🧺 {self.claimed_by}", inline=False
            )
