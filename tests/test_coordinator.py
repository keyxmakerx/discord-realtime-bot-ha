"""Tests for the coordinator's escape hatches: recovering when a decision is
wrong, late, or against a session that has already moved on. Imports the
integration normally rather than stubbing it; only the HA timer helpers are
swapped for a recorder. Runnable with plain python3.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import os
import sys
import time
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from custom_components.laundry_discord import coordinator as coord_mod  # noqa: E402
from custom_components.laundry_discord import const  # noqa: E402
from custom_components.laundry_discord import discord_bot as bot_mod  # noqa: E402
from custom_components.laundry_discord import diagnose  # noqa: E402
from custom_components.laundry_discord.detect import EnergyDetector  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# --- the outside world, recorded rather than run ------------------------------
class FakeHass:
    """Enough ``hass`` for a callback to schedule work and read no states."""

    def __init__(self) -> None:
        self.tasks: list = []
        self.states = _NoStates()

    def async_create_task(self, coro):
        task = _FakeTask(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        pending, self.tasks = self.tasks, []
        for task in pending:
            await task


class _NoStates:
    def get(self, _entity_id):
        return None


class _FakeTask:
    """A task object matching real asyncio.Task's interface.

    cancelled() and exception() are methods, not attributes, since the
    done-callback calls them that way on a finished task.
    """

    def __init__(self, coro) -> None:
        self.coro = coro
        self._cancelled = False
        self._exception = None
        self._done = False
        self._callbacks: list = []

    def done(self) -> bool:
        return self._done

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self):
        if self._cancelled:
            raise asyncio.CancelledError
        return self._exception

    def cancel(self) -> None:
        if self._done:
            return
        self._cancelled = True
        self.coro.close()
        self._finish()

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)

    def _finish(self) -> None:
        self._done = True
        for callback in list(self._callbacks):
            callback(self)

    async def _run(self):
        try:
            if not self._cancelled:
                await self.coro
        except Exception as err:  # noqa: BLE001 - mirrors a real Task
            self._exception = err
        finally:
            self._finish()

    def __await__(self):
        return self._run().__await__()


class FakeBot:
    """Every Discord call, recorded. None of them ever fails or blocks."""

    def __init__(self) -> None:
        self.calls: list = []
        self.closed = False

    async def async_post(self, embed, **kwargs):
        self.calls.append(("post", embed))
        return 999

    async def async_edit(self, message_id, embed, **kwargs):
        self.calls.append(("edit", message_id))

    async def async_announce_done(self, content):
        self.calls.append(("announce", content))

    async def async_close(self) -> None:
        self.closed = True


class FakeAssistant:
    def __init__(self) -> None:
        self.running: list = []

    def note_running(self, started_ts, eta_ts) -> None:
        self.running.append((started_ts, eta_ts))


class Timers:
    """Stands in for async_call_later / async_track_time_interval, recording
    arm calls instead of scheduling on a live loop. One shared instance,
    cleared per test, since the coordinator calls these as module functions.
    """

    def __init__(self) -> None:
        self.armed: list = []

    def call_later(self, hass, delay, action):
        self.armed.append((delay, action))

        def _unsub():
            self.armed[:] = [row for row in self.armed if row[1] is not action]

        return _unsub

    def track_interval(self, hass, action, interval):
        self.armed.append((interval, action))
        return lambda: None


TIMERS = Timers()
coord_mod.async_call_later = TIMERS.call_later
coord_mod.async_track_time_interval = TIMERS.track_interval


def _coordinator(**state):
    """A coordinator carrying only the state the method under test reads."""
    c = coord_mod.LaundryCoordinator.__new__(coord_mod.LaundryCoordinator)
    c.hass = FakeHass()
    c.bot = FakeBot()
    c.assistant = FakeAssistant()
    c._lock = asyncio.Lock()
    c._tasks = set()
    c.stage = const.STAGE_IDLE
    c.message_id = None
    c.emptied = False
    c.claimed_by = const.UNCLAIMED
    c.claimed_by_id = None
    c.waiting = False
    c.quiet = False
    c.queue = []
    c.handoff_name = None
    c.handoff_hedged = False
    c.paused = False
    c.cancelled = False
    c.catch_up = False
    c._restored = False
    c._session_started_ts = None
    c._offline_since = None
    c._last_eta_ts = None
    c._offline_unverified = False
    c._last_real_phase = None
    c._energy_start = c._water_start = None
    c._detector = EnergyDetector(start_jump=0.05, idle_timeout=900)
    c._eta_unsub = None
    c._unsubs = []
    c._job_confirm_unsub = None
    c._stop_confirm_unsub = None
    c._selfclean_unsub = None
    c._handoff_unsub = None
    c._empty_unsub = None
    c.empty_reminded = False
    c.__dict__.update(state)
    # Stubbed: a Store write, dispatcher send, and Discord embed - not what
    # these tests check.
    saves: list = []

    async def _save():
        saves.append(c.stage)

    c._async_save = _save
    c._notify_entities = lambda: None
    c.build_embed = lambda **kw: object()
    c.saves = saves
    return c


# --- the drying edit, arriving after the load has already finished ------------
def test_the_drying_edit_cannot_resurrect_a_finished_session() -> None:
    # A finish and a drying edit can be queued from the same tick; the edit
    # must be refused unless the stage is still washing.
    TIMERS.armed.clear()
    for stage in (
        const.STAGE_DONE_WAITING,
        const.STAGE_IDLE,
        const.STAGE_SELF_CLEAN,
    ):
        c = _coordinator(stage=stage, message_id=555)
        _run(c._async_handle_drying())
        assert c.stage == stage, stage
        assert c.bot.calls == [], stage
        assert c.saves == [], stage
    # The live case still works.
    live = _coordinator(stage=const.STAGE_WASHING, message_id=555)
    _run(live._async_handle_drying())
    assert live.stage == const.STAGE_DRYING
    assert live.bot.calls == [("edit", 555)]


# --- a self-clean nobody can end ---------------------------------------------
def test_a_self_clean_has_the_same_time_nets_a_load_has() -> None:
    # _check_time_completion used to cover only washing/drying; a self-clean
    # has no other way to end once an outage silences the energy detector too.
    TIMERS.armed.clear()
    # These two nets read the real clock rather than an injected moment.
    now = time.time()
    over = now - (const.MAX_SESSION_MINUTES + 60) * 60
    c = _coordinator(stage=const.STAGE_SELF_CLEAN,
        message_id=42,
        _session_started_ts=over,
    )
    c._check_time_completion()
    assert len(c.hass.tasks) == 1, "no completion queued for a stranded self-clean"
    _run(c.hass.drain())
    assert c.stage == const.STAGE_IDLE
    assert c.message_id is None and c._session_started_ts is None

    # Same ending via the offline route: long unavailable, stale ETA.
    offline = _coordinator(stage=const.STAGE_SELF_CLEAN,
        message_id=43,
        _session_started_ts=now - 3600,
        _offline_since=now - 24 * 3600,
        _last_eta_ts=now - 24 * 3600,
    )
    offline._check_time_completion()
    assert len(offline.hass.tasks) == 1
    _run(offline.hass.drain())
    assert offline.stage == const.STAGE_IDLE

    # A running self-clean is left alone.
    fine = _coordinator(stage=const.STAGE_SELF_CLEAN,
        _session_started_ts=now - 600,
    )
    fine._check_time_completion()
    assert fine.hass.tasks == []


# --- the backstop a restart used to drop --------------------------------------
def test_a_restart_re_arms_the_handoff_backstop() -> None:
    # async_on_bot_ready restored the ETA timer for an active session but
    # nothing for a finished one, dropping this backstop on restart.
    TIMERS.armed.clear()
    c = _coordinator(stage=const.STAGE_DONE_WAITING,
        message_id=7,
        claimed_by="Alex",
        claimed_by_id=111,
        queue=[{"id": "222", "name": "Bo", "ts": 1.0}],
    )
    c._cfg = {const.CONF_HANDOFF_FALLBACK: 25, const.CONF_EMPTY_REMINDER: 15}
    # Detector feed is stubbed; only the timers matter here.
    c._feed_detector = lambda *a, **kw: None
    _run(c.async_on_bot_ready())
    assert [delay for delay, _ in TIMERS.armed] == [25 * 60, 15 * 60]

    # A claimant already reminded isn't reminded again after a restart.
    TIMERS.armed.clear()
    reminded = _coordinator(stage=const.STAGE_DONE_WAITING,
        message_id=7,
        claimed_by="Alex",
        claimed_by_id=111,
        empty_reminded=True,
    )
    reminded._cfg = dict(c._cfg)
    reminded._feed_detector = lambda *a, **kw: None
    _run(reminded.async_on_bot_ready())
    assert [delay for delay, _ in TIMERS.armed] == [25 * 60]

    # No claimant means nothing to back up.
    TIMERS.armed.clear()
    unclaimed = _coordinator(stage=const.STAGE_DONE_WAITING, message_id=7)
    unclaimed._cfg = {const.CONF_HANDOFF_FALLBACK: 25}
    unclaimed._feed_detector = lambda *a, **kw: None
    _run(unclaimed.async_on_bot_ready())
    assert TIMERS.armed == []

    # Nor once emptied is confirmed - the backstop's job is done.
    done = _coordinator(stage=const.STAGE_DONE_WAITING,
        message_id=7,
        claimed_by="Alex",
        claimed_by_id=111,
        emptied=True,
    )
    done._cfg = {const.CONF_HANDOFF_FALLBACK: 25}
    done._feed_detector = lambda *a, **kw: None
    _run(done.async_on_bot_ready())
    assert TIMERS.armed == []


# --- a send that could wait for ever ------------------------------------------
class _NeverReadyClient:
    """A gateway whose ready event nothing will ever set - what discord.py
    leaves behind when login() fails before the gateway task starts."""

    async def wait_until_ready(self) -> None:
        # Built here: an Event binds to whichever loop touches it first.
        await asyncio.Event().wait()


def test_no_send_can_wait_for_the_gateway_for_ever() -> None:
    # Every send awaits wait_until_ready() inside the session lock; a gateway
    # that never becomes ready must not hang that lock forever.
    bot = bot_mod.DiscordBot.__new__(bot_mod.DiscordBot)
    bot._client = _NeverReadyClient()
    bot._messages = {}
    original = bot_mod._READY_TIMEOUT
    bot_mod._READY_TIMEOUT = 0.05

    async def _scenario():
        for send in (
            lambda: bot.async_post(object()),
            lambda: bot.async_send_ping("hi"),
            lambda: bot.async_dm_user(1, "hi"),
            lambda: bot.async_announce_done("hi"),
        ):
            # wait_for here is the test's own timeout, not the code's.
            try:
                await asyncio.wait_for(send(), timeout=2)
            except TimeoutError:
                continue
            raise AssertionError("a send returned without a ready gateway")

    try:
        _run(_scenario())
    finally:
        bot_mod._READY_TIMEOUT = original


def test_the_ready_wait_is_bounded_in_one_place_only() -> None:
    # Guards against a new send bypassing the bound: wait_until_ready may
    # appear only inside _wait_ready.
    path = os.path.join(
        HERE, "..", "custom_components", "laundry_discord", "discord_bot.py"
    )
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "wait_until_ready"
            ):
                callers.add(node.name)
    assert callers == {"_wait_ready"}, callers


# --- work that outlives the entry ---------------------------------------------
def test_shutdown_stops_the_work_that_is_already_running() -> None:
    # async_shutdown dropped listeners/timers and closed the client without
    # cancelling scheduled tasks, leaving a parked one unkillable.
    TIMERS.armed.clear()
    c = _coordinator()

    async def _scenario():
        started = asyncio.Event()

        async def _parked():
            started.set()
            await asyncio.Event().wait()  # never satisfied

        # Real tasks, not the recorder: cancellation itself is under test.
        c.hass.async_create_task = asyncio.ensure_future
        c._create_task(_parked())
        assert len(c._tasks) == 1
        task = next(iter(c._tasks))
        await asyncio.sleep(0)  # let it reach the wait
        assert started.is_set() and not task.done()
        await c.async_shutdown()
        assert task.done() and task.cancelled()
        assert c._tasks == set()
        # Closed after cancelling, never before - a close first is what made
        # a parked task unwakeable.
        assert c.bot.closed

    _run(_scenario())


def test_every_scheduled_task_is_one_shutdown_can_reach() -> None:
    # Guards against a new callback bypassing _create_task: only _create_task
    # itself may call async_create_task.
    path = os.path.join(
        HERE, "..", "custom_components", "laundry_discord", "coordinator.py"
    )
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    callers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "async_create_task"
            ):
                callers.append(node.name)
    assert callers == ["_create_task"], callers


def test_a_gateway_outage_does_not_log_a_stack_trace_every_hour() -> None:
    # An unretrieved TimeoutError from the bounded wait logs a full traceback
    # every outage; _task_done retrieves and debug-logs only that one.
    coord = coord_mod.LaundryCoordinator.__new__(coord_mod.LaundryCoordinator)
    coord._tasks = set()

    class _Task:
        def __init__(self, exc, cancelled=False):
            self._exc, self._cancelled = exc, cancelled

        def cancelled(self):
            return self._cancelled

        def exception(self):
            return self._exc

    # Retrieved and dropped: asyncio stays quiet.
    timed_out = _Task(TimeoutError("gateway not ready"))
    coord._tasks.add(timed_out)
    coord._task_done(timed_out)
    assert timed_out not in coord._tasks

    # A real bug still reaches HA's handler instead of being swallowed.
    bug = _Task(KeyError("claimed_by"))
    coord._tasks.add(bug)
    try:
        coord._task_done(bug)
    except KeyError:
        pass
    else:  # pragma: no cover - the point of the test
        raise AssertionError("an unexpected exception was silently swallowed")
    assert bug not in coord._tasks

    # A cancelled task at unload is neither: asking it for .exception() raises.
    cancelled = _Task(None, cancelled=True)
    coord._tasks.add(cancelled)
    coord._task_done(cancelled)
    assert cancelled not in coord._tasks
    ok = _Task(None)
    coord._tasks.add(ok)
    coord._task_done(ok)
    assert ok not in coord._tasks


class _Ev:
    """A minimal state-change event: only new_state/old_state are read."""

    class _S:
        def __init__(self, state):
            self.state = state

    def __init__(self, old, new):
        self.data = {
            "old_state": None if old is None else self._S(old),
            "new_state": None if new is None else self._S(new),
        }


def test_a_reconnect_cannot_mint_a_load_out_of_a_replayed_phase() -> None:
    # _on_job_state filtered values arriving AS unavailable, not values
    # arriving FROM it: a replayed phase must not get the fast-start accelerant.
    c = _coordinator()
    c._job_confirm_unsub = None
    c._job_from_flap = False
    c._flap_recovery_ts = None
    c._cfg = {}  # confirm_delay falls back to its default for the time memory
    # Only the settled value and whether it drives the fast paths matter here.
    c._schedule_job_confirm = lambda: None
    c._flap_times = []
    c._notify_entities = lambda: None
    # Closes the coroutine _record_flap schedules, to avoid an un-awaited
    # coroutine warning.
    c._create_task = lambda coro: coro.close()
    fed = []
    c._feed_detector = lambda *a, **kw: fed.append(kw.get("allow_early"))
    c._job_phase = lambda: "wash"

    def _age_past_window():
        # Ages the recovery stamp past its window; these cases test the
        # per-value flag alone.
        if c._flap_recovery_ts is not None:
            c._flap_recovery_ts -= 10_000

    # unavailable -> wash: the cloud reconnecting.
    c._on_job_state(_Ev("unavailable", "wash"))
    assert c._job_from_flap is True
    c._async_job_confirmed()
    assert fed == [False], "a replayed phase must not arm the fast start"

    # Cloud up throughout: untouched, since the accelerant is why the card
    # appears before the meter moves.
    fed.clear()
    _age_past_window()
    c._on_job_state(_Ev("none", "wash"))
    assert c._job_from_flap is False
    c._async_job_confirmed()
    assert fed == [True]

    # First-ever reading (no prior state) counts as a flap, matching is_flap.
    fed.clear()
    c._on_job_state(_Ev(None, "wash"))
    c._async_job_confirmed()
    assert fed == [False]

    # Not sticky: a later genuine change must re-arm despite an earlier flap.
    fed.clear()
    c._on_job_state(_Ev("unavailable", "wash"))
    c._on_job_state(_Ev("wash", "rinse"))
    _age_past_window()
    c._async_job_confirmed()
    assert fed == [True]

    # ...and it is consumed, so a later tick cannot inherit it.
    fed.clear()
    c._async_job_confirmed()
    assert fed == [True]

    # A value that IS a flap still returns early and arms nothing.
    fed.clear()
    c._job_from_flap = False
    c._on_job_state(_Ev("wash", "unavailable"))
    assert fed == [] and c._job_from_flap is False


def test_a_restart_or_reload_cannot_invent_a_wash_either() -> None:
    # _job_phase() reads whatever is published now, often a stale phase from
    # before HA restarted or reloaded; neither may trust it as a fresh wash.
    for stage in (const.STAGE_IDLE, const.STAGE_DONE_WAITING):
        c = _coordinator(stage=stage)
        c._restored = False
        fed = []
        c._feed_detector = lambda *a, **kw: fed.append(bool(kw.get("allow_early")))
        c._arm_handoff_timer = lambda: None
        _run(c.async_on_bot_ready())
        assert fed == [False], f"{stage}: a restore must not arm the fast start"

    # Restore still feeds the detector - a load that is really running is
    # still picked up by the meter.
    assert fed, "the restore must still feed the detector"

    # And an active session restores its ETA timer rather than feeding at all.
    live = _coordinator(stage=const.STAGE_WASHING, message_id=555)
    live._restored = False
    live._feed_detector = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("an active restore must not re-feed the detector")
    )
    live._start_eta_timer = lambda: None
    _run(live.async_on_bot_ready())


def test_the_diagnostic_snapshot_is_reachable_and_serialisable() -> None:
    # diagnostic_snapshot() once read a nonexistent attribute, hidden by a
    # never-raise wrapper. Exercise the real method and a real JSON round trip.
    import json as _json

    c = _coordinator(stage=const.STAGE_WASHING, message_id=1542881883527057553)
    c._cfg = {const.CONF_RUNNING_ENTITY: "binary_sensor.washer_running",
              const.CONF_JOB_STATE_ENTITY: "sensor.job",
              const.CONF_ETA_ENTITY: "sensor.eta"}
    c._flap_times = [1.0, 2.0]
    c.queue = [{"id": 4242, "name": "Alex", "ts": 3.0}]  # snowflake int rides
    snap = c.diagnostic_snapshot()
    _json.dumps(snap)  # the response path must survive it verbatim
    assert snap["config"]["energy_idle_s"] == const.DEFAULT_ENERGY_IDLE * 60
    assert snap["session"]["stage"] == const.STAGE_WASHING
    assert snap["session"]["detector"]["phase"] is not None
    assert snap["watched"]["running"] is None  # unset entity = None, honestly
    # The pure checks accept the real shape too.
    findings = diagnose.check(snap["session"], 10_000.0, watched=snap["watched"])
    assert isinstance(findings, list)


def test_attribute_churn_cannot_hand_a_replayed_phase_the_fast_start() -> None:
    # HA fires state_changed for attribute-only updates too; those must not
    # overwrite _job_from_flap and re-arm the debounce for a replayed phase.
    c = _coordinator()
    c._job_confirm_unsub = None
    c._job_from_flap = False
    c._flap_recovery_ts = None
    c._cfg = {}
    c._schedule_job_confirm = lambda: None
    c._flap_times = []
    c._notify_entities = lambda: None
    c._create_task = lambda coro: coro.close()
    fed = []
    c._feed_detector = lambda *a, **kw: fed.append(kw.get("allow_early"))
    c._job_phase = lambda: "wash"

    c._on_job_state(_Ev("unavailable", "wash"))
    assert c._job_from_flap is True
    c._on_job_state(_Ev("wash", "wash"))  # attribute-only churn
    assert c._job_from_flap is True, "same-state events must not touch the flag"
    c._async_job_confirmed()
    assert fed == [False]

    # unavailable -> none -> wash: the second hop's old_state is clean, so
    # only the recovery time-memory (not the per-value flag) catches it.
    fed.clear()
    c._on_job_state(_Ev("unavailable", "none"))
    c._on_job_state(_Ev("none", "wash"))
    assert c._job_from_flap is False  # the flag really is blind here
    c._async_job_confirmed()
    assert fed == [False], "the time memory must cover what the flag cannot"

    # Expires: a genuine start well after reconnect still gets the fast card
    # (window is confirm_delay + 90, aged by hand here).
    fed.clear()
    c._flap_recovery_ts -= 10_000
    c._on_job_state(_Ev("none", "wash"))
    c._async_job_confirmed()
    assert fed == [True]


def test_restore_trusts_the_meter_not_the_replayed_phase() -> None:
    # Turning the fast paths fully off on restore fixed the phantom but broke
    # genuinely mid-cycle loads too; one may start only if the meter moved
    # since idle.
    for stage in (const.STAGE_IDLE, const.STAGE_DONE_WAITING):
        c = _coordinator(stage=stage)
        c._restored = False
        c._arm_handoff_timer = lambda: None
        seen = []
        c._feed_detector = lambda *a, **kw: seen.append(
            (kw.get("allow_early"), kw.get("allow_catchup"))
        )
        _run(c.async_on_bot_ready())
        assert seen == [(None, True)], f"{stage}: catch-up only at restore"


def test_the_accel_split_separates_the_two_bets() -> None:
    # allow_early trusts an early phase on the cloud's word alone (the
    # phantom risk); allow_catchup trusts a mid-cycle phase only because the
    # meter corroborates it. finish rides with allow_early for the same reason.
    c = _coordinator(stage=const.STAGE_IDLE)
    c._cfg = {}  # the entity properties read config even when their reads are stubbed
    c._flap_times = []
    seen = {}

    class _Det:
        phase = "idle"
        last_energy = 11.6
        last_rise_ts = None
        idle_energy = 11.6

        def observe(self, *a, **kw):
            seen.update(kw)
            return None

    c._detector = _Det()
    c._track_offline = lambda: None
    c._eta_status = lambda: (False, False)
    c._wrinkle_active = lambda: False
    c._machine_state = lambda: None
    c._entity_float = lambda _x: 11.6

    c._job_phase = lambda: "wash"  # early phase
    c._feed_detector(allow_early=False, allow_catchup=True)
    assert seen["job_is_real"] is False and seen["job_is_early"] is False
    c._feed_detector(allow_early=True, allow_catchup=False)
    assert seen["job_is_real"] is True and seen["job_is_early"] is True

    c._job_phase = lambda: "rinse"  # mid-cycle phase
    c._feed_detector(allow_early=True, allow_catchup=False)
    assert seen["job_is_real"] is False
    c._feed_detector(allow_early=False, allow_catchup=True)
    assert seen["job_is_real"] is True and seen["job_is_early"] is False

    c._job_phase = lambda: "finish"
    c._feed_detector(allow_early=False, allow_catchup=True)
    assert seen["job_is_finish"] is False
    c._feed_detector(allow_early=True, allow_catchup=False)
    assert seen["job_is_finish"] is True


# The three entity properties with no default; everything else falls back.
_ENTITY_CFG = {
    const.CONF_RUNNING_ENTITY: const.DEFAULT_RUNNING_ENTITY,
    const.CONF_JOB_STATE_ENTITY: const.DEFAULT_JOB_STATE_ENTITY,
    const.CONF_ETA_ENTITY: const.DEFAULT_ETA_ENTITY,
}


class _FakeInteraction:
    """A component interaction with only the surface a card button touches."""

    def __init__(self, message_id) -> None:
        self.message = types.SimpleNamespace(id=message_id)
        self.user = types.SimpleNamespace(display_name="Robin", id=7)
        self.replies: list = []
        self.edits: list = []
        outer = self

        class _Response:
            async def send_message(self, text, ephemeral=False):
                outer.replies.append((text, ephemeral))

            async def edit_message(self, **kwargs):
                outer.edits.append(kwargs)

        class _Followup:
            async def send(self, text, ephemeral=False):
                outer.replies.append((text, ephemeral))

        self.response = _Response()
        self.followup = _Followup()


# --- a start post that fails must not take the previous load with it ---------
def test_a_failed_start_post_puts_the_superseded_load_back() -> None:
    # _async_start_session mutates ~20 fields before posting; the old failure
    # path restored only two, wiping the superseded load's state.
    watched = (
        "stage", "waiting", "claimed_by", "claimed_by_id", "quiet", "message_id",
        "queue", "emptied", "handoff_name", "handoff_hedged", "cancelled",
        "paused", "catch_up", "_last_real_phase", "_energy_start", "_water_start",
        "_session_started_ts", "_offline_since", "_last_eta_ts",
        "_offline_unverified",
    )
    c = _coordinator(
        stage=const.STAGE_DONE_WAITING,
        claimed_by="Robin",
        claimed_by_id=7,
        waiting=True,
        emptied=True,
        quiet=True,
        message_id=4242,
        queue=[{"id": 9, "name": "Sam", "ts": time.time()}],
        handoff_name="Sam",
        handoff_hedged=True,
        cancelled=True,
        catch_up=True,
        _energy_start=1.5,
        _water_start=20.0,
        _session_started_ts=123.0,
        _offline_since=99.0,
        _last_eta_ts=456.0,
        _offline_unverified=True,
        _last_real_phase="spin",
    )
    c._cfg = dict(_ENTITY_CFG)
    before = {name: getattr(c, name) for name in watched}

    async def _boom(*args, **kwargs):
        raise RuntimeError("Discord is down")

    c.bot.async_post = _boom
    logger = logging.getLogger(coord_mod.__name__)
    was = logger.level
    logger.setLevel(logging.CRITICAL)  # the handler logs the traceback we caused
    try:
        _run(c._async_start_session())
    finally:
        logger.setLevel(was)

    for name, value in before.items():
        assert getattr(c, name) == value, f"{name} not restored: {getattr(c, name)!r}"
    # ...and the detector must not be left believing a wash is running.
    assert c._detector.phase == "idle"


# --- a tap on a card the bot is no longer tracking ---------------------------
def test_a_tap_on_an_older_card_cannot_touch_the_live_load() -> None:
    # Buttons dispatch by custom_id, not per message; a message check must
    # stop an old card from claiming today's load.
    c = _coordinator(stage=const.STAGE_WASHING, message_id=999)
    c._cfg = dict(_ENTITY_CFG)
    claimed: list = []

    async def _claim(who, user_id):
        claimed.append((who, user_id))
        return True

    async def _dm_notice(_interaction):
        return None

    c.handle_claim = _claim
    c.assistant.async_followup_dm_notice = _dm_notice
    button = bot_mod._ClaimButton(c)

    stale = _FakeInteraction(message_id=111)
    _run(button.callback(stale))
    assert claimed == []
    assert stale.edits == []
    assert stale.replies and stale.replies[0][1] is True   # a private refusal

    live = _FakeInteraction(message_id=999)
    _run(button.callback(live))
    assert claimed == [("Robin", 7)]

    # The assistant button is the exception: it touches no load, so it must
    # keep working from an old card.
    opened: list = []

    async def _open(interaction):
        opened.append(interaction)

    c.assistant.async_open_panel = _open
    _run(bot_mod._AssistantButton(c).callback(_FakeInteraction(message_id=111)))
    assert len(opened) == 1


# --- the washer can only be handed to one person per load --------------------
def test_the_washer_is_handed_off_only_once_per_load() -> None:
    # expect_emptied only catches the opposite ordering; a second ping after
    # a backstop handoff must not pop the queue again.
    was_send = coord_mod.async_dispatcher_send
    coord_mod.async_dispatcher_send = lambda *a, **kw: None
    try:
        now = time.time()
        pings: list = []

        async def _route(user_id, **kwargs):
            pings.append(user_id)

        # The backstop has already handed it to Alex.
        c = _coordinator(
            stage=const.STAGE_DONE_WAITING,
            claimed_by="Robin",
            claimed_by_id=7,
            emptied=True,
            message_id=4242,
            queue=[{"id": 9, "name": "Sam", "ts": now}],
            handoff_name="Alex",
            handoff_hedged=True,
        )
        c._cfg = dict(_ENTITY_CFG)
        c.assistant.async_route_ping = _route
        _run(c._async_ping_next_locked(hedged=False))
        assert pings == []
        assert [e["id"] for e in c.queue] == [9]
        assert c.handoff_name == "Alex"
        assert ("edit", 4242) in c.bot.calls

        # The first handoff of a load is unaffected.
        fresh = _coordinator(
            stage=const.STAGE_DONE_WAITING,
            claimed_by="Robin",
            claimed_by_id=7,
            emptied=True,
            message_id=4242,
            queue=[{"id": 9, "name": "Sam", "ts": now}],
        )
        fresh._cfg = dict(_ENTITY_CFG)
        fresh.assistant.async_route_ping = _route
        _run(fresh._async_ping_next_locked(hedged=False))
        assert pings == [9]
        assert fresh.queue == []
        assert fresh.handoff_name == "Sam"
    finally:
        coord_mod.async_dispatcher_send = was_send


class _State:
    """A minimal HA State: the value, and when it last actually changed."""

    def __init__(self, state, last_changed_ts) -> None:
        self.state = state
        self.last_changed = datetime.fromtimestamp(last_changed_ts, timezone.utc)
        self.last_updated = self.last_changed


class _States:
    def __init__(self, mapping) -> None:
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


# --- a meter that never reported must not be read as a finished load ---------
def test_a_meter_that_never_reported_cannot_finish_a_load() -> None:
    # The flat-energy backstop only checked energy is not None; a meter
    # frozen since before this load must not count as flat for an hour.
    now = time.time()
    started = now - 7200  # the load began two hours ago
    c = _coordinator(stage=const.STAGE_WASHING, _session_started_ts=started)
    c._cfg = dict(_ENTITY_CFG)
    frozen = _State("13.9", started - 3600)  # last moved before this load
    c.hass.states = _States({
        const.DEFAULT_ENERGY_ENTITY: frozen,
        const.DEFAULT_JOB_STATE_ENTITY: _State("drying", started + 60),
    })
    c._detector = EnergyDetector(start_jump=0.3, idle_timeout=3600)
    c._detector.phase = coord_mod.RUN_ACTIVE
    c._detector.last_energy = 13.9
    c._detector.last_rise_ts = started
    finished: list = []
    c._on_detector_finished = lambda: finished.append(True)

    c._feed_detector()
    assert finished == [], "a dead meter must not complete a load"
    assert c._detector.phase == coord_mod.RUN_ACTIVE

    # Only vetoed, not removed: once the meter reports for this load, a flat
    # hour still ends it.
    c.hass.states = _States({
        const.DEFAULT_ENERGY_ENTITY: _State("13.9", started + 60),
        const.DEFAULT_JOB_STATE_ENTITY: _State("drying", started + 60),
    })
    c._feed_detector()
    assert finished == [True], "a reporting meter that went flat still finishes"


# --- picking up a load the bot never noticed ---------------------------------
def _trackable(**state):
    c = _coordinator(**state)
    c._cfg = dict(_ENTITY_CFG)
    c._start_eta_timer = lambda: None  # needs a real HA time tracker
    return c


def test_the_missed_load_button_picks_the_wash_up_as_a_catch_up() -> None:
    # The escape hatch when the bot never opened a session for a full cycle:
    # no card, no Claim button, no completion ping, and reset_session doesn't help.
    c = _trackable(stage=const.STAGE_DONE_WAITING, claimed_by="Ginko",
                   claimed_by_id=4188, message_id=4242)
    assert _run(c.async_track_current_load()) is True
    assert c.stage == const.STAGE_WASHING
    # A catch-up, not a fresh start - no start time or usage baseline is known.
    assert c.catch_up is True
    assert c._energy_start is None and c._water_start is None
    # The previous claim is cleared too.
    assert c.claimed_by == const.UNCLAIMED and c.claimed_by_id is None
    # The detector must move with the session; left idle behind an active
    # stage it is the wedge diagnose calls unrecoverable.
    assert c._detector.phase == "active"
    assert c._detector.last_rise_ts is not None
    # Dated from now, not the meter: a stale reading would arm the backstop
    # to fire almost immediately.
    assert c._detector.last_rise_ts >= c._session_started_ts - 1
    # Saved after the detector moved, or a restart restores the wedge.
    assert c.saves, "the detector change was never persisted"


def test_it_will_not_hijack_a_load_that_is_already_being_tracked() -> None:
    # Not an override; repointing a live card at a different wash would
    # strand whoever claimed it. reset_session is the fix instead.
    for stage in (const.STAGE_WASHING, const.STAGE_DRYING, const.STAGE_SELF_CLEAN):
        c = _trackable(stage=stage, claimed_by="Robin", claimed_by_id=7,
                       message_id=4242)
        assert _run(c.async_track_current_load()) is False
        assert c.stage == stage
        assert c.claimed_by == "Robin"      # untouched
        assert c._detector.phase == "idle"  # and never armed


def test_a_failed_post_leaves_no_wedge_behind() -> None:
    # The detector is armed only after the post lands; arming it first would
    # leave it ACTIVE against an idle session, refusing every load until reset.
    c = _trackable(stage=const.STAGE_DONE_WAITING, claimed_by="Ginko",
                   claimed_by_id=4188, message_id=4242)

    async def _boom(*args, **kwargs):
        raise RuntimeError("Discord is down")

    c.bot.async_post = _boom
    logger = logging.getLogger(coord_mod.__name__)
    was = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        assert _run(c.async_track_current_load()) is False
    finally:
        logger.setLevel(was)
    assert c.stage == const.STAGE_DONE_WAITING     # rolled back
    assert c.claimed_by == "Ginko"                 # ...with the claim intact
    assert c._detector.phase == "idle"             # and no wedge


def test_the_dashboard_only_references_entities_the_platforms_create() -> None:
    # A wrong entity id fails silently in the UI ("Entity not available"), so
    # ids are derived from the platforms' translation keys instead of hardcoded.
    import yaml
    from homeassistant.util import slugify

    from custom_components.laundry_discord import number as number_mod
    from custom_components.laundry_discord import switch as switch_mod

    pkg = os.path.join(HERE, "..", "custom_components", "laundry_discord")
    with io.open(os.path.join(pkg, "translations", "en.json"), encoding="utf-8") as fh:
        names = json.load(fh)["entity"]

    def _translation_keys(filename):
        """Class-level _attr_translation_key strings, read via ast since HA's
        entity metaclass turns _attr_* attributes into descriptors."""
        tree = ast.parse(io.open(os.path.join(pkg, filename), encoding="utf-8").read())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "_attr_translation_key"
                        for t in stmt.targets
                    )
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    found.append(stmt.value.value)
        return found

    keys = {
        "sensor": _translation_keys("sensor.py"),
        "binary_sensor": _translation_keys("binary_sensor.py"),
        "button": _translation_keys("button.py"),
        "number": [row[0] for row in number_mod._NUMBERS],
        "switch": [row[0] for row in switch_mod._SWITCHES],
    }
    expected = {
        domain + "." + slugify(const.DEVICE_NAME + " " + names[domain][key]["name"])
        for domain, domain_keys in keys.items()
        for key in domain_keys
    }
    assert len(expected) >= 15, f"only found {len(expected)} entities: {expected}"

    path = os.path.join(HERE, "..", "dashboards", "laundry.yaml")
    doc = yaml.safe_load(io.open(path, encoding="utf-8").read())

    def _walk(node):
        """Every entity id the dashboard names, as a string or an ``entity:``."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "entity" and isinstance(value, str):
                    yield value
                else:
                    yield from _walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    yield item
                else:
                    yield from _walk(item)

    referenced = set(_walk(doc))
    assert referenced, "the dashboard referenced no entities at all"
    # Only our own entities; the washer's ids belong to another integration.
    ours = {e for e in referenced if e.split(".", 1)[-1].startswith("laundry")}
    missing = sorted(ours - expected)
    assert not missing, f"dashboard names entities nothing creates: {missing}"
    unused = sorted(expected - referenced)
    assert not unused, f"entities exist but the dashboard never shows them: {unused}"



