"""Tests for the pure reminder decisions: may this person be messaged, about
this slot, right now.

Runnable with plain ``python3 tests/test_reminders.py``. ``nudge.py`` is
loaded by file path so it doesn't import Home Assistant.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys


def _load(name: str, filename: str):
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "custom_components",
        "laundry_discord",
        filename,
    )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Registered by bare name so relative imports inside each module resolve;
# load in dependency order (plan, people/habit, then nudge, which needs all three).
_plan = _load("ld_plan", "plan.py")
sys.modules["plan"] = _plan
_people = _load("ld_people", "people.py")
sys.modules["people"] = _people
_habit = _load("ld_habit", "habit.py")
sys.modules["habit"] = _habit
_nudge = _load("ld_nudge", "nudge.py")

BUDGET_DAY = _habit.BUDGET_DAY
BUDGET_OK = _habit.BUDGET_OK
MAX_NUDGES_PER_DAY = _habit.MAX_NUDGES_PER_DAY
MAX_NUDGES_PER_WEEK = _habit.MAX_NUDGES_PER_WEEK
budget_for = _habit.budget_for
check_nudge = _habit.check_nudge
next_day_cell = _habit.next_day_cell
predict = _habit.predict
record_load = _habit.record_load

REASON_ALREADY = _nudge.REASON_ALREADY
REASON_BUDGET_DAY = _nudge.REASON_BUDGET_DAY
REASON_BUDGET_WEEK = _nudge.REASON_BUDGET_WEEK
REASON_DM_CLOSED = _nudge.REASON_DM_CLOSED
REASON_KIND_OFF = _nudge.REASON_KIND_OFF
REASON_MOMENT = _nudge.REASON_MOMENT
REASON_MONITOR_OFF = _nudge.REASON_MONITOR_OFF
REASON_NOTHING_TODAY = _nudge.REASON_NOTHING_TODAY
REASON_NOT_DM = _nudge.REASON_NOT_DM
REASON_NOT_OPTED_IN = _nudge.REASON_NOT_OPTED_IN
REASON_NO_PREDICTION = _nudge.REASON_NO_PREDICTION
REASON_OK = _nudge.REASON_OK
REASON_OUTSIDE_SLOT = _nudge.REASON_OUTSIDE_SLOT
REASON_PAUSED = _nudge.REASON_PAUSED
REASON_PREDICT_OFF = _nudge.REASON_PREDICT_OFF
REASON_QUIET = _nudge.REASON_QUIET
REASON_REMINDERS_OFF = _nudge.REASON_REMINDERS_OFF
REASON_WASHER_BUSY = _nudge.REASON_WASHER_BUSY
BUDGET_REASONS = _nudge.BUDGET_REASONS
MSG_NONE = _nudge.MSG_NONE
MSG_OPPORTUNITY = _nudge.MSG_OPPORTUNITY
MSG_SLOT = _nudge.MSG_SLOT
REASON_NOT_DUE = _nudge.REASON_NOT_DUE
claim_plan_dm = _nudge.claim_plan_dm
claim_select = _nudge.claim_select
eligible = _nudge.eligible
heads_up_clock = _nudge.heads_up_clock
heads_up_text = _nudge.heads_up_text
in_quiet_hours = _nudge.in_quiet_hours
is_booked = _nudge.is_booked
is_paused = _nudge.is_paused
minutes_until_slot = _nudge.minutes_until_slot
opportunity_cell = _nudge.opportunity_cell
opportunity_text = _nudge.opportunity_text
parse_clock = _nudge.parse_clock
plan_dm = _nudge.plan_dm
plan_dm_text = _nudge.plan_dm_text
select = _nudge.select
slot_ended = _nudge.slot_ended
slot_soon = _nudge.slot_soon
slot_start_ts = _nudge.slot_start_ts

KIND_CHECKIN = _people.KIND_CHECKIN
KIND_OPPORTUNITY = _people.KIND_OPPORTUNITY
KIND_SLOT = _people.KIND_SLOT
KIND_TRADES = _people.KIND_TRADES

THU_EVE = "3-eve"

# Fixed offset, not a named zone: no DST transitions, so slot boundaries stay exact.
TZ = datetime.timezone(datetime.timedelta(hours=-5))


def at(year, month, day, hour=21, minute=0) -> datetime.datetime:
    """A timezone-aware local moment. 21:00 (the Eve slot) by default."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-08-06 is a Thursday. THU is inside the Eve slot (20:00-24:00); HEADS_UP
