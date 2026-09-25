"""Tests for the reminder loop: which trigger fired, what the coordinator was
doing, which config entry owns the loop, and what a DM reply acts on.

``reminders.py`` is imported for real with ``homeassistant`` and ``discord``
stubbed out; ``const``, ``habit``, ``nudge`` and ``plan`` are the real modules.
Runnable with plain ``python3 tests/test_reminder_loop.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(HERE, "..", "custom_components", "laundry_discord")


# --- the outside world, stubbed ----------------------------------------------
def _install_stubs() -> None:
    """Enough of ``homeassistant`` and ``discord`` for reminders.py to import.

    Deliberately minimal: the trigger registrars just record what they were
    asked for.
    """
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    core = types.ModuleType("homeassistant.core")

    def _callback(func):
        return func

    class HomeAssistant:  # noqa: D401 - a name for annotations
        pass

    core.callback = _callback
    core.HomeAssistant = HomeAssistant

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    config_entries.ConfigEntry = ConfigEntry

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")

    def async_dispatcher_connect(hass, signal, target):
        hass.signals.setdefault(signal, []).append(target)

        def _unsub():
            hass.signals.get(signal, []).remove(target)

        return _unsub

    dispatcher.async_dispatcher_connect = async_dispatcher_connect

    event = types.ModuleType("homeassistant.helpers.event")

    def async_track_time_change(hass, action, *, hour=None, minute=None, second=None):
        hass.time_triggers.append((hour, minute, second, action))

        def _unsub():
            hass.time_triggers[:] = [
                row for row in hass.time_triggers if row[3] is not action
            ]

        return _unsub

    event.async_track_time_change = async_track_time_change

    util = types.ModuleType("homeassistant.util")
    util.__path__ = []
    dt_util = types.ModuleType("homeassistant.util.dt")

    def as_local(value):
        return value.astimezone(TZ)

    dt_util.as_local = as_local
    util.dt = dt_util

    discord = types.ModuleType("discord")
    discord.__path__ = []

    class _Style:
        secondary = "secondary"

    class Interaction:
        pass

    class Forbidden(Exception):
        pass

    ui = types.ModuleType("discord.ui")

    class Button:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children: list = []

        def add_item(self, item):
            self.children.append(item)

    ui.Button = Button
    ui.View = View
    discord.ui = ui
    discord.ButtonStyle = _Style
    discord.Interaction = Interaction
    discord.Forbidden = Forbidden

    for name, module in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.config_entries", config_entries),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.dispatcher", dispatcher),
        ("homeassistant.helpers.event", event),
        ("homeassistant.util", util),
        ("homeassistant.util.dt", dt_util),
        ("discord", discord),
        ("discord.ui", ui),
    ):
        sys.modules[name] = module


TZ = datetime.timezone(datetime.timedelta(hours=-5))

_install_stubs()

# A synthetic 'ld' package pointing at the real integration dir, so
# reminders.py's relative imports resolve to the real modules.
_pkg = types.ModuleType("ld")
_pkg.__path__ = [os.path.abspath(PKG_DIR)]
sys.modules["ld"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PKG_DIR, filename)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reminders = _load("ld.reminders", "reminders.py")
const = sys.modules["ld.const"]
nudge = sys.modules["ld.nudge"]
plan = sys.modules["ld.plan"]
people = sys.modules["ld.people"]

THU_EVE = "3-eve"
# Half an hour before the Eve slot opens at 20:00, when the heads-up sends.
THU = datetime.datetime(2026, 8, 6, 19, 30, tzinfo=TZ)


# --- the household, faked ----------------------------------------------------
class FakeTask:
    """A minimal ``asyncio.Task`` stand-in: done/cancelled/exception plus
    done-callbacks, since ``shutdown()`` needs those, not a bare coroutine.
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


