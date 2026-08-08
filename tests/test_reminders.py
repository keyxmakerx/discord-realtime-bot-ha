"""Tests for the pure reminder decisions (design doc §10).

Runnable with plain ``python3 tests/test_reminders.py`` — no pytest / Home
Assistant, mirroring ``tests/test_habit.py``, ``tests/test_plan.py``,
``tests/test_people.py`` and ``tests/test_queue.py``. ``nudge.py`` is loaded by
file path so importing it does not pull in the package ``__init__`` (which
imports Home Assistant).

What is under test here is the answer to one question — *may this person be
messaged, about this slot, right now?* — because this is the first code in the
integration that puts a message on somebody's phone without being asked to, and
every way it can be wrong is a way somebody gets nagged.
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


# Loaded by file path, so the relative imports inside each module fall back to
# bare ones — put each module where that fallback will find it, in dependency
# order (plan, then people/habit, then nudge, which needs all three).
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

# A fixed offset rather than a named zone: nothing here reads the clock, so the
# tests never need a DST transition, and exact arithmetic is what makes the
# slot boundaries assertable to the second.
TZ = datetime.timezone(datetime.timedelta(hours=-5))


def at(year, month, day, hour=21, minute=0) -> datetime.datetime:
    """A timezone-aware local moment. 21:00 (the Eve slot) by default."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-08-06 is a Thursday. THU is that Thursday evening, inside the Eve slot
# (20:00-24:00). HEADS_UP and LATE are both inside the hour *before* it opens,
# which is when a message about that slot is now sent — the retired day-of
# nudge fired near the slot's end, too late to act on.
THU = at(2026, 8, 6, 20, 30)
HEADS_UP = at(2026, 8, 6, 19, 30)
LATE = at(2026, 8, 6, 19, 55)


def lead(day, slot_hour, minutes=30):
    """A moment ``minutes`` before a slot opens, i.e. inside the heads-up lead."""
    return at(2026, 8, day, slot_hour - 1, 60 - minutes)


def _claim(prefs, budgets, user_id, cell, moment, **kw):
    """The reminder loop's own call, in the shape these tests assert on.

    :func:`nudge.claim_select` returns the chosen message as well as the
    verdict; most of what is checked here is the verdict and the accounting, so
    the two are unpacked once rather than in twenty places.
    """
    _kind, _cell, reason, updated = claim_select(
        prefs, budgets, user_id, moment, booked=[cell] if cell else (), **kw
    )
    return reason, updated


def _person(**changes) -> dict:
    """Somebody who has opted into DM reminders, plus any overrides.

    Built through :mod:`people` rather than by hand, so a record here is exactly
    the shape the 🤖 panel writes — including the defaults, which is where the
    interesting cases are.
    """
    prefs = _people.set_reminders({}, "1", _people.REMIND_DM, name="Alex")
    if changes:
        prefs = _people.set_person(prefs, "1", **changes)
    return prefs


def _guess(cell=THU_EVE, count=5, total=8) -> dict:
    """A prediction dict of the shape :func:`habit.predict` returns."""
    return {"cell": cell, "count": count, "total": total, "confident": True}


# --- eligibility -------------------------------------------------------------


def test_somebody_the_bot_has_never_seen_gets_nothing() -> None:
    # P1: enrolment is free, but it is still enrolment. An empty store is the
    # normal state of a house that has just installed this, and the reminder
    # loop running over it must send exactly nothing.
    assert eligible({}, "1", THU) == REASON_NOT_OPTED_IN
    assert eligible(None, "1", THU) == REASON_NOT_OPTED_IN
    # A record that exists but has never answered the panel is not consent
    # either — booking a slot on the grid creates one of these.
    unanswered = _people.set_person({}, "1", name="Alex")
    assert eligible(unanswered, "1", THU) == REASON_NOT_OPTED_IN


