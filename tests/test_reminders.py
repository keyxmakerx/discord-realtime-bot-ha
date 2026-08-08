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
REASON_REMINDERS_OFF = _nudge.REASON_REMINDERS_OFF
REASON_WASHER_BUSY = _nudge.REASON_WASHER_BUSY
BUDGET_REASONS = _nudge.BUDGET_REASONS
claim_day_nudge = _nudge.claim_day_nudge
claim_plan_dm = _nudge.claim_plan_dm
current_cell = _nudge.current_cell
day_nudge = _nudge.day_nudge
day_nudge_text = _nudge.day_nudge_text
eligible = _nudge.eligible
fallback_clock = _nudge.fallback_clock
is_booked = _nudge.is_booked
is_paused = _nudge.is_paused
parse_clock = _nudge.parse_clock
plan_dm = _nudge.plan_dm
plan_dm_text = _nudge.plan_dm_text
slot_start_ts = _nudge.slot_start_ts

THU_EVE = "3-eve"

# A fixed offset rather than a named zone: nothing here reads the clock, so the
# tests never need a DST transition, and exact arithmetic is what makes the
# slot boundaries assertable to the second.
TZ = datetime.timezone(datetime.timedelta(hours=-5))


def at(year, month, day, hour=21, minute=0) -> datetime.datetime:
    """A timezone-aware local moment. 21:00 (the Eve slot) by default."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-08-06 is a Thursday. THU is that Thursday evening, inside the Eve slot
# (20:00-24:00); FALLBACK is the backstop time for that same slot at the
# default lead.
THU = at(2026, 8, 6, 20, 30)
FALLBACK = at(2026, 8, 6, 23, 0)


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


def test_the_nudge_only_fires_inside_the_slot_it_is_about() -> None:
    # The washer coming free at 5pm says nothing about somebody's evening, so
    # "what are you down for" is asked about *this moment*, not about today.
    prefs, guess = _person(), _guess()
    assert current_cell([], guess, THU) == THU_EVE
    assert current_cell([], guess, at(2026, 8, 6, 17)) is None  # their day, PM
    assert current_cell([], guess, at(2026, 8, 5, 21)) is None  # Wednesday
    assert current_cell([], guess, at(2026, 8, 6, 3)) is None  # in no slot
    # Nothing on right now is the common answer, and it sends nothing.
    assert (
        day_nudge(prefs, {}, "1", None, at(2026, 8, 6, 17), washer_free=True)
        == REASON_NOTHING_TODAY
    )
    # A cell handed in from elsewhere still has to be the one that is running.
    assert (
        day_nudge(prefs, {}, "1", THU_EVE, at(2026, 8, 6, 17), washer_free=True)
        == REASON_OUTSIDE_SLOT
    )
    assert day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True) == REASON_OK


def test_a_booking_beats_a_guess_and_is_worded_as_one() -> None:
    # Same precedence the grid draws in: a booking is something the person
    # said, a prediction is arithmetic about their past (P4). The distinction
    # is invisible except in the wording, which is exactly where it matters.
    assert current_cell([THU_EVE], None, THU) == THU_EVE
    assert is_booked([THU_EVE], THU_EVE) is True
    assert is_booked([[3, "eve"]], THU_EVE) is True  # the §12 stored form
    assert is_booked([], THU_EVE) is False
    booked_text = day_nudge_text(THU_EVE, None)
    guess_text = day_nudge_text(THU_EVE, _guess())
    assert "You're down for **tonight**" in booked_text
    assert "I've got you down for **tonight**" in guess_text
    assert _habit.explain(_guess()) in guess_text  # the arithmetic comes along
    assert booked_text.endswith("the washer's free right now.")
    # Every slot has a today-relative phrase, and PM and Eve stay apart so
    # somebody down for 16:00-20:00 isn't told to go and wash at ten.
    phrases = {
        slot: day_nudge_text(f"3-{slot}", None) for slot in _plan.SLOTS
    }
    assert len({text for text in phrases.values()}) == len(_plan.SLOTS)
    assert "this evening" in phrases["pm"] and "tonight" in phrases["eve"]
    assert day_nudge_text("nonsense") is None


def test_the_nudge_says_the_washer_is_free_so_it_needs_it_to_be() -> None:
    # §10.4: the washer being free is the fact the whole trigger is built on.
    # A message that says "and the washer's free right now" while somebody
    # else's load is spinning is the one that stops the rest being believed.
    prefs = _person()
    assert day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=False) == (
        REASON_WASHER_BUSY
    )
    reason, budgets = claim_day_nudge(
        prefs, {}, "1", THU_EVE, THU, washer_free=False
    )
    assert reason == REASON_WASHER_BUSY
    # ...and a washer that was busy costs nobody their allowance.
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK


def test_whichever_trigger_comes_first_wins_and_the_other_is_dropped() -> None:
    # The heart of §10.4. Both triggers run the same decision; the first one to
    # get there sends, and the second must be a no-op rather than a second DM
    # about one evening.
    prefs = _person()
    first, budgets = claim_day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True)
    assert first == REASON_OK
    second, budgets = claim_day_nudge(
        prefs, budgets, "1", THU_EVE, FALLBACK, washer_free=True
    )
    assert second == REASON_ALREADY
    # ...and it holds the other way round: the backstop fires at 23:00, then
    # somebody empties the washer at 23:30.
    reason, budgets = claim_day_nudge(prefs, {}, "1", THU_EVE, FALLBACK, washer_free=True)
    assert reason == REASON_OK
    reason, budgets = claim_day_nudge(
        prefs, budgets, "1", THU_EVE, at(2026, 8, 6, 23, 30), washer_free=True
    )
    assert reason == REASON_ALREADY
    # This is deliberately NOT just the day cap doing the work. The cap would
    # give the same answer today because it happens to be 1; the rule is one
    # nudge per slot, so it is checked against the slot's own start.
    assert slot_start_ts(THU_EVE, THU) == at(2026, 8, 6, 20, 0).timestamp()
    assert _nudge.already_nudged_in_slot(budgets, "1", THU_EVE, FALLBACK) is True
    # A nudge sent in the afternoon does not block the evening's slot.
    assert _nudge.already_nudged_in_slot(budgets, "1", "3-pm", FALLBACK) is False


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
    reason, budgets = claim_day_nudge(
        prefs, budgets, "1", THU_EVE, THU, washer_free=True
    )
    assert reason == REASON_OK  # Thursday: the first of the new week
    reason, budgets = claim_day_nudge(
        prefs, budgets, "1", "4-eve", at(2026, 8, 7, 21), washer_free=True
    )
    assert reason == REASON_OK  # Friday: the second, and the last
    # A third in that week is over the weekly cap, and the reason says which
    # cap said no — the two are very different answers to "is this working".
    reason, budgets = claim_day_nudge(
        prefs, budgets, "1", "5-eve", at(2026, 8, 8, 21), washer_free=True
    )
    assert reason == REASON_BUDGET_WEEK
    assert reason in BUDGET_REASONS  # the only reasons worth a log line
    # The next ISO week starts a fresh allowance, without anybody resetting it.
    reason, budgets = claim_day_nudge(
        prefs, budgets, "1", "0-eve", at(2026, 8, 10, 21), washer_free=True
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
        reason, budgets = claim_day_nudge(
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
    assert _attempt(prefs, "3-mid", at(2026, 8, 6, 13), bounce=True) == "forbidden"
    assert sent == []
    # The nudge is spent even though nothing arrived, so the backstop for that
    # same slot finds it already used...
    assert _attempt(prefs, "3-mid", at(2026, 8, 6, 15), bounce=False) == (
        REASON_ALREADY
    )
    # ...and so does the evening, on the day cap rather than the slot rule.
    assert _attempt(prefs, THU_EVE, THU, bounce=False) == REASON_BUDGET_DAY
    assert sent == []
    # And the person themselves is now marked closed, so eligibility refuses
    # them before the budget is even consulted next time.
    assert eligible(_people.mark_dm_failed(prefs, "1"), "1", THU) == REASON_DM_CLOSED


def test_the_accounting_survives_a_restart() -> None:
    # It lives in the planner Store, so it round-trips through JSON. A restart
    # that handed everybody a fresh allowance would turn "1 DM a day" into "1
    # DM per restart", which is the same feature with the cap removed.
    prefs = _person()
    reason, budgets = claim_day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True)
    assert reason == REASON_OK
    reloaded = json.loads(json.dumps(budgets))
    assert check_nudge(budget_for(reloaded, "1"), FALLBACK) == BUDGET_DAY
    reason, _budgets = claim_day_nudge(
        prefs, reloaded, "1", THU_EVE, FALLBACK, washer_free=True
    )
    assert reason == REASON_ALREADY
    # The id survives the trip too: an int key written in memory and a string
    # key read back off disk must not be two allowances for one human.
    reason, budgets = claim_day_nudge(prefs, {}, 1, THU_EVE, THU, washer_free=True)
    assert reason == REASON_OK
    assert list(json.loads(json.dumps(budgets))) == ["1"]
    reason, _budgets = claim_day_nudge(
        prefs, json.loads(json.dumps(budgets)), "1", THU_EVE, FALLBACK,
        washer_free=True,
    )
    assert reason == REASON_ALREADY


# --- the backstop's timing ---------------------------------------------------


def test_the_backstop_lands_near_the_end_of_the_slot_it_belongs_to() -> None:
    # §10.4 trigger (b) is "a fallback time near the end of *that* slot", which
    # a single clock time cannot be for four different slots — and the person
    # who washes on Saturday mornings is exactly the one a fixed evening
    # reminder never reaches.
    assert fallback_clock("am", 60) == (11, 0)
    assert fallback_clock("mid", 60) == (15, 0)
    assert fallback_clock("pm", 60) == (19, 0)
    assert fallback_clock("eve", 60) == (23, 0)
    # Whatever the lead, the time lands inside the window it belongs to — which
    # is what lets the backstop and the washer-freed event be the same decision
    # asked twice, with no special case for either.
    for slot in _plan.SLOTS:
        for lead in (1, 5, 60, 120, 179, 180, 5000, -30, "60", None):
            clock = fallback_clock(slot, lead)
            if clock is None:
                assert lead is None
                continue
            assert _plan.slot_for_hour(clock[0]) == slot, (slot, lead, clock)
    assert fallback_clock("nonsense", 60) is None


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
    assert (
        day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True, loads=loads)
        == _nudge.REASON_ALREADY_WASHED
    )
    reason, budgets = claim_day_nudge(
        prefs, {}, "1", THU_EVE, THU, washer_free=True, loads=loads
    )
    assert reason == _nudge.REASON_ALREADY_WASHED
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK  # costs nothing
    # Yesterday's load is not today's, and neither is one still to come.
    assert _nudge.washed_today([at(2026, 8, 5, 21).timestamp()], THU) is False
    assert _nudge.washed_today([at(2026, 8, 6, 23).timestamp()], THU) is False
    assert _nudge.washed_today([], THU) is False
    assert _nudge.washed_today(["junk", None], THU) is False
    assert day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True) == REASON_OK


def test_the_person_who_just_emptied_it_is_never_the_one_nudged() -> None:
    # The load history is NOT a reliable answer to "have they washed today": it
    # is written only when the house has day-learning on AND the person has 👁
    # monitoring on AND somebody actually tapped Claim. Miss any of those and
    # `loads` is empty, `washed_today` is False, and the single most likely
    # nudge in the feature — the washer coming free because *this* person just
    # emptied it — sails straight through.
    prefs = _person()
    # No history at all, which is the default configuration's normal state.
    assert (
        day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True, loads=[])
        == REASON_OK
    )
    # The coordinator knows whose load it was, so it says so.
    assert (
        day_nudge(
            prefs, {}, "1", THU_EVE, THU, washer_free=True, loads=[], just_washed=True
        )
        == _nudge.REASON_ALREADY_WASHED
    )
    reason, budgets = claim_day_nudge(
        prefs, {}, "1", THU_EVE, THU, washer_free=True, just_washed=True
    )
    assert reason == _nudge.REASON_ALREADY_WASHED
    # ...and it costs them nothing, like every other "no" that isn't the budget.
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    # It is about *them*, not about everybody: the housemate who didn't touch
    # the machine still gets their nudge.
    assert (
        day_nudge(prefs, {}, "1", THU_EVE, THU, washer_free=True, just_washed=False)
        == REASON_OK
    )


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
    assert current_cell(list(occupancy), None, monday) == "0-eve"
    # The old behaviour, kept as the thing that must not come back.
    stranded, _b = _plan.toggle_booking({}, {}, "2026-W31", "0-eve", "1")
    assert _plan.effective_week({}, stranded, "2026-W32") == {}


def test_push_to_tomorrow_is_where_the_nudge_moves_to() -> None:
    # ⏭ books tomorrow's same slot, and the day-of trigger reads bookings
    # first — so "moved" means the nudge actually follows, rather than the
    # button being a polite way of saying no.
    tomorrow = next_day_cell(THU_EVE)
    assert tomorrow == "4-eve"
    friday = at(2026, 8, 7, 21)
    assert current_cell([tomorrow], None, friday) == tomorrow
    assert (
        day_nudge(_person(), {}, "1", tomorrow, friday, washer_free=True)
        == REASON_OK
    )
    # Sunday wraps to Monday rather than stranding the push.
    assert next_day_cell("6-eve") == "0-eve"


def test_nothing_is_sent_about_a_guess_the_person_cannot_argue_with() -> None:
    # 🔕 Stop asking sets `predict` off, and that is the permanent opt-out from
    # this whole feature: no Sunday DM, no day-of nudge, and no ? on the grid
    # either. It must hold even when a prediction is handed straight in.
    off = _person(predict=False)
    assert plan_dm(off, {}, "1", _guess(), THU) == REASON_PREDICT_OFF
    assert day_nudge(off, {}, "1", THU_EVE, THU, washer_free=True) == (
        REASON_PREDICT_OFF
    )
    reason, budgets = claim_day_nudge(off, {}, "1", THU_EVE, THU, washer_free=True)
    assert reason == REASON_PREDICT_OFF
    assert check_nudge(budget_for(budgets, "1"), THU) == BUDGET_OK
    # Somebody down for the slot who booked it themselves is refused too: the
    # opt-out is from being messaged, not from being guessed at.
    assert current_cell([THU_EVE], None, THU) == THU_EVE
    assert (
        day_nudge(off, {}, "1", THU_EVE, THU, washer_free=True) == REASON_PREDICT_OFF
    )


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