# --- the empty-it reminder, the washer-free line and the claim signal ----------
def _no_dispatch():
    """Swap the module's dispatcher send for a recorder; returns (sent, undo)."""
    sent: list = []
    was = coord_mod.async_dispatcher_send
    coord_mod.async_dispatcher_send = lambda hass, signal, *args: sent.append(
        (signal, args)
    )

    def _undo():
        coord_mod.async_dispatcher_send = was

    return sent, _undo


def test_the_claimant_is_reminded_once_while_the_load_is_not_emptied() -> None:
    calls: list = []

    async def _remind(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return "dm"

    c = _coordinator(stage=const.STAGE_DONE_WAITING,
        claimed_by="Alex",
        claimed_by_id=111,
        queue=[{"id": 222, "name": "Bo", "ts": time.time()}],
    )
    c._cfg = dict(_ENTITY_CFG)
    c.assistant.async_remind_empty = _remind
    _run(c._async_send_empty_reminder())
    assert calls == [(111, {"name": "Alex", "waiting": True, "quiet": False})]
    assert c.empty_reminded
    _run(c._async_send_empty_reminder())
    assert len(calls) == 1  # once per load

    emptied = _coordinator(stage=const.STAGE_DONE_WAITING,
        claimed_by="Alex", claimed_by_id=111, emptied=True,
    )
    emptied._cfg = dict(_ENTITY_CFG)
    emptied.assistant.async_remind_empty = _remind
    _run(emptied._async_send_empty_reminder())
    assert len(calls) == 1


def test_emptying_cancels_the_reminder_and_announces_the_washer_is_free() -> None:
    sent, undo = _no_dispatch()
    try:
        TIMERS.armed.clear()
        c = _coordinator(stage=const.STAGE_DONE_WAITING,
            claimed_by="Robin", claimed_by_id=7, message_id=4242,
        )
        c._cfg = {**_ENTITY_CFG, const.CONF_EMPTY_REMINDER: 15}
        c._arm_empty_timer()
        assert [delay for delay, _ in TIMERS.armed] == [15 * 60]
        _run(c.handle_emptied())
        _run(c.hass.drain())
        assert TIMERS.armed == []
        assert ("announce", "🧺 Washer's free.") in c.bot.calls
    finally:
        undo()


def test_the_free_line_names_the_next_person_unless_they_were_told_in_channel() -> None:
    sent, undo = _no_dispatch()
    try:
        for route, expected in (
            ("dm", [("announce", "🔜 Washer's free — Sam's up next.")]),
            (None, [("announce", "🔜 Washer's free — Sam's up next.")]),
            ("channel", []),
        ):
            async def _route(user_id, _route=route, **kwargs):
                return _route

            c = _coordinator(stage=const.STAGE_DONE_WAITING,
                claimed_by="Robin", claimed_by_id=7, emptied=True,
                queue=[{"id": 9, "name": "Sam", "ts": time.time()}],
            )
            c._cfg = dict(_ENTITY_CFG)
            c.assistant.async_route_ping = _route
            _run(c._async_ping_next_locked(hedged=False))
            assert [call for call in c.bot.calls if call[0] == "announce"] == expected

        # Switched off, and an unclaimed completion (which already posted), say nothing.
        off = _coordinator(stage=const.STAGE_DONE_WAITING, emptied=True)
        off._cfg = {**_ENTITY_CFG, const.CONF_ANNOUNCE_FREE: False}
        _run(off._async_ping_next_locked(hedged=False))
        quiet = _coordinator(stage=const.STAGE_DONE_WAITING)
        quiet._cfg = dict(_ENTITY_CFG)
        _run(quiet._async_ping_next_locked(hedged=False, announce=False))
        hedged = _coordinator(stage=const.STAGE_DONE_WAITING, claimed_by_id=7)
        hedged._cfg = dict(_ENTITY_CFG)
        _run(hedged._async_ping_next_locked(hedged=True))
        for coordinator in (off, quiet, hedged):
            assert not [call for call in coordinator.bot.calls if call[0] == "announce"]
    finally:
        undo()


def test_a_claim_signals_the_reminder_loop_and_arms_a_late_reminder() -> None:
    sent, undo = _no_dispatch()
    try:
        TIMERS.armed.clear()
        noted: list = []

        async def _note(user_id):
            noted.append(user_id)

        washing = _coordinator(stage=const.STAGE_WASHING)
        washing._cfg = dict(_ENTITY_CFG)
        washing.assistant.async_note_claim = _note
        assert _run(washing.handle_claim("Bo", 222))
        assert (const.SIGNAL_LOAD_CLAIMED, ({"claimant_id": 222},)) in sent
        assert TIMERS.armed == []  # armed at completion, not at a mid-wash claim

        # A stopped load is not a wash: no signal.
        sent.clear()
        stopped = _coordinator(stage=const.STAGE_DONE_WAITING, cancelled=True)
        stopped._cfg = {**_ENTITY_CFG, const.CONF_EMPTY_REMINDER: 15}
        stopped.assistant.async_note_claim = _note
        _run(stopped.handle_claim("Bo", 222))
        assert sent == []
        # ...but claiming a finished load still arms its empty-it reminder.
        assert [delay for delay, _ in TIMERS.armed] == [15 * 60]
    finally:
        undo()


def test_joining_from_a_dm_adds_once_and_refuses_a_dead_card() -> None:
    c = _coordinator(stage=const.STAGE_DRYING, message_id=5)
    c._cfg = dict(_ENTITY_CFG)
    assert _run(c.handle_next_join("Sam", 9)) == ("added", 1)
    _run(c.hass.drain())
    assert ("edit", 5) in c.bot.calls  # the card's "Next up" is re-rendered
    assert _run(c.handle_next_join("Sam", 9)) == ("already", 1)
    assert [entry["id"] for entry in c.queue] == [9]
    idle = _coordinator()
    assert _run(idle.handle_next_join("Sam", 9)) == ("stale", None)


def _run_all() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok  {name}")
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"\n{count} passed")


if __name__ == "__main__":
    _run_all()