def test_each_preference_gates_on_its_own() -> None:
    # Every one of these is somebody's explicit choice, and each has to be able
    # to stop a DM by itself — a gate that only works in combination with
    # another is a gate that quietly stops working when the other one moves.
    assert eligible(_person(), "1", THU) == REASON_OK
    assert (
        eligible(_people.set_reminders({}, "1", _people.REMIND_OFF), "1", THU)
        == REASON_REMINDERS_OFF
    )
    # The panel's *default* is the channel, and a personal reminder in the
    # channel is both noise for six other people and a per-person fact §11 says
    # is never surfaced to the household. So the channel means no DM, not a DM
    # in the channel.
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
    # An unreadable clock denies, like every other "we cannot tell" in this
    # feature: a missed nudge costs nothing, a mistimed one costs trust.
    assert eligible(_person(), "1", None) == REASON_MOMENT
    assert eligible(_person(), "1", datetime.datetime(2026, 8, 6, 20)) == REASON_MOMENT


def test_a_pause_expires_on_its_own() -> None:
    # ⏭ Skip this week is a week off, not another permanent setting somebody
    # has to remember having set.
    person = _people.get_person(_person(paused_until=THU.timestamp() + 1), "1")
    assert is_paused(person, THU) is True
    assert is_paused(person, at(2026, 8, 7, 20, 30)) is False
    assert is_paused(_people.get_person(_person(), "1"), THU) is False


# --- 🔔 which kinds, and when (per-person DM settings) -----------------------


def test_naming_no_kind_asks_the_older_broader_question() -> None:
    # Every caller written before these settings — including the reminder
    # loop's own cheap pre-filter, which runs before it knows what it would
    # even say — passes no kind, and must get the answer it always got. If a
    # kind switch or a quiet window could bite here, an upgrade would silence
    # people through a call that never mentioned either.
    quiet = _person(dm_checkin=False, dm_headsup=False, dm_opportunity=False,
                    dm_trades=False, quiet_start=0, quiet_end=23)
    assert eligible(quiet, "1", THU) == REASON_OK
    # ...and the gates it *does* apply still bite, so this is not a way past
    # the preferences that were already there.
    assert eligible(_people.set_person(quiet, "1", monitor=False), "1", THU) == (
        REASON_MONITOR_OFF
    )


def test_each_message_kind_is_switched_off_on_its_own() -> None:
    # The point of four switches rather than one: the unit somebody opts out of
    # is the message that annoyed them. A gate that silenced its neighbours
    # would be the single 📬 switch again, which is the thing this replaces.
    guess = _guess()
    checkin_off = _person(dm_checkin=False)
    # The kind names belong to :mod:`people` (they name stored fields, and this
    # module already imports that one), and the two messages this module can
    # send *are* those names — so select() hands eligible() the kind it just
    # worked out with no lookup table in between to fall out of step.
    assert (MSG_SLOT, MSG_OPPORTUNITY) == (KIND_SLOT, KIND_OPPORTUNITY)
    assert eligible(checkin_off, "1", THU, kind=KIND_CHECKIN) == REASON_KIND_OFF
    assert eligible(checkin_off, "1", THU, kind=KIND_TRADES) == REASON_OK
    assert plan_dm(checkin_off, {}, "1", guess, THU) == REASON_KIND_OFF
    # ...and it is only the Sunday DM: the heads-up they left on still goes.
    assert select(checkin_off, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_SLOT, THU_EVE, REASON_OK
    )
    headsup_off = _person(dm_headsup=False)
    assert plan_dm(headsup_off, {}, "1", guess, THU) == REASON_OK
    assert select(headsup_off, {}, "1", HEADS_UP, booked=[THU_EVE]) == (
        MSG_NONE, None, REASON_KIND_OFF
    )
    # THE case the gate's placement exists for: ⏰ off and 💡 on still gets the
    # opportunity. Gating on the kind before select() has chosen one would have
    # had to ask about both switches at once, and this is what that would break.
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
    # 🔁 is the broker's switch, not this module's: turning swaps off must not
    # cost somebody the reminders they opted into.
    trades_off = _person(dm_trades=False)
    assert plan_dm(trades_off, {}, "1", guess, THU) == REASON_OK
    assert select(trades_off, {}, "1", HEADS_UP, booked=[THU_EVE])[2] == REASON_OK