class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.signals: dict = {}
        self.time_triggers: list = []
        self.tasks: list = []

    def async_create_task(self, coro):
        task = FakeTask(coro)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        """Run whatever the callbacks scheduled, like the event loop would."""
        pending, self.tasks = self.tasks, []
        for task in pending:
            await task


class FakeEntry:
    def __init__(self, entry_id="entry-a", **options) -> None:
        self.entry_id = entry_id
        self.data: dict = {}
        self.options = {const.CONF_REMIND_DMS: True, **options}


class FakeAssistant:
    """Only the surface :mod:`reminders` uses, and none of the Store."""

    def __init__(self, moment=THU, *, learn_habits=True) -> None:
        self.moment = moment
        self.learn_habits = learn_habits
        self._people = people.set_reminders({}, "1", people.REMIND_DM, name="Alex")
        self._people = people.set_reminders(
            self._people, "2", people.REMIND_DM, name="Bo"
        )
        self.budgets: dict = {}
        self.booked: dict = {"1": [THU_EVE], "2": [THU_EVE]}
        self.loads: dict = {}
        self.sent: list = []
        self.booked_calls: list = []
        self.pushes: list = []
        self.freed: list = []
        self.dm_delay = 0.0
        self.due: dict = {}
        self.gap: dict = {}
        self.nudge_cells: dict = {}
        self.next_message_id = 100
        self.week: dict = {}
        self.running: list = []
        self.taken_sent: dict = {}

    def now(self):
        return self.moment

    @property
    def people_map(self):
        return dict(self._people)

    def prediction_for(self, user_id):
        return None

    def load_times(self, user_id):
        return list(self.loads.get(str(user_id), []))

    def booked_cells(self, user_id, week=None):
        return list(self.booked.get(str(user_id), []))

    def occupancy(self):
        return dict(self.week)

    def running_cells(self):
        return list(self.running)

    async def async_claim_taken_notice(self, user_id, cell):
        if self.taken_sent.get(str(user_id)) == cell:
            return False
        self.taken_sent[str(user_id)] = cell
        return True

    def is_due(self, user_id):
        return bool(self.due.get(str(user_id), False))

    def typical_gap(self, user_id):
        return self.gap.get(str(user_id))

    async def async_note_nudge_cell(self, user_id, cell, message_id):
        # Keyed by message id, matching the real implementation.
        if cell is None or message_id is None:
            return
        self.nudge_cells[str(user_id)] = {
            "cell": cell,
            "message": str(message_id),
        }

    def nudge_cell(self, user_id, message_id):
        row = self.nudge_cells.get(str(user_id))
        if not row or message_id is None:
            return None
        return row["cell"] if row["message"] == str(message_id) else None

    async def async_store_budgets(self, budgets):
        self.budgets = budgets

    async def async_send_dm(self, user_id, text, view=None):
        if self.dm_delay:
            await asyncio.sleep(self.dm_delay)
        self.sent.append((str(user_id), text))
        self.next_message_id += 1
        return FakeMessage(self.moment, message_id=self.next_message_id)

    async def async_book_cell(self, user_id, cell, week=None):
        self.booked_calls.append((str(user_id), cell, week))
        return True

    async def async_record_push(self, user_id, cell):
        self.pushes.append((str(user_id), cell))

    async def async_free_cell(self, user_id, cell, week=None):
        self.freed.append((str(user_id), cell, week))
        return True


class FakeCoordinator:
    def __init__(self, assistant, *, stage=None, emptied=True, claimed_by="Alex"):
        self.assistant = assistant
        self.stage = const.STAGE_DONE_WAITING if stage is None else stage
        self.emptied = emptied
        self.claimed_by = claimed_by
        self.queue: list = []
        self.joined: list = []

    async def handle_next_join(self, who, user_id):
        self.joined.append((who, user_id))
        return ("added", len(self.joined))


def _loop(hass=None, entry=None, assistant=None, coordinator=None):
    hass = hass or FakeHass()
    entry = entry or FakeEntry()
    assistant = assistant or FakeAssistant()
    coordinator = coordinator or FakeCoordinator(assistant)
    return reminders.LaundryReminders(hass, entry, coordinator), hass, assistant