# and LATE are both in the hour before it opens, when the heads-up sends.
THU = at(2026, 8, 6, 20, 30)
HEADS_UP = at(2026, 8, 6, 19, 30)
LATE = at(2026, 8, 6, 19, 55)


def lead(day, slot_hour, minutes=30):
    """A moment ``minutes`` before a slot opens, i.e. inside the heads-up lead."""
    return at(2026, 8, day, slot_hour - 1, 60 - minutes)


def _claim(prefs, budgets, user_id, cell, moment, **kw):
    """Wraps claim_select, returning only the verdict and updated budgets."""
    _kind, _cell, reason, updated = claim_select(
        prefs, budgets, user_id, moment, booked=[cell] if cell else (), **kw
    )
    return reason, updated


def _person(**changes) -> dict:
    """A person opted into DM reminders (via people.set_person), plus overrides."""
    prefs = _people.set_reminders({}, "1", _people.REMIND_DM, name="Alex")
    if changes:
        prefs = _people.set_person(prefs, "1", **changes)
    return prefs


def _guess(cell=THU_EVE, count=5, total=8) -> dict:
    """A prediction dict of the shape :func:`habit.predict` returns."""
    return {"cell": cell, "count": count, "total": total, "confident": True}


# --- eligibility -------------------------------------------------------------


def test_somebody_the_bot_has_never_seen_gets_nothing() -> None:
    assert eligible({}, "1", THU) == REASON_NOT_OPTED_IN
    assert eligible(None, "1", THU) == REASON_NOT_OPTED_IN
    unanswered = _people.set_person({}, "1", name="Alex")
    assert eligible(unanswered, "1", THU) == REASON_NOT_OPTED_IN


def test_each_preference_gates_on_its_own() -> None:
    assert eligible(_person(), "1", THU) == REASON_OK
    assert (
        eligible(_people.set_reminders({}, "1", _people.REMIND_OFF), "1", THU)
        == REASON_REMINDERS_OFF
    )
    # Channel is the default, and means no DM: a personal reminder must not
    # leak into the shared channel.
    assert (
        eligible(_people.set_reminders({}, "1", _people.REMIND_CHANNEL), "1", THU)
        == REASON_NOT_DM
    )
    assert eligible(_people.mark_dm_failed(_person(), "1"), "1", THU) == (
        REASON_DM_CLOSED
    )
    assert eligible(_person(predict=False), "1", THU) == REASON_PREDICT_OFF
    assert eligible(_person(monitor=False), "1", THU) == REASON_MONITOR_OFF
    assert eligible(_person(paused_until=THU.timestamp() + 60), "1", THU) == (
        REASON_PAUSED
    )
    # An unreadable clock denies: a missed nudge costs less than a mistimed one.
    assert eligible(_person(), "1", None) == REASON_MOMENT
    assert eligible(_person(), "1", datetime.datetime(2026, 8, 6, 20)) == REASON_MOMENT


def test_a_pause_expires_on_its_own() -> None:
    person = _people.get_person(_person(paused_until=THU.timestamp() + 1), "1")
    assert is_paused(person, THU) is True
    assert is_paused(person, at(2026, 8, 7, 20, 30)) is False
    assert is_paused(_people.get_person(_person(), "1"), THU) is False


# --- which kinds, and when (per-person DM settings) --------------------------


def test_naming_no_kind_asks_the_older_broader_question() -> None:
    # Callers that predate per-kind settings pass no kind and must get the
    # answer they always got: no kind-specific gate may apply here.
    quiet = _person(dm_checkin=False, dm_headsup=False, dm_opportunity=False,
                    dm_trades=False, quiet_start=0, quiet_end=23)
    assert eligible(quiet, "1", THU) == REASON_OK
    assert eligible(_people.set_person(quiet, "1", monitor=False), "1", THU) == (
        REASON_MONITOR_OFF
    )


