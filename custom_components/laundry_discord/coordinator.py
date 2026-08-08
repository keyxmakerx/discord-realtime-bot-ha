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
    CONF_ENERGY_LOAD_JUMP,
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

# Colors per stage. One colour, one meaning: grey used to serve **both**
# "somebody has claimed this load" and "nothing is happening", which are the two
# states a glance at the channel most needs to tell apart — a claimed load is
# waiting on a person, an idle card is waiting on nobody. They are split here.
_COLOR_WASHING = 0x3498DB
_COLOR_DRYING = 0xE67E22
_COLOR_DONE = 0x2ECC71
_COLOR_CLAIMED = 0xF1C40F  # amber — this load has an owner and they're not done
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
        # The 🤖 assistant: per-person prefs (its own Store), the private panel
        # and the DM plumbing. The dependency runs one way — the coordinator
        # asks it to deliver a message or open a panel, and it never touches
        # session state — so a fault in there can't reach the state machine.
        # It gets the entry to read its own options; it does not get `self`.
        self.assistant = LaundryAssistant(hass, self.bot, entry)

        # Session state (persisted).
        self.stage: str = STAGE_IDLE
        self.waiting: bool = False
        self.claimed_by: str = UNCLAIMED
        self.claimed_by_id: int | None = None
        # When True, the claimant is named in plain text at completion instead of
        # being @mentioned — a visible, push-silent "done" (e.g. they're asleep).
        self.quiet: bool = False
        # The "I'm next" line: FIFO entries {"id", "name", "ts"} (see queue.py).
        # Session state, not planner state — it belongs to the machine, carries
        # into the next load, and dies with a reset like everything else here.
        self.queue: list[dict] = []
        # True once the claimant confirms they've cleared the drum. A finished
        # washer is not an *empty* one — their clothes are still in it — so the
        # handoff to whoever is next waits for this, not for completion.
        self.emptied: bool = False
        # Who this load's handoff ping went to, and whether it was the hedged
        # backstop rather than a confirmed one. Recorded because the handoff
        # *pops* the head off the line: without it the "Next up" field simply
        # disappears at the moment that person was told, and to everybody else
        # the card reads as though they were never waiting at all.
        self.handoff_name: str | None = None
        self.handoff_hedged: bool = False
        self.message_id: int | None = None
        # True when the session was picked up mid-cycle (washer already running
        # at startup) rather than caught at its off->on start.
        self.catch_up: bool = False
        # True while machine_state reports the load is paused mid-cycle.
        self.paused: bool = False
        # True when the load in ``done_waiting`` was ended by somebody stopping
        # the washer rather than by the cycle finishing. It is what stops the
        # card and the ping claiming a cycle completed when we know it didn't,
        # and it is the flag ``handle_claim`` checks before logging anything to
        # the habit model — a stopped load is not a wash.
        self.cancelled: bool = False
        # Last confirmed real job phase, ignoring unavailable/unknown blips —
        # used purely for enrichment now (the wash->dry transition display).
        self._last_real_phase: str | None = None
        # Energy/water meter baselines captured at session start (None when not
        # measurable, e.g. a mid-cycle catch-up where there's no true baseline).
        self._energy_start: float | None = None
        self._water_start: float | None = None
        # Wall-clock (unix) time the current session started — used to gate the
        # completion ETA freshness and back the absolute max-session safety net.
        # None whenever no load is being tracked.
        self._session_started_ts: float | None = None
        # Offline tracking: when the washer first went unavailable during this
        # load, the last completion estimate we saw, and whether the load was
        # completed while offline (so the 'done' message is flagged unverified).
        self._offline_since: float | None = None
        self._last_eta_ts: float | None = None
        self._offline_unverified: bool = False
        # The energy-primary liveness core: the single source of truth for load
        # start/finish. job_state/machine_state are only accelerants + enrichment
        # feeding it; they can never override the meter. See detect.EnergyDetector.
        self._detector = EnergyDetector(
            start_jump=self.energy_load_jump,
            idle_timeout=float(self.energy_idle_timeout),
        )
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
        # Pending stop-confirm timer — the same `for: 30s` shape, for "somebody
        # stopped the wash on the machine". Its own handle rather than a second
        # debounce mechanism: a stop and a job_state change can be in flight at
        # the same moment (they are at the end of every normal load) and one
        # must not disarm the other.
        self._stop_confirm_unsub = None
        # Pending self-clean detect/end timer (running + job_state stuck none).
        self._selfclean_unsub = None
        # Pending handoff-fallback timer (nobody tapped "Emptied it").
        self._handoff_unsub = None

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

        The claimant's "Emptied it" tap is the real handoff trigger; this is the
        backstop for when they forget, and its wording hedges accordingly. 0
        disables the backstop entirely (the tap still works).
        """
        return (
            int(self._cfg.get(CONF_HANDOFF_FALLBACK, DEFAULT_HANDOFF_FALLBACK)) * 60
        )

    @property
    def queue_expiry(self) -> int:
        """Seconds an "I'm next" entry survives before it ages out of the line."""
        return int(self._cfg.get(CONF_QUEUE_EXPIRY, DEFAULT_QUEUE_EXPIRY)) * 3600

    @property
    def show_assistant(self) -> bool:
        """Whether the 🤖 button is on the card.

        On by default: the panel is inert until tapped and it's the onboarding
        surface. Off hides the button only — anything already chosen in it
        keeps working.
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
        # The energy meter is now the primary detection signal — react promptly
        # to every reading (a rise sustains a load; a jump while idle starts one).
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.energy_entity], self._on_energy
            )
        )
        # Always-on tick: feeds the detector while idle (start polling) and drives
        # the time-based completion, and keeps the connection-health sensor fresh.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_health_tick, timedelta(minutes=5)
            )
        )

    @callback
    def _async_health_tick(self, now) -> None:
        # Always-on heartbeat: feed the detector (catches an offline load's
        # energy jump while idle, and drives the time-based completion even when
        # no state events arrive) and keep the health sensor fresh.
        self._feed_detector()
        self._check_time_completion()
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
        self._cancel_stop_confirm()
        if self._selfclean_unsub is not None:
            self._selfclean_unsub()
            self._selfclean_unsub = None
        self._cancel_handoff_timer()
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
            # Not tracking an active wash, but the washer may already be mid-cycle
            # (installed/restarted during a load) or a load may have run entirely
            # while HA was down. One feed covers both; the restored state is
            # settled, so the job fast-paths are safe to use here.
            self._feed_detector(job_accel=True)
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

    def _running_state(self) -> bool | None:
        """Whether the running sensor says on/off, or None when unreadable.

        Deliberately three-valued, unlike the self-clean check's
        ``running is not None and state == "on"``: for the cancel path
        "unavailable" and "off" have to be different answers, because the cloud
        drops hourly and reading that silence as *off* would end a live wash.
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

    def _eta_status(self) -> tuple[bool, bool]:
        """(has_eta, eta_passed) for the washer's own completion estimate.

        ``has_eta`` is True when completion_time holds a real value that was
        (re)published during the current cycle — a value frozen from a previous
        load (``last_changed`` before this session started) is treated as no
        estimate, so a stale ETA can't drive completion. ``eta_passed`` is True
        once that estimate is in the past. Both False when no load is tracked or
        completion_time is unavailable (a genuinely offline load). Also snapshots
        the last good estimate into ``_last_eta_ts`` for the offline-completion.
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
        # Treat the estimate as belonging to this cycle if it was last published
        # around or after the session began. The margin covers confirm_delay (the
        # session starts that long after the washer first sets the estimate) so we
        # don't wrongly reject a fresh estimate and fall back to the offline
        # backstop; a value frozen from a *previous* load changed long before this.
        margin = self.confirm_delay + 300
        if st.last_changed.timestamp() < self._session_started_ts - margin:
            return (False, False)  # stale estimate frozen from a previous load
        self._last_eta_ts = target.timestamp()  # remember for offline completion
        return (True, dt_util.utcnow() >= target)

    @callback
    def _check_time_completion(self, _now=None) -> None:
        """Time-based completions on the periodic ticks (job 'finish' / the ETA
        gate normally complete far earlier).

        - **Offline completion:** the washer has been unavailable a long time AND
          its last-known ETA has passed (+grace) — finish, flagged *unverified*.
        - **Max-session:** absolute safety net so a stuck session can't live on.
        """
        if self.stage not in (STAGE_WASHING, STAGE_DRYING):
            return
        now = dt_util.utcnow().timestamp()
        if offline_completion_due(
            offline_since=self._offline_since,
            last_eta_ts=self._last_eta_ts,
            now=now,
            offline_after=float(self.offline_after),
            eta_grace=float(self.offline_complete_grace),
        ):
            _LOGGER.debug("Offline completion (washer unavailable, ETA passed)")
            self._offline_unverified = True
            self.hass.async_create_task(self._async_handle_finished())
            return
        if session_too_long(self._session_started_ts, now, float(self.max_session)):
            _LOGGER.debug("Max-session safety completion (stage=%s)", self.stage)
            self.hass.async_create_task(self._async_handle_finished())

    @callback
    def _feed_detector(self, _now=None, *, job_accel: bool = False) -> None:
        """Drive the energy-primary liveness core from current sensor readings.

        Called from every signal (energy/job/machine/running changes, the health
        tick, the ETA tick, bot-ready). The detector dedupes — it only emits an
        event on a real transition — so calling this often is safe and cheap.

        ``job_accel`` enables the job_state fast-paths (early-phase start, finish
        completion). It is only set on the *debounced* confirm path (and the
        one-shot restore), so a transient phase flap can never start a load — the
        energy meter remains the authority for everything else.
        """
        self._track_offline()
        phase = self._job_phase()
        is_real = job_accel and phase in REAL_PHASES and phase != JOB_STATE_FINISH
        is_early = is_real and phase not in MIDCYCLE_PHASES
        is_finish = job_accel and phase == JOB_STATE_FINISH
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
        )
        if ev == EV_STARTED:
            self._on_detector_started(phase)
        elif ev == EV_FINISHED:
            self._on_detector_finished()
        elif (self._detector.last_energy, self._detector.last_rise_ts) != before:
            # No transition, but the baseline advanced — persist it.
            self.hass.async_create_task(self._async_save())

    @callback
    def _on_detector_started(self, phase: str | None) -> None:
        """A load began. Decide normal vs self-clean and open a session."""
        if self.stage in (STAGE_WASHING, STAGE_DRYING, STAGE_SELF_CLEAN):
            return  # already tracking (shouldn't happen; detector is deduped)
        # A real wash phase => a normal load (online). No phase but the washer is
        # running with job stuck at 'none' => a self-clean. Otherwise the data
        # arrived after the fact (offline) => a normal catch-up load.
        if phase in REAL_PHASES:
            _LOGGER.debug("Detector: load started (job=%s)", phase)
            self.hass.async_create_task(self._async_start_session())
        elif self._looks_like_selfclean():
            _LOGGER.debug("Detector: self-clean started")
            self.hass.async_create_task(self._async_start_selfclean())
        else:
            _LOGGER.debug("Detector: offline load started (job dark)")
            self.hass.async_create_task(self._async_start_session(offline=True))

    @callback
    def _on_detector_finished(self) -> None:
        """The active cycle's energy went flat (or job hit 'finish')."""
        if self.stage == STAGE_SELF_CLEAN:
            self.hass.async_create_task(self._async_finish_selfclean())
        elif self.stage in (STAGE_WASHING, STAGE_DRYING):
            self.hass.async_create_task(self._async_handle_finished())

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
        # Self-clean ends quickly when the washer stops (faster than the energy
        # idle-timeout, which is the backstop).
        if self.stage == STAGE_SELF_CLEAN and new.state == "off":
            self._schedule_selfclean_end()
            return
        old = event.data.get("old_state")
        if self.stage in (STAGE_WASHING, STAGE_DRYING) and (
            cancel_mod.running_off_signalled(
                old.state if old is not None else None,
                new.state,
                # This sensor cannot tell a pause from a stop; machine_state can,
                # both live and via ``self.paused``. Without that entity there is
                # nothing to veto it, so it does not arm a stop alone — see
                # cancel.running_off_signalled.
                machine_state_configured=bool(self.machine_state_entity),
            )
        ):
            # The second way "somebody stopped it" reaches us: a corroborating
            # trigger for the machine_state signal, faster on units where the
            # running sensor moves first.
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
                self.hass.async_create_task(self._async_render_active("paused"))
            elif new_s == MACHINE_RUN and self.paused:
                self.paused = False
                self.hass.async_create_task(self._async_render_active("resumed"))
            elif new_s == MACHINE_STOP and self.paused:
                # Cancelling is Start/Pause then Stop on most combo units, so
                # `pause -> stop` is the *ordinary* cancel. Clear the flag at
                # the source: the load is no longer on hold, and a stale
                # "paused" would otherwise outlive the session in the card, in
                # the persisted state and in the stop verdict. No "resumed"
                # render — the stop confirm below decides what this card says.
                self.paused = False
            old = event.data.get("old_state")
            if cancel_mod.machine_stop_signalled(
                old.state if old is not None else None, new_s
            ):
                # A stop while a load is being tracked. Debounced, then judged —
                # this is the only route by which "the human ended the cycle"
                # can ever finish a load (see cancel.py for why it isn't in
                # detect.py).
                self._schedule_stop_confirm()
        elif self.stage == STAGE_SELF_CLEAN:
            if new_s == MACHINE_STOP:
                self._schedule_selfclean_end()
        else:
            self._feed_detector()

    @callback
    def _on_job_state(self, event: Event) -> None:
        """Record connection flaps, then (re)arm the confirm-debounce.

        job_state is no longer authoritative for start/stop — that's the energy
        meter. We still debounce it (``confirm_delay``) and act on the settled
        value for *enrichment*: the fast-start accelerant, the wash->dry display
        transition, and the fast 'finish' completion path.
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
        """Act on the *settled* job_state: feed the detector + drive enrichment.

        Start/finish decisions live in the detector now (energy-primary); here we
        only (a) feed it the early/finish accelerants and (b) flip the embed to
        'Drying' when the wash->dry transition is confirmed mid-load.
        """
        self._job_confirm_unsub = None
        job = self._job_phase()
        if job is None:
            return  # not settled to a real value yet

        # Let the detector see the settled phase, with the job fast-paths enabled
        # (this is the debounced path, so it's safe from transient flaps).
        self._feed_detector(job_accel=True)

        if job == JOB_STATE_FINISH:
            self._last_real_phase = JOB_STATE_FINISH
            return

        if job in REAL_PHASES:
            # Enrichment only: flip the live embed to 'Drying' on the confirmed
            # wash->dry transition while a load is being tracked.
            if (
                self.stage in (STAGE_WASHING, STAGE_DRYING)
                and job == JOB_STATE_DRYING
                and self._last_real_phase not in (None, JOB_STATE_DRYING)
            ):
                self.hass.async_create_task(self._async_handle_drying())
            self._last_real_phase = job
        elif job == JOB_STATE_NONE:
            self._last_real_phase = None

    # ------------------------------------------------- stopped on the machine
    @callback
    def _schedule_stop_confirm(self) -> None:
        """(Re)arm the stop-confirm debounce; collapses rapid changes.

        The same shape as :meth:`_schedule_job_confirm` and on the same
        ``confirm_delay``, which is the point: this washer's cloud drops to
        ``unavailable`` on a ~51-minute timer, and an undebounced stop signal
        would end live washes. ``confirm_delay`` is sufficient on its own here
        and gets no second setting — the flap values never arm this at all
        (:func:`cancel.is_flap`), and :meth:`_async_stop_confirmed` re-reads the
        live sensors rather than trusting the event that armed it, so a stop
        that has gone away by the time this fires decides nothing.
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
        """Act on a stop that is *still* a stop ``confirm_delay`` later.

        Everything decided here is decided in :mod:`cancel`, from the live
        readings — including the difference between "stopped early" and "it
        finished", which is the washer's own estimate. Nothing in
        :mod:`detect` is consulted or changed: the ETA gate keeps a frozen
        meter from firing a false done, and this path is keyed off a different
        signal entirely.
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
            # The last *confirmed* phase counts as well as the live one: at the
            # natural end of a cycle the machine stops and job_state reports
            # 'finish', and if that value has since flapped to unavailable we
            # would otherwise call a real completion "stopped early" on the
            # strength of an estimate that ran long. A cancel can't be mistaken
            # for it — job_state goes to 'none' there, which clears this.
            job_finished=JOB_STATE_FINISH
            in (self._job_phase(), self._last_real_phase),
        )
        if verdict == cancel_mod.VERDICT_IGNORE:
            return
        # ``_eta_status`` has just refreshed ``_last_eta_ts`` if there is a real
        # estimate for this cycle; without one there is no margin to measure and
        # the retraction test refuses on its own.
        remaining = (
            self._last_eta_ts - dt_util.utcnow().timestamp()
            if has_eta and self._last_eta_ts is not None
            else None
        )
        _LOGGER.debug("Washer reports stopped mid-load (verdict=%s)", verdict)
        self.hass.async_create_task(
            self._async_handle_finished(
                cancelled=verdict == cancel_mod.VERDICT_STOPPED,
                # Deliberately *not* the same boolean as the wording. Deleting
                # somebody's history row is irreversible; calling a completion
                # "stopped early" is not.
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

        Used purely to *label* a detector-started cycle (energy is what decides a
        cycle is running). A real load shows wash phases; a self-clean never
        leaves 'none' while the machine reports running.
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
    async def _async_start_session(self, *, offline: bool = False) -> None:
        async with self._lock:
            # A wash already in progress wins. A previous *finished* load (still
            # showing its claim/unclaim message) is simply superseded by this one.
            if self.stage in (STAGE_WASHING, STAGE_DRYING):
                _LOGGER.debug("Start ignored; wash already active (stage=%s)", self.stage)
                return
            # An offline detection can race the self-clean path (both fire while
            # job_state is dark); if a self-clean took the session first, yield.
            if offline and self.stage == STAGE_SELF_CLEAN:
                return
            # Who claimed the *previous* load, captured before the reset below
            # wipes it — they're the one person the line rolls forward without.
            prev_claimant_id = self.claimed_by_id
            self.waiting = False
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.quiet = False
            self.message_id = None
            # The line survives into this load minus whoever just took the
            # machine: A's load finishes, B claims the next one, C is still
            # waiting. Stale taps age out here too, so a line nobody cleared
            # overnight can't strand tomorrow's handoff.
            self.queue = queue.carry_forward(
                self.queue,
                prev_claimant_id,
                dt_util.utcnow().timestamp(),
                float(self.queue_expiry),
            )
            self.emptied = False
            # ...and so did its handoff record. Reset wherever `emptied` is:
            # the two describe the same finished load, and a stale name here
            # would have the new card announcing the last load's handoff.
            self.handoff_name = None
            self.handoff_hedged = False
            self.cancelled = False  # belonged to the load this one supersedes
            # Any pending handoff belonged to the load this one supersedes.
            self._cancel_handoff_timer()
            # ...as did any stop signal still waiting to be confirmed: this load
            # is the proof that whatever stopped, stopped before it.
            self._cancel_stop_confirm()
            self.paused = self._machine_state() == MACHINE_PAUSE
            # Seed the last confirmed phase from the current job state so a
            # caught-up (already-drying) load still detects its finish.
            job = self.hass.states.get(self.job_state_entity)
            phase = job.state if job is not None else None
            self._last_real_phase = phase if phase in REAL_PHASES else None
            # Start directly in the Drying stage if caught already drying.
            self.stage = STAGE_DRYING if phase == JOB_STATE_DRYING else STAGE_WASHING
            # An offline load is detected after the fact (job_state was dark, so
            # no phase to anchor to) — treat it as a mid-cycle pickup so it reads
            # "in progress" and shows no false usage baseline; the detector's
            # idle-timeout closes it once the meter settles.
            self.catch_up = offline or phase in MIDCYCLE_PHASES
            # Capture meter baselines for the usage stat — only meaningful for a
            # load we see from the start (a catch-up has no true baseline).
            if self.catch_up:
                self._energy_start = self._water_start = None
            else:
                self._energy_start = self._entity_float(self.energy_entity)
                self._water_start = self._entity_float(self.water_entity)
            # Anchor the ETA-freshness check + max-session net to this load.
            self._session_started_ts = dt_util.utcnow().timestamp()
            self._offline_since = None
            self._last_eta_ts = None
            self._offline_unverified = False

            # The start post is a normal, visible message with the Claim button
            # so people can call dibs early. It never @mentions anyone — the only
            # ping is to the claimant when the load is done.
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
                self.stage = STAGE_IDLE
                self._detector.reset()  # don't strand the detector as active
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

    async def _async_handle_finished(
        self, *, cancelled: bool = False, retract: bool = False
    ) -> None:
        """End the tracked load. ``cancelled`` means the cycle didn't finish.

        One completion path for every way a load can end (job 'finish', the ETA
        gate, the offline path, the max-session net and now a stop on the
        machine) so the handoff, the queue carry and the persistence can't drift
        apart.

        ``cancelled`` decides what is *said* — the card, the ping — and whether
        a claim tapped from here on is logged. ``retract`` decides the one
        irreversible thing: deleting the claimant's existing history row for
        this load. They are separate arguments because they answer to different
        standards of proof (see :func:`cancel.retracts_history`); a hedged
        wording costs one card, a wrong deletion is permanent.
        """
        async with self._lock:
            if self.stage not in (STAGE_WASHING, STAGE_DRYING):
                return
            self._stop_eta_timer()
            self._cancel_stop_confirm()
            self.stage = STAGE_DONE_WAITING
            self.paused = False
            self.cancelled = cancelled
            # Keep the detector in lockstep with the session, regardless of which
            # path completed the load (dry timer, job 'finish', energy backstop).
            self._detector.reset()
            # Captured before the reset below: it bounds the history retraction
            # to this load's own session.
            started_ts = self._session_started_ts
            self._session_started_ts = None
            self._offline_since = None
            self._last_eta_ts = None
            unverified = self._offline_unverified
            # A pre-claim made during the wash carries through to completion.
            claimed = self.claimed_by != UNCLAIMED and self.claimed_by_id is not None
            self.waiting = not claimed
            # Done is not empty: this load's clothes are still in the drum until
            # somebody says otherwise, so every completion starts un-emptied,
            # and with nobody yet told the machine is theirs.
            self.emptied = False
            self.handoff_name = None
            self.handoff_hedged = False
            # A cancel is not a wash (design doc §2). A claim made *during* the
            # wash has already written its history row, so the row is retracted
            # here — inside the transition that decided this load never
            # happened — rather than being left for a reader to filter out.
            # Bounded to this session's window, so an earlier real load of
            # theirs can't be caught by it. A claim tapped *after* this point is
            # stopped at the other end, by the `cancelled` check in
            # handle_claim: between them a stopped cycle has no route into the
            # model that predicts everybody's wash times.
            # ...and only when the stop is unambiguous enough to stand behind
            # deleting data (``retract``, not ``cancelled``): a real completion
            # that merely beat its own drifting estimate reaches this transition
            # worded as a stop, and must not take somebody's history with it.
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
                # Never "done". The bot knows this cycle didn't finish, and a
                # completion ping that turns out to be wrong is how the pings
                # stop being read at all.
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
                    # Quiet mode: name them, but no @mention and no push — a
                    # visible "done" that won't wake them.
                    await self.bot.async_announce_done(quiet_body)
                elif claimed and self.ping_claimant_on_complete:
                    # The one push per load, routed by whatever the claimant
                    # chose in 🤖 — an @mention in the channel unless they've
                    # opted into a DM. Unset means channel, i.e. unchanged.
                    await self.assistant.async_route_ping(
                        self.claimed_by_id,
                        dm_text=ping_body,
                        channel_text=f"<@{self.claimed_by_id}> {ping_body}",
                    )
                elif not claimed:
                    # Nobody claimed it: no @ping, but drop a short, push-silent
                    # text nudge at the bottom of the channel so the finished load
                    # is visible (the original card above keeps the Claim button).
                    # Plain text, not a second embed, so it isn't a duplicate card.
                    await self.bot.async_announce_done(grabs)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to update finished state")
            # The handoff. A claimed load is released by the claimant's ✅ tap,
            # with the fallback timer as the backstop for when they forget. An
            # *unclaimed* one has nobody to do the emptying, so whoever's next
            # is told straight away — in addition to the up-for-grabs nudge
            # above, which is aimed at the house rather than at them.
            # NOTE: the lock is already held here, hence the _locked variant —
            # asyncio.Lock is not reentrant and would deadlock the session.
            if claimed:
                self._arm_handoff_timer()
            else:
                await self._async_ping_next_locked(hedged=False)
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
        # Taking the machine means you are no longer waiting for it (§4.3). The
        # carry-forward at session start can only drop the *previous* load's
        # claimant — this load's claimant isn't known until right now — so
        # without this the card would read "Claimed by 🧺 Alex / Next up 🔜
        # Alex", and Alex would later be handed the washer they are using.
        self.queue = queue.remove_user(self.queue, user_id)
        if self.stage == STAGE_DONE_WAITING:
            self.waiting = False
        await self._async_save()
        self._notify_entities()
        # A Claim tap is the habit model's only data point (design doc §7.1),
        # and reporting it is the whole of the coordinator's involvement:
        # consent, de-duplication, retention and whether any of it is ever
        # rendered are the assistant's business. Never raises.
        #
        # ...except on a load that was stopped on the machine, which is not a
        # wash and must never move somebody's predicted times. Claiming one is
        # still allowed and still useful — it is how you say "those are mine,
        # I'll deal with it" — it just isn't evidence. The card says "stopped
        # early", so the state that decides the wording is also the state that
        # decides this: one flag, no second notion of a cancelled load.
        if not self.cancelled:
            await self.assistant.async_note_claim(user_id)
        return True

    async def handle_unclaim(self) -> bool:
        """Undo a claim — the load is up for grabs again. Called from the button."""
        if self.stage not in self._CLAIMABLE_STAGES:
            return False
        self.claimed_by = UNCLAIMED
        self.claimed_by_id = None
        self.quiet = False  # quiet belonged to the (now gone) claimant
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

        Returns one of the ``queue.TOGGLE_*`` results so the button can pick its
        reply — ``TOGGLE_STALE`` on a tap against an old card with no live load
        behind it, which is what happens when someone scrolls up in the channel.
        """
        if self.stage not in self._CLAIMABLE_STAGES:
            return queue.TOGGLE_STALE
        now = dt_util.utcnow().timestamp()
        # Prune before toggling: an expired entry must not hold a slot against
        # the cap, and whatever we re-render should already be the current line.
        pruned = queue.prune(self.queue, now, float(self.queue_expiry))
        self.queue, result = queue.toggle_member(pruned, user_id, who, now)
        await self._async_save()
        self._notify_entities()
        return result

    async def handle_emptied(self) -> bool:
        """The claimant confirming the drum is actually clear (the ✅ button).

        This is the handoff trigger — completion only means the machine stopped,
        and pinging someone to a full washer is how the ping stops being
        trusted. Returns False on a stale tap.

        The ``emptied`` half of the guard matters: the button vanishes once
        tapped, but a card further up the channel that never got its edit
        through still shows it — and a second tap must not hand the machine to
        a second person.
        """
        if self.stage != STAGE_DONE_WAITING or self.emptied:
            return False
        self.emptied = True
        # The backstop exists for exactly this not happening; it just did.
        self._cancel_handoff_timer()
        await self._async_save()
        self._notify_entities()
        # The handoff is deliberately *not* awaited. It takes the session lock
        # and does two Discord round trips (the ping, then the card refresh),
        # and the button's callback still has to answer the interaction inside
        # Discord's 3-second window — one rate-limit sleep on the message-edit
        # bucket and the token is dead, leaving the tapper with "interaction
        # failed" and a card that still offers ✅. State is already saved, so
        # the ping is safe to finish on its own.
        self.hass.async_create_task(
            self._async_ping_next(hedged=False, expect_emptied=True)
        )
        return True

    # ------------------------------------------------------------- the handoff
    async def _async_ping_next_locked(self, *, hedged: bool) -> None:
        """Hand the washer to whoever is next. **Assumes the lock is held.**

        ``asyncio.Lock`` is not reentrant, so the caller that already owns it
        (``_async_handle_finished``) must use this variant — taking the lock a
        second time there would deadlock the whole session machine.

        ``hedged`` softens the wording for the fallback timer, where nobody has
        confirmed anything and we genuinely don't know the machine is free.
        """
        now = dt_util.utcnow().timestamp()
        claimant_id = self.claimed_by_id
        # Expiry, the claimant exclusion and the pop are one decision, made in
        # queue.py so they're covered by the pure tests.
        head, self.queue = queue.select_handoff(
            self.queue, now, float(self.queue_expiry), self.claimed_by_id
        )
        # All three handoff moments — the ✅ tap, the fallback timer and an
        # unclaimed completion — arrive here, so this is where "the washer is
        # now free" already exists. Announcing it costs nothing when nobody is
        # listening and saves the planner inventing a second one (§14 rule 5).
        #
        # Announced *after* the pop, and carrying its outcome, because the
        # answer to "is the washer free" is not the same before and after: a
        # machine that has just been handed to the head of the 🔜 line is
        # somebody's, and a listener told otherwise would tell a second person
        # the same machine is theirs. ``hedged`` travels for the same reason —
        # at the backstop nobody has confirmed anything, so "free" is a guess
        # and a listener must be allowed to apply its own stricter test.
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
            return  # nobody waiting — the line being empty is the normal case
        # Recorded *here*, next to the pop that causes the problem, and before
        # the ping rather than after it: the line has already lost them either
        # way, so a ping that fails and gets logged must still leave the card
        # saying who it was for. The card render below picks this up.
        self.handoff_name = queue.entry_name(head)
        self.handoff_hedged = hedged
        if hedged:
            body = (
                "🔜 The washer's been done a while and nobody's checked in — "
                "probably free, worth a look."
            )
        else:
            body = "🔜 Washer's free — you're up."
        try:
            # However they asked to be reached in 🤖, defaulting to a real
            # @mention in the channel (users only) — the whole point is a push,
            # and an embed edit never makes a phone buzz. A DM that bounces
            # falls back to that same mention, so the handoff is never lost.
            await self.assistant.async_route_ping(
                head.get("id"),
                dm_text=body,
                channel_text=f"<@{head.get('id')}> {body}",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to ping the next person in line")
        # The line just moved, so the card's "Next up" is stale. Silent edit,
        # but with the state-derived view attached: on the ✅ path the button's
        # own edit may never land (expired token, transient 5xx), and without
        # this the card would keep offering "✅ Emptied it" forever — there is
        # no further re-render in done_waiting, the ETA timer having stopped.
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

        Both callers are scheduled tasks that can sit behind the lock for a
        while — a session transition posts to Discord while holding it — so the
        check they did before scheduling is stale by the time we get in. A new
        load may have started, or the ✅ tap may have already handed this one
        off, and pinging then would push "washer's free" mid-wash *and* burn
        that person's place in line. ``expect_emptied`` is the state this ping
        was decided under; anything else means it no longer applies.
        """
        async with self._lock:
            if self.stage != STAGE_DONE_WAITING or self.emptied != expect_emptied:
                return
            await self._async_ping_next_locked(hedged=hedged)

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
        # Never fire against a superseded session: a new load may have started
        # since, or the claimant may have cleared it and cancelled this timer's
        # reason to exist a moment before it fired.
        if self.stage != STAGE_DONE_WAITING or self.emptied:
            return
        self.hass.async_create_task(
            self._async_ping_next(hedged=True, expect_emptied=False)
        )

    async def async_reset_session(self) -> None:
        """Service: force-close whatever session is being tracked.

        The backstop for the failures we haven't thought of. Detection ends a
        load on its own in every case it knows about — job 'finish', the ETA
        gate, the flat-energy backstop, an offline completion, and now a stop on
        the machine — but the absolute net is ``MAX_SESSION_MINUTES``, and
        waiting 12 hours for a card to unstick itself is not a recovery plan.

        Deliberately a *reset*, not a completion: it announces nothing, pings
        nobody and hands the machine to no one, because a human calling this is
        saying "you are wrong about the state of the washer", which is no basis
        for telling six other people their laundry is ready. The card is closed
        so it can't be tapped, and the next real load posts a fresh one.

        The 🔜 line is deliberately left alone — the people in it are still
        waiting for the machine, whatever the bot got wrong about the load —
        and so is history: an unwedging is not a statement about whether the
        wash happened, and the cancel path is what retracts a load that didn't.
        """
        async with self._lock:
            self._stop_eta_timer()
            self._cancel_stop_confirm()
            self._cancel_handoff_timer()
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
            # In lockstep with the session, as at every other completion — a
            # detector left ACTIVE would refuse to start the next real load.
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

    async def async_test_post(self) -> None:
        """Debug service: post a sample embed with a working Claim button."""
        async with self._lock:
            self.stage = STAGE_DONE_WAITING
            self.waiting = True
            self.claimed_by = UNCLAIMED
            self.claimed_by_id = None
            self.cancelled = False
            # A real load's handoff must not fire against this synthetic state:
            # the debug post forces done_waiting and repoints message_id, so a
            # timer armed by an actual finished load would sail past its guard,
            # ping the head of the line for a load that no longer exists and
            # overwrite the sample message with the live embed.
            self.emptied = False
            self.handoff_name = None
            self.handoff_hedged = False
            self._cancel_handoff_timer()
            embed = self.build_embed(test=True)
            try:
                self.message_id = await self.bot.async_post(
                    embed,
                    # 🤖 rides along so the panel can be opened (and the DM
                    # route tested) without waiting for a real load.
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
        # Drive the detector + the time-based completion (dry timer / max
        # session) while a cycle is active, then refresh the live embed.
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
                # "Stopped", never "done": the cycle didn't finish and the card
                # says so. Claiming still works — it is how somebody says the
                # clothes in the drum are theirs — so the buttons are unchanged.
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
                    # Grey rather than amber: the machine is idle and the bot is
                    # not waiting on anybody. It deliberately does not borrow the
                    # green of a finished load — this cycle did not finish.
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

        Named only — the line is never @mentioned from the card; the one ping
        anybody in it gets is the handoff, sent as its own message.
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

        Without this the handoff is a disappearance: the ping pops whoever was
        first, so "Next up" loses them at the exact moment they were told, and
        to everyone else the card reads as though they never tapped 🔜 at all.
        Done-waiting only — it is the one stage in which a handoff can have
        happened for the load the card is describing.

        Named, like "Claimed by" and "Next up" above it. This is the live card,
        where the house can already see who is doing what; §11's anonymity rule
        governs the forward plan, not this.
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