def _run(coro):
    return asyncio.run(coro)


# --- the kill switch ---------------------------------------------------------


def test_with_the_option_off_nothing_at_all_is_registered() -> None:
    hass = FakeHass()
    entry = FakeEntry(**{const.CONF_REMIND_DMS: False})
    loop, hass, _a = _loop(hass=hass, entry=entry)
    _run(loop.async_setup())
    assert hass.time_triggers == [] and hass.signals == {} and hass.data == {}


def test_the_nudge_never_runs_without_the_guard_that_protects_it() -> None:
    # The already-washed guard needs load history, which is only written with
    # day-learning on; the nudge must not run without it either.
    hass = FakeHass()
    assistant = FakeAssistant(learn_habits=False)
    loop, hass, _a = _loop(hass=hass, assistant=assistant)
    assert loop.enabled is False
    _run(loop.async_setup())
    assert hass.time_triggers == [] and hass.signals == {}
    assistant.learn_habits = True
    assert loop.enabled is True


def test_only_one_config_entry_ever_runs_the_loop() -> None:
    # The budget store is global but entries are per-channel; two loops would
    # double-send at double the intended cap.
    hass = FakeHass()
    first, _h, _a = _loop(hass=hass, entry=FakeEntry("entry-a"))
    second, _h, _a2 = _loop(hass=hass, entry=FakeEntry("entry-b"))
    _run(first.async_setup())
    registered = len(hass.time_triggers)
    assert registered and len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    _run(second.async_setup())
    assert len(hass.time_triggers) == registered  # the second added nothing
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    # Unloading the passenger (non-owner) leaves the owner running...
    _run(second.shutdown())
    assert hass.data[const.DATA_REMINDER_OWNER] == "entry-a"
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    # ...and unloading the owner hands the loop to the passenger, so a reload
    # doesn't leave the household with no reminders.
    _run(first.shutdown())
    assert const.DATA_REMINDER_OWNER not in hass.data
    _run(second.async_setup())
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1


def test_unloading_the_entry_stops_a_pass_that_is_already_running() -> None:
    # shutdown() must cancel an in-flight send pass, not just stop future ones,
    # or an options reload could leave a stale pass sending under old settings.
    loop, hass, assistant = _loop()
    _run(loop.async_setup())
    assistant.dm_delay = 30  # a gateway that is reconnecting

    async def _scenario():
        # A real task: one that never started can't demonstrate being
        # stopped mid-flight.
        hass.async_create_task = asyncio.ensure_future
        loop._create_task(loop._async_send_nudges(released=True))
        assert len(loop._tasks) == 1
        task = next(iter(loop._tasks))
        await asyncio.sleep(0)  # let it reach the DM that is going nowhere
        assert not task.done()
        await loop.shutdown()
        assert task.done() and task.cancelled()
        assert loop._tasks == set()
        assert assistant.sent == []

    _run(_scenario())


# --- what "the washer is free" is allowed to mean ----------------------------


def _fire_washer_free(loop, hass, payload):
    handler = hass.signals[const.SIGNAL_WASHER_FREE][0]
    handler(payload)
    _run(hass.drain())


def test_a_washer_handed_to_the_queue_is_not_a_free_washer() -> None:
    loop, hass, assistant = _loop()
    _run(loop.async_setup())
    _fire_washer_free(
        loop, hass, {"handed_off": True, "hedged": False, "claimant_id": None}
    )
    assert assistant.sent == []
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": False, "claimant_id": None}
    )
    assert [uid for uid, _text in assistant.sent] == ["1", "2"]