def test_a_switched_off_heads_up_is_not_swapped_for_an_opportunity() -> None:
    # These settings may only ever *subtract*. Falling through to the other
    # message when the chosen one is refused would hand somebody a
    # differently-worded DM about the very evening they just switched off —
    # routing around the tap they made, which is worse than ignoring it.
    prefs, guess = _person(dm_headsup=False), _guess()
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], prediction=guess, due=True
    ) == (MSG_NONE, None, REASON_KIND_OFF)


def test_a_silenced_message_costs_nobody_their_allowance() -> None:
    # Suppressions are checked before the budget for the same reason every
    # other one is: a message that was never sent must not spend the single
    # daily nudge that the message they *do* want would have used.
    reason, budgets = _claim(_person(dm_headsup=False), {}, "1", THU_EVE, HEADS_UP)
    assert reason == REASON_KIND_OFF
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    quiet = _person(quiet_start=19, quiet_end=8)
    reason, budgets = _claim(quiet, {}, "1", THU_EVE, HEADS_UP)
    assert reason == REASON_QUIET
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_the_quiet_window_wraps_midnight() -> None:
    # The one bit of arithmetic in this feature, and the one worth its own
    # test: 22 -> 8 is not an interval on the number line, it is two arcs of a
    # clock face. Getting the comparison backwards would silence somebody all
    # day and message them all night — the exact inverse of what they asked
    # for, which reads as the bot being malicious rather than broken.
    person = _people.get_person(_person(quiet_start=22, quiet_end=8), "1")
    quiet = [h for h in range(24) if in_quiet_hours(person, at(2026, 8, 6, h))]
    assert quiet == [0, 1, 2, 3, 4, 5, 6, 7, 22, 23]
    # A window that does not wrap is the plain reading, start inclusive and end
    # exclusive, so the two ends never both belong to it.
    daytime = _people.get_person(_person(quiet_start=10, quiet_end=14), "1")
    assert [h for h in range(24) if in_quiet_hours(daytime, at(2026, 8, 6, h))] == (
        [10, 11, 12, 13]
    )
    # No window, and the degenerate one, are both "never quiet" — no single tap
    # may collapse into total silence.
    assert in_quiet_hours(_people.get_person(_person(), "1"), THU) is False
    same = _people.get_person(_person(quiet_start=22, quiet_end=22), "1")
    assert [h for h in range(24) if in_quiet_hours(same, at(2026, 8, 6, h))] == []
    # An unreadable clock keeps quiet, like every "we cannot tell" here — but
    # only for somebody who set a window, so one bad datetime cannot mute the
    # house.
    assert in_quiet_hours(person, None) is True
    assert in_quiet_hours(_people.get_person(_person(), "1"), None) is False
    assert in_quiet_hours({}, THU) is False


def test_quiet_hours_hold_the_dawn_heads_up_and_not_the_evening_one() -> None:
    # The 05:00 trigger is the only message in the system that can wake
    # somebody up, and "quiet before 8am" is aimed precisely at it. The same
    # person's evening heads-up is untouched, which is the whole reason this is
    # a window rather than another way of saying "stop messaging me".
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
    # Somebody with no window keeps the dawn heads-up they always had.
    assert select(_person(), {}, "1", dawn, booked=["3-am"])[2] == REASON_OK


# --- the Sunday plan DM (§10.2) ---------------------------------------------


def test_no_confident_prediction_means_no_sunday_dm_at_all() -> None:
    # P6. The alternative is a push notification whose entire content is that
    # the bot has nothing to say, and for the first month that is every person
    # in the house.
    assert plan_dm(_person(), {}, "1", None, THU) == REASON_NO_PREDICTION
    assert plan_dm(_person(), {}, "1", {}, THU) == REASON_NO_PREDICTION
    assert plan_dm(_person(), {}, "1", {"cell": "nonsense"}, THU) == (
        REASON_NO_PREDICTION
    )
    # ...and nothing is spent working that out.
    reason, budgets = claim_plan_dm(_person(), {}, "1", None, THU)
    assert reason == REASON_NO_PREDICTION
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_the_sunday_dm_quotes_the_model_rather_than_a_new_sentence() -> None:
    # The wording is habit.describe_prediction + habit.explain, so the sentence
    # in the DM cannot drift from the one the 🔮 panel shows when somebody goes
    # to argue with it (P4).
    guess = _guess()
    text = plan_dm_text(guess)
    assert _habit.describe_prediction(guess) in text  # "Thursday evenings"
    assert _habit.explain(guess) in text  # "5 of your last 8 loads"
    assert text.startswith("🗓️ **Next week's laundry**")
    assert text.endswith("Look right?")
    # A real prediction, straight out of the model, renders the same way — the
    # dict above is not a shape only the test knows about.
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


