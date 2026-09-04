"""Tests for the session state machine's escape hatches (coordinator.py).

Everything in ``tests/test_detect.py``, ``tests/test_cancel.py`` and
``tests/test_energy_detector.py`` asks whether the *decisions* are right. This
file asks the question those cannot reach: when a decision is wrong, or arrives
late, or arrives against a session that has already moved on, can the machine
still get out of it? Every case here is one where the answer was no — a stage
that could never be left, a session with no time-based ending, a backstop that a
restart threw away, and a lock that could be taken and never released.

``coordinator.py`` imports Home Assistant and ``discord`` for real, so this
imports the integration package the way Home Assistant does (both libraries are
installed) rather than stubbing them. What *is* replaced is the small set of HA
helpers that need a live event loop and a real ``hass`` — ``async_call_later``
and ``async_track_time_interval`` — because what the tests need to know about
those is only ever "was a timer armed", which a recorder answers exactly.

The coordinator itself is built with ``__new__`` and given just the state each
method reads. That is deliberate: ``__init__`` opens a ``Store``, a Discord
client and an assistant, none of which any of these behaviours depend on, and a
test that needed them would be testing the wiring instead of the rule.

Runnable with plain ``python3 tests/test_coordinator.py``.
"""

from __future__ import annotations

import ast
import asyncio
import io
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
    """A task object with the manners the coordinator's set needs.

    ``cancelled()`` and ``exception()`` are *methods*, as they are on a real
    ``asyncio.Task``. They were an attribute and absent respectively until the
    done-callback started asking a finished task what it raised — at which
    point a fake that answered differently from the real thing would have let
    the log-noise regression pass while the integration still logged a stack
    trace an hour.
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

    async def async_close(self) -> None:
        self.closed = True


class FakeAssistant:
    def __init__(self) -> None:
        self.running: list = []

    def note_running(self, started_ts, eta_ts) -> None:
        self.running.append((started_ts, eta_ts))


class Timers:
    """Stands in for ``async_call_later`` / ``async_track_time_interval``.

    Both HA helpers reach for ``hass.loop`` and schedule against a live event
    loop, and every assertion here is only ever "was a timer armed, and for how
    long" — so they are swapped for a recorder at import time. One instance,
    cleared per test, because the module-level names are what the coordinator
    calls and there is nowhere to hand a per-test object.
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
    c.__dict__.update(state)
    # The three things every transition ends with, and none of these tests is
    # about: a Store write, an HA dispatcher send, and a Discord embed.
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
    # REGRESSION (v0.22-0.27, critical): _async_job_confirmed feeds the detector
    # *first* — which can emit EV_FINISHED and queue _async_handle_finished —
    # and only then queues _async_handle_drying on a stage check that is by then
    # stale. The two run FIFO, so the drying edit lands last and used to be
    # refused only for STAGE_IDLE, which let it put a done_waiting session back
    # into drying.
    #
    # That state closed every exit at once: the detector had been reset to idle
    # so it could not finish again, _session_started_ts was None so the 12-hour
    # max-session net read False, _last_eta_ts was None so the offline
    # completion read False, and both start paths return early on "drying" so no
    # later load posted a card either. Somebody was DMed "your laundry's done"
    # and then watched the card go back to "🌀 Drying", and only
    # laundry_discord.reset_session got it out.
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
    # ...and the one case it is actually for still works.
    live = _coordinator(stage=const.STAGE_WASHING, message_id=555)
    _run(live._async_handle_drying())
    assert live.stage == const.STAGE_DRYING
    assert live.bot.calls == [("edit", 555)]