def test_the_hedged_backstop_does_not_assert_the_drum_is_empty() -> None:
    # "Hedged" only means probably free; the nudge states free as fact, so it
    # needs the same strict check the clock trigger gets.
    assistant = FakeAssistant()
    coordinator = FakeCoordinator(assistant, emptied=False, claimed_by="Dan")
    loop, hass, assistant = _loop(assistant=assistant, coordinator=coordinator)
    _run(loop.async_setup())
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": True, "claimant_id": None}
    )
    assert assistant.sent == []
    # The clock backstop, same state, same answer — which is the point.
    _run(loop._async_send_nudges(released=False))
    assert assistant.sent == []
    # Unclaimed is free even hedged, matching how the coordinator treats it.
    coordinator.claimed_by = const.UNCLAIMED
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": True, "claimant_id": None}
    )
    assert [uid for uid, _text in assistant.sent] == ["1", "2"]


def test_the_person_who_just_emptied_the_machine_is_not_told_to_use_it() -> None:
    # The coordinator knows whose load just finished, so it's asked directly
    # rather than relying on load history.
    loop, hass, assistant = _loop()
    _run(loop.async_setup())
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": False, "claimant_id": 1}
    )
    assert [uid for uid, _text in assistant.sent] == ["2"]


# --- the send itself ---------------------------------------------------------


def test_a_nudge_that_cannot_leave_inside_its_slot_is_dropped() -> None:
    # A send stuck waiting for the gateway must time out rather than deliver
    # hours late and block the rest of the queue.
    assistant = FakeAssistant()
    assistant.dm_delay = 5
    loop, hass, assistant = _loop(assistant=assistant)
    reminders._SEND_TIMEOUT = 0.01
    try:
        _run(loop._async_send_nudges(released=True))
    finally:
        reminders._SEND_TIMEOUT = 30
    assert assistant.sent == []
    # The budget stays spent: undeliverable is treated the same as dropped.
    assert assistant.budgets["1"]["last_nudge_ts"] is not None


# --- replying to a DM that has been sitting in an inbox ----------------------
class FakeUser:
    def __init__(self, user_id) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, created_at, message_id=1) -> None:
        self.created_at = created_at
        self.id = message_id


class FakeInteraction:
    def __init__(self, user_id="1", created_at=None, message_id=1) -> None:
        self.user = FakeUser(user_id)
        self.message = (
            None if created_at is None else FakeMessage(created_at, message_id)
        )


def _button(cls, assistant):
    return cls(assistant)


def test_a_reply_acts_on_the_slot_the_dm_was_about() -> None:
    # A timestamp alone is ambiguous near slot boundaries, so the cell is
    # recorded when the DM is sent, and the reply reads that instead.
    assistant = FakeAssistant(moment=THU)
    _run(assistant.async_note_nudge_cell("1", THU_EVE, 7001))
    on_it = _button(reminders._NudgeOnItButton, assistant)
    sent_at = THU.astimezone(datetime.timezone.utc)
    tapped_in_time = FakeInteraction("1", created_at=sent_at, message_id=7001)
    note = _run(on_it.act(tapped_in_time))
    assert assistant.booked_calls == [("1", THU_EVE, None)]
    assert "marked the slot taken" in note
    # By timestamp alone, 19:30 reads as PM: the wrong cell.
    assert plan.slot_for_hour(19) == "pm"

    # Opened the next morning: nothing is written, and the reply says so.
    assistant.booked_calls.clear()
    assistant.moment = datetime.datetime(2026, 8, 7, 8, 15, tzinfo=TZ)
    note = _run(on_it.act(tapped_in_time))
    assert assistant.booked_calls == []
    assert "left the week grid alone" in note

    # Push is the dangerous one: acting on the wrong cell would book a slot
    # nobody chose.
    push = _button(reminders._NudgePushButton, assistant)
    note = _run(push.act(tapped_in_time))
    assert assistant.booked_calls == [] and assistant.pushes == []