# --- the day-of nudge (§10.3 / §10.4) ---------------------------------------


def test_the_heads_up_arrives_before_the_slot_rather_than_inside_it() -> None:
    # The single line that made a reservation worthless: the old nudge fired on
    # a lead before the slot *ended*, so booking Thursday Eve bought nothing
    # until you were already standing inside it. This fires before it opens.
    prefs = _person()
    assert slot_soon([THU_EVE], HEADS_UP) == THU_EVE  # 19:30, Eve opens at 20
    assert slot_soon([THU_EVE], at(2026, 8, 6, 18)) is None  # too early
    assert slot_soon([THU_EVE], THU) is None  # already open: now, not soon
    assert slot_soon([THU_EVE], at(2026, 8, 5, 19, 30)) is None  # Wednesday
    assert slot_soon([[3, "eve"]], HEADS_UP) == THU_EVE  # the §12 stored form
    assert slot_soon([], HEADS_UP) is None
    # Ties go to the slot they can act on first.
    assert slot_soon(["3-am", THU_EVE], at(2026, 8, 6, 5, 30)) == "3-am"
    # Nothing booked and no reason to think they're overdue sends nothing,
    # which is the overwhelmingly common answer.
    kind, cell, reason = select(prefs, {}, "1", HEADS_UP)
    assert (kind, cell, reason) == (MSG_NONE, None, REASON_NOT_DUE)
    kind, cell, reason = select(prefs, {}, "1", HEADS_UP, booked=[THU_EVE])
    assert (kind, cell, reason) == (MSG_SLOT, THU_EVE, REASON_OK)


def test_a_booking_beats_a_guess_and_the_two_are_worded_apart() -> None:
    # Same precedence the grid draws in: a booking is something the person
    # said, a prediction is arithmetic about their past (P4). Saying the second
    # while ignoring the first answers a question they didn't ask.
    prefs, guess = _person(), _guess()
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], prediction=guess, due=True
    )
    assert (kind, cell, reason) == (MSG_SLOT, THU_EVE, REASON_OK)
    assert is_booked([THU_EVE], THU_EVE) is True
    assert is_booked([[3, "eve"]], THU_EVE) is True  # the §12 stored form
    assert is_booked([], THU_EVE) is False
    booked_text = heads_up_text(THU_EVE, 60)
    assert "You're down for tonight" in booked_text
    assert "in about 60 minutes" in booked_text
    assert booked_text.endswith("Still want it?")  # a question, not an order
    assert "in about" not in heads_up_text(THU_EVE, None)
    # The opportunity carries only what they cannot see for themselves: that
    # nobody has booked it, and how the timing was worked out.
    chance = opportunity_text(THU_EVE, guess, 7)
    assert "Tonight is wide open" in chance
    assert "Nobody's booked tonight" in chance
    assert _habit.explain(guess) in chance  # the arithmetic comes along
    assert "about 7 days" in chance
    assert "7 days" not in opportunity_text(THU_EVE, guess, None)
    # Every slot has a today-relative phrase, and PM and Eve stay apart so
    # somebody down for 16:00-20:00 isn't told to go and wash at ten.
    phrases = {slot: heads_up_text(f"3-{slot}", 60) for slot in _plan.SLOTS}
    assert len(set(phrases.values())) == len(_plan.SLOTS)
    assert "this evening" in phrases["pm"] and "tonight" in phrases["eve"]
    assert heads_up_text("nonsense", 60) is None
    assert opportunity_text("nonsense") is None


