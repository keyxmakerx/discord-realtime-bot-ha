"""Tests for the pure habit model.

Runnable with plain ``python3 tests/test_habit.py`` — no pytest / Home
Assistant, mirroring ``tests/test_plan.py``, ``tests/test_people.py`` and
``tests/test_queue.py``. ``habit.py`` is loaded by file path so importing it
does not pull in the package ``__init__`` (which imports Home Assistant).
"""

from __future__ import annotations

import datetime
import importlib.util
import inspect
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


_plan = _load("ld_plan", "plan.py")
# habit.py imports its sibling for the slot windows and the ISO week. Loaded by
# file path there is no package to be relative to, so it falls back to a bare
# ``import plan`` — put the module just loaded where that will find it.
sys.modules["plan"] = _plan
_habit = _load("ld_habit", "habit.py")

BUDGET_DAY = _habit.BUDGET_DAY
BUDGET_OK = _habit.BUDGET_OK
BUDGET_UNREADABLE = _habit.BUDGET_UNREADABLE
BUDGET_WEEK = _habit.BUDGET_WEEK
CORRECTION_PUSHED = _habit.CORRECTION_PUSHED
CORRECTION_WRONG = _habit.CORRECTION_WRONG
GATE_OBSERVATIONS = _habit.GATE_OBSERVATIONS
GATE_SHARE = _habit.GATE_SHARE
GATE_WEEKS = _habit.GATE_WEEKS
HISTORY_DAYS = _habit.HISTORY_DAYS
HISTORY_MAX = _habit.HISTORY_MAX
MAX_NUDGES_PER_DAY = _habit.MAX_NUDGES_PER_DAY
MAX_NUDGES_PER_WEEK = _habit.MAX_NUDGES_PER_WEEK
MIN_OBSERVATIONS = _habit.MIN_OBSERVATIONS
MIN_SHARE_PERCENT = _habit.MIN_SHARE_PERCENT
MIN_WEEKS = _habit.MIN_WEEKS
bucket_counts = _habit.bucket_counts
bucket_for = _habit.bucket_for
bucket_stats = _habit.bucket_stats
budget_for = _habit.budget_for
check_nudge = _habit.check_nudge
claim_nudge = _habit.claim_nudge
claim_nudge_for = _habit.claim_nudge_for
corrected_since = _habit.corrected_since
corrections_for = _habit.corrections_for
day_key = _habit.day_key
describe_bucket = _habit.describe_bucket
describe_prediction = _habit.describe_prediction
explain = _habit.explain
first_load_ts = _habit.first_load_ts
forget_person = _habit.forget_person
history_days = _habit.history_days
history_for = _habit.history_for
history_weeks = _habit.history_weeks
load_count = _habit.load_count
load_record = _habit.load_record
mark_nudge_pushed = _habit.mark_nudge_pushed
mark_prediction_wrong = _habit.mark_prediction_wrong
moment_ts = _habit.moment_ts
next_day_cell = _habit.next_day_cell
normalise_budget = _habit.normalise_budget
normalise_corrections = _habit.normalise_corrections
normalise_history = _habit.normalise_history
normalise_record = _habit.normalise_record
nudges_this_week = _habit.nudges_this_week
nudges_today = _habit.nudges_today
predict = _habit.predict
predicted_cells = _habit.predicted_cells
predictions = _habit.predictions
prune_history = _habit.prune_history
record_correction = _habit.record_correction
record_load = _habit.record_load
record_nudge = _habit.record_nudge

THU_EVE = "3-eve"

# A fixed offset rather than a named zone: the module never reads the clock, so
# the tests never need a DST transition, and exact arithmetic is what makes the
# 90-day and 4-week boundaries assertable to the second.
TZ = datetime.timezone(datetime.timedelta(hours=-5))