def test_each_message_kind_is_switched_off_on_its_own() -> None:
    guess = _guess()
    checkin_off = _person(dm_checkin=False)
    # select() passes eligible() the same kind names people.py defines.
    assert (MSG_SLOT, MSG_OPPORTUNITY) == (KIND_SLOT, KIND_OPPORTUNITY)
    assert eligible(checkin_off, "1", THU, kind=KIND_CHECKIN) == REASON_KIND_OFF
    assert eligible(checkin_off, "1", THU, kind=KIND_TRADES) == REASON_OK
    assert plan_dm(checkin_off, {}, "1", guess, THU) == REASON_KIND_OFF
    assert select(checkin_off, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_SLOT, THU_EVE, REASON_OK
    )
    headsup_off = _person(dm_headsup=False)
    assert plan_dm(headsup_off, {}, "1", guess, THU) == REASON_OK
    assert select(headsup_off, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_NONE, None, REASON_KIND_OFF
    )
    # Headsup off + opportunity on still sends the opportunity: kind gating
    # happens after select() picks a message, not before.
    assert select(
        headsup_off, {}, "1", HEADS_UP, prediction=guess, due=True
    ) == (MSG_OPPORTUNITY, THU_EVE, REASON_OK)
    opportunity_off = _person(dm_opportunity=False)
    assert select(
        opportunity_off, {}, "1", HEADS_UP, prediction=guess, due=True
    ) == (MSG_NONE, None, REASON_KIND_OFF)
    assert select(opportunity_off, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_SLOT, THU_EVE, REASON_OK
    )
    # Trades is the broker's switch and must not affect these reminders.
    trades_off = _person(dm_trades=False)
    assert plan_dm(trades_off, {}, "1", guess, THU) == REASON_OK
    assert select(trades_off, {}, "1", HEADS_UP, booked=[THU_EVE])[2] == REASON_OK


def test_a_switched_off_heads_up_is_not_swapped_for_an_opportunity() -> None:
    # Switches only subtract: a refused message must not fall through to a
    # different one about the same slot.
    prefs, guess = _person(dm_headsup=False), _guess()
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], prediction=guess, due=True
    ) == (MSG_NONE, None, REASON_KIND_OFF)


def test_a_silenced_message_costs_nobody_their_allowance() -> None:
    reason, budgets = _claim(_person(dm_headsup=False), {}, "1", THU_EVE, HEADS_UP)
    assert reason == REASON_KIND_OFF
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    quiet = _person(quiet_start=19, quiet_end=8)
    reason, budgets = _claim(quiet, {}, "1", THU_EVE, HEADS_UP)
    assert reason == REASON_QUIET
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_the_quiet_window_wraps_midnight() -> None:
    # 22 -> 8 wraps like a clock face, not a plain number-line interval.
    person = _people.get_person(_person(quiet_start=22, quiet_end=8), "1")
    quiet = [h for h in range(24) if in_quiet_hours(person, at(2026, 8, 6, h))]
    assert quiet == [0, 1, 2, 3, 4, 5, 6, 7, 22, 23]
    # A non-wrapping window is start inclusive, end exclusive.
    daytime = _people.get_person(_person(quiet_start=10, quiet_end=14), "1")
    assert [h for h in range(24) if in_quiet_hours(daytime, at(2026, 8, 6, h))] == (
        [10, 11, 12, 13]
    )
    # No window, or a degenerate one, is never quiet.
    assert in_quiet_hours(_people.get_person(_person(), "1"), THU) is False
    same = _people.get_person(_person(quiet_start=22, quiet_end=22), "1")
    assert [h for h in range(24) if in_quiet_hours(same, at(2026, 8, 6, h))] == []
    # An unreadable clock keeps quiet, but only if a window is set, so one bad
    # datetime can't mute the whole house.
    assert in_quiet_hours(person, None) is True
    assert in_quiet_hours(_people.get_person(_person(), "1"), None) is False
    assert in_quiet_hours({}, THU) is False


def test_quiet_hours_hold_the_dawn_heads_up_and_not_the_evening_one() -> None:
    prefs = _person(quiet_start=22, quiet_end=8)
    dawn = at(2026, 8, 6, 5, 30)  # the AM slot opens at 06:00
    assert slot_soon(["3-am"], dawn) == "3-am"
    assert select(prefs, {}, "1", dawn, booked=["3-am"]) == (
        MSG_NONE, None, REASON_QUIET
    )
    assert select(prefs, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_SLOT, THU_EVE, REASON_OK
    )
    # It holds the Sunday DM the same way, and by the same gate.
    assert plan_dm(prefs, {}, "1", _guess(), dawn) == REASON_QUIET
    assert plan_dm(prefs, {}, "1", _guess(), HEADS_UP) == REASON_OK
    assert select(_person(), {}, "1", dawn, booked=["3-am"])[2] == REASON_OK


# --- the Sunday plan DM ------------------------------------------------------