def test_the_heads_up_says_how_long_it_really_is() -> None:
    # REGRESSION: the loop rendered heads_up_text(cell, self.nudge_lead) — the
    # *option* that decided when to look, not the distance to the slot. This
    # message deliberately has two triggers, and only one of them fires at
    # slot-start-minus-lead: the other is SIGNAL_WASHER_FREE, which arrives
    # whenever the previous load happens to finish. Booked for 20:00, nothing
    # sent at 19:00 because the washer was busy, load finishes at 19:55 — and
    # the DM said "your slot starts in about 60 minutes" about a slot five
    # minutes away. At the maximum lead of three hours it could be out by three
    # hours, on the one message somebody is supposed to act on immediately.
    assert minutes_until_slot(THU_EVE, HEADS_UP) == 30
    assert minutes_until_slot(THU_EVE, LATE) == 5
    assert "in about 5 minutes" in heads_up_text(
        THU_EVE, minutes_until_slot(THU_EVE, LATE)
    )
    # Rounded up, so the smallest thing it ever quantifies is one minute...
    assert minutes_until_slot(THU_EVE, at(2026, 8, 6, 19, 59)) == 1
    # ...and a slot already open, or an unreadable moment, is not quantified at
    # all: "soon" is the honest word for a number we do not have.
    assert minutes_until_slot(THU_EVE, THU) is None
    assert minutes_until_slot(THU_EVE, None) is None
    assert minutes_until_slot("nonsense", LATE) is None
    assert "in about" not in heads_up_text(THU_EVE, minutes_until_slot(THU_EVE, THU))