# --- a self-clean nobody can end ---------------------------------------------
def test_a_self_clean_has_the_same_time_nets_a_load_has() -> None:
    # REGRESSION: _check_time_completion returned early for any stage but
    # washing/drying, so a self-clean was covered by neither session_too_long()
    # nor offline_completion_due(). Its only two endings are the energy detector
    # and _schedule_selfclean_end, and an outage silences both together — the
    # detector sees energy=None on every feed, and the end timer is armed by
    # running->"off" or machine_state->"stop", neither of which is
    # "unavailable". A cloud drop outlasting the cycle held the session open for
    # ever, and _on_detector_started returns early on self_clean, so every later
    # load got no card, no claim button and no completion ping.
    TIMERS.armed.clear()
    # Real wall-clock, because the nets read the real clock: these two are the
    # only decisions in the integration that are not handed a moment.
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

    # The offline route too: unavailable long enough, with its last known ETA
    # well past. Same net, same ending.
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

    # A self-clean that is simply running is left alone, which is the point of
    # the nets being time-based rather than stage-based.
    fine = _coordinator(stage=const.STAGE_SELF_CLEAN,
        _session_started_ts=now - 600,
    )
    fine._check_time_completion()
    assert fine.hass.tasks == []


# --- the backstop a restart used to drop --------------------------------------
def test_a_restart_re_arms_the_handoff_backstop() -> None:
    # REGRESSION: _arm_handoff_timer is called from exactly one place, the
    # completion that put the session in done_waiting. async_on_bot_ready
    # restored the ETA timer for an active session and nothing at all for a
    # finished one, so a restart between "done" and the claimant's ✅ dropped the
    # 25-minute backstop — and with it the only remaining route to
    # SIGNAL_WASHER_FREE for that load, so reminders never heard either. Bo, at
    # the head of the 🔜 line, learned nothing until the *next* load completed.
    TIMERS.armed.clear()
    c = _coordinator(stage=const.STAGE_DONE_WAITING,
        message_id=7,
        claimed_by="Alex",
        claimed_by_id=111,
        queue=[{"id": "222", "name": "Bo", "ts": 1.0}],
    )
    c._cfg = {const.CONF_HANDOFF_FALLBACK: 25}  # minutes
    # The detector feed is a separate concern with its own tests, and it wants a
    # full set of live entities; what is under test here is only the timer.
    c._feed_detector = lambda *a, **kw: None
    _run(c.async_on_bot_ready())
    assert [delay for delay, _ in TIMERS.armed] == [25 * 60]

    # Not for a load nobody claimed — that one was handed to the queue at the
    # moment it finished, so there is nothing left to back up...
    TIMERS.armed.clear()
    unclaimed = _coordinator(stage=const.STAGE_DONE_WAITING, message_id=7)
    unclaimed._cfg = {const.CONF_HANDOFF_FALLBACK: 25}
    unclaimed._feed_detector = lambda *a, **kw: None
    _run(unclaimed.async_on_bot_ready())
    assert TIMERS.armed == []

    # ...nor once the claimant has confirmed the drum is clear, which is the
    # one thing the backstop exists to cover for.
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
    """A gateway whose ready event nothing will ever set.

    Exactly what discord.py leaves behind when the gateway task dies: ``login()``
    creates ``Client._ready`` *before* the HTTP call that can fail, so a bad
    token or no network at boot leaves an unset Event and no task alive to set
    it. ``async_run_bot`` has already swallowed the exception and returned.
    """

    async def wait_until_ready(self) -> None:
        # Built inside the coroutine: an Event binds to whichever loop first
        # touches it, and each test here runs its own.
        await asyncio.Event().wait()