def test_no_confident_prediction_means_no_sunday_dm_at_all() -> None:
    # A DM saying only "nothing to report" is worse than no DM.
    assert plan_dm(_person(), {}, "1", None, THU) == REASON_NO_PREDICTION
    assert plan_dm(_person(), {}, "1", {}, THU) == REASON_NO_PREDICTION
    assert plan_dm(_person(), {}, "1", {"cell": "nonsense"}, THU) == (
        REASON_NO_PREDICTION
    )
    reason, budgets = claim_plan_dm(_person(), {}, "1", None, THU)
    assert reason == REASON_NO_PREDICTION
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_the_sunday_dm_quotes_the_model_rather_than_a_new_sentence() -> None:
    # Built from describe_prediction + explain, so it can't drift from the
    # panel's own wording.
    guess = _guess()
    text = plan_dm_text(guess)
    assert _habit.describe_prediction(guess) in text  # "Thursday evenings"
    assert _habit.explain(guess) in text  # "5 of your last 8 loads"
    assert text.startswith("🗓️ **Next week's laundry**")
    assert text.endswith("Look right?")
    history = []
    for week in range(5):
        history = record_load(
            history, "1", at(2026, 7, 2 + week * 7, 21), monitor=True
        )
    real = predict(history, "1", at(2026, 8, 7, 12))
    assert real is not None and plan_dm_text(real).count("Thursday evenings") == 1
    # Nothing renderable, nothing sent — never a hedge.
    assert plan_dm_text(None) is None
    assert plan_dm_text({"cell": None}) is None


# --- the day-of nudge --------------------------------------------------------


def test_the_heads_up_arrives_before_the_slot_rather_than_inside_it() -> None:
    prefs = _person()
    assert slot_soon([THU_EVE], HEADS_UP) == THU_EVE  # 19:30, Eve opens at 20
    assert slot_soon([THU_EVE], at(2026, 8, 6, 18)) is None  # too early
    assert slot_soon([THU_EVE], THU) is None  # already open: now, not soon
    assert slot_soon([THU_EVE], at(2026, 8, 5, 19, 30)) is None  # Wednesday
    assert slot_soon([[3, "eve"]], HEADS_UP) == THU_EVE  # the stored [day, slot] form
    assert slot_soon([], HEADS_UP) is None
    # Ties go to the slot they can act on first.
    assert slot_soon(["3-am", THU_EVE], at(2026, 8, 6, 5, 30)) == "3-am"
    kind, cell, reason = select(prefs, {}, "1", HEADS_UP)
    assert (kind, cell, reason) == (MSG_NONE, None, REASON_NOT_DUE)
    kind, cell, reason = select(prefs, {}, "1", HEADS_UP, booked=[THU_EVE])
    assert (kind, cell, reason) == (MSG_SLOT, THU_EVE, REASON_OK)


def test_a_booking_beats_a_guess_and_the_two_are_worded_apart() -> None:
    # A booking is something the person said; a prediction is a guess about
    # their past. The booking wins.
    prefs, guess = _person(), _guess()
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], prediction=guess, due=True
    )
    assert (kind, cell, reason) == (MSG_SLOT, THU_EVE, REASON_OK)
    assert is_booked([THU_EVE], THU_EVE) is True
    assert is_booked([[3, "eve"]], THU_EVE) is True  # the stored form
    assert is_booked([], THU_EVE) is False
    booked_text = heads_up_text(THU_EVE, 60)
    assert "You're down for tonight" in booked_text
    assert "in about 60 minutes" in booked_text
    assert booked_text.endswith("Still want it?")  # a question, not an order
    assert "in about" not in heads_up_text(THU_EVE, None)
    chance = opportunity_text(THU_EVE, guess, 7)
    assert "Tonight is wide open" in chance
    assert "Nobody's booked tonight" in chance
    assert _habit.explain(guess) in chance  # the arithmetic comes along
    assert "about 7 days" in chance
    assert "7 days" not in opportunity_text(THU_EVE, guess, None)
    # PM and Eve phrasing stay distinct so nobody's told to wash at ten.
    phrases = {slot: heads_up_text(f"3-{slot}", 60) for slot in _plan.SLOTS}
    assert len(set(phrases.values())) == len(_plan.SLOTS)
    assert "this evening" in phrases["pm"] and "tonight" in phrases["eve"]
    assert heads_up_text("nonsense", 60) is None
    assert opportunity_text("nonsense") is None