def test_the_nudge_says_the_washer_is_free_so_it_needs_it_to_be() -> None:
    # §10.4: the washer being free is the fact the whole trigger is built on.
    # A message that says "and the washer's free right now" while somebody
    # else's load is spinning is the one that stops the rest being believed.
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
    # The heart of §10.4. Both triggers run the same decision; the first one to
    # get there sends, and the second must be a no-op rather than a second DM
    # about one evening.
    prefs = _person()
    first, budgets = _claim(prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert first == REASON_OK
    second, budgets = _claim(prefs, budgets, "1", THU_EVE, LATE, washer_free=True)
    assert second == REASON_ALREADY
    # ...and it holds the other way round: the washer is emptied at 19:55, then
    # the heads-up tick would have fired.
    reason, budgets = _claim(prefs, {}, "1", THU_EVE, LATE, washer_free=True)
    assert reason == REASON_OK
    reason, budgets = _claim(prefs, budgets, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_ALREADY
    # This is deliberately NOT just the day cap doing the work. The cap would
    # give the same answer today because it happens to be 1; the rule is one
    # message per slot, so it is checked against the slot's own window.
    assert slot_start_ts(THU_EVE, THU) == at(2026, 8, 6, 20, 0).timestamp()
    # ...and the window it is checked against reaches back over the lead,
    # because both triggers for an evening slot land before 20:00. Measuring
    # from the slot's own start would put every heads-up outside the window it
    # was about, and the second trigger would send a second DM.
    assert _nudge.already_nudged_in_slot(budgets, "1", THU_EVE, THU, 60) is True
    assert _nudge.already_nudged_in_slot(budgets, "1", THU_EVE, THU) is False
    # A different slot is still a different window: this evening's message does
    # not retire this morning's. (Eve's lead does overlap PM's window, since
    # 19:30 is inside 16:00-20:00 — which is the right answer anyway, because
    # somebody who has just been messaged should not be messaged again.)
    assert _nudge.already_nudged_in_slot(budgets, "1", "3-am", THU, 60) is False


# --- the budget (P2) ---------------------------------------------------------


def test_over_budget_is_dropped_not_queued() -> None:
    # P2 in both windows. There is no pending list here, in habit.py or
    # anywhere else: a nudge that arrives a day late is a reminder about a slot
    # that has already passed.
    assert MAX_NUDGES_PER_DAY == 1 and MAX_NUDGES_PER_WEEK == 2
    prefs, guess = _person(), _guess()
    reason, budgets = claim_plan_dm(prefs, {}, "1", guess, at(2026, 8, 2, 18))
    assert reason == REASON_OK  # Sunday
    # Same day again: the day cap.
    reason, budgets = claim_plan_dm(prefs, budgets, "1", guess, at(2026, 8, 2, 19))
    assert reason == REASON_BUDGET_DAY
    # Sunday is the *last* day of an ISO week, so the plan DM about next week
    # is charged to the week that is ending — it does not eat into the two the
    # week it's about gets. That falls out of habit.py reusing plan's ISO key
    # rather than inventing week maths, and it is the right way round.
    reason, budgets = _claim(
        prefs, budgets, "1", THU_EVE, HEADS_UP, washer_free=True
    )
    assert reason == REASON_OK  # Thursday: the first of the new week
    reason, budgets = _claim(
        prefs, budgets, "1", "4-eve", lead(7, 20), washer_free=True
    )
    assert reason == REASON_OK  # Friday: the second, and the last
    # A third in that week is over the weekly cap, and the reason says which
    # cap said no — the two are very different answers to "is this working".
    reason, budgets = _claim(
        prefs, budgets, "1", "5-eve", lead(8, 20), washer_free=True
    )
    assert reason == REASON_BUDGET_WEEK
    assert reason in BUDGET_REASONS  # the only reasons worth a log line
    # The next ISO week starts a fresh allowance, without anybody resetting it.
    reason, budgets = _claim(
        prefs, budgets, "1", "0-eve", lead(10, 20), washer_free=True
    )
    assert reason == REASON_OK


def test_a_bounced_dm_still_costs_a_nudge() -> None:
    # The ordering this whole module is arranged around: claim, persist, then
    # send. A DM to somebody whose privacy settings refuse it raises Forbidden
    # and is gone — if the bounce refunded the nudge they would be retried at
    # every single trigger, forever.
    sent: list[str] = []
    stored: dict = {}

    def _attempt(prefs, cell, moment, *, bounce):
        """The reminder loop's exact order of operations, in miniature."""
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
    # The washer comes free during their Thursday afternoon slot; the DM
    # bounces off their privacy settings and nobody hears anything.
    assert _attempt(prefs, "3-mid", lead(6, 12), bounce=True) == "forbidden"
    assert sent == []
    # The nudge is spent even though nothing arrived, so a second trigger for
    # that same slot finds it already used...
    assert _attempt(prefs, "3-mid", lead(6, 12, 5), bounce=False) == (
        REASON_ALREADY
    )
    # ...and so does the evening, on the day cap rather than the slot rule.
    assert _attempt(prefs, THU_EVE, HEADS_UP, bounce=False) == REASON_BUDGET_DAY
    assert sent == []
    # And the person themselves is now marked closed, so eligibility refuses
    # them before the budget is even consulted next time.
    assert eligible(_people.mark_dm_failed(prefs, "1"), "1", THU) == REASON_DM_CLOSED


def test_the_accounting_survives_a_restart() -> None:
    # It lives in the planner Store, so it round-trips through JSON. A restart
    # that handed everybody a fresh allowance would turn "1 DM a day" into "1
    # DM per restart", which is the same feature with the cap removed.
    prefs = _person()
    reason, budgets = _claim(prefs, {}, "1", THU_EVE, HEADS_UP, washer_free=True)
    assert reason == REASON_OK
    reloaded = json.loads(json.dumps(budgets))
    assert check_nudge(budget_for(reloaded, "1"), LATE) == BUDGET_DAY
    reason, _budgets = _claim(
        prefs, reloaded, "1", THU_EVE, LATE, washer_free=True
    )
    assert reason == REASON_ALREADY
    # The id survives the trip too: an int key written in memory and a string
    # key read back off disk must not be two allowances for one human.
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
    # One trigger per slot, off that slot's own START. A single clock time
    # cannot be an hour before four different slots — and the person who washes
    # on Saturday mornings is exactly the one a fixed evening reminder misses.
    assert heads_up_clock("am", 60) == (5, 0)
    assert heads_up_clock("mid", 60) == (11, 0)
    assert heads_up_clock("pm", 60) == (15, 0)
    assert heads_up_clock("eve", 60) == (19, 0)
    # Whatever the lead, the answer stays on the slot's own day and strictly
    # before it opens. AM opens at 06:00, so an over-long lead is clamped
    # rather than wrapping into yesterday and announcing an open window.
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
    # HA's time selector stores "HH:MM:SS"; a hand-edited options file can hold
    # anything, and an unparseable one must leave the trigger unregistered
    # rather than registering it at some invented hour.
    assert parse_clock("18:00:00") == (18, 0)
    assert parse_clock("07:35") == (7, 35)
    for bad in (None, "", "18", "25:00:00", "18:99", "six", 18, ["18", "00"]):
        assert parse_clock(bad) is None, bad


def test_nobody_is_told_to_do_the_laundry_they_just_did() -> None:
    # The washer coming free is very often this person's own load finishing, so
    # without this the single most likely nudge in the whole feature is the one
    # that arrives while somebody is folding.
    prefs = _person()
    # A load claimed at 16:05 that finished at 20:30 spans two slots, which is
    # why this is asked about the whole day and not about the slot: checking
    # only the Eve window would miss exactly the case that fires most.
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
    # The load history is NOT a reliable answer to "have they washed today": it
    # is written only when the house has day-learning on AND the person has 👁
    # monitoring on AND somebody actually tapped Claim. Miss any of those and
    # `loads` is empty, `washed_today` is False, and the single most likely
    # nudge in the feature — the washer coming free because *this* person just
    # emptied it — sails straight through.
    prefs = _person()
    # No history at all, which is the default configuration's normal state.
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True, loads=[]
    )[2] == REASON_OK
    # The coordinator knows whose load it was, so it says so.
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
    # It is about *them*, not about everybody: the housemate who didn't touch
    # the machine still gets their message.
    assert select(
        prefs, {}, "1", HEADS_UP, booked=[THU_EVE], washer_free=True,
        just_washed=False,
    )[2] == REASON_OK


def test_a_sunday_push_lands_in_the_week_it_actually_means() -> None:
    # A cell key carries a weekday and no date, so "tomorrow" on a Sunday is a
    # Monday in the *next* ISO week. Deriving the week from today instead would
    # write the Monday that is already six days past — the nudge would not
    # move, and a cell nobody booked would show as taken on the shared grid for
    # the rest of Sunday.
    sunday = at(2026, 8, 2, 20, 30)  # 2026-W31, a Sunday
    assert _plan.weekday_of(sunday) == 6
    assert next_day_cell("6-eve") == "0-eve"
    # The arithmetic the fix relies on: the week comes from tomorrow's date.
    assert _plan.iso_week_key(sunday) == "2026-W31"
    assert _plan.iso_week_key(sunday + datetime.timedelta(days=1)) == "2026-W32"
    # ...and it is the same as today's on the other six days, so this is not a
    # special case anybody has to remember.
    for offset in range(1, 7):
        day = sunday + datetime.timedelta(days=offset)
        assert _plan.iso_week_key(day + datetime.timedelta(days=1)) == (
            _plan.iso_week_key(day)
        ), day
    # Booked into the right week, the push is visible where Monday's trigger
    # looks; booked into the wrong one it is invisible everywhere.
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
    # ⏭ books tomorrow's same slot, and the day-of trigger reads bookings
    # first — so "moved" means the nudge actually follows, rather than the
    # button being a polite way of saying no.
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
    # 🔕 Stop asking sets `predict` off, and that is the permanent opt-out from
    # this whole feature: no Sunday DM, no day-of nudge, and no ? on the grid
    # either. It must hold even when a prediction is handed straight in.
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


# --- the opportunity, and only ever one message (live-use design §3) --------


def test_an_opportunity_needs_them_to_be_overdue_by_their_own_cadence() -> None:
    # The gate that makes this useful rather than nagging. A fixed number of
    # days would be wrong for the twice-a-week washer and the fortnightly one
    # alike, so the threshold is each person's own learned gap.
    prefs, guess = _person(), _guess()
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, prediction=guess, due=False
    )
    assert (kind, cell, reason) == (MSG_NONE, None, REASON_NOT_DUE)
    kind, cell, reason = select(
        prefs, {}, "1", HEADS_UP, prediction=guess, due=True
    )
    assert (kind, cell, reason) == (MSG_OPPORTUNITY, THU_EVE, REASON_OK)
    # Overdue with nothing to say is still silence: no confident guess, no
    # message (P6). "I don't know your days yet" is a notification whose whole
    # content is that the bot has nothing to tell you.
    assert select(prefs, {}, "1", HEADS_UP, prediction=None, due=True) == (
        MSG_NONE, None, REASON_NO_PREDICTION
    )


