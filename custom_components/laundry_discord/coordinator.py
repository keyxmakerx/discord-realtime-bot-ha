"""Session state machine for the Laundry Discord Bot.

Watches the washer entities, drives the Discord bot (one embed per load), and
mirrors the lifecycle into HA entities. Discord work is funnelled through a
lock, and every bot call is wrapped so a failure logs rather than crashing HA.
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
    CONF_ENERGY_LOAD_JUMP,
    CONF_ANNOUNCE_FREE,
    CONF_EMPTY_REMINDER,
    CONF_HANDOFF_FALLBACK,
    CONF_JOB_STATE_ENTITY,
    CONF_MACHINE_STATE_ENTITY,
    CONF_PING_CLAIMANT_ON_COMPLETE,
    CONF_QUEUE_EXPIRY,
    CONF_RUNNING_ENTITY,
    CONF_SHOW_ASSISTANT,
    CONF_WATER_ENTITY,
    CONF_WRINKLE_ENTITY,
    DEFAULT_AVAILABILITY_GRACE,
    DEFAULT_CONFIRM_DELAY,
    DEFAULT_ENERGY_ENTITY,
    DEFAULT_ENERGY_IDLE,
    DEFAULT_ENERGY_LOAD_JUMP,
    DEFAULT_ETA_INTERVAL,
    DEFAULT_ANNOUNCE_FREE,
    DEFAULT_EMPTY_REMINDER,
    DEFAULT_HANDOFF_FALLBACK,
    DEFAULT_MACHINE_STATE_ENTITY,
    DEFAULT_PING_CLAIMANT_ON_COMPLETE,
    DEFAULT_QUEUE_EXPIRY,
    DEFAULT_SHOW_ASSISTANT,
    DEFAULT_WATER_ENTITY,
    DEFAULT_WRINKLE_ENTITY,
    MACHINE_PAUSE,
    MACHINE_RUN,
    MACHINE_STOP,
    MAX_SESSION_MINUTES,
    OFFLINE_COMPLETE_GRACE_MINUTES,
    OFFLINE_NOTICE_MINUTES,
    MIDCYCLE_PHASES,
    PROGRESS_PHASES,
    UNAVAILABLE_STATES,
    JOB_STATE_DRYING,
    JOB_STATE_FINISH,
    JOB_STATE_NONE,
    REAL_PHASES,
    SIGNAL_LOAD_CLAIMED,
    SIGNAL_UPDATE,
    SIGNAL_WASHER_FREE,
    STAGE_DONE_WAITING,
    STAGE_DRYING,
    STAGE_IDLE,
    STAGE_SELF_CLEAN,
    STAGE_WASHING,
    STORAGE_KEY,
    STORAGE_VERSION,
    UNCLAIMED,
)
from . import cancel as cancel_mod
from . import diagnose as diagnose_mod
from . import people as people_mod
from . import queue
from .assistant import LaundryAssistant
from .detect import (
    EV_FINISHED,
    EV_STARTED,
    RUN_ACTIVE,
    RUN_IDLE,
    EnergyDetector,
    offline_completion_due,
    session_too_long,
)
from .discord_bot import ClaimView, DiscordBot, view_for

_LOGGER = logging.getLogger(__name__)

# Claimed (amber) and idle (grey) are kept visually distinct on purpose.
_COLOR_WASHING = 0x3498DB
_COLOR_DRYING = 0xE67E22
_COLOR_DONE = 0x2ECC71
_COLOR_CLAIMED = 0xF1C40F  # amber — has an owner, not done
_COLOR_IDLE = 0x95A5A6  # grey — nothing running, nobody waited on
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
        # One-way dependency: never touches session state; gets the entry, not `self`.
        self.assistant = LaundryAssistant(hass, self.bot, entry)

        # Session state (persisted).
        self.stage: str = STAGE_IDLE
        self.waiting: bool = False
        self.claimed_by: str = UNCLAIMED
        self.claimed_by_id: int | None = None
        self.quiet: bool = False  # named in plain text at completion, not @mentioned
        # FIFO "I'm next" entries {"id", "name", "ts"}; see queue.py.
        self.queue: list[dict] = []
        # True once the claimant confirms the drum is cleared; handoff waits for this.
        self.emptied: bool = False
        # Who the handoff ping went to, and whether it was the hedged backstop;
        # needed because the handoff pops the queue head (keeps "Next up" accurate).
        self.handoff_name: str | None = None
        self.handoff_hedged: bool = False
        # Whether this load's claimant has had their one empty-it reminder.
        self.empty_reminded: bool = False
        self.message_id: int | None = None
        self.catch_up: bool = False  # picked up mid-cycle, not at the off->on start
        self.paused: bool = False  # machine_state reports the load paused mid-cycle
        self.cancelled: bool = False  # ended by a stop; gates wording + habit-model logging
        # Last confirmed real job phase; enrichment only (wash->dry display).
        self._last_real_phase: str | None = None
        # Energy/water baselines at session start (None if not measurable, e.g. catch-up).
        self._energy_start: float | None = None
        self._water_start: float | None = None
        # Gates ETA freshness and the max-session net; None when no load is tracked.
        self._session_started_ts: float | None = None
        # First-unavailable time, last ETA seen, and whether completion was offline.
        self._offline_since: float | None = None
        self._last_eta_ts: float | None = None
        self._offline_unverified: bool = False
        # Single source of truth for start/finish; job_state/machine_state only
        # accelerate or enrich it, never override it.
        self._detector = EnergyDetector(
            start_jump=self.energy_load_jump,
            idle_timeout=float(self.energy_idle_timeout),
        )
        self._flap_times: list[float] = []  # unavailable transitions; rolling 24h window

        self._eta_unsub = None
        self._unsubs: list = []
        self._lock = asyncio.Lock()
        self._restored = False
        self._eta_cache: tuple[datetime, float] | None = None  # last-good ETA, flap hold
        self._job_confirm_unsub = None  # job_state confirm-debounce (`for: 30s` equivalent)
        # Whether the debounce was armed by a value coming from `unavailable` (a cloud
        # reconnect, not the machine); suppresses the job fast-path once.
        self._job_from_flap = False
        # When the last unavailable recovery happened. Kept separately from the flag
        # above, which an intermediate value or attribute churn can launder or stomp.
        self._flap_recovery_ts = None
        # Stop-confirm timer, separate from the job_state one: both can be in flight
        # together at load end, and neither may disarm the other.
        self._stop_confirm_unsub = None
        self._selfclean_unsub = None  # self-clean detect/end timer
        self._handoff_unsub = None  # handoff-fallback timer (nobody tapped "Emptied it")
        self._empty_unsub = None  # empty-it reminder timer
        self._tasks: set[asyncio.Task] = set()  # in-flight tasks; see _create_task

    def _create_task(self, coro) -> None:
        """Schedule session work and track it so shutdown can cancel it.

        Use this, not `hass.async_create_task`: an untracked task can survive
        shutdown, holding the lock and a closed client forever. Also keeps a
        strong reference, since asyncio holds tasks only weakly.
        """
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    @callback
    def _task_done(self, task) -> None:
        """Drop a finished task and surface what it raised.

        A `TimeoutError` (gateway never ready) is expected and logged at debug
        only; anything else is re-raised into HA's handler so real bugs surface.
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, TimeoutError):
            _LOGGER.debug("Session work abandoned: gateway never became ready")
            return
        raise exc

    async def _async_cancel_tasks(self) -> None:
        """Cancel every in-flight task and wait for it to actually stop.

        Awaited so nothing is left holding the session lock or a closing client
        when this returns; relies on bot calls being timeout-bounded to unwind.
        """
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

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
    def wrinkle_entity(self) -> str:
        """Wrinkle-prevent 'active' binary_sensor (optional but recommended)."""
        return str(self._cfg.get(CONF_WRINKLE_ENTITY) or DEFAULT_WRINKLE_ENTITY)

    @property
    def eta_interval(self) -> int:
        return int(self._cfg.get(CONF_ETA_INTERVAL, DEFAULT_ETA_INTERVAL))

    @property
    def confirm_delay(self) -> int:
        """Seconds a job_state value must persist before we act on it."""
        return int(self._cfg.get(CONF_CONFIRM_DELAY, DEFAULT_CONFIRM_DELAY))

    @property
    def energy_idle_timeout(self) -> int:
        """Seconds the energy meter may be flat before a cycle counts as done."""
        return int(self._cfg.get(CONF_ENERGY_IDLE, DEFAULT_ENERGY_IDLE)) * 60

    @property
    def max_session(self) -> int:
        """Seconds before a tracked load is force-finished as a safety net."""
        return MAX_SESSION_MINUTES * 60

    @property
    def offline_after(self) -> int:
        """Seconds the washer must be offline before we flag it / offline-finish."""
        return OFFLINE_NOTICE_MINUTES * 60

    @property
    def offline_complete_grace(self) -> int:
        """Seconds past the last-known ETA before an offline completion fires."""
        return OFFLINE_COMPLETE_GRACE_MINUTES * 60

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

    @property
    def handoff_fallback(self) -> int:
        """Seconds after a load finishes before the queue head is pinged anyway.

        Backstop for when the claimant forgets "Emptied it"; hedged wording
        since nobody confirmed. 0 disables the backstop (the tap still works).
        """
        return (
            int(self._cfg.get(CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK)) * 60
        )

    @property
    def empty_reminder(self) -> int:
        """Seconds after a claimed load finishes before its claimant is
        reminded to empty it. 0 disables."""
        return int(self._cfg.get(CONF_EMPTY_REMINDER, DEFAULT_EMPTY_REMINDER)) * 60

    @property
    def announce_free(self) -> bool:
        """Whether a handoff also posts a push-silent "washer's free" line."""
        return bool(self._cfg.get(CONF_ANNOUNCE_FREE, DEFAULT_ANNOUNCE_FREE))

    @property
    def queue_expiry(self) -> int:
        """Seconds an "I'm next" entry survives before it ages out of the line."""
        return int(self._cfg.get(CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY)) * 3600

    @property
    def show_assistant(self) -> bool:
        """Whether the 🤖 button is on the card.

        Off hides the button only; prefs already set through it keep working.
        """
        return bool(self._cfg.get(CONF_SHOW_ASSISTANT, DEFAULT_SHOW_ASSISTANT))

    @property
    def energy_load_jump(self) -> float:
        """kWh rise in one sample that marks a load run while job_state was dark."""
        return float(
            self._cfg.get(CONF_ENERGY_LOAD_JUMP, DEFAULT_ENERGY_LOAD_JUMP)
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
        await self.assistant.async_load()
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
        # Energy is the primary detection signal; react to every reading.
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.energy_entity], self._on_energy
            )
        )
        # 5-minute heartbeat; see _async_health_tick.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_health_tick, timedelta(minutes=5)
            )
        )

    @callback
    def _async_health_tick(self, now) -> None:
        # Catches an offline load's energy jump while idle, drives time-based
        # completion with no state events, and keeps the health sensor fresh.
        self._feed_detector()
        self._check_time_completion()
        self.refresh_health()
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
        """Tear down listeners, timers, in-flight work and the connection.

        Order matters: listeners/timers first, then cancel in-flight work, then
        close the client. Closing first leaves a task waiting on the gateway
        unwakeable forever — `Client.close()` clears the ready event.
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._stop_eta_timer()
        if self._job_confirm_unsub is not None:
            self._job_confirm_unsub()
            self._job_confirm_unsub = None
        self._cancel_stop_confirm()
        if self._selfclean_unsub is not None:
            self._selfclean_unsub()
            self._selfclean_unsub = None
        self._cancel_handoff_timer()
        self._cancel_empty_timer()
        await self._async_cancel_tasks()
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
            # The washer may be mid-cycle, or a load may have run entirely while HA
            # was down; one feed covers both. Only catch-up fires here (`allow_early`
            # off): a restored/reloaded `job_state` is often a replayed phase, and
            # trusting it would mint a phantom load. Catch-up needs the meter to have
            # moved since idle, so it can't fire on that shape.
            self._feed_detector(allow_catchup=True)
            # A claimed done_waiting load's handoff backstop is re-armed here (armed
            # in exactly one place: the completion that reached done_waiting). The
            # delay restarts from now, not the original completion.
            if (
                self.stage == STAGE_DONE_WAITING
                and not self.emptied
                and self.claimed_by_id is not None
            ):
                self._arm_handoff_timer()
                if not self.empty_reminded:
                    self._arm_empty_timer()
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
        self.quiet = data.get("quiet", False)
        # .get() with defaults: a store written before the queue shipped has
        # neither key, and an upgrade must not KeyError on the first load.
        self.queue = list(data.get("queue") or [])
        self.emptied = data.get("emptied", False)
        self.handoff_name = data.get("handoff_name")
        self.handoff_hedged = data.get("handoff_hedged", False)
        self.empty_reminded = data.get("empty_reminded", False)
        self.message_id = data.get("message_id")
        self.catch_up = data.get("catch_up", False)
        self.paused = data.get("paused", False)
        self.cancelled = data.get("cancelled", False)
        self._last_real_phase = data.get("last_real_phase")
        self._energy_start = data.get("energy_start")
        self._water_start = data.get("water_start")
        self._session_started_ts = data.get("session_started_ts")
        self._offline_since = data.get("offline_since")
        self._last_eta_ts = data.get("last_eta_ts")
        self._offline_unverified = data.get("offline_unverified", False)
        # Restore the liveness detector so a load in progress (or its baseline)
        # survives a restart and a load that ran during downtime is caught.
        det = data.get("detector") or {}
        self._detector.last_energy = det.get("last_energy")
        self._detector.last_rise_ts = det.get("last_rise_ts")
        self._detector.idle_energy = det.get("idle_energy")
        # Force the detector's phase to match the restored session stage so a
        # divergent persist can never strand it (active detector / idle session).
        self._detector.phase = (
            RUN_ACTIVE
            if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN)
            else RUN_IDLE
        )
        self._flap_times = list(data.get("flap_times", []))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "stage": self.stage,
                "waiting": self.waiting,
                "claimed_by": self.claimed_by,
                "claimed_by_id": self.claimed_by_id,
                "quiet": self.quiet,
                "queue": self.queue,
                "emptied": self.emptied,
                "handoff_name": self.handoff_name,
                "handoff_hedged": self.handoff_hedged,
                "empty_reminded": self.empty_reminded,
                "message_id": self.message_id,
                "catch_up": self.catch_up,
                "paused": self.paused,
                "cancelled": self.cancelled,
                "last_real_phase": self._last_real_phase,
                "energy_start": self._energy_start,
                "water_start": self._water_start,
                "session_started_ts": self._session_started_ts,
                "offline_since": self._offline_since,
                "last_eta_ts": self._last_eta_ts,
                "offline_unverified": self._offline_unverified,
                "detector": {
                    "phase": self._detector.phase,
                    "last_energy": self._detector.last_energy,
                    "last_rise_ts": self._detector.last_rise_ts,
                    "idle_energy": self._detector.idle_energy,
                },
                "flap_times": self._flap_times,
            }
        )

    @callback
    def _notify_entities(self) -> None:
        # Publishing the planner's running-window here, not at each of the five
        # session start/end sites, means none of them can forget it.
        self._publish_running()
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.entry.entry_id}")

    # ---------------------------------------------------------- state handlers
    def _machine_state(self) -> str | None:
        """Current machine_state, or None if not configured / not readable."""
        if not self.machine_state_entity:
            return None
        st = self.hass.states.get(self.machine_state_entity)
        if st is None or st.state in UNAVAILABLE_STATES:
            return None
        return st.state

    def _running_state(self) -> bool | None:
        """Whether the running sensor says on/off, or None when unreadable.

        Three-valued on purpose: the cancel path must not read "unavailable" as
        "off", or a cloud drop would end a live wash.
        """
        st = self.hass.states.get(self.running_entity) if self.running_entity else None
        if st is None or st.state in UNAVAILABLE_STATES | {"", None}:
            return None
        return st.state == "on"

    def _job_phase(self) -> str | None:
        """Current job_state value, or None when unavailable/unknown."""
        st = self.hass.states.get(self.job_state_entity)
        if st is None or st.state in UNAVAILABLE_STATES:
            return None
        return st.state

    def _wrinkle_active(self) -> bool:
        """True if the wrinkle-prevent sensor says the drum is tumbling.

        Wrinkle-prevent nudges the energy meter for hours *after* a cycle; when
        this is on we tell the detector not to treat those nudges as load energy.
        """
        st = self.hass.states.get(self.wrinkle_entity) if self.wrinkle_entity else None
        return st is not None and st.state == "on"

    def _washer_online(self) -> bool:
        """True if any key washer entity is reporting a real value.

        When the device/cloud drops, all its entities go ``unavailable`` together
        (observed), so 'none readable' = the washer is offline.
        """
        for ent in (
            self.job_state_entity,
            self.running_entity,
            self.energy_entity,
            self.eta_entity,
        ):
            st = self.hass.states.get(ent) if ent else None
            if st is not None and st.state not in UNAVAILABLE_STATES | {"", None}:
                return True
        return False

    @callback
    def _track_offline(self) -> None:
        """Maintain ``_offline_since`` from current availability (called per feed)."""
        if self._washer_online():
            self._offline_since = None
        elif self._offline_since is None:
            self._offline_since = dt_util.utcnow().timestamp()

    def _offline_for(self) -> float:
        """Seconds the washer has been continuously offline (0 if online)."""
        if self._offline_since is None:
            return 0.0
        return dt_util.utcnow().timestamp() - self._offline_since

    def _meter_reported_this_session(self) -> bool:
        """Has the energy meter produced a reading since this load began?

        Guards the flat-energy backstop against a meter frozen since before
        the session started. Uses `last_changed`, not `last_updated`: a
        same-value republish isn't the meter moving. True with no session.
        """
        if self._session_started_ts is None:
            return True
        if not self.energy_entity:
            return False
        st = self.hass.states.get(self.energy_entity)
        if st is None or st.state in UNAVAILABLE_STATES | {"", None}:
            return False
        return st.last_changed.timestamp() >= self._session_started_ts

    def _eta_status(self) -> tuple[bool, bool]:
        """(has_eta, eta_passed) for the washer's own completion estimate.

        `has_eta` is False for a value frozen from a previous load, so a
        stale ETA can't drive completion; also False with no load tracked or
        an unavailable ETA. Snapshots the estimate into `_last_eta_ts`.
        """
        if self._session_started_ts is None:
            return (False, False)
        st = self.hass.states.get(self.eta_entity)
        if st is None or st.state in UNAVAILABLE_STATES | {"", None}:
            return (False, False)
        target = dt_util.parse_datetime(st.state)
        if target is None:
            return (False, False)
        if target.tzinfo is None:
            target = dt_util.as_utc(target)
        # Margin covers confirm_delay, since the session starts that long after
        # the washer sets the estimate; a value from a previous load predates
        # it by much more than this.
        margin = self.confirm_delay + 300
        if st.last_changed.timestamp() < self._session_started_ts - margin:
            return (False, False)  # stale estimate frozen from a previous load
        self._last_eta_ts = target.timestamp()  # remember for offline completion
        return (True, dt_util.utcnow() >= target)

    def refresh_health(self) -> None:
        """Recompute the health findings and push them to the sensor.

        Cached, not computed on read: the recorder logs history on every
        attribute change, and a recomputed age-in-minutes finding would write
        constantly. Never raises — a crash here is worse than a stale check.
        """
        try:
            snap = self.diagnostic_snapshot()
            findings = diagnose_mod.check(
                snap["session"],
                dt_util.utcnow().timestamp(),
                watched=snap["watched"],
                max_session_minutes=snap["config"]["max_session_minutes"],
            )
            self._health = {
                "severity": diagnose_mod.worst_severity(findings),
                "summary": diagnose_mod.summarise(findings),
                "findings": findings,
            }
        except Exception:  # noqa: BLE001 - see the docstring
            _LOGGER.exception("Health check failed")
            self._health = {
                "severity": "unknown",
                "summary": "the health check itself failed — see the log",
                "findings": [],
            }

    @property
    def health(self) -> dict:
        """Last computed health, or a neutral placeholder before the first tick."""
        return getattr(self, "_health", None) or {
            "severity": "unknown",
            "summary": "not checked yet",
            "findings": [],
        }

    def diagnostic_snapshot(self) -> dict:
        """Everything the health check needs, gathered in one place.

        Reads live objects, not the Store, which lags one save behind. Only
        `watched` is external to the integration, so it's what can catch the
        bot and the machine disagreeing.
        """
        return {
            "session": {
                "stage": self.stage,
                "waiting": self.waiting,
                "claimed_by": self.claimed_by,
                "claimed_by_id": self.claimed_by_id,
                "quiet": self.quiet,
                "queue": list(self.queue),
                "emptied": self.emptied,
                "message_id": self.message_id,
                "catch_up": self.catch_up,
                "paused": self.paused,
                "cancelled": self.cancelled,
                "last_real_phase": self._last_real_phase,
                "energy_start": self._energy_start,
                "session_started_ts": self._session_started_ts,
                "offline_since": self._offline_since,
                "last_eta_ts": self._last_eta_ts,
                "offline_unverified": self._offline_unverified,
                "detector": {
                    "phase": self._detector.phase,
                    "last_energy": self._detector.last_energy,
                    "last_rise_ts": self._detector.last_rise_ts,
                    "idle_energy": self._detector.idle_energy,
                },
                "flap_times": list(self._flap_times),
            },
            "watched": {
                "running": self._entity_state(self.running_entity),
                "machine_state": self._entity_state(self.machine_state_entity),
                "job_state": self._entity_state(self.job_state_entity),
                "eta": self._entity_state(self.eta_entity),
                "energy": self._entity_state(self.energy_entity),
                # For observation only, never detection: honest value, but its
                # timing arrives through the same laggy cloud as everything else.
                "water": self._entity_state(self.water_entity),
            },
            "config": {
                "confirm_delay": self.confirm_delay,
                # Read through the real property, not a guessed attribute name.
                "energy_idle_s": self.energy_idle_timeout,
                "max_session_minutes": MAX_SESSION_MINUTES,
            },
        }

    def _entity_state(self, entity_id):
        """One watched entity's raw state, or None when unset/missing.

        None covers both "not configured" and "absent"; the health check
        treats them the same, so a typo in an entity id silently disables the
        guard rather than erroring.
        """
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return None if st is None else st.state

    @callback
    def _publish_running(self) -> None:
        """Tell the planner the live load's window, so its grid can draw ``*``.

        Pushed, never pulled, so the planner still opens if detection is
        wedged. Called from every transition and the 5-minute tick, so the
        worst case is a stale grid, never a stuck ``*``.
        """
        running = self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN)
        self.assistant.note_running(
            self._session_started_ts if running else None,
            self._last_eta_ts if running else None,
        )

    @callback
    def _check_time_completion(self, _now=None) -> None:
        """Time-based completions on the periodic ticks.

        - Offline completion: washer unavailable a long time AND its
          last-known ETA has passed (+grace) — finish, flagged unverified.
        - Max-session: absolute safety net so a stuck session can't live on.

        Both apply to a self-clean too: its only other endings (the energy
        detector, `_schedule_selfclean_end`) go silent during an outage,
        wedging the session and blocking every load after it.
        """
        # Before the stage guard: idle is exactly when the planner needs telling.
        self._publish_running()
        if self.stage not in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            return
        # Self-clean has no claimant/queue/card, so it closes through its own
        # finisher; `_async_handle_finished` refuses any stage but wash/dry.
        selfclean = self.stage == STAGE_SELF_CLEAN
        now = dt_util.utcnow().timestamp()
        if offline_completion_due(
            offline_since=self._offline_since,
            last_eta_ts=self._last_eta_ts,
            now=now,
            offline_after=float(self.offline_after),
            eta_grace=float(self.offline_complete_grace),
        ):
            _LOGGER.debug("Offline completion (washer unavailable, ETA passed)")
            if selfclean:
                self._create_task(self._async_finish_selfclean())
                return
            # Only a load's card hedges wording; self-clean says "clean" either way.
            self._offline_unverified = True
            self._create_task(self._async_handle_finished())
            return
        if session_too_long(self._session_started_ts, now, float(self.max_session)):
            _LOGGER.debug("Max-session safety completion (stage=%s)", self.stage)
            self._create_task(
                self._async_finish_selfclean()
                if selfclean
                else self._async_handle_finished()
            )

    @callback
    def _feed_detector(
        self, _now=None, *, allow_early: bool = False, allow_catchup: bool = False
    ) -> None:
        """Drive the energy-primary liveness core from current sensor readings.

        Called from every signal; the detector dedupes, so calling this often
        is safe. The two fast-paths fail differently:

        * `allow_early` — trust an early phase (or `finish`) on the cloud's word
          alone; only the debounced, non-flap confirm may set it, since a
          replayed phase would mint (or end) a phantom load.
        * `allow_catchup` — trust a mid-cycle phase only once the meter has
          moved since idle (detect.py), safe even for a flap-arrived value.
        """
        self._track_offline()
        phase = self._job_phase()
        is_early = (
            allow_early
            and phase in REAL_PHASES
            and phase != JOB_STATE_FINISH
            and phase not in MIDCYCLE_PHASES
        )
        is_catchup = (
            allow_catchup
            and phase in MIDCYCLE_PHASES
            and phase != JOB_STATE_FINISH
        )
        is_real = is_early or is_catchup
        is_finish = allow_early and phase == JOB_STATE_FINISH
        has_eta, eta_passed = self._eta_status()
        before = (self._detector.last_energy, self._detector.last_rise_ts)
        ev = self._detector.observe(
            dt_util.utcnow().timestamp(),
            self._entity_float(self.energy_entity),
            job_is_early=is_early,
            job_is_real=is_real,
            job_is_finish=is_finish,
            wrinkle_active=self._wrinkle_active(),
            has_eta=has_eta,
            eta_passed=eta_passed,
            machine_idle=self._machine_state() == MACHINE_STOP,
            meter_reporting=self._meter_reported_this_session(),
        )
        if ev == EV_STARTED:
            self._on_detector_started(phase)
        elif ev == EV_FINISHED:
            self._on_detector_finished()
        elif (self._detector.last_energy, self._detector.last_rise_ts) != before:
            # No transition, but the baseline advanced — persist it.
            self._create_task(self._async_save())

    @callback
    def _on_detector_started(self, phase: str | None) -> None:
        """A load began. Decide normal vs self-clean and open a session."""
        if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            return  # already tracking (shouldn't happen; detector is deduped)
        # A real phase: normal load. No phase but running with job stuck at
        # 'none': self-clean. Otherwise: an offline catch-up load.
        if phase in REAL_PHASES:
            _LOGGER.debug("Detector: load started (job=%s)", phase)
            self._create_task(self._async_start_session())
        elif self._looks_like_selfclean():
            _LOGGER.debug("Detector: self-clean started")
            self._create_task(self._async_start_selfclean())
        else:
            _LOGGER.debug("Detector: offline load started (job dark)")
            self._create_task(self._async_start_session(offline=True))

    @callback
    def _on_detector_finished(self) -> None:
        """The active cycle's energy went flat (or job hit 'finish')."""
        if self.stage == STAGE_SELF_CLEAN:
            self._create_task(self._async_finish_selfclean())
        elif self.stage in (STAGE_WASHING, STAGE_DRYING):
            self._create_task(self._async_handle_finished())

    @callback
    def _on_energy(self, event: Event) -> None:
        """Energy is the primary signal — feed every reading to the detector."""
        if event.data.get("new_state") is None:
            return
        self._feed_detector()

    @callback
    def _on_running(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is None:
            return
        # Faster than the energy idle-timeout backstop.
        if self.stage == STAGE_SELF_CLEAN and new.state == "off":
            self._schedule_selfclean_end()
            return
        old = event.data.get("old_state")
        if self.stage in (STAGE_WASHING, STAGE_DRYING) and (
            cancel_mod.running_off_signalled(
                old.state if old is not None else None,
                new.state,
                # This sensor can't tell pause from stop; machine_state can veto
                # it. Without that entity configured, nothing vetoes a stop alone.
                machine_state_configured=bool(self.machine_state_entity),
            )
        ):
            # Corroborating trigger — faster on units where running moves first.
            self._schedule_stop_confirm()
        self._feed_detector()

    @callback
    def _on_machine_state(self, event: Event) -> None:
        """Pause display for an active load; prompt self-clean end; feed detector."""
        new = event.data.get("new_state")
        if new is None:
            return
        new_s = new.state
        if self.stage in (STAGE_WASHING, STAGE_DRYING):
            if new_s == MACHINE_PAUSE and not self.paused:
                self.paused = True
                self._create_task(self._async_render_active("paused"))
            elif new_s == MACHINE_RUN and self.paused:
                self.paused = False
                self._create_task(self._async_render_active("resumed"))
            elif new_s == MACHINE_STOP and self.paused:
                # pause -> stop is the ordinary cancel on combo units. Clear the
                # flag here so it can't outlive the session; no "resumed" render,
                # the stop confirm below decides what the card says.
                self.paused = False
            old = event.data.get("old_state")
            if cancel_mod.machine_stop_signalled(
                old.state if old is not None else None, new_s
            ):
                # The only route from "a human ended the cycle" to a finished load.
                self._schedule_stop_confirm()
        elif self.stage == STAGE_SELF_CLEAN:
            if new_s == MACHINE_STOP:
                self._schedule_selfclean_end()
        else:
            self._feed_detector()

    @callback
    def _on_job_state(self, event: Event) -> None:
        """Record connection flaps, then (re)arm the confirm-debounce.

        job_state no longer decides start/stop (the energy meter does); the
        debounced, settled value only drives enrichment: the fast-start
        accelerant, the wash->dry display, and the fast 'finish' completion.
        """
        new = event.data.get("new_state")
        if new is None:
            return
        new_s = new.state
        old = event.data.get("old_state")
        old_s = old.state if old is not None else None

        # An attribute-only republish is not a transition. Load-bearing, not
        # tidiness: without this return, a later same-value update re-arms the
        # debounce and can hand a replayed phase the fast start after all.
        if old_s == new_s:
            return

        # Connection health: record each transition INTO unavailable.
        if new_s == "unavailable" and old_s not in (None, "unavailable"):
            self._record_flap()

        # Ignore values that are themselves a flap.
        if new_s in UNAVAILABLE_STATES:
            return

        # A value arriving FROM `unavailable` is a cloud reconnect replaying the
        # last phase, not a new load — trusting it would mint (or end) a session
        # that never happened. See cancel.is_flap.
        self._job_from_flap = cancel_mod.is_flap(old_s)
        # The flag alone can be laundered by an intermediate value
        # (`unavailable -> none -> wash`), so also keep a short recovery window.
        if self._job_from_flap:
            self._flap_recovery_ts = dt_util.utcnow().timestamp()
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
        """Act on the settled job_state: feed the detector, drive enrichment.

        Start/finish decisions live in the detector (energy-primary); this only
        feeds the early/finish accelerants and flips the embed to 'Drying' on a
        confirmed mid-load wash->dry transition.
        """
        self._job_confirm_unsub = None
        from_flap = self._job_from_flap
        self._job_from_flap = False
        # Window must outlast the debounce, or a launder chain whose last hop
        # re-armed the timer would settle just past its own recovery stamp.
        if not from_flap and self._flap_recovery_ts is not None:
            since = dt_util.utcnow().timestamp() - self._flap_recovery_ts
            from_flap = since < (self.confirm_delay + 90)
        job = self._job_phase()
        if job is None:
            return  # not settled to a real value yet

        # allow_early is suppressed for a flap-arrived value: debouncing proves
        # settled, not new. allow_catchup stays on regardless, since it requires
        # the meter to have moved since idle.
        self._feed_detector(allow_early=not from_flap, allow_catchup=True)

        if job == JOB_STATE_FINISH:
            self._last_real_phase = JOB_STATE_FINISH
            return

        if job in REAL_PHASES:
            if (
                self.stage in (STAGE_WASHING, STAGE_DRYING)
                and job == JOB_STATE_DRYING
                and self._last_real_phase not in (None, JOB_STATE_DRYING)
            ):
                self._create_task(self._async_handle_drying())
            self._last_real_phase = job
        elif job == JOB_STATE_NONE:
            self._last_real_phase = None

    # ------------------------------------------------- stopped on the machine
    @callback
    def _schedule_stop_confirm(self) -> None:
        """(Re)arm the stop-confirm debounce; collapses rapid changes.

        Same `confirm_delay` as the job debounce: an undebounced stop would end
        live washes on every cloud drop. `_async_stop_confirmed` re-reads live
        sensors rather than trusting the arming event, so a stop that's gone by
        the time it fires decides nothing.
        """
        self._cancel_stop_confirm()
        delay = self.confirm_delay
        if delay <= 0:
            self._async_stop_confirmed()
            return
        self._stop_confirm_unsub = async_call_later(
            self.hass, delay, self._async_stop_confirmed
        )

    @callback
    def _cancel_stop_confirm(self) -> None:
        if self._stop_confirm_unsub is not None:
            self._stop_confirm_unsub()
            self._stop_confirm_unsub = None

    @callback
    def _async_stop_confirmed(self, _now=None) -> None:
        """Act on a stop that is still a stop `confirm_delay` later.

        The verdict (stopped-early vs finished) is decided in `cancel`, from
        live readings. Doesn't touch `detect` — this path is keyed off a
        different signal entirely.
        """
        self._stop_confirm_unsub = None
        has_eta, eta_passed = self._eta_status()
        machine_state = self._machine_state()
        verdict = cancel_mod.stop_verdict(
            tracked=self.stage in (STAGE_WASHING, STAGE_DRYING),
            machine_state=machine_state,
            running_on=self._running_state(),
            paused=self.paused,
            has_eta=has_eta,
            eta_passed=eta_passed,
            # Checks the last confirmed phase too, not just the live one: if
            # job_state hit 'finish' and then flapped to unavailable, this must
            # not read a real completion as "stopped early".
            job_finished=JOB_STATE_FINISH
            in (self._job_phase(), self._last_real_phase),
        )
        if verdict == cancel_mod.VERDICT_IGNORE:
            return
        # `_eta_status` just refreshed `_last_eta_ts` if there's a real
        # estimate; without one there's no margin, and the retraction test
        # refuses on its own.
        remaining = (
            self._last_eta_ts - dt_util.utcnow().timestamp()
            if has_eta and self._last_eta_ts is not None
            else None
        )
        _LOGGER.debug("Washer reports stopped mid-load (verdict=%s)", verdict)
        self._create_task(
            self._async_handle_finished(
                cancelled=verdict == cancel_mod.VERDICT_STOPPED,
                # Not the same boolean as the wording: deleting a history row
                # is irreversible, calling a completion "stopped early" isn't.
                retract=cancel_mod.retracts_history(
                    verdict=verdict,
                    machine_state=machine_state,
                    eta_remaining_s=remaining,
                ),
            )
        )

    # ---------------------------------------------------------- self-clean
    def _looks_like_selfclean(self) -> bool:
        """Washer is running but job_state is stuck at 'none' (a self-clean).

        Only labels a detector-started cycle; energy decides that one is
        running at all. A real load shows wash phases, a self-clean never does.
        """
        if self._job_phase() != JOB_STATE_NONE:
            return False
        running = self.hass.states.get(self.running_entity)
        running_on = running is not None and running.state == "on"
        return running_on or self._machine_state() == MACHINE_RUN

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
            self._create_task(self._async_finish_selfclean())

    @callback
    def _record_flap(self) -> None:
        """Record a connection drop and refresh the health sensor."""
        now = dt_util.utcnow().timestamp()
        cutoff = now - 86400
        self._flap_times = [t for t in self._flap_times if t >= cutoff]
        self._flap_times.append(now)
        self._notify_entities()
        self._create_task(self._async_save())

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
    async def _async_start_session(self, *, offline: bool = False) -> None:
        async with self._lock:
            # A wash in progress wins; a previous finished load is simply superseded.
            if self.stage in (STAGE_WASHING, STAGE_DRYING):
                _LOGGER.debug("Start ignored; wash already active (stage=%s)", self.stage)
                return
            # Can race the self-clean path; yield if self-clean already took it.
            if offline and self.stage == STAGE_SELF_CLEAN:
                return
            # Captured before the reset wipes it, for the queue carry-forward below.
            prev_claimant_id = self.claimed_by_id
            # Snapshot every mutated field before the post, which can fail —
            # rollback must restore all of them, or a failed post silently loses
            # the superseded load's claim, queue and handoff.
            rollback = {
                "waiting": self.waiting,
                "claimed_by": self.claimed_by,
                "claimed_by_id": self.claimed_by_id,
                "quiet": self.quiet,
                "message_id": self.message_id,
                "queue": list(self.queue),
                "emptied": self.emptied,
                "handoff_name": self.handoff_name,
                "handoff_hedged": self.handoff_hedged,
                "empty_reminded": self.empty_reminded,
                "cancelled": self.cancelled,
                "paused": self.paused,
                "stage": self.stage,
                "catch_up": self.catch_up,
                "_last_real_phase": self._last_real_phase,
                "_energy_start": self._energy_start,
                "_water_start": self._water_start,
                "_session_started_ts": self._session_started_ts,
                "_offline_since": self._offline_since,
                "_last_eta_ts": self._last_eta_ts,
                "_offline_unverified": self._offline_unverified,
            }
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.quiet = False
            self.message_id = None
            # Carries forward minus whoever just took the machine; stale entries age out too.
            self.queue = queue.carry_forward(
                self.queue,
                prev_claimant_id,
                dt_util.utcnow().timestamp(),
                float(self.queue_expiry),
            )
            self.emptied = False
            # Reset alongside `emptied`: a stale name would misattribute the handoff.
            self.handoff_name = None
            self.handoff_hedged = False
            self.empty_reminded = False
            self.cancelled = False  # belonged to the load this one supersedes
            # Any pending handoff or stop-confirm belonged to the superseded load.
            self._cancel_handoff_timer()
            self._cancel_empty_timer()
            self._cancel_stop_confirm()
            self.paused = self._machine_state() == MACHINE_PAUSE
            # Seeds the phase so an already-drying catch-up still detects its finish.
            job = self.hass.states.get(self.job_state_entity)
            phase = job.state if job is not None else None
            self._last_real_phase = phase if phase in REAL_PHASES else None
            self.stage = STAGE_DRYING if phase == JOB_STATE_DRYING else STAGE_WASHING
            # Offline load: treat as mid-cycle so it shows no false usage baseline.
            self.catch_up = offline or phase in MIDCYCLE_PHASES
            # Baselines are only meaningful for a load seen from the start.
            if self.catch_up:
                self._energy_start = self._water_start = None
            else:
                self._energy_start = self._entity_float(self.energy_entity)
                self._water_start = self._entity_float(self.water_entity)
            self._session_started_ts = dt_util.utcnow().timestamp()
            self._offline_since = None
            self._last_eta_ts = None
            self._offline_unverified = False

            # Visible message with the Claim button; never @mentions anyone.
            embed = self.build_embed()
            try:
                self.message_id = await self.bot.async_post(
                    embed,
                    view=view_for(self),
                    content=None,
                    silent=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to post laundry start message")
                # The two timers cancelled above stay cancelled rather than faked
                # back: a restored-but-dead timer would be a lie the machine acts on.
                for attr, value in rollback.items():
                    setattr(self, attr, value)
                self._detector.reset()  # don't strand the detector as active
                # A concurrent reader may have seen the half-built session; refresh now.
                self._notify_entities()
                return

            self._start_eta_timer()
            await self._async_save()
            self._notify_entities()

    async def _async_handle_drying(self) -> None:
        """Flip the live card to 'Drying'. Display only — it starts nothing.

        The guard must be a whitelist (only washing/drying), not "not idle":
        this is queued from `_async_job_confirmed`, whose `_feed_detector`
        can itself queue `_async_handle_finished` on the same lock. If that
        runs first, the stage here is stale (`done_waiting`), and writing
        `drying` over it wedges the session for good.
        """
        async with self._lock:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING):
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

    async def _async_handle_finished(
        self, *, cancelled: bool = False, retract: bool = False
    ) -> None:
        """End the tracked load. `cancelled` means the cycle didn't finish.

        One completion path for every way a load can end, so the handoff,
        queue carry and persistence can't drift apart. `cancelled` decides
        what's said (card, ping) and whether a later claim is logged;
        `retract` decides the one irreversible thing — deleting the
        claimant's history row — and needs stronger evidence (see
        `cancel.retracts_history`).
        """
        async with self._lock:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING):
                return
            self._stop_eta_timer()
            self._cancel_stop_confirm()
            self.stage = STAGE_DONE_WAITING
            self.paused = False
            self.cancelled = cancelled
            # Keep the detector in lockstep regardless of which path completed the load.
            self._detector.reset()
            # Captured before the reset: bounds the history retraction to this session.
            started_ts = self._session_started_ts
            self._session_started_ts = None
            self._offline_since = None
            self._last_eta_ts = None
            unverified = self._offline_unverified
            claimed = self.claimed_by != UNCLAIMED and self.claimed_by_id is not None
            self.waiting = not claimed
            self.emptied = False  # done is not empty; every completion starts un-emptied
            self.handoff_name = None
            self.handoff_hedged = False
            self.empty_reminded = False
            # A cancel is not a wash: retract a mid-wash claim's history row, bounded
            # to this session. Gated on `retract`, not `cancelled` — a real completion
            # that merely beat its own estimate must not take history with it.
            if retract and claimed and started_ts is not None:
                await self.assistant.async_forget_load(
                    self.claimed_by_id, started_ts, dt_util.utcnow().timestamp()
                )
            embed = self.build_embed()
            view = view_for(self)
            # Unverified (offline) completions hedge the wording.
            done = "should be done" if unverified else "done"
            unv = (
                " ⚠️ the washer went **offline**, so I couldn't verify — worth a peek."
                if unverified
                else ""
            )
            if cancelled:
                # Never "done" — a wrong completion ping is how pings stop
                # being trusted.
                ping_body = (
                    "🛑 Looks like your load was **stopped early** — the washer's "
                    "free, but your things are probably still in it."
                )
                quiet_body = (
                    f"🌙 {self.claimed_by}, it looks like your load was **stopped "
                    "early** — your things are probably still in the washer."
                )
                grabs = (
                    "🛑 **Looks like that load was stopped early** — it didn't "
                    "finish, and the washer's free again."
                )
            else:
                ping_body = f"🧺 Your laundry's {done} — don't forget the lint tray!{unv}"
                quiet_body = (
                    f"🌙 {self.claimed_by}, your laundry's {done} — "
                    f"don't forget the lint tray!{unv}"
                )
                grabs = (
                    f"🧺 **Laundry's {done} and up for grabs** — come move it, "
                    f"and don't forget the **lint tray**!{unv}"
                )
            try:
                if self.message_id:
                    await self.bot.async_edit(self.message_id, embed, view=view)
                if claimed and self.quiet:
                    # Named, no @mention, no push — visible but won't wake them.
                    await self.bot.async_announce_done(quiet_body)
                elif claimed and self.ping_claimant_on_complete:
                    # Routed by their 🤖 preference: @mention unless they chose a DM.
                    await self.assistant.async_route_ping(
                        self.claimed_by_id,
                        dm_text=ping_body,
                        channel_text=f"<@{self.claimed_by_id}> {ping_body}",
                    )
                elif not claimed:
                    # Push-silent nudge, not a second embed, so it's visible
                    # without duplicating the card.
                    await self.bot.async_announce_done(grabs)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update finished state")
            # Claimed: released by the tap, fallback timer as backstop. Unclaimed:
            # nobody to empty it, so whoever's next is told now. Lock already held;
            # asyncio.Lock isn't reentrant, hence _locked.
            if claimed:
                self._arm_handoff_timer()
                self._arm_empty_timer()
            else:
                # The "up for grabs" line above already tells the channel.
                await self._async_ping_next_locked(hedged=False, announce=False)
            self._offline_unverified = False
            await self._async_save()
            self._notify_entities()

    async def _async_start_selfclean(self) -> None:
        async with self._lock:
            if self.stage not in (STAGE_IDLE, STAGE_DONE_WAITING):
                return
            self.stage = STAGE_SELF_CLEAN
            self.paused = False
            self.cancelled = False
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.message_id = None
            self._last_real_phase = None
            self._energy_start = self._entity_float(self.energy_entity)
            self._water_start = self._entity_float(self.water_entity)
            self._session_started_ts = dt_util.utcnow().timestamp()
            self._offline_since = None
            self._last_eta_ts = None
            self._offline_unverified = False
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
                self._detector.reset()  # don't strand the detector as active
                return
            self._start_eta_timer()
            await self._async_save()
            self._notify_entities()

    async def _async_finish_selfclean(self) -> None:
        async with self._lock:
            if self.stage != STAGE_SELF_CLEAN:
                return
            self._stop_eta_timer()
            self._detector.reset()  # keep the detector in lockstep
            self._session_started_ts = None
            self._offline_since = None
            self._last_eta_ts = None
            self._offline_unverified = False
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
        # Taking the machine means you're no longer waiting for it; the session-start
        # carry-forward can only drop the previous claimant, not this one.
        self.queue = queue.remove_user(self.queue, user_id)
        if self.stage == STAGE_DONE_WAITING:
            self.waiting = False
        await self._async_save()
        self._notify_entities()
        # Reports the claim as the habit model's only data point; consent, dedup
        # and retention are the assistant's business. Skipped when cancelled: a
        # stopped load isn't a wash and must not move anyone's predicted times.
        if not self.cancelled:
            await self.assistant.async_note_claim(user_id)
            # Lets the reminder loop tell anyone whose booked slot this load is in.
            async_dispatcher_send(
                self.hass, SIGNAL_LOAD_CLAIMED, {"claimant_id": user_id}
            )
        # A load claimed after it finished gets its empty-it reminder from now.
        if (
            self.stage == STAGE_DONE_WAITING
            and not self.emptied
            and not self.empty_reminded
            and self._empty_unsub is None
        ):
            self._arm_empty_timer()
        return True

    async def handle_unclaim(self) -> bool:
        """Undo a claim — the load is up for grabs again. Called from the button."""
        if self.stage not in self._CLAIMABLE_STAGES:
            return False
        self.claimed_by = UNCLAIMED
        self.claimed_by_id = None
        self.quiet = False  # quiet belonged to the (now gone) claimant
        self._cancel_empty_timer()  # nobody left to remind
        if self.stage == STAGE_DONE_WAITING:
            self.waiting = True
        await self._async_save()
        self._notify_entities()
        return True

    async def handle_toggle_quiet(self) -> bool:
        """Toggle quiet mode for the claimed load (the 🌙 button).

        When on, completion names the claimant in plain text instead of an
        @mention — visible, but no push. Returns False on a stale tap.
        """
        if self.stage not in self._CLAIMABLE_STAGES:
            return False
        self.quiet = not self.quiet
        await self._async_save()
        self._notify_entities()
        return True

    async def handle_next_toggle(self, who: str, user_id: int) -> str:
        """Join or leave the "I'm next" line (the 🔜 button).

        Returns one of the `queue.TOGGLE_*` results; `TOGGLE_STALE` means a tap
        against an old card with no live load behind it.
        """
        if self.stage not in self._CLAIMABLE_STAGES:
            return queue.TOGGLE_STALE
        now = dt_util.utcnow().timestamp()
        # Prune before toggling: an expired entry must not hold a slot against the cap.
        pruned = queue.prune(self.queue, now, float(self.queue_expiry))
        self.queue, result = queue.toggle_member(pruned, user_id, who, now)
        await self._async_save()
        self._notify_entities()
        return result

    async def handle_next_join(self, who: str, user_id: int) -> tuple[str, int | None]:
        """Join the 🔜 line from a DM reply; unlike the card's toggle, never leaves.

        Returns `(result, place)` with a `queue.TOGGLE_*` result.
        """
        if self.stage not in self._CLAIMABLE_STAGES:
            return (queue.TOGGLE_STALE, None)
        now = dt_util.utcnow().timestamp()
        pruned = queue.prune(self.queue, now, float(self.queue_expiry))
        self.queue, result = queue.join_member(pruned, user_id, who, now)
        if result == queue.TOGGLE_ADDED:
            await self._async_save()
            self._notify_entities()
            # The card's "Next up" is now stale, and it wasn't the card that was tapped.
            self._create_task(self._async_refresh_card())
        return (result, queue.position(self.queue, user_id))

    async def _async_refresh_card(self) -> None:
        """Re-render the live card after a change made outside it."""
        async with self._lock:
            if not self.message_id or self.stage not in self._CLAIMABLE_STAGES:
                return
            try:
                await self.bot.async_edit(
                    self.message_id, self.build_embed(), view=view_for(self)
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to refresh the card")

    async def handle_emptied(self) -> bool:
        """The claimant confirming the drum is clear (the ✅ button).

        The real handoff trigger — completion only means the machine stopped.
        Returns False on a stale tap. The `emptied` guard matters because a
        stale card (an edit that never landed) can still show the button, and
        a second tap must not hand the machine to a second person.
        """
        if self.stage != STAGE_DONE_WAITING or self.emptied:
            return False
        self.emptied = True
        self._cancel_handoff_timer()
        self._cancel_empty_timer()
        await self._async_save()
        self._notify_entities()
        # Not awaited: takes the lock and does two round trips, and this callback
        # must answer Discord's interaction within 3 seconds. State is already saved.
        self._create_task(
            self._async_ping_next(hedged=False, expect_emptied=True)
        )
        return True

    # ------------------------------------------------------------- the handoff
    async def _async_ping_next_locked(
        self, *, hedged: bool, announce: bool = True
    ) -> None:
        """Hand the washer to whoever is next. Assumes the lock is held.

        `asyncio.Lock` isn't reentrant, so a caller that already holds it
        (`_async_handle_finished`) must use this variant, not take it again.
        `hedged` softens the wording for the fallback timer, where nothing is
        actually confirmed. `announce` posts the channel's "washer's free" line
        (off for an unclaimed completion, which has already posted one).
        """
        now = dt_util.utcnow().timestamp()
        claimant_id = self.claimed_by_id
        # Handed off only once: the backstop timer and a later ✅ tap can both
        # reach here for the same load. `handoff_name` set means it already
        # happened — skip the pop (the card refresh below still runs).
        head = None
        already_handed_off = self.handoff_name is not None
        if not already_handed_off:
            # Expiry, the claimant exclusion and the pop are one decision, made
            # in queue.py so they're covered by the pure tests.
            head, self.queue = queue.select_handoff(
                self.queue, now, float(self.queue_expiry), self.claimed_by_id
            )
            # Announced after the pop, since "is the washer free" changes once it's
            # handed off; `hedged` travels so a listener can apply its own stricter test.
            async_dispatcher_send(
                self.hass,
                SIGNAL_WASHER_FREE,
                {
                    "handed_off": head is not None,
                    "hedged": hedged,
                    "claimant_id": claimant_id,
                },
            )
            if head is None:
                if announce:
                    await self._async_announce_free(None, hedged=hedged)
                return  # nobody waiting — an empty line is the normal case
        if head is not None:
            # Recorded before the ping: the queue has already lost them either way,
            # so a failed ping must still leave the card saying who it was for.
            self.handoff_name = queue.entry_name(head)
            self.handoff_hedged = hedged
            if hedged:
                body = (
                    "🔜 The washer's been done a while and nobody's checked in — "
                    "probably free, worth a look."
                )
            else:
                body = "🔜 Washer's free — you're up."
            route = None
            try:
                # Routed via 🤖 prefs: a channel @mention (a push) unless a DM bounces.
                route = await self.assistant.async_route_ping(
                    head.get("id"),
                    dm_text=body,
                    channel_text=f"<@{head.get('id')}> {body}",
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to ping the next person in line")
            if announce:
                await self._async_announce_free(
                    self.handoff_name, hedged=hedged, route=route
                )
        # The queue moved, so "Next up" is stale. Re-attach the view too: if the
        # button's own edit never lands, ✅ would otherwise offer forever.
        if self.message_id:
            try:
                await self.bot.async_edit(
                    self.message_id, self.build_embed(), view=view_for(self)
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to refresh the card after a handoff")
        await self._async_save()
        self._notify_entities()

    async def _async_ping_next(self, *, hedged: bool, expect_emptied: bool) -> None:
        """Lock-taking wrapper for callers that don't already hold the lock.

        Both callers are scheduled tasks that can sit behind the lock a while,
        so the check that scheduled them may be stale by the time this runs.
        `expect_emptied` is the state the ping was decided under; anything
        else means it no longer applies.
        """
        async with self._lock:
            if self.stage != STAGE_DONE_WAITING or self.emptied != expect_emptied:
                return
            await self._async_ping_next_locked(hedged=hedged)

    async def _async_announce_free(self, name, *, hedged: bool, route=None) -> None:
        """Post the push-silent "washer's free" line, if enabled.

        Skipped when the next person's own ping already went to the channel
        (`route`), since that message already says it.
        """
        if not self.announce_free or route in (
            people_mod.REMIND_CHANNEL,
            people_mod.REMIND_OFF,
        ):
            return
        line = queue.free_announcement(name, hedged=hedged)
        if line is None:
            return
        try:
            await self.bot.async_announce_done(line)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to post the washer-free line")

    @callback
    def _arm_handoff_timer(self) -> None:
        """Arm the "nobody tapped Emptied it" backstop for this finished load.

        Armed even with an empty line: people tap 🔜 *after* seeing the done
        card, and the callback re-checks the line anyway.
        """
        self._cancel_handoff_timer()
        delay = self.handoff_fallback
        if delay <= 0:
            return  # disabled — the ✅ tap is then the only handoff trigger
        self._handoff_unsub = async_call_later(
            self.hass, delay, self._async_handoff_fallback
        )

    @callback
    def _cancel_handoff_timer(self) -> None:
        if self._handoff_unsub is not None:
            self._handoff_unsub()
            self._handoff_unsub = None

    @callback
    def _async_handoff_fallback(self, _now=None) -> None:
        """The claimant never confirmed — tell the next person anyway, hedged."""
        self._handoff_unsub = None
        # Never fire against a superseded session: a new load may have started,
        # or the claimant may have emptied it moments before this fired.
        if self.stage != STAGE_DONE_WAITING or self.emptied:
            return
        self._create_task(
            self._async_ping_next(hedged=True, expect_emptied=False)
        )

    # ---------------------------------------------------------- empty-it reminder
    @callback
    def _arm_empty_timer(self) -> None:
        """Arm the one-shot reminder to this finished load's claimant."""
        self._cancel_empty_timer()
        delay = self.empty_reminder
        if delay <= 0:
            return
        self._empty_unsub = async_call_later(
            self.hass, delay, self._async_empty_reminder_due
        )

    @callback
    def _cancel_empty_timer(self) -> None:
        if self._empty_unsub is not None:
            self._empty_unsub()
            self._empty_unsub = None

    @callback
    def _async_empty_reminder_due(self, _now=None) -> None:
        self._empty_unsub = None
        if self.stage != STAGE_DONE_WAITING or self.emptied or self.empty_reminded:
            return
        self._create_task(self._async_send_empty_reminder())

    async def _async_send_empty_reminder(self) -> None:
        """Remind the claimant once. Marked sent before sending, so a restart
        can't repeat it."""
        async with self._lock:
            if (
                self.stage != STAGE_DONE_WAITING
                or self.emptied
                or self.empty_reminded
                or self.claimed_by_id is None
            ):
                return
            self.empty_reminded = True
            await self._async_save()
            now = dt_util.utcnow().timestamp()
            waiting = any(
                not queue.same_user(entry, self.claimed_by_id)
                for entry in queue.prune(self.queue, now, float(self.queue_expiry))
            )
            try:
                await self.assistant.async_remind_empty(
                    self.claimed_by_id,
                    name=self.claimed_by,
                    waiting=waiting,
                    quiet=self.quiet,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to send the empty-it reminder")

    async def async_reset_session(self) -> None:
        """Service: force-close whatever session is being tracked.

        Manual backstop for failures detection misses on its own (the
        absolute net, `MAX_SESSION_MINUTES`, is too slow to be a recovery
        plan). A reset, not a completion: announces and pings nobody, since
        calling this means the bot is wrong about the washer, not that
        laundry is ready. Leaves the 🔜 line and history untouched — that's
        the cancel path's job.
        """
        async with self._lock:
            self._stop_eta_timer()
            self._cancel_stop_confirm()
            self._cancel_handoff_timer()
            self._cancel_empty_timer()
            if self._job_confirm_unsub is not None:
                self._job_confirm_unsub()
                self._job_confirm_unsub = None
            if self._selfclean_unsub is not None:
                self._selfclean_unsub()
                self._selfclean_unsub = None
            message_id = self.message_id
            self.stage = STAGE_IDLE
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.quiet = False
            self.emptied = False
            self.handoff_name = None
            self.handoff_hedged = False
            self.empty_reminded = False
            self.paused = False
            self.cancelled = False
            self.catch_up = False
            self.message_id = None
            self._last_real_phase = None
            self._energy_start = self._water_start = None
            self._session_started_ts = None
            self._offline_since = None
            self._last_eta_ts = None
            self._offline_unverified = False
            # Left ACTIVE, the detector would refuse the next real load.
            self._detector.reset()
            if message_id:
                try:
                    await self.bot.async_edit(
                        message_id,
                        discord.Embed(
                            title="🧺 Card closed",
                            description=(
                                "This card was closed by hand — I'm not tracking "
                                "a load right now. The next one posts a new card."
                            ),
                            color=_COLOR_IDLE,
                        ),
                        view=None,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to close the card on reset_session")
            _LOGGER.debug("Session force-closed via reset_session")
            await self._async_save()
            self._notify_entities()

    async def async_track_current_load(self) -> bool:
        """Service: start tracking the load that is running right now.

        Mirror of `async_reset_session`, for a missed load instead of an
        invented one: a human saying "it's running" beats a sensor that can
        freeze, lag, or get replayed. Tracked as a catch-up (no start time or
        baseline claimed). Returns True when opened; False if already tracked
        (`reset_session` first) or the post failed and rolled itself back.
        """
        if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            _LOGGER.debug(
                "track_load ignored; already tracking (stage=%s)", self.stage
            )
            return False
        await self._async_start_session(offline=True)
        if self.stage not in (STAGE_WASHING, STAGE_DRYING):
            return False  # the post failed and rolled itself back
        # Order matters: `_async_start_session` can fail and restore stage to idle.
        # Seeding the detector first would leave it ACTIVE against an idle session,
        # refusing every real load after (the wedge `diagnose` reports).
        now = dt_util.utcnow().timestamp()
        self._detector.phase = RUN_ACTIVE
        self._detector.last_energy = self._entity_float(self.energy_entity)
        # Armed from now, not the meter's last move, which may be an hour old.
        self._detector.last_rise_ts = now
        # `_async_start_session` already saved, but before these two lines ran; save
        # again so a restart can't restore an active session behind an idle detector.
        await self._async_save()
        self._notify_entities()
        _LOGGER.debug("Now tracking the running load, by hand (track_load)")
        return True

    async def async_test_post(self) -> None:
        """Debug service: post a sample embed with a working Claim button."""
        async with self._lock:
            self.stage = STAGE_DONE_WAITING
            self.waiting = True
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.cancelled = False
            # A real handoff timer must not fire against this synthetic state:
            # cancel it, since the debug post forces done_waiting and repoints message_id.
            self.emptied = False
            self.handoff_name = None
            self.handoff_hedged = False
            self.empty_reminded = False
            self._cancel_handoff_timer()
            self._cancel_empty_timer()
            embed = self.build_embed(test=True)
            try:
                self.message_id = await self.bot.async_post(
                    embed,
                    # 🤖 rides along so the panel/DM route can be tested without a real load.
                    view=ClaimView(
                        self, show="claim", with_assistant=self.show_assistant
                    ),
                    silent=True,
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
        # Drives the detector + time-based completion, then refreshes the live embed.
        self._feed_detector()
        self._check_time_completion()
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
            self._add_queue(embed)
            self._add_offline_notice(embed)
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
            self._add_queue(embed)
            self._add_offline_notice(embed)
        elif self.stage == STAGE_DONE_WAITING:
            if self.cancelled:
                # "Stopped", never "done" — claiming still works, unchanged.
                who = (
                    f"**{self.claimed_by}**'s load"
                    if self.claimed_by and self.claimed_by != UNCLAIMED
                    else "This load"
                )
                embed = discord.Embed(
                    title="🛑 Stopped early",
                    description=(
                        f"{who} looks like it was **stopped on the machine** "
                        "rather than finishing.\nThe washer's free — whatever's "
                        "in the drum still needs moving."
                    ),
                    # Grey, not amber or green: idle, and this cycle didn't finish.
                    color=_COLOR_IDLE,
                )
            elif self.claimed_by and self.claimed_by != UNCLAIMED:
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
            self._add_queue(embed)
            self._add_handoff(embed)
            if self._offline_unverified:
                embed.add_field(
                    name="⚠️ Unverified",
                    value=(
                        "The washer was **offline** at the end, so I couldn't "
                        "confirm it actually finished — worth a quick check."
                    ),
                    inline=False,
                )
        else:
            embed = discord.Embed(
                title="Laundry", description="Idle.", color=_COLOR_IDLE
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
            value = f"🧺 {self.claimed_by}"
            if self.quiet:
                value += "  ·  🌙 quiet (named, not pinged)"
            embed.add_field(name="Claimed by", value=value, inline=False)

    def _add_queue(self, embed: discord.Embed) -> None:
        """Show the "I'm next" line, so contention is visible without asking.

        Named only; the one ping anybody in it gets is the handoff, sent separately.
        """
        line = queue.format_queue(self.queue)
        if not line:
            return  # no line, no field — an empty "Next up" is just clutter
        value = f"🔜 {line}"
        if (
            self.stage == STAGE_DONE_WAITING
            and not self.emptied
            and self.claimed_by != UNCLAIMED
        ):
            # Set the expectation: the washer being done doesn't make it free.
            value += f" — you're up once {self.claimed_by} clears it"
        embed.add_field(name="Next up", value=value, inline=False)

    def _add_handoff(self, embed: discord.Embed) -> None:
        """Show that the line moved, once the head has been taken off it.

        Otherwise the handoff is a disappearance: the pop loses them from "Next
        up" the moment they were told, as if they'd never tapped 🔜 at all.
        """
        if not self.handoff_name:
            return
        embed.add_field(
            name="Handed over",
            value=queue.handoff_line(self.handoff_name, hedged=self.handoff_hedged),
            inline=False,
        )

    def _add_offline_notice(self, embed: discord.Embed) -> None:
        """Warn on the live card when the washer has been offline a while."""
        offline = self._offline_for()
        if offline >= self.offline_after:
            mins = int(offline // 60)
            embed.add_field(
                name="⚠️ Washer offline",
                value=(
                    f"No data from the washer for ~{mins} min — can't verify "
                    "progress right now. I'll still post when it should be done."
                ),
                inline=False,
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


type LaundryConfigEntry = ConfigEntry[LaundryCoordinator]