def test_the_heads_up_says_how_long_it_really_is() -> None:
    # Must use the actual time-to-slot, not the configured lead: the washer-free
    # trigger can fire later than slot-start-minus-lead and must not misquote it.
    assert minutes_until_slot(THU_EVE, HEADS_UP) == 30
    assert minutes_until_slot(THU_EVE, LATE) == 5
    assert "in about 5 minutes" in heads_up_text(
        THU_EVE, minutes_until_slot(THU_EVE, LATE)
    )
    # Rounded up; the smallest quantified value is one minute.
    assert minutes_until_slot(THU_EVE, at(2026, 8, 6, 19, 59)) == 1
    # An already-open slot or unreadable moment has no number: "soon" instead.
    assert minutes_until_slot(THU_EVE, THU) is None
    assert minutes_until_slot(THU_EVE, None) is None
    assert minutes_until_slot("nonsense", LATE) is None
    assert "in about" not in heads_up_text(THU_EVE, minutes_until_slot(THU_EVE, THU))


def test_the_nudge_says_the_washer_is_free_so_it_needs_it_to_be() -> None:
    prefs = _person()
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=False
    )[2] == REASON_WASHER_BUSY
    reason, budgets = _claim(
        prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=False
    )
    assert reason == REASON_WASHER_BUSY
    # ...and a washer that was busy costs nobody their allowance.
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_whichever_trigger_comes_first_wins_and_the_other_is_dropped() -> None:
    # Both triggers run the same decision; only the first to arrive sends.
    prefs = _person()
    first, budgets = _claim(prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert first == REASON_OK
    second, budgets = _claim(prefs, budgets, "1", THU_EVE, LATE, washer_free=True)
    assert second == REASON_ALREADY
    # Holds in the other order too (washer freed before the heads-up tick).
    reason, budgets = _claim(prefs, {}, "1", THU_EVE, LATE, washer_free=True)
    assert reason == REASON_OK
    reason, budgets = _claim(prefs, budgets, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_ALREADY
    # Not just the day cap: the rule is one message per SLOT, checked against
    # that slot's own window.
    assert slot_start_ts(THU_EVE, THU) == at(2026, 8, 6, 20, 0).timestamp()
    # The window reaches back over the lead, since both triggers for an
    # evening slot land before it opens.
    assert _nudge.already_nudged_in_slot(budgets, "1", THU_EVE, THU, 60) is True
    assert _nudge.already_nudged_in_slot(budgets, "1", THU_EVE, THU) is False
    # A different slot has its own window; it doesn't retire this morning's nudge.
    assert _nudge.already_nudged_in_slot(budgets, "1", "3-am", THU, 60) is False


# --- the budget ---------------------------------------------------------------


def test_over_budget_is_dropped_not_queued() -> None:
    # No pending list: a nudge a day late is about a slot that's already passed.
    assert MAX_NUDGES_PER_DAY == 1 and MAX_NUDGES_PER_WEEK == 2
    prefs, guess = _person(), _guess()
    reason, budgets = claim_plan_dm(prefs, {}, "1", guess, at(2026, 8, 2, 18))
    assert reason == REASON_OK  # Sunday
    reason, budgets = claim_plan_dm(prefs, budgets, "1", guess, at(2026, 8, 2, 19))
    assert reason == REASON_BUDGET_DAY
    # Sunday is the last day of its ISO week, so the plan DM about next week
    # is charged to the ending week, not the week it's about.
    reason, budgets = _claim(
        prefs, budgets, "1", THU_EVE, HEADS_UP, washer_free=True
    )
    assert reason == REASON_OK  # Thursday: the first of the new week
    reason, budgets = _claim(
        prefs, budgets, "1", "4-eve", lead(7, 20), washer_free=True
    )
    assert reason == REASON_OK  # Friday: the second, and the last
    reason, budgets = _claim(
        prefs, budgets, "1", "5-eve", lead(8, 20), washer_free=True
    )
    assert reason == REASON_BUDGET_WEEK
    assert reason in BUDGET_REASONS  # the only reasons worth a log line
    reason, budgets = _claim(
        prefs, budgets, "1", "0-eve", lead(10, 20), washer_free=True
    )
    assert reason == REASON_OK


def test_a_bounced_dm_still_costs_a_nudge() -> None:
    # Claim, persist, then send: a bounced DM must not refund the nudge, or it
    # would retry forever.
    sent: list[str] = []
    stored: dict = {}

    def _attempt(prefs, cell, moment, *, bounce):
        """Mimics the loop's claim-persist-send order."""
        nonlocal stored
        reason, budgets = _claim(
            prefs, stored, "1", cell, moment, washer_free=True
        )
        stored = budgets  # persisted BEFORE the send, never after
        if reason != REASON_OK:
            return reason
        if bounce:
            return "forbidden"  # discord.Forbidden, 50007
        sent.append("dm")
        return reason

    prefs = _person()
    assert _attempt(prefs, "3-mid", lead(6, 12), bounce=True) == "forbidden"
    assert sent == []
    # Spent even though nothing arrived, so a second trigger for the same slot
    # finds it already used...
    assert _attempt(prefs, "3-mid", lead(6, 12, 5), bounce=False) == (
        REASON_ALREADY
    )
    # ...and so does the evening, on the day cap rather than the slot rule.
    assert _attempt(prefs, THU_EVE, HEADS_UP, bounce=False) == REASON_BUDGET_DAY
    assert sent == []
    # The person is now marked closed, so eligibility refuses them next time.
    assert eligible(_people.mark_dm_failed(prefs, "1"), "1", THU) == REASON_DM_CLOSED


def test_the_accounting_survives_a_restart() -> None:
    # Round-trips through JSON; a restart must not hand out a fresh allowance.
    prefs = _person()
    reason, budgets = _claim(prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_OK
    reloaded = json.loads(json.dumps(budgets))
    assert check_nudge(budget_for(reloaded, "1"), LATE) == BUDGET_DAY
    reason, _budgets = _claim(
        prefs, reloaded, "1", THU_EVE, LATE, washer_free=True
    )
    assert reason == REASON_ALREADY
    # An int key in memory and a string key off disk must be one allowance.
    reason, budgets = _claim(prefs, {}, 1, THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_OK
    assert list(json.loads(json.dumps(budgets))) == ["1"]
    reason, _budgets = _claim(
        prefs, json.loads(json.dumps(budgets)), "1", THU_EVE, LATE,
        washer_free=True,
    )
    assert reason == REASON_ALREADY


# --- the backstop's timing ---------------------------------------------------


def test_the_heads_up_lands_an_hour_before_the_slot_it_belongs_to() -> None:
    # One trigger per slot, off that slot's own start time.
    assert heads_up_clock("am", 60) == (5, 0)
    assert heads_up_clock("mid", 60) == (11, 0)
    assert heads_up_clock("pm", 60) == (15, 0)
    assert heads_up_clock("eve", 60) == (19, 0)
    # A too-long lead is clamped to stay on the slot's own day, not wrap into
    # yesterday.
    for slot in _plan.SLOTS:
        start = _plan.SLOT_WINDOWS[slot][0]
        for minutes in (1, 5, 60, 120, 600, 5000, -30, "60", None):
            clock = heads_up_clock(slot, minutes)
            if clock is None:
                assert minutes is None
                continue
            assert 0 <= clock[0] < start, (slot, minutes, clock)
    assert heads_up_clock("nonsense", 60) is None


def test_a_configured_time_is_read_defensively() -> None:
    # An unparseable time leaves the trigger unregistered, not registered at
    # some invented hour.
    assert parse_clock("18:00:00") == (18, 0)
    assert parse_clock("07:35") == (7, 35)
    for bad in (None, "", "18", "25:00:00", "18:99", "six", 18, ["18", "00"]):
        assert parse_clock(bad) is None, bad


def test_nobody_is_told_to_do_the_laundry_they_just_did() -> None:
    prefs = _person()
    # Checked against the whole day, not the slot: a load can start in one
    # slot and finish in the next.
    loads = [at(2026, 8, 6, 16, 5).timestamp()]
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True, loads=loads
    )[2] == _nudge.REASON_ALREADY_WASHED
    reason, budgets = _claim(
        prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True, loads=loads
    )
    assert reason == _nudge.REASON_ALREADY_WASHED
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK  # costs nothing
    # Yesterday's load is not today's, and neither is one still to come.
    assert _nudge.washed_today([at(2026, 8, 5, 21).timestamp()], THU) is False
    assert _nudge.washed_today([at(2026, 8, 6, 23).timestamp()], THU) is False
    assert _nudge.washed_today([], THU) is False
    assert _nudge.washed_today(["junk", None], THU) is False
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True
    )[2] == REASON_OK


def test_the_person_who_just_emptied_it_is_never_the_one_nudged() -> None:
    # Load history alone is unreliable (needs day-learning + monitoring + a
    # Claim tap), so the coordinator's own just_washed flag is the fallback.
    prefs = _person()
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True, loads=[]
    )[2] == REASON_OK
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True,
        loads=[], just_washed=True,
    )[2] == _nudge.REASON_ALREADY_WASHED
    reason, budgets = _claim(
        prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True, just_washed=True
    )
    assert reason == _nudge.REASON_ALREADY_WASHED
    # ...and it costs them nothing, like every other "no" that isn't the budget.
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    # Only the person who washed is held back; a housemate still gets theirs.
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True,
        just_washed=False,
    )[2] == REASON_OK