def test_no_send_can_wait_for_the_gateway_for_ever() -> None:
    # REGRESSION (critical): every send began with a bare wait_until_ready(),
    # and the coordinator awaits those *inside* the session lock. One wash at
    # 09:00 after a 03:00 restart with the router down took the lock, set the
    # stage to washing, parked in that await and never came back: no card, no
    # completion, sensor.laundry_stage reading "Washing" indefinitely — and
    # laundry_discord.reset_session, the documented way out, takes the same lock
    # and so never ran either. Every health tick queued another blocked task.
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
            # The outer wait_for is the test's own patience, not the code's: it
            # is what turns "this hangs" into a failure instead of a hung suite.
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
    # The bound is only worth anything if every send goes through it, and a send
    # added later is exactly the change that would forget. Read rather than
    # called, in the style of tests/test_copy.py: wait_until_ready may appear in
    # _wait_ready and nowhere else.
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
    # REGRESSION: the coordinator scheduled everything with hass.async_create_task,
    # which ties a task to nothing, and async_shutdown dropped listeners and
    # timers and closed the client without cancelling any of it. A handoff ping
    # scheduled by ✅ Emptied it and parked on a reconnecting gateway then became
    # unkillable *and* unfinishable, because Client.close() clears the ready
    # event and drops the loop: it never pinged the head of the 🔜 line, never
    # refreshed the card, held the old session lock for the life of the process
    # and made hass.async_block_till_done() never return.
    TIMERS.armed.clear()
    c = _coordinator()

    async def _scenario():
        started = asyncio.Event()

        async def _parked():
            started.set()
            await asyncio.Event().wait()  # a wait nothing will ever satisfy

        # Real tasks here, not the recorder the other tests use: cancellation is
        # the whole of what is being asserted, and a task that never started
        # cannot demonstrate being stopped.
        c.hass.async_create_task = asyncio.ensure_future
        c._create_task(_parked())
        assert len(c._tasks) == 1
        task = next(iter(c._tasks))
        await asyncio.sleep(0)  # let it reach the wait
        assert started.is_set() and not task.done()
        await c.async_shutdown()
        assert task.done() and task.cancelled()
        assert c._tasks == set()
        # ...and the client is closed *after* the cancelling, never before: a
        # close first is what made a parked task unwakeable.
        assert c.bot.closed

    _run(_scenario())