def test_an_opportunity_is_never_about_a_slot_anybody_booked() -> None:
    # The whole claim of this message is that nobody has it — that is the bit
    # the person cannot see from their bedroom. A slot somebody else booked is
    # not an opportunity even with the drum standing empty, because the point
    # of the grid is that the booking is the thing to respect.
    prefs, guess = _person(), _guess()
    theirs = _plan.effective_week({}, {"2026-W32": {THU_EVE: ["2"]}}, "2026-W32")
    assert opportunity_cell(guess, theirs, HEADS_UP) is None
    assert select(
        prefs, {}, "1", HEADS_UP, prediction=guess, occupancy=theirs, due=True
    )[2] == REASON_NO_PREDICTION
    # REGRESSION: nor is one **they** booked. This used to ask only "does
    # somebody else have it", so a person's own ║ standing slot qualified the
    # moment slot_soon stopped offering the heads-up for it — and the message
    # they got was "Nobody's booked this evening", about a cell the rest of the
    # house can see drawn against their name-less █ on the shared grid. It also
    # carries the opportunity view, so it offered to book a slot they already
    # held and gave them no 🆓 Free it up to do the one useful thing.
    mine = _plan.effective_week({}, {"2026-W32": {THU_EVE: ["1"]}}, "2026-W32")
    assert opportunity_cell(guess, mine, HEADS_UP) is None
    assert select(
        prefs, {}, "1", HEADS_UP, prediction=guess, occupancy=mine, due=True
    )[2] == REASON_NO_PREDICTION
    # ...including a ♻ standing slot, which is the case that reaches this most
    # often: it is on the grid every week without anybody re-booking it.
    standing = _plan.effective_week({"1": {"slots": [THU_EVE]}}, {}, "2026-W32")
    assert opportunity_cell(guess, standing, HEADS_UP) is None
    # A guess about another day is not about right now.
    assert opportunity_cell(_guess("5-eve"), {}, HEADS_UP) is None
    # ...nor is one whose window has already closed today.
    assert opportunity_cell(_guess("3-am"), {}, HEADS_UP) is None
    assert opportunity_cell(None, {}, HEADS_UP) is None