def at(year, month, day, hour=21, minute=0) -> datetime.datetime:
    """A timezone-aware local moment. 21:00 (the Eve slot) by default."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


def _loads(*moments, user_id="1", history=None) -> list[dict]:
    """A history built the only way one can be: one real claim at a time."""
    rows = history if history is not None else []
    for moment in moments:
        rows = record_load(rows, user_id, moment, monitor=True)
    return rows


# 2026-08-03 is a Monday, so these are Thursdays. Five Thursday-evening loads
# and three scattered ones: the §7.3 example, "5 of your last 8 loads".
THURSDAYS = (at(2026, 7, 9), at(2026, 7, 16), at(2026, 7, 23), at(2026, 7, 30),
             at(2026, 8, 6))
SCATTERED = (at(2026, 7, 11, 9), at(2026, 7, 20, 17), at(2026, 8, 2, 13))
NOW = at(2026, 8, 7, 12)  # the Friday after


def _regular() -> list[dict]:
    """The history of somebody who really does wash on Thursday evenings."""
    moments = sorted(THURSDAYS + SCATTERED)
    return _loads(*moments)


# --- the moment -> bucket mapping -------------------------------------------


def test_a_moment_maps_to_the_grids_own_cell() -> None:
    # The model and the grid must agree about what Thursday evening is, so this
    # is plan's slot window and plan's weekday, not a second copy of them.
    moment = at(2026, 8, 6, 21)
    assert bucket_for(moment) == THU_EVE
    assert bucket_for(moment) == _plan.cell_key(
        _plan.weekday_of(moment), _plan.slot_for_hour(moment.hour)
    )
    assert bucket_for(at(2026, 8, 6, 9)) == "3-am"
    assert bucket_for(at(2026, 8, 6, 13)) == "3-mid"
    assert bucket_for(at(2026, 8, 6, 17)) == "3-pm"
    assert bucket_for(at(2026, 8, 9, 21)) == "6-eve"  # Sunday
    # 00:00-06:00 belongs to no slot; a 3am wash is real but not a habit the
    # grid can show.
    assert bucket_for(at(2026, 8, 6, 3)) is None
    # A date has no time of day, so it cannot be a load.
    assert bucket_for(datetime.date(2026, 8, 6)) is None
    assert bucket_for(None) is None
    assert moment_ts(datetime.date(2026, 8, 6)) is None
    assert moment_ts(None) is None
    assert day_key(at(2026, 8, 6)) == "2026-08-06"
    assert day_key(None) is None


# --- history writes ---------------------------------------------------------


def test_a_claim_becomes_one_history_row() -> None:
    history = record_load([], 12345, at(2026, 8, 6, 21), monitor=True)
    assert history == [
        {"ts": at(2026, 8, 6, 21).timestamp(), "user_id": "12345", "cell": THU_EVE}
    ]
    # The shape has nowhere to put a name — §11 as a property of the data.
    assert set(history[0]) == {"ts", "user_id", "cell"}
    assert load_record(None, at(2026, 8, 6)) is None
    assert load_record("", at(2026, 8, 6)) is None
    assert load_record(1, None) is None
    assert record_load([], "", at(2026, 8, 6), monitor=True) == []


def test_monitoring_off_writes_nothing_at_all() -> None:
    # Design doc §11 / §5.2: the bot stops logging that person entirely. This
    # is the one failure here that cannot be undone by a later fix.
    history = _loads(at(2026, 7, 9))
    assert record_load(history, "1", at(2026, 7, 16), monitor=False) == history
    assert record_load([], "1", at(2026, 7, 16), monitor=False) == []
    # Anything that is not exactly True is not consent: a half-written record
    # holding "false" is truthy, and reading it as True would log an opt-out.
    for not_consent in ("false", "yes", 1, None, [], {}):
        assert record_load(history, "1", at(2026, 7, 16), monitor=not_consent) == (
            history
        )


def test_the_same_load_claimed_twice_is_one_load() -> None:
    # Unclaim then reclaim is a real pair of buttons on the live card, and a
    # cycle is 4-5 hours, so two claims inside an hour are one load.
    history = _loads(at(2026, 7, 9, 21, 0))
    assert len(record_load(history, "1", at(2026, 7, 9, 21, 30), monitor=True)) == 1
    assert len(record_load(history, "1", at(2026, 7, 9, 22, 30), monitor=True)) == 2
    # ...but two people in the same minute are two loads.
    assert len(record_load(history, "2", at(2026, 7, 9, 21, 30), monitor=True)) == 2


def test_history_prunes_at_ninety_days() -> None:
    first = at(2026, 5, 4, 21)
    history = _loads(first)
    day = datetime.timedelta(days=1)
    # One second short of the window: the old row survives.
    just_inside = record_load(
        history, "2", first + HISTORY_DAYS * day - datetime.timedelta(seconds=1),
        monitor=True,
    )
    assert len(just_inside) == 2
    # Exactly 90 days old: gone, like queue.prune's half-open window.
    exactly_90 = record_load(history, "2", first + HISTORY_DAYS * day, monitor=True)
    assert [row["user_id"] for row in exactly_90] == ["2"]


def test_a_clock_that_jumps_backwards_does_not_delete_history() -> None:
    # An unsynced RTC that corrects minutes after boot is the realistic version
    # of "a row is in the future". Ageing is one-sided so that write cannot
    # take every real row with it; HISTORY_MAX is what bounds junk instead.
    history = _regular()
    unsynced = record_load(history, "1", at(1970, 1, 1, 9), monitor=True)
    assert len(unsynced) == len(history) + 1
    assert load_count(unsynced, "1") == 9
    # ...and the bogus row itself ages out on the next real write.
    recovered = record_load(unsynced, "1", at(2026, 8, 13), monitor=True)
    assert load_count(recovered, "1") == 9
    assert min(row["ts"] for row in recovered) == history[0]["ts"]


def test_history_is_capped_so_it_can_never_grow_without_bound() -> None:
    ts = NOW.timestamp()
    # All well inside the 90 days, so only the count cap can bite.
    raw = [
        {"ts": ts - (i + 1) * 3600, "user_id": "1", "cell": THU_EVE}
        for i in range(HISTORY_MAX + 200)
    ]
    pruned = prune_history(raw, NOW)
    assert len(pruned) == HISTORY_MAX
    assert pruned[-1]["ts"] == ts - 3600  # the newest survive
    assert pruned[0]["ts"] == ts - HISTORY_MAX * 3600
    # ...and a write to a full history stays capped.
    grown = record_load(raw[:HISTORY_MAX], "2", NOW, monitor=True)
    assert len(grown) == HISTORY_MAX
    assert grown[-1]["user_id"] == "2"


def test_an_unreadable_moment_prunes_nothing_by_age() -> None:
    # Deleting somebody's history because the clock was briefly unreadable is
    # unrecoverable; keeping it for one more write is not.
    history = _loads(at(2026, 1, 1), at(2026, 5, 4))
    assert prune_history(history, None) == normalise_history(history)
    assert record_load(history, "1", None, monitor=True) == history


def test_a_junk_history_normalises_to_nothing() -> None:
    assert normalise_history(None) == []
    assert normalise_history("junk") == []
    assert normalise_history([None, "junk", 7]) == []
    assert normalise_record({"user_id": "1"}) is None  # no timestamp: never ages
    assert normalise_record({"ts": 1}) is None  # belongs to nobody
    assert normalise_record({"ts": "x", "user_id": "1"}) is None
    assert normalise_record({"ts": 1, "user_id": True}) is None
    # An unparseable cell is dropped to None rather than taking the row with it.
    assert normalise_record({"ts": 1, "user_id": 2, "cell": "9-eve"}) == {
        "ts": 1.0,
        "user_id": "2",
        "cell": None,
    }
    # Rows come back oldest first whatever order they were stored in.
    out_of_order = [
        {"ts": 20, "user_id": "1", "cell": THU_EVE},
        {"ts": 10, "user_id": "1", "cell": THU_EVE},
    ]
    assert [row["ts"] for row in normalise_history(out_of_order)] == [10.0, 20.0]


# --- the JSON string/int id hazard ------------------------------------------


def test_ids_still_match_after_a_json_round_trip() -> None:
    # HA's Store serialises to JSON, so the id is written as a string and the
    # next tap arrives as interaction.user.id, an int. A mismatch would split
    # one human's histogram across two spellings and the model would simply
    # never get confident — silently, which is the worst kind.
    history = []
    for moment in sorted(THURSDAYS + SCATTERED):
        history = record_load(history, 12345, moment, monitor=True)
    restored = json.loads(json.dumps(history))
    assert load_count(restored, 12345) == 8
    assert load_count(restored, "12345") == 8
    assert bucket_counts(restored, 12345)[THU_EVE] == 5
    assert predict(restored, 12345, NOW)["cell"] == THU_EVE
    assert predict(restored, "12345", NOW)["cell"] == THU_EVE
    # ...and a further claim lands on the same person, not a second one.
    grown = record_load(restored, 12345, at(2026, 8, 13), monitor=True)
    assert load_count(grown, "12345") == 9
    assert {row["user_id"] for row in grown} == {"12345"}


# --- the confidence gate (design doc §7.2) ----------------------------------


def test_a_real_habit_is_predicted_and_explains_itself() -> None:
    history = _regular()
    guess = predict(history, "1", NOW)
    assert guess["cell"] == THU_EVE
    assert (guess["weekday"], guess["slot"]) == (3, "eve")
    assert (guess["count"], guess["total"]) == (5, 8)
    # The exact sentence §7.3 shows.
    assert explain(guess) == "5 of your last 8 loads"
    assert describe_prediction(guess) == "Thursday evenings"
    assert predicted_cells(history, "1", NOW) == [THU_EVE]
    assert guess["gates"] == {
        GATE_OBSERVATIONS: True,
        GATE_SHARE: True,
        GATE_WEEKS: True,
    }


def test_too_few_observations_alone_vetoes_the_prediction() -> None:
    # Two loads, both Thursday evening, over five weeks: the share and the span
    # are fine, so only the observation count can be saying no.
    history = _loads(at(2026, 7, 2), at(2026, 8, 6))
    stats = bucket_stats(history, "1", THU_EVE, NOW)
    assert stats["count"] == MIN_OBSERVATIONS - 1
    assert stats["gates"] == {
        GATE_OBSERVATIONS: False,
        GATE_SHARE: True,
        GATE_WEEKS: True,
    }
    assert predict(history, "1", NOW) is None
    # One more real Thursday and it clears, with nothing else changed.
    more = _loads(at(2026, 7, 16), history=history)
    assert predict(more, "1", NOW)["count"] == MIN_OBSERVATIONS


def test_too_small_a_share_alone_vetoes_the_prediction() -> None:
    # Three Thursday evenings — enough observations — but eight other loads
    # spread over four other buckets, so they wash whenever.
    others = (
        at(2026, 7, 13, 9), at(2026, 7, 20, 9),  # Mon AM
        at(2026, 7, 14, 13), at(2026, 7, 21, 13),  # Tue Mid
        at(2026, 7, 11, 17), at(2026, 7, 18, 17),  # Sat PM
        at(2026, 7, 12), at(2026, 7, 19),  # Sun Eve
    )
    thursdays = (at(2026, 7, 9), at(2026, 7, 16), at(2026, 7, 23))
    history = _loads(*sorted(thursdays + others))
    stats = bucket_stats(history, "1", THU_EVE, NOW)
    assert (stats["count"], stats["total"]) == (3, 11)  # 27%
    assert stats["gates"] == {
        GATE_OBSERVATIONS: True,
        GATE_SHARE: False,
        GATE_WEEKS: True,
    }
    assert predict(history, "1", NOW) is None
    # Exactly at the threshold it is a habit: 3 of 10 is 30%. Integer maths, so
    # the boundary is not decided by binary rounding.
    ten = _loads(*sorted(thursdays + others[:-1]))
    stats = bucket_stats(ten, "1", THU_EVE, NOW)
    assert stats["count"] * 100 == stats["total"] * MIN_SHARE_PERCENT
    assert predict(ten, "1", NOW)["cell"] == THU_EVE


def test_too_little_history_alone_vetoes_the_prediction() -> None:
    # Four loads, all Thursday evening, so the count and the share are as
    # strong as they can be. Only the span is short.
    history = _loads(*THURSDAYS[:4])
    one_minute_short = at(2026, 8, 6, 20, 59)
    stats = bucket_stats(history, "1", THU_EVE, one_minute_short)
    assert stats["weeks"] < MIN_WEEKS
    assert stats["gates"] == {
        GATE_OBSERVATIONS: True,
        GATE_SHARE: True,
        GATE_WEEKS: False,
    }
    assert predict(history, "1", one_minute_short) is None
    # Exactly four weeks after their first load, it clears.
    exactly_four_weeks = at(2026, 8, 6, 21)
    assert history_days(history, "1", exactly_four_weeks) == MIN_WEEKS * 7
    assert history_weeks(history, "1", exactly_four_weeks) == MIN_WEEKS
    assert predict(history, "1", exactly_four_weeks)["cell"] == THU_EVE


def test_silence_is_the_default_for_a_stranger() -> None:
    assert predict([], "1", NOW) is None
    assert predict(None, "1", NOW) is None
    assert predictions([], "1", NOW) == []
    assert predicted_cells([], "1", NOW) == []
    assert bucket_counts([], "1") == {}
    assert load_count([], "1") == 0
    assert first_load_ts([], "1") is None
    assert history_weeks([], "1", NOW) == 0.0
    # ...and a person's own history says nothing about a slot they never use.
    history = _regular()
    assert bucket_stats(history, "1", "0-am", NOW)["confident"] is False
    assert bucket_stats(history, "1", "junk", NOW)["confident"] is False
    assert bucket_stats(history, "1", None, NOW)["cell"] is None


def test_a_load_in_no_slot_still_counts_toward_the_total() -> None:
    # A 3am wash votes for no weekday but they still did it, so it stays in the
    # denominator. Being wrong in the direction of silence is the right way.
    history = _loads(at(2026, 7, 9), at(2026, 7, 16), at(2026, 7, 23),
                     at(2026, 7, 26, 3))
    assert bucket_counts(history, "1") == {THU_EVE: 3}
    stats = bucket_stats(history, "1", THU_EVE, NOW)
    assert (stats["count"], stats["total"]) == (3, 4)


def test_at_most_three_buckets_can_ever_be_confident() -> None:
    # 30% each leaves no room for a fourth, which is what stops a "prediction"
    # being a shrug that covers the whole week.
    history = _loads(*sorted((
        at(2026, 7, 9), at(2026, 7, 16), at(2026, 7, 23),  # Thu Eve
        at(2026, 7, 12, 9), at(2026, 7, 19, 9), at(2026, 7, 26, 9),  # Sun AM
        at(2026, 7, 13, 9), at(2026, 7, 20, 9), at(2026, 7, 27, 9),  # Mon AM
    )))
    found = predicted_cells(history, "1", NOW)
    assert found == ["0-am", THU_EVE, "6-am"]  # tied counts, day-then-slot order
    assert len(found) <= 3
    # Deterministic: the same input always produces the same order.
    assert predicted_cells(history, "1", NOW) == found


# --- explanation (design doc §7.3 / P4) -------------------------------------


def test_the_numbers_are_available_even_when_there_is_no_guess() -> None:
    # P4: the guesses are visible and correctable, which means the UI has to be
    # able to say why there is *no* guess as well as why there is one.
    history = _loads(at(2026, 7, 2), at(2026, 8, 6))
    stats = bucket_stats(history, "1", THU_EVE, NOW)
    assert stats["confident"] is False
    assert stats["gates"][GATE_OBSERVATIONS] is False
    assert explain(stats) == "2 of your last 2 loads"


def test_the_explanation_is_strictly_second_person() -> None:
    assert explain({"count": 5, "total": 8}) == "5 of your last 8 loads"
    assert "your" in explain({"count": 1, "total": 1})
    assert explain({"count": 1, "total": 1}) == "1 of your last 1 load"
    assert explain({"count": 0, "total": 0}) is None
    assert explain({"count": 2}) is None
    assert explain(None) is None


def test_a_bucket_describes_itself_in_prose() -> None:
    assert describe_bucket(THU_EVE) == "Thursday evenings"
    assert describe_bucket("6-am") == "Sunday mornings"
    assert describe_bucket("0-mid") == "Monday afternoons"
    assert describe_bucket("1-pm") == "Tuesday early evenings"
    assert describe_bucket([3, "eve"]) == "Thursday evenings"
    assert describe_bucket("9-eve") is None
    assert describe_bucket(None) is None
    assert describe_prediction(None) is None
    assert describe_prediction({}) is None


# --- corrections (design doc §7.3) ------------------------------------------


def test_saying_the_guess_is_wrong_retires_it() -> None:
    history = _regular()
    assert predict(history, "1", NOW)["cell"] == THU_EVE
    corrections = mark_prediction_wrong([], "1", THU_EVE, NOW)
    assert predict(history, "1", NOW, corrections) is None
    assert bucket_counts(history, "1", corrections) == {"5-am": 1, "0-pm": 1,
                                                        "6-mid": 1}
    assert corrected_since(corrections, "1", THU_EVE) == NOW.timestamp()
    assert corrected_since(corrections, "1", "6-am") is None
    assert corrections_for(corrections, "1")[0]["kind"] == CORRECTION_WRONG
    # The correction belongs to the person who made it and to nobody else.
    assert predict(history, "1", NOW, mark_prediction_wrong([], "2", THU_EVE, NOW))


def test_only_real_washing_brings_a_retired_guess_back() -> None:
    # Being told "no" and then arguing from the same data would be the model
    # learning from itself with extra steps, so the correction resets the
    # evidence clock and the loads it vetoed never vote again.
    history = _regular()
    corrections = mark_prediction_wrong([], "1", THU_EVE, NOW)
    history = _loads(at(2026, 8, 13), at(2026, 8, 20), at(2026, 8, 27),
                     history=history)
    later = at(2026, 8, 28, 12)
    stats = bucket_stats(history, "1", THU_EVE, later, corrections)
    assert (stats["count"], stats["total"]) == (3, 11)  # only the fresh ones
    assert predict(history, "1", later, corrections) is None
    history = _loads(at(2026, 9, 3), history=history)
    assert predict(history, "1", at(2026, 9, 4, 12), corrections)["cell"] == THU_EVE


def test_a_pushed_nudge_is_not_the_model_being_wrong() -> None:
    # §7.3 is exact about this: the day was right, they just aren't doing it
    # tonight. Counting it as a miss would train the model out of every correct
    # guess anyone was ever too busy to act on.
    history = _regular()
    before = predict(history, "1", NOW)
    corrections = mark_nudge_pushed([], "1", THU_EVE, NOW)
    assert predict(history, "1", NOW, corrections) == before
    assert corrected_since(corrections, "1", THU_EVE) is None
    assert corrections_for(corrections, "1")[0]["kind"] == CORRECTION_PUSHED


def test_a_push_lands_on_the_next_day_and_wraps_the_week() -> None:
    assert next_day_cell(THU_EVE) == "4-eve"
    assert next_day_cell("6-am") == "0-am"  # Sunday pushes to Monday
    assert next_day_cell([6, "eve"]) == "0-eve"
    assert next_day_cell("9-eve") is None
    assert next_day_cell(None) is None


def test_a_junk_correction_store_normalises_to_nothing() -> None:
    assert normalise_corrections(None) == []
    assert normalise_corrections([{"ts": 1, "user_id": "1"}]) == []  # no kind
    assert normalise_corrections([{"ts": 1, "user_id": "1", "cell": THU_EVE,
                                   "kind": "shrug"}]) == []
    assert record_correction([], "1", THU_EVE, "shrug", NOW) == []
    assert record_correction([], "1", "9-eve", CORRECTION_WRONG, NOW) == []
    assert record_correction([], "1", THU_EVE, CORRECTION_WRONG, None) == []
    stored = record_correction([], 7, [3, "eve"], CORRECTION_WRONG, NOW)
    assert stored == [{"ts": NOW.timestamp(), "user_id": "7", "cell": THU_EVE,
                       "kind": CORRECTION_WRONG}]
    # Corrections round-trip through JSON with the same string-id hazard.
    assert corrected_since(json.loads(json.dumps(stored)), 7, THU_EVE) == (
        NOW.timestamp()
    )


# --- the model never learns from itself (design doc §7.3) -------------------


def test_predicting_and_nudging_can_never_add_to_history() -> None:
    # Structural, not documented: record_load is the only writer, and nothing
    # in the guess/nudge/push cycle can reach it.
    history = _regular()
    snapshot = json.dumps(history, sort_keys=True)
    corrections: list = []
    budgets: dict = {}
    guess = None
    for _ in range(50):
        guess = predict(history, "1", NOW, corrections)
        assert guess is not None
        _allowed, budgets = claim_nudge_for(budgets, "1", NOW)
        corrections = mark_nudge_pushed(corrections, "1", guess["cell"], NOW)
    assert json.dumps(history, sort_keys=True) == snapshot
    assert predict(history, "1", NOW, corrections) == guess
    assert load_count(history, "1") == 8
    # The nudge accounting's whole vocabulary is timestamps and counters: there
    # is nowhere in it for an observation to hide.
    assert set(record_nudge({}, NOW)) == {
        "last_nudge_ts",
        "nudge_day",
        "nudges_today",
        "nudge_week",
        "nudges_this_week",
    }
    # Only a real claim moves the count.
    assert load_count(record_load(history, "1", at(2026, 8, 13), monitor=True),
                      "1") == 9


# --- the nudge budget (design doc P2) ---------------------------------------


def test_one_nudge_a_day() -> None:
    assert MAX_NUDGES_PER_DAY == 1
    allowed, budget = claim_nudge({}, at(2026, 8, 3, 18))
    assert allowed is True
    assert budget["nudges_today"] == 1
    assert budget["nudge_day"] == "2026-08-03"
    assert check_nudge(budget, at(2026, 8, 3, 23)) == BUDGET_DAY
    allowed, after = claim_nudge(budget, at(2026, 8, 3, 23))
    assert allowed is False
    # Dropped, not queued: nothing accumulated for later.
    assert after == budget
    # The next day is a fresh day (and still inside the week's two).
    allowed, budget = claim_nudge(budget, at(2026, 8, 4, 18))
    assert allowed is True
    assert (budget["nudges_today"], budget["nudges_this_week"]) == (1, 2)


def test_two_nudges_a_week() -> None:
    assert MAX_NUDGES_PER_WEEK == 2
    budget: dict = {}
    for day in (3, 4):
        allowed, budget = claim_nudge(budget, at(2026, 8, day, 18))
        assert allowed is True
    assert check_nudge(budget, at(2026, 8, 5, 18)) == BUDGET_WEEK
    allowed, after = claim_nudge(budget, at(2026, 8, 5, 18))
    assert (allowed, after) == (False, budget)
    # Still denied on the last day of the week...
    assert check_nudge(budget, at(2026, 8, 9, 18)) == BUDGET_WEEK  # Sunday
    # ...and the following Monday starts over with one, not three.
    allowed, rolled = claim_nudge(budget, at(2026, 8, 10, 18))
    assert allowed is True
    assert rolled["nudges_this_week"] == 1
    assert rolled["nudge_week"] == "2026-W33"


def test_the_week_rolls_on_the_iso_week_not_the_calendar_year() -> None:
    # The boundary that bites: 2027-01-01 is a Friday of ISO week 2026-W53. A
    # week counter keyed on the calendar year would hand everybody a fresh
    # allowance on New Year's Day, which is exactly the week nobody wants two
    # extra DMs.
    budget: dict = {}
    allowed, budget = claim_nudge(budget, at(2026, 12, 31, 18))  # Thu, W53
    assert allowed is True
    allowed, budget = claim_nudge(budget, at(2027, 1, 1, 18))  # Fri, still W53
    assert allowed is True
    assert budget["nudge_week"] == "2026-W53"
    assert budget["nudges_this_week"] == 2
    assert check_nudge(budget, at(2027, 1, 2, 18)) == BUDGET_WEEK  # Sat, W53
    allowed, after = claim_nudge(budget, at(2027, 1, 2, 18))
    assert (allowed, after) == (False, budget)
    assert nudges_this_week(budget, at(2027, 1, 3, 18)) == 2  # Sun, still W53
    # Monday 2027-01-04 is 2027-W01: now it resets.
    allowed, rolled = claim_nudge(budget, at(2027, 1, 4, 18))
    assert allowed is True
    assert (rolled["nudge_week"], rolled["nudges_this_week"]) == ("2027-W01", 1)


def test_the_day_window_is_the_calendar_day_not_a_rolling_24_hours() -> None:
    _allowed, budget = claim_nudge({}, at(2026, 8, 3, 23, 30))
    assert nudges_today(budget, at(2026, 8, 3, 23, 59)) == 1
    # Half an hour later it is a different day, and the day cap has reset even
    # though far less than 24 hours have passed. "One a day" is what a person
    # experiences, not a sliding window they cannot see.
    assert nudges_today(budget, at(2026, 8, 4, 0, 1)) == 0
    assert check_nudge(budget, at(2026, 8, 4, 0, 1)) == BUDGET_OK


def test_an_unreadable_moment_denies_rather_than_sends() -> None:
    # If we cannot count it we cannot cap it, and the safe direction for a
    # message budget is always "don't send".
    assert check_nudge({}, None) == BUDGET_UNREADABLE
    assert check_nudge({}, "Tuesday") == BUDGET_UNREADABLE
    allowed, after = claim_nudge({}, None)
    assert allowed is False
    assert after == normalise_budget(None)
    assert record_nudge({}, None) == normalise_budget(None)


def test_the_budget_survives_a_json_round_trip_and_a_junk_store() -> None:
    _allowed, budget = claim_nudge({}, at(2026, 8, 3, 18))
    restored = json.loads(json.dumps(budget))
    assert check_nudge(restored, at(2026, 8, 3, 23)) == BUDGET_DAY
    assert normalise_budget("junk") == normalise_budget(None)
    assert normalise_budget(None)["nudges_this_week"] == 0
    assert normalise_budget({"nudges_this_week": True})["nudges_this_week"] == 0
    assert normalise_budget({"nudges_this_week": -3})["nudges_this_week"] == 0
    assert normalise_budget({"nudge_week": 32})["nudge_week"] is None
    assert normalise_budget({"last_nudge_ts": "x"})["last_nudge_ts"] is None
    # §12's older shape has no week key, so it reads as a fresh week: one extra
    # permitted nudge at upgrade time and no lost messages.
    legacy = {"last_nudge_ts": 1754000000, "nudges_this_week": 1}
    assert nudges_this_week(legacy, at(2026, 8, 3, 18)) == 0
    assert check_nudge(legacy, at(2026, 8, 3, 18)) == BUDGET_OK


def test_the_budget_mapping_collapses_int_and_string_keys() -> None:
    allowed, budgets = claim_nudge_for({123: {}}, 123, at(2026, 8, 3, 18))
    assert allowed is True
    assert list(budgets) == ["123"]
    restored = json.loads(json.dumps(budgets))
    allowed, budgets = claim_nudge_for(restored, 123, at(2026, 8, 3, 23))
    assert allowed is False
    assert list(budgets) == ["123"]  # never two accountings for one human
    # One person's cap is not another's.
    allowed, budgets = claim_nudge_for(budgets, 456, at(2026, 8, 3, 23))
    assert allowed is True
    assert sorted(budgets) == ["123", "456"]
    assert budget_for(budgets, "123")["nudges_today"] == 1
    assert budget_for(None, 1) == normalise_budget(None)
    assert budget_for({}, 1) == normalise_budget(None)


# --- privacy (design doc §11 / P5) ------------------------------------------


def test_no_output_can_name_or_count_another_person() -> None:
    mine, theirs = "90001", "90002"
    history = _loads(*sorted(THURSDAYS + SCATTERED), user_id=mine)
    history = _loads(at(2026, 7, 12, 9), at(2026, 7, 19, 9), at(2026, 7, 26, 9),
                     at(2026, 8, 2, 9), user_id=theirs, history=history)
    corrections = mark_prediction_wrong([], theirs, THU_EVE, NOW)
    _allowed, budgets = claim_nudge_for({}, theirs, NOW)
    stats = bucket_stats(history, mine, THU_EVE, NOW, corrections)
    outputs = [
        history_for(history, mine),
        bucket_counts(history, mine, corrections),
        stats,
        predict(history, mine, NOW, corrections),
        predictions(history, mine, NOW, corrections),
        predicted_cells(history, mine, NOW, corrections),
        explain(stats),
        describe_prediction(stats),
        describe_bucket(THU_EVE),
        corrections_for(corrections, mine),
        budget_for(budgets, mine),
        load_count(history, mine),
        history_weeks(history, mine, NOW),
    ]
    blob = json.dumps(outputs)
    assert theirs not in blob
    assert "name" not in blob
    # Their correction cannot retire this person's guess, and their four loads
    # cannot dilute this person's share.
    assert predict(history, mine, NOW, corrections)["cell"] == THU_EVE
    assert stats["total"] == 8  # theirs, not the household's 12
    assert len(normalise_history(history)) == 12


def test_every_read_of_history_is_scoped_to_one_person() -> None:
    # There is no household aggregate to accidentally render: the only two
    # functions that take a history without a user_id are shape operations that
    # return rows, never counts.
    shape_only = {"normalise_history", "prune_history"}
    for name, function in sorted(vars(_habit).items()):
        if name.startswith("_") or not inspect.isfunction(function):
            continue
        params = inspect.signature(function).parameters
        if "history" in params and name not in shape_only:
            assert "user_id" in params, name


def test_forgetting_a_person_removes_only_them() -> None:
    history = _loads(at(2026, 7, 9), at(2026, 7, 16), user_id="1")
    history = _loads(at(2026, 7, 10), user_id="2", history=history)
    remaining = forget_person(history, 1)
    assert [row["user_id"] for row in remaining] == ["2"]
    assert load_count(remaining, "1") == 0
    corrections = mark_prediction_wrong([], "1", THU_EVE, NOW)
    assert forget_person(corrections, "1") == []
    assert forget_person(None, "1") == []


# --- non-mutation -----------------------------------------------------------


def test_nothing_mutates_the_data_it_is_given() -> None:
    # The caller assigns the result and then saves; a rejected write must not
    # have already changed the in-memory model.
    history = _regular()
    corrections = mark_prediction_wrong([], "1", "0-am", NOW)
    budgets = {"1": normalise_budget(None)}
    snapshot = json.dumps([history, corrections, budgets], sort_keys=True)
    record_load(history, "1", at(2026, 8, 13), monitor=True)
    record_load(history, "2", at(2026, 8, 13), monitor=False)
    prune_history(history, NOW)
    forget_person(history, "1")
    predict(history, "1", NOW, corrections)
    predictions(history, "1", NOW, corrections)
    bucket_counts(history, "1", corrections)
    bucket_stats(history, "1", THU_EVE, NOW, corrections)
    mark_prediction_wrong(corrections, "1", THU_EVE, NOW)
    mark_nudge_pushed(corrections, "1", THU_EVE, NOW)
    claim_nudge(budgets["1"], NOW)
    claim_nudge_for(budgets, "1", NOW)
    record_nudge(budgets["1"], NOW)
    assert json.dumps([history, corrections, budgets], sort_keys=True) == snapshot


def test_a_read_is_never_a_window_onto_the_store() -> None:
    history = _regular()
    rows = history_for(history, "1")
    rows[0]["cell"] = "0-am"
    rows.append({"ts": 0, "user_id": "1", "cell": THU_EVE})
    assert len(history_for(history, "1")) == 8
    assert history_for(history, "1")[0]["cell"] == THU_EVE
    counts = bucket_counts(history, "1")
    counts[THU_EVE] = 99
    assert bucket_counts(history, "1")[THU_EVE] == 5


def test_a_dormant_household_is_not_predicted_at_from_expired_history() -> None:
    # Pruning only happens on a write, so a house that stops claiming for a
    # season never rewrites its store. The read side has to apply the same
    # 90-day window or the model keeps being confident about a year-old habit.
    history = _loads(at(2025, 7, 3), at(2025, 7, 10), at(2025, 7, 17),
                     at(2025, 7, 24), at(2025, 7, 31))
    later = at(2026, 7, 30, 12)
    assert prune_history(history, later) == []  # nothing survives the window
    # ...so nothing may be read out of it either.
    assert predict(history, "1", later) is None
    assert predictions(history, "1", later) == []
    assert predicted_cells(history, "1", later) == []
    assert bucket_counts(history, "1", None, later) == {}
    stats = bucket_stats(history, "1", THU_EVE, later)
    assert (stats["count"], stats["total"], stats["confident"]) == (0, 0, False)
    assert explain(stats) is None
    assert load_count(history, "1", later) == 0
    assert history_weeks(history, "1", later) == 0.0
    # A read without a moment is still a shape read — the raw rows are there.
    assert len(history_for(history, "1")) == 5


def test_a_stale_row_cannot_unlock_the_four_week_gate() -> None:
    # Two weeks of real Thursdays: correctly silent, the span gate vetoes.
    history = _loads(at(2026, 7, 9), at(2026, 7, 16), at(2026, 7, 23))
    now = at(2026, 7, 24, 12)
    assert predict(history, "1", now) is None
    # One unsynced-RTC write later, the store holds a 1970 row (kept on
    # purpose — ageing is one-sided). It must not stretch "how long have I been
    # watching you" past four weeks and turn two weeks of data into a habit.
    unsynced = record_load(history, "1", at(1970, 1, 1, 9), monitor=True)
    assert load_count(unsynced, "1") == 4  # retained in the store...
    assert history_weeks(unsynced, "1", now) < MIN_WEEKS  # ...but not counted
    assert bucket_stats(unsynced, "1", THU_EVE, now)["gates"][GATE_WEEKS] is False
    assert predict(unsynced, "1", now) is None
    # The four real weeks still clear it once they have actually elapsed.
    later = _loads(at(2026, 8, 6), history=unsynced)
    assert predict(later, "1", at(2026, 8, 6, 21))["cell"] == THU_EVE


def test_a_future_dated_row_does_not_disable_the_dedupe() -> None:
    # A row dated ahead of now never ages out, and it sorts last. Dedupe that
    # only looked at the newest row would compare against it forever, so
    # Unclaim -> Reclaim would count twice for the rest of that person's life.
    junk = [{"ts": at(2027, 1, 1).timestamp(), "user_id": "1", "cell": THU_EVE}]
    history = record_load(junk, "1", at(2026, 7, 30, 21), monitor=True)
    history = record_load(history, "1", at(2026, 7, 30, 21, 10), monitor=True)
    assert len(history) == 2  # the junk row plus one real load
    assert bucket_counts(history, "1")[THU_EVE] == 2  # not 3
    # Somebody else's future-dated row cannot break this person's dedupe either.
    theirs = [{"ts": at(2027, 1, 1).timestamp(), "user_id": "2", "cell": THU_EVE}]
    mine = record_load(theirs, "1", at(2026, 7, 30, 21), monitor=True)
    assert len(record_load(mine, "1", at(2026, 7, 30, 21, 10), monitor=True)) == 2


def test_a_naive_moment_is_refused_rather_than_guessed_at() -> None:
    # utcnow() instead of dt_util.now() is the realistic slip, and it is the
    # one bad input that does not look bad: the timestamp would be read in the
    # process timezone while the bucket is read off the bare wall clock, so a
    # 01:00 Friday wash in UTC+2 becomes a plausible Thursday-evening
    # observation. Three of those clear the gate for a habit nobody has.
    naive = datetime.datetime(2026, 8, 6, 21)
    assert moment_ts(naive) is None
    assert bucket_for(naive) is None
    assert day_key(naive) is None
    assert load_record("1", naive) is None
    assert record_load([], "1", naive, monitor=True) == []
    # ...and it routes down the existing "don't send" path, not a wrong bucket.
    assert check_nudge({}, naive) == BUDGET_UNREADABLE
    assert claim_nudge({}, naive) == (False, normalise_budget(None))
    assert record_nudge({}, naive) == normalise_budget(None)
    # An aware moment of the same wall clock is unaffected.
    assert bucket_for(at(2026, 8, 6, 21)) == THU_EVE


def test_a_backwards_clock_cannot_refill_the_nudge_budget() -> None:
    # An unsynced RTC after a restart reads a moment in the past. Comparing
    # window keys for *inequality* would call that a different week and hand
    # out a fresh allowance — the P2 cap failing open, which is the direction
    # that ends with the bot muted.
    budget: dict = {}
    for day in (3, 4):
        allowed, budget = claim_nudge(budget, at(2026, 8, day, 18))
        assert allowed is True
    assert check_nudge(budget, at(2026, 8, 5, 18, 30)) == BUDGET_WEEK
    # The clock reads last year. Still denied, and nothing is rewritten.
    stale = at(2025, 6, 1, 18, 30)
    assert check_nudge(budget, stale) != BUDGET_OK
    allowed, after = claim_nudge(budget, stale)
    assert (allowed, after) == (False, budget)
    assert nudges_today(budget, stale) == 1
    assert nudges_this_week(budget, stale) == 2
    # NTP corrects it minutes later and the scheduler fires again the same real
    # evening: still the same spent week, so still no DM.
    assert check_nudge(after, at(2026, 8, 5, 18, 35)) == BUDGET_WEEK
    assert claim_nudge(after, at(2026, 8, 6, 18, 30))[0] is False
    # The real Monday still resets, so a backwards excursion costs no messages.
    allowed, rolled = claim_nudge(after, at(2026, 8, 10, 18))
    assert (allowed, rolled["nudges_this_week"]) == (True, 1)


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