def test_every_scheduled_task_is_one_shutdown_can_reach() -> None:
    # The set is only complete if nothing bypasses _create_task, and a callback
    # added later reaching for hass.async_create_task directly is exactly how it
    # would stop being complete. One permitted use: _create_task's own body.
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
    # Bounding the gateway wait turned "hangs for ever" into "raises
    # TimeoutError", which is the right trade — but nothing retrieved that
    # exception, so asyncio logged `Task exception was never retrieved` with a
    # full traceback at ERROR. On a washer whose cloud drops roughly hourly
    # that is a stack trace an hour describing the bot handling an outage
    # exactly as designed, in an integration that otherwise holds itself to
    # 0 info and 1 warning.
    #
    # The expected failure is retrieved and logged at debug. Anything else is
    # re-raised into HA's own handler rather than swallowed: a gateway that was
    # not ready is ordinary, a KeyError in the embed builder is not, and eating
    # the second to silence the first is how a real bug hides for a month.
    coord = coord_mod.LaundryCoordinator.__new__(coord_mod.LaundryCoordinator)
    coord._tasks = set()

    class _Task:
        def __init__(self, exc, cancelled=False):
            self._exc, self._cancelled = exc, cancelled

        def cancelled(self):
            return self._cancelled

        def exception(self):
            return self._exc

    # The ordinary outage: retrieved, so asyncio stays quiet, and dropped.
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
    # ...and a clean finish is the common case.
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
    # REGRESSION (v0.28.0, from a live incident). This washer's cloud drops on
    # a metronome — 19 drops in 15.5 hours, 3087s apart — and on reconnect it
    # republishes the phase it last saw. `_on_job_state` filtered values that
    # were themselves `unavailable` but not values arriving *from* it, while
    # its own comment claimed the opposite. So a stale `wash` replayed on
    # reconnect took the fast-start accelerant and minted a load out of
    # nothing: the session began exactly confirm_delay after the drop, the
    # energy meter never moved, no ETA was ever published, and an hour later
    # it closed itself by announcing a wash that never happened.
    #
    # The rule already existed and was already trusted — cancel.is_flap guards
    # both stop routes, and its docstring cites _on_job_state as the prior art.
    # This was the one place not applying it.
    c = _coordinator()
    c._job_confirm_unsub = None
    c._job_from_flap = False
    c._flap_recovery_ts = None
    c._cfg = {}  # confirm_delay falls back to its default for the time memory
    # The debounce itself is not under test — only which value it settles on,
    # and whether that value is allowed to drive the fast paths.
    c._schedule_job_confirm = lambda: None
    c._flap_times = []
    c._notify_entities = lambda: None
    # _record_flap schedules a save; close it rather than leaving an
    # un-awaited coroutine warning in the output.
    c._create_task = lambda coro: coro.close()
    fed = []
    c._feed_detector = lambda *a, **kw: fed.append(kw.get("allow_early"))
    c._job_phase = lambda: "wash"

    def _age_past_window():
        # The recovery time-memory deliberately outlives one event (see the
        # attribute-churn test). These sub-cases are about the per-value flag
        # alone, so the stamp is aged past the window between them.
        if c._flap_recovery_ts is not None:
            c._flap_recovery_ts -= 10_000

    # The incident: unavailable -> wash, i.e. the cloud coming back.
    c._on_job_state(_Ev("unavailable", "wash"))
    assert c._job_from_flap is True
    c._async_job_confirmed()
    assert fed == [False], "a replayed phase must not arm the fast start"

    # A real start, cloud up throughout, is untouched — the accelerant is the
    # whole reason the card appears before the meter has moved.
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

    # The flag is per settled value, not sticky: a flap-armed debounce that a
    # genuine change then re-arms must not stay suppressed.
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
    # REGRESSION. v0.28.1 stopped a cloud reconnect minting a phantom load, and
    # left the identical hole standing on the other entry point: the restore
    # fed the detector with the job fast-paths ON, asserting "the restored
    # state is settled". Settled was never the question. `_job_phase()` reads
    # whatever the washer integration is publishing at that instant, and for a
    # cloud integration that is routinely the last phase it saw before HA went
    # down — so a stale `wash` started a whole session, card and completion
    # ping included, for a load that never ran.
    #
    # Every reload counts, not only restarts: an options change calls
    # async_reload, which builds a fresh coordinator with _restored = False.
    # Changing a setting must not be able to invent a wash.
    for stage in (const.STAGE_IDLE, const.STAGE_DONE_WAITING):
        c = _coordinator(stage=stage)
        c._restored = False
        fed = []
        c._feed_detector = lambda *a, **kw: fed.append(bool(kw.get("allow_early")))
        c._arm_handoff_timer = lambda: None
        _run(c.async_on_bot_ready())
        assert fed == [False], f"{stage}: a restore must not arm the fast start"

    # The guard is not a blanket ban on feeding the detector — a restore still
    # feeds it, so a load that really is running is picked up by the meter.
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
    # REGRESSION (v0.29.0, critical). The first snapshot read `self.energy_idle`
    # — an attribute that does not exist; the property is `energy_idle_timeout`
    # — so the diagnostics action raised AttributeError on every call, and the
    # handler's never-raise wrapper converted the flagship feature into
    # "could not be read" for every entry, every time. 399 tests stayed green
    # because nothing ever called the REAL method: the pure suite fed check()
    # hand-built dicts, and the one seam between them was the one that broke.
    # This test exists to make that seam a tested path: real method, real
    # properties, and the exact JSON trip the websocket response takes.
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
    # ...and the pure checks accept the real shape end to end.
    findings = diagnose.check(snap["session"], 10_000.0, watched=snap["watched"])
    assert isinstance(findings, list)