def test_only_one_message_is_ever_chosen() -> None:
    # Four independent triggers racing each other into four DMs about one
    # evening is the failure this function exists to prevent. It returns one
    # kind, one cell, one verdict — there is no path that returns two.
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
    # Silence is the default, and it is the answer for somebody the bot knows
    # nothing about — which is every person for the first month.
    assert select(prefs, {}, "1", HEADS_UP)[0] == MSG_NONE


def test_the_spare_slot_nudge_cannot_fire_in_the_dead_hours() -> None:
    # Found by adversarial review of the 🔔 change. "Not over yet" used to be
    # the only bound, so from midnight onwards the 06:00 AM slot qualified: a
    # housemate's load finishing at 02:00 fires SIGNAL_WASHER_FREE, and
    # somebody due for a morning wash got "this morning is wide open" at two in
    # the morning — four hours early, inside the hours SLOT_WINDOWS exists to
    # declare nobody does laundry in.
    #
    # Worse here than for the heads-up, which at least concerns a slot the
    # person deliberately booked. This one is the bot volunteering, which is
    # exactly the nagging the message was designed not to be.
    prefs, guess = _person(), _guess("3-am")  # AM opens at 06:00
    def opportunity_at(hour, minute=0):
        return select(
            prefs, {}, "1", at(2026, 8, 6, hour, minute),
            prediction=guess, due=True, washer_free=True,
        )
    for hour in (0, 2, 3, 4):
        assert opportunity_at(hour)[0] == MSG_NONE, hour
    # It opens exactly one lead before the slot, the same bound the heads-up
    # uses — one answer to "how far ahead may this bot talk about a slot".
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
    # What the reply buttons check. Booking a window that has closed tells the
    # house nothing and blocks a slot nobody can use.
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