def test_a_reply_to_an_unrecognised_dm_touches_nothing() -> None:
    # An unidentifiable DM (e.g. its note was lost across a restart) must
    # write nothing, not guess a slot from the send hour.
    assistant = FakeAssistant(moment=datetime.datetime(2026, 8, 5, 19, 5, tzinfo=TZ))
    assert assistant.nudge_cell("1", 4242) is None
    sent = datetime.datetime(2026, 8, 5, 19, 0, tzinfo=TZ)
    stale = FakeInteraction(
        "1", created_at=sent.astimezone(datetime.timezone.utc), message_id=4242
    )
    assert plan.slot_for_hour(19) == "pm"  # what a send-hour guess would say (wrongly)
    for cls in (
        reminders._NudgeOnItButton,
        reminders._NudgeFreeButton,
        reminders._NudgePushButton,
    ):
        note = _run(_button(cls, assistant).act(stale))
        assert "left the week grid alone" in note
    assert assistant.booked_calls == [] and assistant.freed == []
    assert assistant.pushes == []


def test_an_older_dms_buttons_never_act_on_a_newer_dms_cell() -> None:
    # The remembered cell is keyed by message id, not just person, so tapping
    # an older DM's button can't act on a newer DM's slot.
    wednesday = datetime.datetime(2026, 8, 5, 19, 5, tzinfo=TZ)
    assistant = FakeAssistant(moment=wednesday)
    monday_dm, wednesday_dm = 5001, 5002
    _run(assistant.async_note_nudge_cell("1", "0-eve", monday_dm))
    _run(assistant.async_note_nudge_cell("1", "2-eve", wednesday_dm))
    free = _button(reminders._NudgeFreeButton, assistant)
    note = _run(_run_tap(free, message_id=monday_dm))
    assert assistant.freed == []
    assert "left the week grid alone" in note
    # Wednesday's own DM still works: the tap is identified, not just blocked.
    note = _run(_run_tap(free, message_id=wednesday_dm))
    assert assistant.freed == [("1", "2-eve", None)]
    assert "Released" in note


def _run_tap(button, *, message_id):
    """One tap on a DM with this id, from the person the fakes are about."""
    sent = datetime.datetime(2026, 8, 5, 19, 0, tzinfo=TZ)
    return button.act(
        FakeInteraction(
            "1",
            created_at=sent.astimezone(datetime.timezone.utc),
            message_id=message_id,
        )
    )


def test_free_it_up_gives_the_slot_back_and_only_that_slot() -> None:
    assistant = FakeAssistant(moment=THU)
    _run(assistant.async_note_nudge_cell("1", THU_EVE, 7002))
    free = _button(reminders._NudgeFreeButton, assistant)
    sent_at = THU.astimezone(datetime.timezone.utc)
    tapped = FakeInteraction("1", created_at=sent_at, message_id=7002)
    note = _run(free.act(tapped))
    assert assistant.freed == [("1", THU_EVE, None)]
    assert "Released" in note and "back next week" in note
    assistant.freed.clear()
    assistant.moment = datetime.datetime(2026, 8, 7, 8, 15, tzinfo=TZ)
    note = _run(free.act(tapped))
    assert assistant.freed == []
    assert "left the week grid alone" in note


def test_the_two_message_kinds_offer_different_replies() -> None:
    # The opportunity has nothing to free or push (no booking exists), so its
    # view is a subset of the slot view's buttons, from one view class.
    assistant = FakeAssistant(moment=THU)

    def ids(view):
        return {item.custom_id for item in view.children}

    template = ids(reminders.NudgeView(assistant))
    slot = ids(reminders.NudgeView(assistant, kind=nudge.MSG_SLOT))
    chance = ids(reminders.NudgeView(assistant, kind=nudge.MSG_OPPORTUNITY))
    assert const.NUDGE_FREE_CUSTOM_ID in slot
    assert const.NUDGE_FREE_CUSTOM_ID not in chance
    assert const.NUDGE_PUSH_CUSTOM_ID in slot
    assert const.NUDGE_SKIP_CUSTOM_ID in chance
    assert const.NUDGE_ON_IT_CUSTOM_ID in slot & chance
    # The template must carry every id, or a button goes dead after a restart.
    assert slot | chance == template