def test_attribute_churn_cannot_hand_a_replayed_phase_the_fast_start() -> None:
    # REGRESSION (v0.29.1). HA fires state_changed for attribute-only updates,
    # and _on_job_state used to overwrite _job_from_flap on every one of them:
    # `unavailable -> wash` set the flag, a `wash -> wash` RSSI update seconds
    # later stomped it back to False and re-armed the debounce — so the
    # replayed phase got the accelerant after all, and the incident's phantom
    # returned through a two-event reconnect.
    c = _coordinator()
    c._job_confirm_unsub = None
    c._job_from_flap = False
    c._flap_recovery_ts = None
    c._cfg = {}  # confirm_delay falls back to its default for the time memory
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

    # The launder chain: unavailable -> none, then none -> wash. The second
    # hop's old_state is clean, so the per-value flag alone would wave the
    # replayed phase through; the recovery time-memory is what catches it.
    fed.clear()
    c._on_job_state(_Ev("unavailable", "none"))
    c._on_job_state(_Ev("none", "wash"))
    assert c._job_from_flap is False  # the flag really is blind here
    c._async_job_confirmed()
    assert fed == [False], "the time memory must cover what the flag cannot"

    # ...and it expires: a genuine start long after the reconnect keeps its
    # fast card. (The stamp is aged past the window by hand — the window is
    # confirm_delay + 90, and nothing else in this test advances the clock.)
    fed.clear()
    c._flap_recovery_ts -= 10_000
    c._on_job_state(_Ev("none", "wash"))
    c._async_job_confirmed()
    assert fed == [True]


def test_restore_trusts_the_meter_not_the_replayed_phase() -> None:
    # REGRESSION (v0.29.1). The restore fed the detector with the fast paths
    # fully off, which stopped the restart-phantom but silently regressed the
    # honest cases with it: a genuinely mid-cycle load at restart waited
    # 15-60+ minutes for the next job transition. The split restores exactly
    # the corroborated half: a mid-cycle phase may start a load if and only
    # if the meter has moved since idle.
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
    # The semantic the split must hold: an early phase is trusted only with
    # allow_early (it starts on the cloud's word alone — the phantom risk);
    # a mid-cycle phase is trusted only through allow_catchup (detect demands
    # the meter moved since idle — corroborated by construction). `finish`
    # rides with allow_early because a replayed finish is the mirror phantom.
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
    # REGRESSION (critical, observed firing on the live install as a run of
    # "Failed to post laundry start message"): _async_start_session mutates 20
    # fields before it posts, and the failure path restored two of them. Every
    # failed post silently wiped the superseded load's claimant, its queue, its
    # handoff and its emptied flag, and left a session anchor behind for a
    # session that does not exist.
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
    # Persistent views are registered by custom_id, not per message, so every
    # card the bot ever posted still dispatches into these callbacks. Without a
    # message check, 🧺 on a three-week-old card claims *today's* load and then
    # rewrites that old card with today's embed.
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
    assert claimed == []                       # the live load was never touched
    assert stale.edits == []                   # and the old card was not rewritten
    assert stale.replies and stale.replies[0][1] is True   # a private refusal

    live = _FakeInteraction(message_id=999)
    _run(button.callback(live))
    assert claimed == [("Robin", 7)]           # the current card still works

    # 🤖 is the deliberate exception: it opens a personal panel and touches no
    # load, so it must keep working from a card somebody scrolled back to.
    opened: list = []

    async def _open(interaction):
        opened.append(interaction)

    c.assistant.async_open_panel = _open
    _run(bot_mod._AssistantButton(c).callback(_FakeInteraction(message_id=111)))
    assert len(opened) == 1


