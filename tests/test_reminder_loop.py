"""Tests for the reminder *loop* — the scheduling half of design doc §10.

``tests/test_reminders.py`` covers the decision (may this person be messaged
about this slot right now?), which is pure and needs nothing. This file covers
the half that decision cannot see: which trigger fired, what the coordinator had
just done when it fired, which config entry is running the loop at all, and what
a tap on a DM that has been sitting in an inbox actually acts on.

Every one of those is a way an **unwanted DM** gets sent, and none of them is
reachable from pure functions — so ``reminders.py`` is imported for real, with
the two things it imports from the outside world (``homeassistant`` and
``discord``) replaced by the smallest stubs that make the module's own code run.
Nothing in the module under test is reimplemented here; ``const``, ``habit``,
``nudge`` and ``plan`` are the real ones, loaded as a package so the relative
imports inside ``reminders.py`` resolve exactly as they do in Home Assistant.

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

    Deliberately tiny and deliberately dumb: anything with behaviour in it is a
    second implementation of something, and a test that passes against a clever
    fake is a test of the fake. The trigger registrars just record what they
    were asked for, which is precisely what the "with the flag off, nothing is
    scheduled" assertions need to read.
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

# A synthetic package pointing at the real integration directory, so the
# relative imports inside reminders.py (`from . import habit`, `from .const
# import ...`) resolve to the real modules — no copies, no shims.
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
# A Thursday, half an hour before the Eve slot opens at 20:00 — which is when a
# message about that slot is now sent. The retired day-of nudge fired *inside*
# the window, which was too late to put a load on.
THU = datetime.datetime(2026, 8, 6, 19, 30, tzinfo=TZ)


# --- the household, faked ----------------------------------------------------
class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.signals: dict = {}
        self.time_triggers: list = []
        self.tasks: list = []

    def async_create_task(self, coro):
        self.tasks.append(coro)
        return coro

    async def drain(self) -> None:
        """Run whatever the callbacks scheduled, like the event loop would."""
        pending, self.tasks = self.tasks, []
        for coro in pending:
            await coro


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
        self.week: dict = {}

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

    def is_due(self, user_id):
        return bool(self.due.get(str(user_id), False))

    def typical_gap(self, user_id):
        return self.gap.get(str(user_id))

    def note_nudge_cell(self, user_id, cell):
        self.nudge_cells[str(user_id)] = cell

    def nudge_cell(self, user_id):
        return self.nudge_cells.get(str(user_id))

    async def async_store_budgets(self, budgets):
        self.budgets = budgets

    async def async_send_dm(self, user_id, text, view=None):
        if self.dm_delay:
            await asyncio.sleep(self.dm_delay)
        self.sent.append((str(user_id), text))
        return True

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
    # The day-of nudge's "have they already washed today" check is fed from the
    # load history, and the load history is written only when the house has
    # day-learning on. With learning off that guard is dead while the nudge is
    # alive — so the washer freeing at 21:40 because somebody just emptied it
    # would DM that same person "you're down for tonight, and the washer's free
    # right now". The option description already promises both are needed.
    hass = FakeHass()
    assistant = FakeAssistant(learn_habits=False)
    loop, hass, _a = _loop(hass=hass, assistant=assistant)
    assert loop.enabled is False
    _run(loop.async_setup())
    assert hass.time_triggers == [] and hass.signals == {}
    assistant.learn_habits = True
    assert loop.enabled is True


def test_only_one_config_entry_ever_runs_the_loop() -> None:
    # The planner Store key is global — one household, one set of people, one
    # nudge budget — but the config flow keys entries on the channel, so a house
    # with two washers has two entries. Two loops means two in-memory copies of
    # the budget, both of which allow the same DM, so everybody is messaged
    # twice at double the intended cap.
    hass = FakeHass()
    first, _h, _a = _loop(hass=hass, entry=FakeEntry("entry-a"))
    second, _h, _a2 = _loop(hass=hass, entry=FakeEntry("entry-b"))
    _run(first.async_setup())
    registered = len(hass.time_triggers)
    assert registered and len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    _run(second.async_setup())
    assert len(hass.time_triggers) == registered  # the second added nothing
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    # Unloading the passenger leaves the owner running...
    second.shutdown()
    assert hass.data[const.DATA_REMINDER_OWNER] == "entry-a"
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1
    # ...and unloading the owner hands the loop back, so a reload of the owning
    # entry doesn't leave the household with no reminders at all.
    first.shutdown()
    assert const.DATA_REMINDER_OWNER not in hass.data
    _run(second.async_setup())
    assert len(hass.signals[const.SIGNAL_WASHER_FREE]) == 1


# --- what "the washer is free" is allowed to mean ----------------------------


def _fire_washer_free(loop, hass, payload):
    handler = hass.signals[const.SIGNAL_WASHER_FREE][0]
    handler(payload)
    _run(hass.drain())


def test_a_washer_handed_to_the_queue_is_not_a_free_washer() -> None:
    # The 🔜 line and the reminder loop must not tell two people the same
    # machine is theirs. The signal now carries the handoff's outcome, so the
    # nudge is not even scheduled when somebody in the line just got it.
    loop, hass, assistant = _loop()
    _run(loop.async_setup())
    _fire_washer_free(
        loop, hass, {"handed_off": True, "hedged": False, "claimant_id": None}
    )
    assert assistant.sent == []
    # Nobody waiting: the same moment, and now it really is free.
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": False, "claimant_id": None}
    )
    assert [uid for uid, _text in assistant.sent] == ["1", "2"]


def test_the_hedged_backstop_does_not_assert_the_drum_is_empty() -> None:
    # The handoff backstop fires when the claimant never confirmed anything, and
    # the queue is deliberately told "probably free, worth a look". The nudge
    # says "the washer's free right now" as flat fact, so it gets the same
    # stricter test a clock trigger gets against the identical state — a
    # finished load with Dan's clothes still in the drum is not a free washer at
    # 19:45 any more than it was at 19:00.
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
    # A load nobody ever claimed is free even hedged, exactly as the coordinator
    # treats it when it hands that one straight to the line.
    coordinator.claimed_by = const.UNCLAIMED
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": True, "claimant_id": None}
    )
    assert [uid for uid, _text in assistant.sent] == ["1", "2"]


def test_the_person_who_just_emptied_the_machine_is_not_told_to_use_it() -> None:
    # The washer coming free is very often the nudged person's own load, and the
    # history that would say so is written only for a load somebody tapped Claim
    # on. The coordinator knows whose it was, so it is asked.
    loop, hass, assistant = _loop()
    _run(loop.async_setup())
    _fire_washer_free(
        loop, hass, {"handed_off": False, "hedged": False, "claimant_id": 1}
    )
    # Alex just emptied it and hears nothing; Bo, who didn't touch it, does.
    assert [uid for uid, _text in assistant.sent] == ["2"]


# --- the send itself ---------------------------------------------------------


def test_a_nudge_that_cannot_leave_inside_its_slot_is_dropped() -> None:
    # async_dm_user starts with wait_until_ready(), so a send attempted while
    # the gateway is down parks there until it reconnects — and would then
    # deliver "the washer's free right now" hours later, about a slot that ended
    # at midnight, while everybody behind this person waited too.
    assistant = FakeAssistant()
    assistant.dm_delay = 5
    loop, hass, assistant = _loop(assistant=assistant)
    reminders._SEND_TIMEOUT = 0.01
    try:
        _run(loop._async_send_nudges(released=True))
    finally:
        reminders._SEND_TIMEOUT = 30
    assert assistant.sent == []
    # ...and the budget stays spent, because over budget is dropped not queued
    # and undeliverable is the same thing.
    assert assistant.budgets["1"]["last_nudge_ts"] is not None


# --- replying to a DM that has been sitting in an inbox ----------------------
class FakeUser:
    def __init__(self, user_id) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, created_at) -> None:
        self.created_at = created_at


class FakeInteraction:
    def __init__(self, user_id="1", created_at=None) -> None:
        self.user = FakeUser(user_id)
        self.message = None if created_at is None else FakeMessage(created_at)


def _button(cls, assistant):
    return cls(assistant)


def test_a_reply_acts_on_the_slot_the_dm_was_about() -> None:
    # A DM sits in an inbox indefinitely, so "whatever slot is running when they
    # finally look" is routinely a different slot. Acting on that books a cell
    # nobody chose — taken on the whole household's grid — while the slot they
    # were actually messaged about stays free.
    #
    # The heads-up makes the old timestamp reading ambiguous as well as stale:
    # sent at 19:30, PM (16:00-20:00) is running *and* Eve opens within the
    # hour, and a timestamp cannot say which. So the cell is recorded when the
    # DM is sent, and that is what the reply reads.
    assistant = FakeAssistant(moment=THU)
    assistant.note_nudge_cell("1", THU_EVE)
    on_it = _button(reminders._NudgeOnItButton, assistant)
    sent_at = THU.astimezone(datetime.timezone.utc)
    tapped_in_time = FakeInteraction("1", created_at=sent_at)
    note = _run(on_it.act(tapped_in_time))
    assert assistant.booked_calls == [("1", THU_EVE, None)]
    assert "marked the slot taken" in note
    # Read from the timestamp instead, 19:30 is the PM slot — the wrong cell,
    # and the reason the note exists at all.
    assert plan.slot_for_hour(19) == "pm"

    # The same DM, opened the next morning. Nothing is written, and the reply
    # does not claim anything was.
    assistant.booked_calls.clear()
    assistant.moment = datetime.datetime(2026, 8, 7, 8, 15, tzinfo=TZ)
    note = _run(on_it.act(tapped_in_time))
    assert assistant.booked_calls == []
    assert "left the week grid alone" in note

    # ⏭ Push is the dangerous one: acting on the wrong cell manufactures a
    # booking for a slot nobody chose, which then produces its own message.
    push = _button(reminders._NudgePushButton, assistant)
    note = _run(push.act(tapped_in_time))
    assert assistant.booked_calls == [] and assistant.pushes == []


def test_a_reply_still_works_when_the_note_was_lost_to_a_restart() -> None:
    # The recorded cell is memory only, so a restart drops it. The fallback is
    # the old timestamp reading rather than a refused tap — exact when we know,
    # the previous best guess when we don't.
    assistant = FakeAssistant(moment=datetime.datetime(2026, 8, 6, 21, tzinfo=TZ))
    assert assistant.nudge_cell("1") is None
    on_it = _button(reminders._NudgeOnItButton, assistant)
    sent = datetime.datetime(2026, 8, 6, 20, 45, tzinfo=TZ)
    _run(on_it.act(FakeInteraction("1", created_at=sent.astimezone(datetime.timezone.utc))))
    assert assistant.booked_calls == [("1", THU_EVE, None)]


def test_free_it_up_gives_the_slot_back_and_only_that_slot() -> None:
    # The reply that serves the house rather than the person: a reservation
    # about to lapse unused is exactly the capacity the grid exists to reclaim.
    assistant = FakeAssistant(moment=THU)
    assistant.note_nudge_cell("1", THU_EVE)
    free = _button(reminders._NudgeFreeButton, assistant)
    sent_at = THU.astimezone(datetime.timezone.utc)
    note = _run(free.act(FakeInteraction("1", created_at=sent_at)))
    assert assistant.freed == [("1", THU_EVE, None)]
    assert "Released" in note and "back next week" in note
    # Once its slot has gone there is nothing useful left to write.
    assistant.freed.clear()
    assistant.moment = datetime.datetime(2026, 8, 7, 8, 15, tzinfo=TZ)
    note = _run(free.act(FakeInteraction("1", created_at=sent_at)))
    assert assistant.freed == []
    assert "left the week grid alone" in note


def test_the_two_message_kinds_offer_different_replies() -> None:
    # Nothing to free and nothing to push about a slot nobody booked, so the
    # opportunity carries just "yes" and "leave me alone this week". One view
    # class, subsets of one button set — two classes sharing 👍 On it would mean
    # the second add_view registration quietly won for both.
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
    # "Tomorrow" on a Sunday is a Monday in the *next* ISO week. Booking it
    # under the current one writes the Monday that is six days past: the nudge
    # does not move, and a cell nobody booked shows as taken on the shared grid.
    sunday = datetime.datetime(2026, 8, 2, 20, 30, tzinfo=TZ)
    assistant = FakeAssistant(moment=sunday)
    push = _button(reminders._NudgePushButton, assistant)
    note = _run(
        push.act(FakeInteraction("1", created_at=sunday.astimezone(datetime.timezone.utc)))
    )
    assert assistant.pushes == [("1", "6-eve")]
    assert assistant.booked_calls == [("1", "0-eve", "2026-W32")]
    assert plan.iso_week_key(sunday) == "2026-W31"  # NOT the week it was booked in
    assert "Moved" in note
    # Every other day of the week the target week is simply this week, so the
    # Sunday case is not a special path anybody has to remember.
    monday = datetime.datetime(2026, 8, 3, 20, 30, tzinfo=TZ)
    assistant.moment = monday
    assistant.booked_calls.clear()
    _run(push.act(FakeInteraction("1", created_at=monday.astimezone(datetime.timezone.utc))))
    assert assistant.booked_calls == [("1", "1-eve", "2026-W32")]


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