def test_a_sunday_push_books_the_week_it_actually_lands_in() -> None:
    # Tomorrow on a Sunday is Monday of the NEXT ISO week; booking under the
    # current week would write a Monday six days in the past.
    sunday = datetime.datetime(2026, 8, 2, 20, 30, tzinfo=TZ)
    assistant = FakeAssistant(moment=sunday)
    _run(assistant.async_note_nudge_cell("1", "6-eve", 8001))
    push = _button(reminders._NudgePushButton, assistant)
    note = _run(
        push.act(
            FakeInteraction(
                "1",
                created_at=sunday.astimezone(datetime.timezone.utc),
                message_id=8001,
            )
        )
    )
    assert assistant.pushes == [("1", "6-eve")]
    assert assistant.booked_calls == [("1", "0-eve", "2026-W32")]
    assert plan.iso_week_key(sunday) == "2026-W31"  # NOT the week it was booked in
    assert "Moved" in note
    # Every other day, the target week is simply the current one.
    monday = datetime.datetime(2026, 8, 3, 20, 30, tzinfo=TZ)
    assistant.moment = monday
    assistant.booked_calls.clear()
    _run(assistant.async_note_nudge_cell("1", "0-eve", 8002))
    _run(
        push.act(
            FakeInteraction(
                "1",
                created_at=monday.astimezone(datetime.timezone.utc),
                message_id=8002,
            )
        )
    )
    assert assistant.booked_calls == [("1", "1-eve", "2026-W32")]



# --- the slot-taken DM ------------------------------------------------------
def _claimed(loop, hass, claimant_id):
    handler = hass.signals[const.SIGNAL_LOAD_CLAIMED][0]
    handler({"claimant_id": claimant_id})
    _run(hass.drain())


def _taken_setup():
    loop, hass, assistant = _loop()
    assistant.running = [THU_EVE]
    assistant.week = {
        THU_EVE: {plan.OCC_HOLDERS: ["1", "2"], plan.OCC_RECURRING: []}
    }
    _run(loop.async_setup())
    return loop, hass, assistant


def test_the_slot_taken_dm_goes_to_the_other_holder_without_names() -> None:
    loop, hass, assistant = _taken_setup()
    _claimed(loop, hass, 1)  # an int id, as Discord hands it over
    assert [uid for uid, _text in assistant.sent] == ["2"]
    text = assistant.sent[0][1]
    assert "Someone else got to the washer first" in text
    assert "Alex" not in text and "Bo" not in text
    # The reply buttons act on the slot the DM was about.
    assert assistant.nudge_cells["2"]["cell"] == THU_EVE


def test_the_slot_taken_dm_goes_out_once_per_slot() -> None:
    loop, hass, assistant = _taken_setup()
    _claimed(loop, hass, 1)
    _claimed(loop, hass, 1)  # unclaim and reclaim
    assert [uid for uid, _text in assistant.sent] == ["2"]


def test_the_slot_taken_dm_respects_the_switch_and_the_line() -> None:
    loop, hass, assistant = _taken_setup()
    assistant._people = people.set_person(assistant._people, "2", dm_taken=False)
    _claimed(loop, hass, 1)
    assert assistant.sent == []

    loop, hass, assistant = _taken_setup()
    loop._coordinator.queue = [{"id": 2, "name": "Bo", "ts": 1.0}]
    _claimed(loop, hass, 1)
    assert assistant.sent == []  # already waiting for the washer


def test_put_me_next_joins_the_line_from_the_dm() -> None:
    assistant = FakeAssistant()
    coordinator = FakeCoordinator(assistant)
    button = reminders._TakenNextButton(assistant, coordinator)
    interaction = FakeInteraction("2")
    interaction.user.display_name = "Bo"
    note = _run(button.act(interaction))
    assert coordinator.joined == [("Bo", "2")]
    assert "next" in note.lower()


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
