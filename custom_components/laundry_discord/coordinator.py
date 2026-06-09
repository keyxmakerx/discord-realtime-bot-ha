"""Session state machine for the Laundry Discord Bot.

Watches the washer entities, drives the Discord bot (one embed per load), and
mirrors the lifecycle into HA entities. All Discord work is funnelled through a
lock so edits never overlap, and every bot call is wrapped so a failure logs
rather than taking HA down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
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
    CONF_AVAILABILITY_GRACE,
    CONF_CONFIRM_DELAY,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_IDLE,
    CONF_JOB_STATE_ENTITY,
    CONF_MACHINE_STATE_ENTITY,
    CONF_PING_CLAIMANT_ON_COMPLETE,
    CONF_RUNNING_ENTITY,
    CONF_SELFCLEAN_DELAY,
    CONF_WATER_ENTITY,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_ENERGY_ENTITY,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_MACHINE_STATE_ENTITY,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    DEFAULT_SELFCLEAN_DELAY,
    DEFAULT_WATER_ENTITY,
    MACHINE_PAUSE,
    MACHINE_RUN,
    MACHINE_STOP,
    MIDCYCLE_PHASES,
    PROGRESS_PHASES,
    UNAVAILABLE_STATES,
    JOB_STATE_DRYING,
    JOB_STATE_FINISH,
    JOB_STATE_NONE,
    REAL_PHASES,
    SIGNAL_UPDATE,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_IDLE,
    STAGE_SELF_CLEAN,
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
_COLOR_SELFCLEAN = 0x1ABC9C

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
        # True while machine_state reports the load is paused mid-cycle.
        self.paused: bool = False
        # Last confirmed real job phase, ignoring unavailable/unknown blips, so a
        # flap landing on the finish moment can't make us miss "done".
        self._last_real_phase: str | None = None
        # Energy/water meter baselines captured at session start (None when not
        # measurable, e.g. a mid-cycle catch-up where there's no true baseline).
        self._energy_start: float | None = None
        self._water_start: float | None = None
        # Energy-idle completion: last seen kWh and when it last increased.
        self._idle_energy_value: float | None = None
        self._idle_energy_ts: float | None = None
        # Energy reading when the last cycle completed. A new session may only
        # (re)start once energy has moved past this — so a frozen leftover phase
        # (energy still flat at this level) can't spawn a duplicate session.
        self._completion_energy: float | None = None
        # Unix timestamps of job_state -> unavailable transitions (connection
        # health). Pruned to a rolling 24h window.
        self._flap_times: list[float] = []

        self._eta_unsub = None
        self._unsubs: list = []
        self._lock = asyncio.Lock()
        self._restored = False
        # Cached last-good ETA (target datetime, when last seen) for flap hold.
        self._eta_cache: tuple[datetime, float] | None = None
        # Pending job_state confirm-debounce timer (the `for: 30s` equivalent).
        self._job_confirm_unsub = None
        # Pending self-clean detect/end timer (running + job_state stuck none).
        self._selfclean_unsub = None

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
    def machine_state_entity(self) -> str:
        return self._cfg.get(
            CONF_MACHINE_STATE_ENTITY, DEFAULT_MACHINE_STATE_ENTITY
        )

    @property
    def energy_entity(self) -> str:
        return str(self._cfg.get(CONF_ENERGY_ENTITY) or DEFAULT_ENERGY_ENTITY)

    @property
    def water_entity(self) -> str:
        return str(self._cfg.get(CONF_WATER_ENTITY) or DEFAULT_WATER_ENTITY)

    @property
    def eta_interval(self) -> int:
        return int(self._cfg.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL))

    @property
    def confirm_delay(self) -> int:
        """Seconds a job_state value must persist before we act on it."""
        return int(self._cfg.get(CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY))

    @property
    def selfclean_delay(self) -> int:
        """Seconds 'running + job_state none' must persist to mean self-clean."""
        return int(self._cfg.get(CONF_SELFCLEAN_DELAY, DEFAULT_SELFCLEAN_DELAY))

    @property
    def energy_idle_timeout(self) -> int:
        """Seconds the energy meter may be flat before a cycle counts as done."""
        return int(self._cfg.get(CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE)) * 60

    @property
    def ping_claimant_on_complete(self) -> bool:
        return bool(
            self._cfg.get(
                CONF_PING_CLAIMANT_ON_COMPLETE, DEFAULT_PING_CLAIMANT_ON_COMPLETE
            )
        )

    @property
    def availability_grace(self) -> int:
        """Seconds to hold the last-good ETA while completion is unavailable."""
        return (
            int(self._cfg.get(CONF_AVAILABILITY_GRACE, DEFAULT_AVAILABILITY_GRACE))
            * 60
        )

    # --- connection health (diagnostic) ---
    @property
    def flap_count_24h(self) -> int:
        cutoff = dt_util.utcnow().timestamp() - 86400
        return sum(1 for t in self._flap_times if t >= cutoff)

    @property
    def last_flap(self) -> datetime | None:
        if not self._flap_times:
            return None
        return dt_util.utc_from_timestamp(max(self._flap_times))

    @property
    def minutes_since_flap(self) -> float | None:
        if not self._flap_times:
            return None
        return round((dt_util.utcnow().timestamp() - max(self._flap_times)) / 60, 1)

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
        if self.machine_state_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.machine_state_entity], self._on_machine_state
                )
            )
        # Keep the diagnostic connection-health sensor fresh (24h count decays,
        # "minutes since last drop" grows) without a live event.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_health_tick, timedelta(minutes=5)
            )
        )

    @callback
    def _async_health_tick(self, now) -> None:
        self._notify_entities()

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
        if self._job_confirm_unsub is not None:
            self._job_confirm_unsub()
            self._job_confirm_unsub = None
        if self._selfclean_unsub is not None:
            self._selfclean_unsub()
            self._selfclean_unsub = None
        try:
            await self.bot.async_close()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error closing Discord bot")

    async def async_on_bot_ready(self) -> None:
        """Restore an in-progress session once the gateway is connected."""
        if self._restored:
            return
        self._restored = True
        if (
            self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN)
            and self.message_id
        ):
            self._start_eta_timer()
            _LOGGER.debug("Restored active laundry session (stage=%s)", self.stage)
        elif self.stage in (STAGE_IDLE, STAGE_DONE_WAITING):
            # Not tracking an active wash, but the washer may already be
            # mid-cycle (installed/restarted during a load or a self-clean). A
            # new normal load supersedes a done message via the job path; a
            # self-clean is checked here since it has no job phase.
            self._evaluate_start()
            self._maybe_schedule_selfclean()
        # DONE_WAITING also keeps its claim/unclaim button working via the
        # persistent ClaimView re-registered in on_ready.
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
        self.paused = data.get("paused", False)
        self._last_real_phase = data.get("last_real_phase")
        self._energy_start = data.get("energy_start")
        self._water_start = data.get("water_start")
        self._idle_energy_value = data.get("idle_energy_value")
        self._idle_energy_ts = data.get("idle_energy_ts")
        self._completion_energy = data.get("completion_energy")
        self._flap_times = list(data.get("flap_times", []))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "stage": self.stage,
                "waiting": self.waiting,
                "claimed_by": self.claimed_by,
                "claimed_by_id": self.claimed_by_id,
                "message_id": self.message_id,
                "catch_up": self.catch_up,
                "paused": self.paused,
                "last_real_phase": self._last_real_phase,
                "energy_start": self._energy_start,
                "water_start": self._water_start,
                "idle_energy_value": self._idle_energy_value,
                "idle_energy_ts": self._idle_energy_ts,
                "completion_energy": self._completion_energy,
                "flap_times": self._flap_times,
            }
        )

    @callback
    def _notify_entities(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ---------------------------------------------------------- state handlers
    def _machine_state(self) -> str | None:
        """Current machine_state, or None if not configured / not readable."""
        if not self.machine_state_entity:
            return None
        st = self.hass.states.get(self.machine_state_entity)
        if st is None or st.state in UNAVAILABLE_STATES:
            return None
        return st.state

    def _load_active(self) -> bool:
        """Authoritative 'a load is running' check: a real job_state phase.

        This is the same clean signal the reliable legacy automations keyed off.
        The running and machine_state sensors flap badly on this washer, so they
        are deliberately NOT required for detection (machine_state is still used
        for the pause display). A flap shows as 'unavailable', never as a fake
        phase, so this stays false-positive safe.
        """
        job = self.hass.states.get(self.job_state_entity)
        if job is None or job.state not in REAL_PHASES:
            return False
        # 'finish' is the *end* of a cycle, not a start — never begin on it.
        if job.state == JOB_STATE_FINISH:
            return False
        # Don't (re)start on a frozen leftover phase: if the energy meter hasn't
        # moved since the last completion, no real load is running.
        energy = self._entity_float(self.energy_entity)
        if (
            energy is not None
            and self._completion_energy is not None
            and abs(energy - self._completion_energy) < 1e-6
        ):
            return False
        return True

    @callback
    def _evaluate_start(self) -> None:
        """Start tracking when job_state shows a real phase and we're not
        already tracking an active wash.

        Fires from job_state (primary), and harmlessly from running/machine and
        bot-ready. Allowed from idle *or* a finished-but-unclaimed load, so a
        new cycle supersedes a lingering done message.
        """
        if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            return
        if self._load_active():
            self.hass.async_create_task(self._async_start_session())

    @callback
    def _on_running(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is None:
            return
        if self.stage == STAGE_SELF_CLEAN:
            # Self-clean ends when the washer stops running.
            if new.state == "off":
                self._schedule_selfclean_end()
            return
        self._schedule_job_confirm()
        self._maybe_schedule_selfclean()

    @callback
    def _on_machine_state(self, event: Event) -> None:
        """Pause display (active load), self-clean lifecycle, start re-check."""
        new = event.data.get("new_state")
        if new is None:
            return
        new_s = new.state
        if self.stage in (STAGE_WASHING, STAGE_DRYING):
            if new_s == MACHINE_PAUSE and not self.paused:
                self.paused = True
                self.hass.async_create_task(self._async_render_active("paused"))
            elif new_s == MACHINE_RUN and self.paused:
                self.paused = False
                self.hass.async_create_task(self._async_render_active("resumed"))
        elif self.stage == STAGE_SELF_CLEAN:
            if new_s == MACHINE_STOP:
                self._schedule_selfclean_end()
        else:
            self._schedule_job_confirm()
            self._maybe_schedule_selfclean()

    @callback
    def _on_job_state(self, event: Event) -> None:
        """Record flaps, then (re)arm the confirm-debounce.

        We never act on a raw job_state change directly — a transient could be a
        flap. Instead we wait for the value to *persist* (``confirm_delay``,
        mirroring the proven automations' ``for: 30s``) and act on the settled
        value in :meth:`_async_job_confirmed`.
        """
        new = event.data.get("new_state")
        if new is None:
            return
        new_s = new.state
        old = event.data.get("old_state")
        old_s = old.state if old is not None else None

        # Connection health: record each transition INTO unavailable.
        if new_s == "unavailable" and old_s not in (None, "unavailable"):
            self._record_flap()

        # Ignore flap values outright (the `not_from: unavailable/unknown`).
        if new_s in UNAVAILABLE_STATES:
            return

        self._schedule_job_confirm()

    @callback
    def _schedule_job_confirm(self) -> None:
        """(Re)arm the confirm-debounce timer; collapses rapid changes."""
        if self._job_confirm_unsub is not None:
            self._job_confirm_unsub()
        delay = self.confirm_delay
        if delay <= 0:
            self._async_job_confirmed()
            return
        self._job_confirm_unsub = async_call_later(
            self.hass, delay, self._async_job_confirmed
        )

    @callback
    def _async_job_confirmed(self, _now=None) -> None:
        """Act on the *settled* job_state once it has persisted confirm_delay.

        Decisions key off the last confirmed real phase, so a flap landing on a
        transition (e.g. ``spin -> unavailable -> none``) can't miss the finish.
        """
        self._job_confirm_unsub = None
        st = self.hass.states.get(self.job_state_entity)
        if st is None:
            return
        job = st.state
        if job in UNAVAILABLE_STATES:
            return  # not settled to a real value yet

        # 'finish' is the real completion. Act on it now — do NOT wait for
        # 'none', which can lag by hours while the drum sits idle after the
        # cycle is actually done.
        if job == JOB_STATE_FINISH:
            self._last_real_phase = JOB_STATE_FINISH
            if self.stage in (STAGE_WASHING, STAGE_DRYING):
                self.hass.async_create_task(self._async_handle_finished())
            return

        if job in REAL_PHASES:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING):
                # Idle, or a previous finished-but-unclaimed load: a confirmed
                # real phase means a (new) load — start it (superseding any
                # lingering done message).
                self._log_corroboration(job)
                self._evaluate_start()
                return
            if (
                job == JOB_STATE_DRYING
                and self._last_real_phase not in (None, JOB_STATE_DRYING)
            ):
                self.hass.async_create_task(self._async_handle_drying())
            self._last_real_phase = job
        elif job == JOB_STATE_NONE:
            # Backup: a cycle that dropped straight to 'none' without 'finish'.
            if (
                self.stage in (STAGE_WASHING, STAGE_DRYING)
                and self._last_real_phase in REAL_PHASES
            ):
                self.hass.async_create_task(self._async_handle_finished())
            self._last_real_phase = None

    def _log_corroboration(self, job: str) -> None:
        """Diagnostic: note whether the other sensors agree at start time."""
        running = self.hass.states.get(self.running_entity)
        _LOGGER.debug(
            "Confirmed load (job=%s); corroboration running=%s machine=%s",
            job,
            running.state if running is not None else "n/a",
            self._machine_state() or "n/a",
        )

    # ---------------------------------------------------------- self-clean
    def _looks_like_selfclean(self) -> bool:
        """Washer is running but job_state never left 'none' (a self-clean)."""
        job = self.hass.states.get(self.job_state_entity)
        if job is None or job.state != JOB_STATE_NONE:
            return False
        running = self.hass.states.get(self.running_entity)
        running_on = running is not None and running.state == "on"
        if not (running_on or self._machine_state() == MACHINE_RUN):
            return False
        # Don't re-detect a self-clean on a frozen 'run' with no fresh energy use.
        energy = self._entity_float(self.energy_entity)
        if (
            energy is not None
            and self._completion_energy is not None
            and abs(energy - self._completion_energy) < 1e-6
        ):
            return False
        return True

    @callback
    def _maybe_schedule_selfclean(self) -> None:
        """Arm a one-shot self-clean check (no reschedule churn).

        Allowed from idle *or* a finished-but-claimed/unclaimed load, so a
        self-clean started after a previous load still gets detected (it has no
        job_state phase to supersede the done message the normal way).
        """
        if (
            self.stage not in (STAGE_IDLE, STAGE_DONE_WAITING)
            or self._selfclean_unsub is not None
        ):
            return
        if self._looks_like_selfclean():
            self._selfclean_unsub = async_call_later(
                self.hass, self.selfclean_delay, self._async_selfclean_confirm
            )

    @callback
    def _async_selfclean_confirm(self, _now=None) -> None:
        self._selfclean_unsub = None
        # Only a self-clean if no active wash is being tracked (a normal load
        # would have started a session via the job path) and still running with
        # job stuck at 'none'.
        if self.stage in (STAGE_IDLE, STAGE_DONE_WAITING) and self._looks_like_selfclean():
            self.hass.async_create_task(self._async_start_selfclean())

    @callback
    def _schedule_selfclean_end(self) -> None:
        if self._selfclean_unsub is not None:
            self._selfclean_unsub()
        self._selfclean_unsub = async_call_later(
            self.hass, self.confirm_delay, self._async_selfclean_end_confirm
        )

    @callback
    def _async_selfclean_end_confirm(self, _now=None) -> None:
        self._selfclean_unsub = None
        if self.stage != STAGE_SELF_CLEAN:
            return
        # Confirm it really stopped (not a flap): not running and not 'run'.
        running = self.hass.states.get(self.running_entity)
        running_on = running is not None and running.state == "on"
        if not running_on and self._machine_state() != MACHINE_RUN:
            self.hass.async_create_task(self._async_finish_selfclean())

    @callback
    def _record_flap(self) -> None:
        """Record a connection drop and refresh the health sensor."""
        now = dt_util.utcnow().timestamp()
        cutoff = now - 86400
        self._flap_times = [t for t in self._flap_times if t >= cutoff]
        self._flap_times.append(now)
        self._notify_entities()
        self.hass.async_create_task(self._async_save())

    def _entity_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        if st is None or st.state in UNAVAILABLE_STATES | {"", None}:
            return None
        try:
            return float(st.state)
        except (ValueError, TypeError):
            return None

    def _entity_unit(self, entity_id: str | None) -> str | None:
        st = self.hass.states.get(entity_id) if entity_id else None
        return st.attributes.get("unit_of_measurement") if st is not None else None

    async def _async_render_active(self, reason: str) -> None:
        """Re-render the live washing/drying message (e.g. on pause/resume)."""
        async with self._lock:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING) or not self.message_id:
                return
            try:
                await self.bot.async_edit(self.message_id, self.build_embed())
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to re-render active message (%s)", reason)
            await self._async_save()
            self._notify_entities()

    # ------------------------------------------------------- lifecycle actions
    async def _async_start_session(self) -> None:
        async with self._lock:
            # A wash already in progress wins. A previous *finished* load (still
            # showing its claim/unclaim message) is simply superseded by this one.
            if self.stage in (STAGE_WASHING, STAGE_DRYING):
                _LOGGER.debug("Start ignored; wash already active (stage=%s)", self.stage)
                return
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.message_id = None
            self.paused = self._machine_state() == MACHINE_PAUSE
            # Seed the last confirmed phase from the current job state so a
            # caught-up (already-drying) load still detects its finish.
            job = self.hass.states.get(self.job_state_entity)
            phase = job.state if job is not None else None
            self._last_real_phase = phase if phase in REAL_PHASES else None
            # Start directly in the Drying stage if caught already drying.
            self.stage = STAGE_DRYING if phase == JOB_STATE_DRYING else STAGE_WASHING
            # Already well into the cycle => a mid-cycle pickup ("in progress").
            self.catch_up = phase in MIDCYCLE_PHASES
            self._reset_energy_idle()
            # Capture meter baselines for the usage stat — only meaningful for a
            # load we see from the start (a catch-up has no true baseline).
            if self.catch_up:
                self._energy_start = self._water_start = None
            else:
                self._energy_start = self._entity_float(self.energy_entity)
                self._water_start = self._entity_float(self.water_entity)

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
            self.paused = False
            self._completion_energy = self._entity_float(self.energy_entity)
            # A pre-claim made during the wash carries through to completion.
            claimed = self.claimed_by != UNCLAIMED and self.claimed_by_id is not None
            self.waiting = not claimed
            embed = self.build_embed()
            view = ClaimView(self, show="unclaim" if claimed else "claim")
            try:
                if self.message_id:
                    await self.bot.async_edit(self.message_id, embed, view=view)
                if claimed and self.ping_claimant_on_complete:
                    # The one push per load: @mention the claimant.
                    await self.bot.async_send_ping(
                        f"<@{self.claimed_by_id}> 🧺 Your laundry's done — "
                        "don't forget the lint tray!"
                    )
                elif not claimed:
                    # Nobody claimed it: no @ping, but still post a fresh,
                    # push-silent 'done' message so the finished load is visible
                    # at the bottom of the channel, not just a buried in-place
                    # edit of the original card.
                    await self.bot.async_announce_done(embed)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update finished state")
            await self._async_save()
            self._notify_entities()

    async def _async_start_selfclean(self) -> None:
        async with self._lock:
            if self.stage not in (STAGE_IDLE, STAGE_DONE_WAITING):
                return
            self.stage = STAGE_SELF_CLEAN
            self.paused = False
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.message_id = None
            self._last_real_phase = None
            self._energy_start = self._entity_float(self.energy_entity)
            self._water_start = self._entity_float(self.water_entity)
            self._reset_energy_idle()
            # Visible, but never a claim button and never a ping.
            try:
                self.message_id = await self.bot.async_post(
                    self._selfclean_embed(done=False),
                    view=None,
                    content=None,
                    silent=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to post self-clean start message")
                self.stage = STAGE_IDLE
                return
            self._start_eta_timer()
            await self._async_save()
            self._notify_entities()

    async def _async_finish_selfclean(self) -> None:
        async with self._lock:
            if self.stage != STAGE_SELF_CLEAN:
                return
            self._stop_eta_timer()
            self._completion_energy = self._entity_float(self.energy_entity)
            embed = self._selfclean_embed(done=True)
            try:
                if self.message_id:
                    await self.bot.async_edit(self.message_id, embed, view=None)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update self-clean finished state")
            self.stage = STAGE_IDLE
            self.message_id = None
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
        # Energy-idle completion: a cycle whose kWh has gone flat is done, even
        # if job_state/machine_state are frozen on this washer.
        if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            if self._energy_idle_done():
                _LOGGER.debug("Energy flat past timeout — completing %s", self.stage)
                if self.stage == STAGE_SELF_CLEAN:
                    await self._async_finish_selfclean()
                else:
                    await self._async_handle_finished()
                return
        if not self.message_id:
            return
        if self.stage in (STAGE_WASHING, STAGE_DRYING):
            embed = self.build_embed()
        elif self.stage == STAGE_SELF_CLEAN:
            embed = self._selfclean_embed(done=False)
        else:
            return
        try:
            await self.bot.async_edit(self.message_id, embed)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ETA edit failed")

    def _reset_energy_idle(self) -> None:
        self._idle_energy_value = self._entity_float(self.energy_entity)
        self._idle_energy_ts = dt_util.utcnow().timestamp()

    def _energy_idle_done(self) -> bool:
        """True once the energy meter has been flat past the idle timeout."""
        energy = self._entity_float(self.energy_entity)
        if energy is None:
            return False
        now = dt_util.utcnow().timestamp()
        if self._idle_energy_value is None or energy > self._idle_energy_value + 1e-6:
            self._idle_energy_value = energy
            self._idle_energy_ts = now
            return False
        if self._idle_energy_ts is None:
            self._idle_energy_ts = now
            return False
        return (now - self._idle_energy_ts) >= self.energy_idle_timeout

    def _selfclean_embed(self, *, done: bool) -> discord.Embed:
        """Embed for a self-clean cycle — no claim button, no ping."""
        if done:
            embed = discord.Embed(
                title="🧼 Self-clean finished",
                description="The drum is clean.",
                color=_COLOR_DONE,
            )
            usage = self._usage_text()
            if usage:
                embed.add_field(name="This cycle used", value=usage, inline=False)
        else:
            embed = discord.Embed(
                title="🧼 Self-clean running",
                description="The washer is running a self-clean (drum clean).",
                color=_COLOR_SELFCLEAN,
            )
            embed.add_field(
                name="Estimated finish", value=self._eta_text(), inline=False
            )
        embed.set_footer(text=_FOOTER)
        embed.timestamp = dt_util.utcnow()
        return embed

    # ------------------------------------------------------------------ embeds
    def _current_eta(self) -> datetime | None:
        """Parsed ETA target, holding the last-good value through a flap.

        If the completion sensor is unavailable, keep returning the last known
        ETA for up to the availability grace window so a connection blip never
        flickers the embed to 'updating…'.
        """
        now = dt_util.utcnow().timestamp()
        state = self.hass.states.get(self.eta_entity)
        if state is not None and state.state not in UNAVAILABLE_STATES | {"", None}:
            target = dt_util.parse_datetime(state.state)
            if target is not None:
                if target.tzinfo is None:
                    target = dt_util.as_utc(target)
                self._eta_cache = (target, now)
                return target
        # Unavailable/unparseable: hold the cached ETA within the grace window.
        if self._eta_cache is not None:
            cached, seen = self._eta_cache
            if now - seen <= self.availability_grace:
                return cached
        return None

    def _eta_text(self) -> str:
        target = self._current_eta()
        if target is None:
            return "ETA updating…"
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
            desc = "The washer is running. Tap **Claim** to call dibs."
            if self.paused:
                desc = "⏸ **Paused** — the cycle is on hold.\n" + desc
            embed = discord.Embed(
                title=(
                    "⏸ Laundry paused"
                    if self.paused
                    else "🫧 Laundry in progress"
                    if self.catch_up
                    else "🫧 Laundry started"
                ),
                description=desc,
                color=_COLOR_WASHING,
            )
            self._add_progress_and_eta(embed)
            self._add_claimant(embed)
        elif self.stage == STAGE_DRYING:
            desc = "Pull out anything you don't want dried!"
            if self.paused:
                desc = "⏸ **Paused** — the cycle is on hold.\n" + desc
            embed = discord.Embed(
                title="⏸ Drying paused" if self.paused else "🌀 Drying",
                description=desc,
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
            usage = self._usage_text()
            if usage:
                embed.add_field(name="This load used", value=usage, inline=False)
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

    def _usage_text(self) -> str | None:
        """Energy/water used this load (meter delta since start), or None."""
        parts: list[str] = []
        if self._energy_start is not None:
            end = self._entity_float(self.energy_entity)
            if end is not None:
                used = end - self._energy_start
                if used < 0:  # meter reset during the cycle
                    used = end
                parts.append(f"⚡ {used:.2f} kWh")
        if self._water_start is not None:
            end = self._entity_float(self.water_entity)
            if end is not None:
                used = end - self._water_start
                if used < 0:
                    used = end
                unit = self._entity_unit(self.water_entity) or "L"
                parts.append(f"💧 {used:.0f} {unit}")
        return " · ".join(parts) if parts else None