def test_a_sunday_push_lands_in_the_week_it_actually_means() -> None:
    # A cell key has a weekday but no date: "tomorrow" on Sunday is Monday of
    # the next ISO week, not the one deriving from today would give.
    sunday = at(2026, 8, 2, 20, 30)  # 2026-W31, a Sunday
    assert _plan.weekday_of(sunday) == 6
    assert next_day_cell("6-eve") == "0-eve"
    assert _plan.iso_week_key(sunday) == "2026-W31"
    assert _plan.iso_week_key(sunday + datetime.timedelta(days=1)) == "2026-W32"
    # Same as today's on the other six days: no special-casing Sunday elsewhere.
    for offset in range(1, 7):
        day = sunday + datetime.timedelta(days=offset)
        assert _plan.iso_week_key(day + datetime.timedelta(days=1)) == (
            _plan.iso_week_key(day)
        ), day
    overrides, _booked = _plan.toggle_booking({}, {}, "2026-W32", "0-eve", "1")
    monday = at(2026, 8, 3, 20, 30)
    assert _plan.iso_week_key(monday) == "2026-W32"
    occupancy = _plan.effective_week({}, overrides, _plan.iso_week_key(monday))
    assert _plan.is_mine(occupancy, "0-eve", "1") is True
    assert slot_soon(list(occupancy), at(2026, 8, 3, 19, 30)) == "0-eve"
    # The old behaviour, kept as the thing that must not come back.
    stranded, _b = _plan.toggle_booking({}, {}, "2026-W31", "0-eve", "1")
    assert _plan.effective_week({}, stranded, "2026-W32") == {}


