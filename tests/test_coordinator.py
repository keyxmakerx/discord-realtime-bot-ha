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
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from custom_components.laundry_discord import coordinator as coord_mod  # noqa: E402
from custom_components.laundry_discord import const  # noqa: E402
from custom_components.laundry_discord import discord_bot as bot_mod  # noqa: E402
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


def _run_all() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok  {name}")
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"\n{count} passed")


if __name__ == "__main__":
    _run_all()