# --- the washer can only be handed to one person per load --------------------
def test_the_washer_is_handed_off_only_once_per_load() -> None:
    # The backstop timer pings the head of the line, and the claimant then taps
    # ✅ afterwards. `expect_emptied` catches the opposite order and not this
    # one, so the queue popped twice and two people were each told the same
    # washer was theirs.
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
        assert pings == []                              # nobody pinged twice
        assert [e["id"] for e in c.queue] == [9]        # Sam keeps his place
        assert c.handoff_name == "Alex"                 # and Alex keeps the washer
        assert ("edit", 4242) in c.bot.calls            # the card still refreshes

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
    # REGRESSION (v0.29.1, observed live 2026-09-04): the flat-energy backstop
    # guarded on `energy is not None`, which only says the entity *has* a value
    # -- and `last_rise_ts` is seeded when the load starts rather than on a real
    # rise. A meter frozen since before the session began therefore satisfied
    # "flat for an hour" on a schedule, and the bot announced a load done 60
    # minutes in while job_state still read `drying`. The drum ran for hours.
    now = time.time()
    started = now - 7200  # the load began two hours ago
    c = _coordinator(stage=const.STAGE_WASHING, _session_started_ts=started)
    c._cfg = dict(_ENTITY_CFG)
    frozen = _State("13.9", started - 3600)  # last moved *before* this load
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

    # The backstop is only vetoed, not removed: once the meter has reported for
    # *this* load, a genuinely flat hour still ends it.
    c.hass.states = _States({
        const.DEFAULT_ENERGY_ENTITY: _State("13.9", started + 60),
        const.DEFAULT_JOB_STATE_ENTITY: _State("drying", started + 60),
    })
    c._feed_detector()
    assert finished == [True], "a reporting meter that went flat still finishes"


# --- the shipped dashboard must name entities that actually exist -----------
def test_the_dashboard_only_references_entities_the_platforms_create() -> None:
    # The dashboard is a text file full of entity ids, and a wrong one does not
    # error -- the card just renders "Entity not available", which is
    # indistinguishable from a broken integration to whoever is reading it.
    # This derives the ids from the platform definitions rather than repeating
    # them, so renaming an entity breaks the test rather than the dashboard.
    import yaml
    from homeassistant.util import slugify

    from custom_components.laundry_discord import number as number_mod
    from custom_components.laundry_discord import switch as switch_mod

    pkg = os.path.join(HERE, "..", "custom_components", "laundry_discord")

    def _names(filename):
        """Class-level ``_attr_name`` strings, read from the source.

        Read with ``ast`` rather than by importing and using getattr: Home
        Assistant's entity metaclass rewrites every ``_attr_*`` class attribute
        into a descriptor, so the value is not a string by the time it is an
        attribute. The source is the honest place to ask.
        """
        tree = ast.parse(io.open(os.path.join(pkg, filename), encoding="utf-8").read())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "_attr_name"
                        for t in stmt.targets
                    )
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    found.append(stmt.value.value)
        return found

    expected = set()
    for domain, filename in (
        ("sensor", "sensor.py"),
        ("binary_sensor", "binary_sensor.py"),
        ("button", "button.py"),
    ):
        expected.update(f"{domain}.{slugify(name)}" for name in _names(filename))
    expected.update(f"number.{slugify(row[2])}" for row in number_mod._NUMBERS)
    expected.update(f"switch.{slugify(row[2])}" for row in switch_mod._SWITCHES)
    assert len(expected) >= 15, f"only found {len(expected)} entities: {expected}"

    path = os.path.join(HERE, "..", "dashboards", "laundry.yaml")
    doc = yaml.safe_load(io.open(path, encoding="utf-8").read())

    def _walk(node):
        """Every entity id the dashboard names, in either card spelling.

        `entities:` takes a bare string *or* a mapping with an `entity:` key,
        and cards nest, so this recurses through everything rather than
        special-casing the two shapes -- the first version of this test only
        saw the string form and passed while four cards were unchecked.
        """
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
    # Only our own entities are checked: the washer's ids belong to whichever
    # integration supplies them and are documented as needing a find-replace.
    ours = {e for e in referenced if e.split(".", 1)[-1].startswith("laundry")}
    missing = sorted(ours - expected)
    assert not missing, f"dashboard names entities nothing creates: {missing}"
    # ...and the reverse, so a new control cannot be added without a card.
    unused = sorted(expected - referenced)
    assert not unused, f"entities exist but the dashboard never shows them: {unused}"


def _run_all() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok  {name}")
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"\n{count} passed")


if __name__ == "__main__":
    _run_all()