def test_push_to_tomorrow_is_where_the_nudge_moves_to() -> None:
    tomorrow = next_day_cell(THU_EVE)
    assert tomorrow == "4-eve"
    friday = lead(7, 20)
    assert slot_soon([tomorrow], friday) == tomorrow
    assert select(
        _person(), {}, "1", friday, booked=[tomorrow], washer_free=True
    )[:3] == (MSG_SLOT, tomorrow, REASON_OK)
    # Sunday wraps to Monday rather than stranding the push.
    assert next_day_cell("6-eve") == "0-eve"


def test_nothing_is_sent_about_a_guess_the_person_cannot_argue_with() -> None:
    # `predict=False` is a full opt-out: no Sunday DM, no day-of nudge, even
    # with a prediction handed straight in.
    off = _person(predict=False)
    assert plan_dm(off, {}, "1", _guess(), THU) == REASON_PREDICT_OFF
    assert select(
        off, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True
    )[2] == REASON_PREDICT_OFF
    reason, budgets = _claim(off, {}, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_PREDICT_OFF
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    # Somebody who booked the slot themselves is refused too: the opt-out is
    # from being messaged, not from being guessed at.
    assert slot_soon([THU_EVE], HEADS_UP) == THU_EVE
    assert select(
        off, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True, due=True,
        prediction=_guess(),
    )[2] == REASON_PREDICT_OFF


# --- the opportunity, and only ever one message ------------------------------


def test_an_opportunity_needs_them_to_be_overdue_by_their_own_cadence() -> None:
    # The threshold is each person's own learned cadence, not a fixed number
    # of days.
    prefs, guess = _person(), _guess()
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, prediction=guess, due=False
    )
    assert (kind, cell, reason) == (MSG_NONE, None, REASON_NOT_DUE)
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, prediction=guess, due=True
    )
    assert (kind, cell, reason) == (MSG_OPPORTUNITY, THU_EVE, REASON_OK)
    # Overdue with no confident guess is still silence, not a "not sure yet" DM.
    assert select(prefs, {}, "1", HEADS_UP, prediction=None, due=True) == (
        MSG_NONE, None, REASON_NO_PREDICTION
    )


def test_an_opportunity_is_never_about_a_slot_anybody_booked() -> None:
    # The message claims nobody has the slot; someone else's booking
    # disqualifies it even with the drum standing empty.
    prefs, guess = _person(), _guess()
    theirs = _plan.effective_week({}, {"2026-W32": {THU_EVE: ["2"]}}, "2026-W32")
    assert opportunity_cell(guess, theirs, HEADS_UP) is None
    assert select(
        prefs, {}, "1", HEADS_UP, prediction=guess, occupancy=theirs, due=True
    )[2] == REASON_NO_PREDICTION
    # Also disqualified: a slot the person booked themselves.
    mine = _plan.effective_week({}, {"2026-W32": {THU_EVE: ["1"]}}, "2026-W32")
    assert opportunity_cell(guess, mine, HEADS_UP) is None
    assert select(
        prefs, {}, "1", HEADS_UP, prediction=guess, occupancy=mine, due=True
    )[2] == REASON_NO_PREDICTION
    # Same for a standing (recurring) slot, the most common case in practice.
    standing = _plan.effective_week({"1": {"slots": [THU_EVE]}}, {}, "2026-W32")
    assert opportunity_cell(guess, standing, HEADS_UP) is None
    assert opportunity_cell(_guess("5-eve"), {}, HEADS_UP) is None
    # ...nor one whose window has already closed today.
    assert opportunity_cell(_guess("3-am"), {}, HEADS_UP) is None
    assert opportunity_cell(None, {}, HEADS_UP) is None


def test_only_one_message_is_ever_chosen() -> None:
    # select() always returns exactly one kind/cell/verdict, never two messages.
    prefs, guess = _person(), _guess()
    for kwargs in (
        {},
        {"booked": [THU_EVE]},
        {"prediction": guess, "due": True},
        {"booked": [THU_EVE], "prediction": guess, "due": True},
        {"booked": [THU_EVE], "washer_free": False},
        {"booked": [THU_EVE], "just_washed": True},
    ):
        kind, cell, reason = select(prefs, {}, "1", HEADS_UP, **kwargs)
        assert kind in (MSG_NONE, MSG_SLOT, MSG_OPPORTUNITY)
        assert (kind == MSG_NONE) == (reason != REASON_OK), (kind, reason)
        assert (cell is None) == (kind == MSG_NONE), (kind, cell)
    assert select(prefs, {}, "1", HEADS_UP)[0] == MSG_NONE


def test_the_spare_slot_nudge_cannot_fire_in_the_dead_hours() -> None:
    # Must not fire overnight (e.g. a housemate's load finishing at 2am):
    # only from one lead before a slot opens until it closes.
    prefs, guess = _person(), _guess("3-am")  # AM opens at 06:00
    def opportunity_at(hour, minute=0):
        return select(
            prefs, {}, "1", at(2026, 8, 6, hour, minute),
            prediction=guess, due=True, washer_free=True,
        )
    for hour in (0, 2, 3, 4):
        assert opportunity_at(hour)[0] == MSG_NONE, hour
    # Opens exactly one lead before the slot, same bound as the heads-up.
    assert opportunity_at(4, 59)[0] == MSG_NONE
    assert opportunity_at(5)[:3] == (MSG_OPPORTUNITY, "3-am", REASON_OK)
    # ...stays available while the slot is actually running...
    assert opportunity_at(9)[:3] == (MSG_OPPORTUNITY, "3-am", REASON_OK)
    # ...and stops when it closes, rather than lingering all afternoon.
    assert opportunity_at(12)[0] == MSG_NONE
    # The bound is the caller's lead, not a second hard-coded number.
    assert opportunity_cell(guess, {}, at(2026, 8, 6, 4), 120) == "3-am"
    assert opportunity_cell(guess, {}, at(2026, 8, 6, 4), 60) is None
    # An unreadable lead falls back to the default rather than opening the gate.
    assert opportunity_cell(guess, {}, at(2026, 8, 6, 2), "junk") is None


def test_a_reply_is_refused_once_its_slot_has_gone() -> None:
    assert slot_ended(THU_EVE, HEADS_UP) is False  # not started yet
    assert slot_ended(THU_EVE, THU) is False  # running
    assert slot_ended("3-am", THU) is True  # this morning, long gone
    assert slot_ended(THU_EVE, at(2026, 8, 7, 9)) is True  # next morning
    # Unreadable input is not "ended" — the caller's fallback is better than
    # silently refusing somebody's tap.
    assert slot_ended("nonsense", THU) is False
    assert slot_ended(THU_EVE, None) is False


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
