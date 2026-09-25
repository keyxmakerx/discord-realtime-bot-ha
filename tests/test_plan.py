"""Tests for the pure week-grid helpers.

Runnable with plain ``python3 tests/test_plan.py`` — no pytest / Home
Assistant, mirroring ``tests/test_queue.py`` and ``tests/test_people.py``.
``plan.py`` is loaded by file path so importing it does not pull in the package
``__init__`` (which imports Home Assistant).
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys

_PLAN_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "plan.py",
)
_spec = importlib.util.spec_from_file_location("ld_plan", _PLAN_PATH)
_plan = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _plan
_spec.loader.exec_module(_plan)

CELL_FREE = _plan.CELL_FREE
CELL_MINE = _plan.CELL_MINE
CELL_TAKEN = _plan.CELL_TAKEN
CELL_TAKEN_EVERY_WEEK = _plan.CELL_TAKEN_EVERY_WEEK
CELL_EXPECTED = _plan.CELL_EXPECTED
CELL_RUNNING = _plan.CELL_RUNNING
CELL_STATES = _plan.CELL_STATES
GRID_WIDTH = _plan.GRID_WIDTH
OCC_HOLDERS = _plan.OCC_HOLDERS
OCC_RECURRING = _plan.OCC_RECURRING
SLOTS = _plan.SLOTS
SLOT_AM = _plan.SLOT_AM
SLOT_EVE = _plan.SLOT_EVE
SLOT_MID = _plan.SLOT_MID
SLOT_PM = _plan.SLOT_PM
STATE_EXPECTED = _plan.STATE_EXPECTED
STATE_FREE = _plan.STATE_FREE
STATE_MINE = _plan.STATE_MINE
STATE_RUNNING = _plan.STATE_RUNNING
STATE_TAKEN = _plan.STATE_TAKEN
STATE_TAKEN_EVERY_WEEK = _plan.STATE_TAKEN_EVERY_WEEK
MAX_RUNNING_CELLS = _plan.MAX_RUNNING_CELLS
cell_char = _plan.cell_char
cell_key = _plan.cell_key
cell_state = _plan.cell_state
cells_between = _plan.cells_between
cells_soonest_first = _plan.cells_soonest_first
days_ahead = _plan.days_ahead
describe_cells = _plan.describe_cells
effective_week = _plan.effective_week
expected_cells = _plan.expected_cells
holders = _plan.holders
is_mine = _plan.is_mine
is_recurring_for_me = _plan.is_recurring_for_me
is_recurring_for_other = _plan.is_recurring_for_other
is_taken = _plan.is_taken
is_taken_by_other = _plan.is_taken_by_other
iso_week_key = _plan.iso_week_key
normalise_cell = _plan.normalise_cell
normalise_holders = _plan.normalise_holders
normalise_overrides = _plan.normalise_overrides
normalise_slots = _plan.normalise_slots
parse_cell = _plan.parse_cell
prune_overrides = _plan.prune_overrides
recurring_cells = _plan.recurring_cells
recurring_holders = _plan.recurring_holders
render_grid = _plan.render_grid
render_legend = _plan.render_legend
render_week = _plan.render_week
running_cells = _plan.running_cells
slot_for_hour = _plan.slot_for_hour
slot_window_text = _plan.slot_window_text
toggle_booking = _plan.toggle_booking
toggle_holder = _plan.toggle_holder
toggle_recurring = _plan.toggle_recurring
week_overrides = _plan.week_overrides
weekday_of = _plan.weekday_of

WEEK = "2026-W32"
THU_EVE = "3-eve"
SUN_AM = "6-am"


def _cell(held, standing=()) -> dict:
    """One cell's entry in effective_week's output: holders and which of them
    are recurring. Centralised so assertions read as reconciliation, not shape.
    """
    return {OCC_HOLDERS: list(held), OCC_RECURRING: list(standing)}


def _person(*cells) -> dict:
    """A stored person record holding recurring slots."""
    return {"name": "x", "slots": [list(parse_cell(c)) for c in cells]}


# --- slots and cell keys ----------------------------------------------------


def test_cell_keys_round_trip() -> None:
    for weekday in range(7):
        for slot in SLOTS:
            key = cell_key(weekday, slot)
            assert key == f"{weekday}-{slot}"
            assert parse_cell(key) == (weekday, slot)


def test_cell_keys_reject_nonsense() -> None:
    # Bad input comes off disk and out of a select menu; it must return None,
    # not raise inside a button callback.
    assert cell_key(7, SLOT_AM) is None  # only 0-6
    assert cell_key(-1, SLOT_AM) is None
    assert cell_key(True, SLOT_AM) is None  # a bool is not a weekday
    assert cell_key(0, "night") is None
    assert parse_cell("") is None
    assert parse_cell("3") is None
    assert parse_cell("x-eve") is None
    assert parse_cell("3-night") is None
    assert parse_cell("9-eve") is None
    assert parse_cell(None) is None
    assert parse_cell(["3", "eve"]) is None


def test_normalise_cell_accepts_both_stored_forms() -> None:
    # The UI uses cell keys; storage uses [weekday, slot] pairs. Either must
    # land on the same key.
    assert normalise_cell(THU_EVE) == THU_EVE
    assert normalise_cell([3, "eve"]) == THU_EVE
    assert normalise_cell((3, "eve")) == THU_EVE
    assert normalise_cell(["3", "eve"]) == THU_EVE  # survived a JSON round trip
    assert normalise_cell([3, "night"]) is None
    assert normalise_cell([3]) is None
    assert normalise_cell(3) is None


def test_slot_windows_cover_the_usable_day() -> None:
    assert [slot_for_hour(h) for h in (6, 11)] == [SLOT_AM, SLOT_AM]
    assert [slot_for_hour(h) for h in (12, 15)] == [SLOT_MID, SLOT_MID]
    assert [slot_for_hour(h) for h in (16, 19)] == [SLOT_PM, SLOT_PM]
    assert [slot_for_hour(h) for h in (20, 23)] == [SLOT_EVE, SLOT_EVE]
    # 00:00-06:00 belongs to no slot, and says so rather than guessing.
    assert slot_for_hour(0) is None
    assert slot_for_hour(5) is None
    assert slot_for_hour(24) is None
    assert slot_for_hour("nope") is None
    assert slot_window_text(SLOT_EVE) == "20:00-00:00"


def test_recurring_slots_normalise_to_the_stored_pair_form() -> None:
    # The stored form is fixed; cell keys are accepted on the way in.
    assert normalise_slots([[3, "eve"], "6-am"]) == [[3, "eve"], [6, "am"]]
    # Deduped and ordered, so two equal weeks serialise identically.
    assert normalise_slots(["6-am", [3, "eve"], "3-eve"]) == [
        [3, "eve"],
        [6, "am"],
    ]
    assert normalise_slots(["junk", None, 7]) == []
    assert normalise_slots("3-eve") == []  # a bare string is not a list
    assert normalise_slots(None) == []
    assert recurring_cells(_person(THU_EVE, SUN_AM)) == [THU_EVE, SUN_AM]
    assert recurring_cells({}) == []
    assert recurring_cells("junk") == []


# --- the ISO week -----------------------------------------------------------


def test_iso_week_key_uses_the_iso_year_not_the_calendar_year() -> None:
    # 2027-01-01 is a Friday still in ISO week 2026-W53; keying it under 2027
    # would move that day's plans into a week that hasn't happened yet.
    assert iso_week_key(datetime.date(2026, 12, 28)) == "2026-W53"  # Mon
    assert iso_week_key(datetime.date(2027, 1, 1)) == "2026-W53"  # Fri
    assert iso_week_key(datetime.date(2027, 1, 3)) == "2026-W53"  # Sun
    assert iso_week_key(datetime.date(2027, 1, 4)) == "2027-W01"  # Mon: rolls
    # ...and the same the other way: 2025-12-29 is already 2026-W01.
    assert iso_week_key(datetime.date(2025, 12, 28)) == "2025-W52"
    assert iso_week_key(datetime.date(2025, 12, 29)) == "2026-W01"


def test_iso_week_keys_sort_chronologically_as_strings() -> None:
    # prune_overrides compares these as strings; zero-padding makes that legal.
    assert iso_week_key(datetime.date(2026, 3, 2)) == "2026-W10"
    assert "2026-W09" < "2026-W10" < "2026-W53" < "2027-W01"


def test_iso_week_key_takes_a_datetime_too_and_never_raises() -> None:
    assert iso_week_key(datetime.datetime(2026, 8, 3, 21, 30)) == "2026-W32"
    assert iso_week_key(None) is None
    assert iso_week_key("Tuesday") is None


def test_weekday_of_is_monday_based() -> None:
    assert weekday_of(datetime.date(2026, 8, 3)) == 0  # Monday
    assert weekday_of(datetime.date(2026, 8, 9)) == 6  # Sunday
    assert weekday_of(None) is None


# --- reconciliation ---------------------------------------------------------


def test_recurring_slots_fill_the_week() -> None:
    people = {"1": _person(THU_EVE, SUN_AM), "2": _person(THU_EVE)}
    week = effective_week(people, {}, WEEK)
    assert week == {
        THU_EVE: _cell(["1", "2"], ["1", "2"]),
        SUN_AM: _cell(["1"], ["1"]),
    }


def test_an_override_replaces_the_cell_for_that_week_only() -> None:
    people = {"1": _person(THU_EVE)}
    overrides = {WEEK: {THU_EVE: ["2"]}}
    # "2" has no standing slots, so this week the cell is a pure one-off.
    assert effective_week(people, overrides, WEEK) == {THU_EVE: _cell(["2"])}
    # Next week is untouched — the standing slot survives, cadence and all.
    assert effective_week(people, overrides, "2026-W33") == {
        THU_EVE: _cell(["1"], ["1"])
    }


def test_an_empty_override_frees_a_recurring_cell_for_one_week() -> None:
    # The empty list is a tombstone ("not this week"), not an absence - which
    # is why normalise_overrides keeps it.
    people = {"1": _person(THU_EVE, SUN_AM)}
    overrides = {WEEK: {THU_EVE: []}}
    assert effective_week(people, overrides, WEEK) == {SUN_AM: _cell(["1"], ["1"])}
    assert effective_week(people, overrides, "2026-W33") == {
        THU_EVE: _cell(["1"], ["1"]),
        SUN_AM: _cell(["1"], ["1"]),
    }


def test_the_single_holder_shape_from_the_design_doc_still_loads() -> None:
    # An older sketch stored a bare string instead of a list; that shape must
    # still load.
    assert normalise_holders("123") == ["123"]
    assert effective_week({}, {WEEK: {THU_EVE: "123"}}, WEEK) == {
        THU_EVE: _cell(["123"])
    }


def test_two_people_can_hold_the_same_cell() -> None:
    # This is information, not permission, so the shape must remember
    # everyone, not just one holder.
    people = {}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is True
    overrides, booked = toggle_booking(people, overrides, WEEK, THU_EVE, 2)
    assert booked is True
    assert effective_week(people, overrides, WEEK) == {THU_EVE: _cell(["1", "2"])}


# --- provenance ---------------------------------------------------------------


def test_a_standing_booking_is_told_apart_from_a_one_off() -> None:
    # With a flat {cell: [ids]}, a standing Thursday and a two-minute-old tap
    # were byte-identical downstream, so the recurring glyph was unrecoverable.
    people = {"1": _person(THU_EVE)}
    week = effective_week(people, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    assert holders(week, THU_EVE) == ["1"]
    assert recurring_holders(week, THU_EVE) == ["1"]
    assert holders(week, SUN_AM) == ["2"]
    assert recurring_holders(week, SUN_AM) == []
    # ...and that is what the two glyphs are keyed off, for a third party.
    assert cell_char(week, THU_EVE, 9) == CELL_TAKEN_EVERY_WEEK
    assert cell_char(week, SUN_AM, 9) == CELL_TAKEN
    assert is_recurring_for_other(week, THU_EVE, 9) is True
    assert is_recurring_for_other(week, SUN_AM, 9) is False


def test_an_override_containing_a_standing_holder_keeps_their_cadence() -> None:
    # Rules out a per-cell source flag: tapping a cell someone else stands on
    # snapshots the whole holder list, so a per-cell flag would wrongly demote
    # their standing booking to a one-off. Provenance must be per holder.
    people = {"1": _person(THU_EVE)}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 2)
    assert booked is True
    week = effective_week(people, overrides, WEEK)
    assert week == {THU_EVE: _cell(["1", "2"], ["1"])}
    assert cell_char(week, THU_EVE, 9) == CELL_TAKEN_EVERY_WEEK
    # To the one-off holder it's still somebody else's standing slot: ║
    # answers "whose cadence", █ answers "whose slot", and █ wins.
    assert cell_char(week, THU_EVE, 2) == CELL_MINE
    assert is_recurring_for_me(week, THU_EVE, 2) is False
    assert is_recurring_for_me(week, THU_EVE, 1) is True


def test_an_occupancy_with_no_provenance_reads_as_this_week_only() -> None:
    # A bare {cell: [ids]} is still legal input everywhere; it honestly reports
    # no standing holders and degrades to the one-off glyph rather than raising.
    flat = {THU_EVE: ["1", "2"]}
    assert holders(flat, THU_EVE) == ["1", "2"]
    assert recurring_holders(flat, THU_EVE) == []
    assert cell_char(flat, THU_EVE, 9) == CELL_TAKEN
    assert is_recurring_for_other(flat, THU_EVE, 9) is False
    # A "recurring" list not a subset of the holders is ignored, not trusted.
    assert recurring_holders({THU_EVE: _cell(["1"], ["1", "2"])}, THU_EVE) == ["1"]
    assert recurring_holders({THU_EVE: "junk"}, THU_EVE) == []


def test_a_junk_store_reconciles_to_an_empty_week() -> None:
    assert effective_week(None, None, WEEK) == {}
    assert effective_week({"1": "junk"}, {"junk": []}, WEEK) == {}
    assert normalise_overrides({WEEK: {"9-eve": ["1"], THU_EVE: ["1"]}}) == {
        WEEK: {THU_EVE: ["1"]}
    }
    assert normalise_overrides("junk") == {}
    assert week_overrides(None, WEEK) == {}


def test_past_weeks_are_pruned() -> None:
    overrides = {
        "2026-W31": {THU_EVE: ["1"]},
        WEEK: {THU_EVE: ["1"]},
        "2026-W33": {SUN_AM: ["2"]},
    }
    pruned = prune_overrides(overrides, WEEK)
    assert sorted(pruned) == [WEEK, "2026-W33"]
    # Across the year boundary, where a naive comparison would keep everything.
    assert sorted(prune_overrides({"2026-W53": {}, "2027-W01": {THU_EVE: ["1"]}},
                                  "2027-W01")) == ["2027-W01"]
    assert prune_overrides(overrides, None) == normalise_overrides(overrides)


# --- the JSON string-key hazard ---------------------------------------------


def test_ids_still_match_after_a_json_round_trip() -> None:
    # JSON round-trips ids and keys as strings, but a tap's id is an int; a
    # mismatch would show up as the grid quietly disowning your own cells.
    overrides, _ = toggle_booking({}, {}, WEEK, THU_EVE, 12345)
    restored = json.loads(json.dumps(overrides))
    week = effective_week({}, restored, WEEK)
    assert week == {THU_EVE: _cell(["12345"])}
    assert is_mine(week, THU_EVE, 12345) is True  # int in, string stored
    assert is_mine(week, THU_EVE, "12345") is True
    assert is_taken_by_other(week, THU_EVE, 12345) is False
    assert cell_char(week, THU_EVE, 12345) == CELL_MINE
    # ...and toggling again removes it rather than adding a second copy.
    overrides, booked = toggle_booking({}, restored, WEEK, THU_EVE, 12345)
    assert booked is False
    assert effective_week({}, overrides, WEEK) == {}


def test_an_int_keyed_people_mapping_still_reconciles() -> None:
    # A mapping that never round-tripped can be int-keyed.
    people = {123: _person(THU_EVE)}
    week = effective_week(people, {}, WEEK)
    assert week == {THU_EVE: _cell(["123"], ["123"])}
    assert is_mine(week, THU_EVE, 123) is True
    assert is_recurring_for_me(week, THU_EVE, 123) is True


def test_holders_are_deduped_and_stringified() -> None:
    assert normalise_holders([1, "1", None, 2]) == ["1", "2"]
    assert normalise_holders({"1": True}) == []
    assert holders({THU_EVE: [1, 1]}, THU_EVE) == ["1"]
    assert holders(None, THU_EVE) == []


# --- toggling ---------------------------------------------------------------


def test_toggling_books_then_frees_the_same_cell() -> None:
    overrides, booked = toggle_booking({}, {}, WEEK, THU_EVE, 1)
    assert booked is True
    assert effective_week({}, overrides, WEEK) == {THU_EVE: _cell(["1"])}
    overrides, booked = toggle_booking({}, overrides, WEEK, THU_EVE, 1)
    assert booked is False
    assert effective_week({}, overrides, WEEK) == {}
    # Still written as an empty list - the tombstone that can cancel a
    # recurring slot.
    assert overrides[WEEK][THU_EVE] == []


def test_toggling_off_a_recurring_slot_leaves_next_week_alone() -> None:
    people = {"1": _person(THU_EVE)}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is False
    assert effective_week(people, overrides, WEEK) == {}
    assert effective_week(people, overrides, "2026-W33") == {
        THU_EVE: _cell(["1"], ["1"])
    }


def test_toggling_one_cell_leaves_the_other_holders_of_it_alone() -> None:
    people = {"1": _person(THU_EVE), "2": _person(THU_EVE)}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is False
    # "2" is still on it every week: one person's drop-out must not rewrite
    # anybody else's cadence.
    assert effective_week(people, overrides, WEEK) == {THU_EVE: _cell(["2"], ["2"])}


def test_toggling_a_cell_leaves_other_cells_and_weeks_alone() -> None:
    overrides = {"2026-W33": {SUN_AM: ["9"]}}
    overrides, _ = toggle_booking({}, overrides, WEEK, THU_EVE, 1)
    overrides, _ = toggle_booking({}, overrides, WEEK, SUN_AM, 1)
    assert overrides["2026-W33"] == {SUN_AM: ["9"]}
    assert sorted(overrides[WEEK]) == [THU_EVE, SUN_AM]


def test_toggling_a_bad_cell_or_week_changes_nothing() -> None:
    overrides, booked = toggle_booking({}, {WEEK: {THU_EVE: ["1"]}}, WEEK, "9-eve", 1)
    assert booked is False
    assert overrides == {WEEK: {THU_EVE: ["1"]}}
    overrides, booked = toggle_booking({}, {}, "", THU_EVE, 1)
    assert (overrides, booked) == ({}, False)


def test_toggle_holder_reports_which_way_it_went() -> None:
    held, booked = toggle_holder([], 1)
    assert (held, booked) == (["1"], True)
    held, booked = toggle_holder(held, "1")
    assert (held, booked) == ([], False)


# --- the "is somebody else on this" question --------------------------------


def test_taken_by_other_is_the_question_the_ui_asks() -> None:
    week = {THU_EVE: ["1", "2"], SUN_AM: ["1"]}
    assert is_taken(week, THU_EVE) is True
    assert is_taken(week, "0-am") is False
    assert is_taken_by_other(week, SUN_AM, 1) is False  # only mine
    assert is_taken_by_other(week, THU_EVE, 1) is True  # mine AND someone's
    assert is_taken_by_other(week, SUN_AM, 3) is True
    assert is_taken_by_other(week, "0-am", 1) is False
    # No viewer (the shared board): anybody's booking is "somebody else's".
    assert is_taken_by_other(week, SUN_AM, None) is True
    assert is_mine(week, SUN_AM, None) is False


# --- rendering --------------------------------------------------------------


def test_the_grid_renders_exactly_this() -> None:
    # Character for character, since alignment is the only reason to draw a
    # text grid and it breaks silently. Person 1's six cells are standing
    # slots and the three Eve cells are one-offs, showing ║ and ▒ are genuinely
    # different things, not two names for "taken".
    people = {"1": _person("2-am", "5-am", "3-mid", "0-pm", "3-pm", "6-pm")}
    overrides = {WEEK: {"1-eve": ["2"], "3-eve": ["7"], "5-eve": ["2"]}}
    week = effective_week(people, overrides, WEEK)
    assert render_grid(week) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ║  ·  ·  ║  ·\n"
        "Mid    ·  ·  ·  ║  ·  ·  ·\n"
        "PM     ║  ·  ·  ║  ·  ·  ║\n"
        "Eve    ·  ▒  ·  ▒  ·  ▒  ·"
    )
    # ...and the same week seen by the person who booked Thursday evening.
    assert render_grid(week, 7) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ║  ·  ·  ║  ·\n"
        "Mid    ·  ·  ·  ║  ·  ·  ·\n"
        "PM     ║  ·  ·  ║  ·  ·  ║\n"
        "Eve    ·  ▒  ·  █  ·  ▒  ·"
    )
    # Seen by the standing holder: their own cadence gets no glyph.
    assert render_grid(week, 1) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  █  ·  ·  █  ·\n"
        "Mid    ·  ·  ·  █  ·  ·  ·\n"
        "PM     █  ·  ·  █  ·  ·  █\n"
        "Eve    ·  ▒  ·  ▒  ·  ▒  ·"
    )


def test_an_empty_week_is_all_dots() -> None:
    assert render_grid({}) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ·  ·  ·  ·  ·\n"
        "Mid    ·  ·  ·  ·  ·  ·  ·\n"
        "PM     ·  ·  ·  ·  ·  ·  ·\n"
        "Eve    ·  ·  ·  ·  ·  ·  ·"
    )


def test_every_rendered_line_fits_a_phone() -> None:
    week = effective_week({"1": _person(THU_EVE)}, {}, WEEK)
    lines = render_grid(week, 1).split("\n")
    for line in lines:
        assert len(line) == GRID_WIDTH
    assert GRID_WIDTH <= 30
    # Day headers and the cells under them must actually line up.
    header = lines[0]
    for weekday in range(7):
        column = header.index(_plan.DAY_ABBRS[weekday]) + 1
        for row in lines[1:]:
            assert row[column] in set(CELL_STATES.values())
    thursday = header.index("Th") + 1
    assert lines[4][thursday] == CELL_MINE  # Eve is the last row


def test_the_grid_is_ascii_and_block_characters_only() -> None:
    # Emoji inside a code block break monospace alignment.
    week = effective_week({"1": _person(THU_EVE)}, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    allowed = set(" \n") | set(CELL_STATES.values())
    for char in render_grid(week, 1):
        assert char.isascii() or char in allowed


def test_every_glyph_in_the_alphabet_fits_the_block() -> None:
    # All six glyphs at once; a glyph one column wider than the rest doesn't
    # error, it just shears the grid.
    people = {"1": _person("0-am"), "2": _person("1-am")}
    week = effective_week(people, {WEEK: {"2-am": ["3"]}}, WEEK)
    rendered = render_grid(
        week, 1, expected=["4-am"], running=["5-am", "6-am", "0-am"]
    )
    assert rendered.split("\n")[1] == "AM     █  ║  ▒  ·  ?  *  *"
    allowed = set(" \n") | set(CELL_STATES.values())
    for char in rendered:
        assert char.isascii() or char in allowed
    for line in rendered.split("\n"):
        assert len(line) == GRID_WIDTH
    # Every glyph is one character, which is what GRID_WIDTH actually rests on.
    assert {len(char) for char in CELL_STATES.values()} == {1}


def test_the_grid_is_deterministic() -> None:
    people = {"2": _person(THU_EVE), "1": _person(THU_EVE, SUN_AM)}
    reordered = {"1": _person(SUN_AM, THU_EVE), "2": _person(THU_EVE)}
    assert render_grid(effective_week(people, {}, WEEK)) == render_grid(
        effective_week(reordered, {}, WEEK)
    )
    week = effective_week(people, {}, WEEK)
    assert render_grid(week, 1) == render_grid(week, 1)


# --- anonymity -------------------------------------------------------------


def test_the_grid_never_renders_a_name_or_a_count() -> None:
    people = {
        "111": {"name": "Alex", "slots": [[3, "eve"]]},
        "222": {"name": "Sam", "slots": [[3, "eve"]]},
        "333": {"name": "Kim", "slots": [[3, "eve"]]},
    }
    week = effective_week(people, {}, WEEK)
    rendered = render_grid(week, "444") + "\n" + render_legend(personal=True)
    for leak in ("Alex", "Sam", "Kim", "111", "222", "333", "3"):
        assert leak not in rendered
    # A cell wanted by three people looks exactly like a cell wanted by one.
    one = effective_week({"111": people["111"]}, {}, WEEK)
    assert render_grid(week, "444") == render_grid(one, "444")


def test_the_shared_board_has_no_yours_state_at_all() -> None:
    # A viewer-less render must never produce the "yours" glyph, or the first
    # person to look at the pinned board would see somebody else's cells as
    # their own.
    people = {"1": _person(THU_EVE), "2": _person(SUN_AM)}
    week = effective_week(people, {}, WEEK)
    assert CELL_MINE not in render_grid(week)
    assert CELL_MINE not in render_grid(week, None)
    assert render_legend(personal=False) == f"{CELL_TAKEN} taken  {CELL_FREE} free"


def test_the_legend_does_not_promise_a_state_nothing_produces() -> None:
    # The guess glyph only appears once the habit model has an opinion; until
    # then, a legend entry for it is noise.
    people = {"1": _person(THU_EVE)}
    week = effective_week(people, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    assert CELL_EXPECTED not in render_legend(personal=True)
    assert CELL_EXPECTED not in render_legend(personal=False)
    assert CELL_EXPECTED not in render_grid(week, 1)


def test_the_running_glyph_is_legended_only_when_it_is_on_the_block() -> None:
    # The running glyph must earn its legend entry the same way every optional
    # glyph does: only once the caller says it is actually drawn.
    assert CELL_RUNNING == "*"
    for personal in (True, False):
        for expected in (True, False):
            for standing in (True, False):
                assert CELL_RUNNING not in render_legend(
                    personal=personal, expected=expected, standing=standing
                )
                # Ungated by personal, unlike the guess: this is a fact, not
                # about a person.
                assert CELL_RUNNING in render_legend(
                    personal=personal,
                    expected=expected,
                    standing=standing,
                    running=True,
                )


def test_running_is_the_one_state_the_shared_board_may_show() -> None:
    # Asymmetric with the guess glyph on purpose: the washer being on is a
    # fact anybody can see, unlike a guess about somebody's habits, so this
    # needs no viewer.
    assert running_cells([SUN_AM, [3, "eve"], "junk", None]) == [THU_EVE, SUN_AM]
    assert running_cells(None) == []
    assert running_cells("3-eve") == []  # a bare string is not a list
    assert cell_char({}, SUN_AM, None, None, [SUN_AM]) == CELL_RUNNING
    assert CELL_RUNNING in render_grid({}, running=[SUN_AM])


# --- predictions -----------------------------------------------------------


def test_a_prediction_draws_on_a_free_cell_for_its_own_viewer() -> None:
    people = {"1": _person(THU_EVE)}
    week = effective_week(people, {}, WEEK)
    assert render_grid(week, 1, expected=[SUN_AM, "0-mid"]) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ·  ·  ·  ·  ?\n"
        "Mid    ?  ·  ·  ·  ·  ·  ·\n"
        "PM     ·  ·  ·  ·  ·  ·  ·\n"
        "Eve    ·  ·  ·  █  ·  ·  ·"
    )
    # The stored pair form works too, and the block stays exactly as wide.
    for line in render_grid(week, 1, expected=[[6, "am"]]).split("\n"):
        assert len(line) == GRID_WIDTH


def test_a_real_booking_always_beats_a_guess() -> None:
    # A booking is something somebody said; a guess is arithmetic about the
    # past. If the guess could cover a booking, the grid would answer with the
    # bot's opinion instead of the house's plans.
    people = {"1": _person(THU_EVE), "2": _person(SUN_AM)}
    week = effective_week(people, {}, WEEK)
    # Predicted onto a cell the viewer holds: still █, never ?.
    assert cell_char(week, THU_EVE, 1, [THU_EVE]) == CELL_MINE
    # Predicted onto somebody else's cell: still theirs, guess not visible.
    assert cell_char(week, SUN_AM, 1, [SUN_AM]) == CELL_TAKEN_EVERY_WEEK
    one_off = effective_week({}, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    assert cell_char(one_off, SUN_AM, 1, [SUN_AM]) == CELL_TAKEN
    assert render_grid(week, 1, expected=[THU_EVE, SUN_AM]) == render_grid(week, 1)
    # ...and only once the booking goes away does the guess get the cell.
    freed = effective_week(people, {WEEK: {SUN_AM: []}}, WEEK)
    assert cell_char(freed, SUN_AM, 1, [SUN_AM]) == CELL_EXPECTED


def test_the_precedence_order_is_exactly_this() -> None:
    # Highest wins: yours, other's-every-week, other's-this-week, running,
    # guess, free. One rule; cell_char and the button styling both read cell_state.
    people = {"1": _person(SUN_AM), "2": _person(THU_EVE)}
    week = effective_week(people, {WEEK: {"0-am": ["3"]}}, WEEK)
    live = ["0-am", "1-am", "3-eve", "6-am"]
    guess = ["0-am", "1-am", "3-eve", "6-am"]
    # Yours beats everything below it, including a load actually running in it.
    assert cell_state(week, SUN_AM, 1, guess, live) == STATE_MINE
    # Somebody else's standing slot beats running and beats a guess.
    assert cell_state(week, THU_EVE, 1, guess, live) == STATE_TAKEN_EVERY_WEEK
    # Somebody else's one-off likewise: a claim outlives the load.
    assert cell_state(week, "0-am", 1, guess, live) == STATE_TAKEN
    # Running beats a guess — the guess never covers anything real.
    assert cell_state(week, "1-am", 1, guess, live) == STATE_RUNNING
    assert cell_state(week, "2-am", 1, ["2-am"], live) == STATE_EXPECTED
    assert cell_state(week, "2-pm", 1, guess, live) == STATE_FREE
    # Every state has exactly one character and no two share one.
    assert len(set(CELL_STATES.values())) == len(CELL_STATES) == 6


def test_a_prediction_is_never_rendered_for_anybody_but_its_viewer() -> None:
    # A guess is a statement about one person's habits, worse to leak than a
    # booking; the anonymous board has no viewer, so it must show no guess at all.
    week = effective_week({"1": _person(THU_EVE)}, {}, WEEK)
    assert CELL_EXPECTED not in render_grid(week, None, expected=[SUN_AM])
    assert CELL_EXPECTED not in render_grid(week, expected=[SUN_AM])
    assert cell_char(week, SUN_AM, None, [SUN_AM]) == CELL_FREE
    assert expected_cells([SUN_AM], None) == []
    assert expected_cells([SUN_AM]) == []
    # The shared board is byte-identical whether a guess exists or not.
    assert render_grid(week, expected=[SUN_AM, "0-am"]) == render_grid(week)


def test_expected_cells_are_normalised_deduped_and_ordered() -> None:
    assert expected_cells([SUN_AM, [3, "eve"], "3-eve", "junk", None, 7], 1) == [
        THU_EVE,
        SUN_AM,
    ]
    assert expected_cells(None, 1) == []
    assert expected_cells("3-eve", 1) == []  # a bare string is not a list
    assert expected_cells((THU_EVE,), 1) == [THU_EVE]
    # A junk guess renders as no guess rather than raising in a callback.
    week = effective_week({}, {}, WEEK)
    assert CELL_EXPECTED not in render_grid(week, 1, expected=["9-eve", "x"])


def test_the_legend_gains_the_guess_only_when_one_is_on_the_grid() -> None:
    assert render_legend(personal=True, expected=True) == (
        f"{CELL_MINE} yours  {CELL_TAKEN} taken  "
        f"{CELL_EXPECTED} expected  {CELL_FREE} free"
    )
    # No guess in play: unchanged, or an unused entry reads as a renderer bug.
    assert render_legend(personal=True, expected=False) == (
        f"{CELL_MINE} yours  {CELL_TAKEN} taken  {CELL_FREE} free"
    )
    # The anonymous board can never show one, even if a caller gets it wrong.
    assert render_legend(personal=False, expected=True) == (
        f"{CELL_TAKEN} taken  {CELL_FREE} free"
    )


def test_the_legend_gains_the_cadence_glyph_only_when_one_is_on_the_grid() -> None:
    assert render_legend(personal=True, standing=True) == (
        f"{CELL_MINE} yours  {CELL_TAKEN} taken  "
        f"{CELL_TAKEN_EVERY_WEEK} taken, every week  {CELL_FREE} free"
    )
    # Unlike the guess glyph, the standing glyph has no viewer guard: it's a
    # fact with no name or count on it, so the shared board draws it too.
    assert render_legend(personal=False, standing=True) == (
        f"{CELL_TAKEN} taken  {CELL_TAKEN_EVERY_WEEK} taken, every week  "
        f"{CELL_FREE} free"
    )
    assert CELL_TAKEN_EVERY_WEEK not in render_legend(personal=True)
    assert CELL_TAKEN_EVERY_WEEK not in render_legend(personal=False)


def test_render_week_reports_what_is_on_the_block_without_reading_it() -> None:
    # Replaces sniffing the rendered string, which couples every caller to
    # whatever character the renderer happens to use. The renderer says what
    # it drew.
    people = {"1": _person(THU_EVE), "2": _person(SUN_AM)}
    week = effective_week(people, {WEEK: {"0-am": ["3"]}}, WEEK)
    drawn = render_week(week, 1, expected=["2-pm"], running=["4-mid"])
    assert drawn.grid == render_grid(
        week, 1, expected=["2-pm"], running=["4-mid"]
    )
    assert (drawn.guessed, drawn.standing, drawn.running) == (True, True, True)
    assert drawn.legend == render_legend(
        personal=True, expected=True, standing=True, running=True
    )
    # A guess whose cells are all booked draws nothing, so it reports nothing.
    covered = render_week(week, 1, expected=[THU_EVE, SUN_AM])
    assert covered.guessed is False
    assert CELL_EXPECTED not in covered.legend
    # An empty week says so on all three counts.
    empty = render_week({}, 1)
    assert (empty.guessed, empty.standing, empty.running) == (False, False, False)
    assert empty.legend == f"{CELL_MINE} yours  {CELL_TAKEN} taken  {CELL_FREE} free"
    # No viewer means no personal legend and no guess, whatever it is handed.
    board = render_week(week, None, expected=["2-pm"])
    assert board.guessed is False
    assert CELL_MINE not in board.legend


def test_a_predicted_grid_is_still_ascii_and_still_fits_a_phone() -> None:
    week = effective_week({"1": _person(THU_EVE)}, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    rendered = render_grid(week, 1, expected=["0-am", "2-pm", "4-eve"])
    allowed = set(" \n" + CELL_FREE + CELL_TAKEN + CELL_MINE + CELL_EXPECTED)
    for char in rendered:
        assert char.isascii() or char in allowed
    for line in rendered.split("\n"):
        assert len(line) == GRID_WIDTH


def test_your_own_cells_can_be_listed_back_to_you() -> None:
    # Your own cadence is words, not a seventh glyph - █ already says "yours",
    # and this is where you read back what you've committed to.
    people = {"1": _person(THU_EVE, SUN_AM), "2": _person("0-am")}
    week = effective_week(people, {}, WEEK)
    assert describe_cells(week, 1) == "Th Eve (every week) · Su AM (every week)"
    assert describe_cells(week, 9) is None
    assert describe_cells(week, None) is None
    # A one-off this week says nothing extra — there is nothing extra to say.
    booked, _ = toggle_booking({}, {}, WEEK, "0-mid", 1)
    mixed = effective_week({"1": _person(THU_EVE)}, booked, WEEK)
    assert describe_cells(mixed, 1) == "Mo Mid · Th Eve (every week)"
    # "Not this week" against a standing slot drops it entirely, rather than
    # listing a cadence it isn't keeping.
    skipped = effective_week(
        {"1": _person(THU_EVE, SUN_AM)}, {WEEK: {THU_EVE: []}}, WEEK
    )
    assert describe_cells(skipped, 1) == "Su AM (every week)"


# --- non-mutation -----------------------------------------------------------


def test_nothing_mutates_the_data_it_is_given() -> None:
    people = {"1": _person(THU_EVE, SUN_AM)}
    overrides = {WEEK: {THU_EVE: ["1", "2"]}}
    snapshot = json.dumps([people, overrides], sort_keys=True)
    effective_week(people, overrides, WEEK)
    toggle_booking(people, overrides, WEEK, THU_EVE, 1)
    toggle_booking(people, overrides, WEEK, SUN_AM, 3)
    prune_overrides(overrides, "2026-W99")
    normalise_overrides(overrides)
    recurring_cells(people["1"])
    assert json.dumps([people, overrides], sort_keys=True) == snapshot


def test_the_effective_week_is_not_a_window_onto_the_store() -> None:
    people = {"1": _person(THU_EVE)}
    overrides = {WEEK: {THU_EVE: ["1", "2"]}}
    week = effective_week(people, overrides, WEEK)
    # Both lists, not just holders - provenance is a new path into the store.
    week[THU_EVE][OCC_HOLDERS].append("999")
    week[THU_EVE][OCC_RECURRING].append("999")
    week["0-am"] = _cell(["999"])
    assert effective_week(people, overrides, WEEK) == {
        THU_EVE: _cell(["1", "2"], ["1"])
    }
    assert holders(overrides[WEEK], THU_EVE) == ["1", "2"]
    assert recurring_cells(people["1"]) == [THU_EVE]


# --- the recurring writer ---------------------------------------------------


def test_a_cell_can_be_promoted_to_every_week_and_back() -> None:
    # The first writer for person["slots"]; "every week" was a shape the store
    # understood but no button could produce.
    slots, standing = toggle_recurring([], THU_EVE)
    assert (slots, standing) == ([[3, "eve"]], True)
    back, standing = toggle_recurring(slots, THU_EVE)
    assert (back, standing) == ([], False)


def test_promoting_stores_the_pair_form_and_keeps_it_ordered() -> None:
    # The stored form is [weekday, slot] pairs; two equal weeks must serialise
    # identically or the store churns.
    slots, _ = toggle_recurring([[6, "am"]], THU_EVE)
    assert slots == [[3, "eve"], [6, "am"]]
    assert json.loads(json.dumps(slots)) == slots
    # Cell keys are accepted on the way in, because the UI works in cell keys.
    assert toggle_recurring([THU_EVE], SUN_AM)[0] == [[3, "eve"], [6, "am"]]


def test_promoting_never_mutates_and_survives_junk() -> None:
    original = [[3, "eve"]]
    toggle_recurring(original, SUN_AM)
    assert original == [[3, "eve"]]
    # A bad cell changes nothing rather than raising in a button callback.
    assert toggle_recurring([[3, "eve"]], "nonsense") == ([[3, "eve"]], False)
    assert toggle_recurring(None, THU_EVE) == ([[3, "eve"]], True)
    assert toggle_recurring("junk", "junk") == ([], False)


def test_promoting_a_cell_does_not_touch_this_weeks_overrides() -> None:
    # The two layers answer different questions; writing both from one tap
    # would make "every week" mean "except where somebody edited that week".
    overrides = {WEEK: {THU_EVE: ["1"]}}
    before = json.dumps(overrides, sort_keys=True)
    toggle_recurring([], THU_EVE)
    assert json.dumps(overrides, sort_keys=True) == before


def test_a_promoted_cell_shows_as_standing_to_everybody_else() -> None:
    # The round trip that matters: the writer's output must reach the
    # standing glyph through the reconciler.
    slots, _ = toggle_recurring([], THU_EVE)
    week = effective_week({"1": {"slots": slots}}, {}, WEEK)
    assert cell_char(week, THU_EVE, 9) == CELL_TAKEN_EVERY_WEEK
    assert cell_char(week, THU_EVE, 1) == CELL_MINE
    assert describe_cells(week, 1) == "Th Eve (every week)"


# --- live occupancy ----------------------------------------------------------


def test_a_running_load_lights_the_cells_it_actually_covers() -> None:
    start = datetime.datetime(2026, 8, 8, 18, 30)  # Saturday PM
    assert cells_between(start, datetime.datetime(2026, 8, 8, 19, 0)) == ["5-pm"]
    assert cells_between(start, datetime.datetime(2026, 8, 8, 23, 0)) == [
        "5-pm",
        "5-eve",
    ]


def test_a_load_finishing_exactly_on_a_boundary_does_not_light_the_next_slot():
    # Half-open like SLOT_WINDOWS: 16:00 occupied PM for no time at all.
    assert cells_between(
        datetime.datetime(2026, 8, 8, 13, 0), datetime.datetime(2026, 8, 8, 16, 0)
    ) == ["5-mid"]


def test_an_overnight_load_skips_the_hours_no_slot_covers() -> None:
    # 00:00-06:00 has no slot on purpose, so an overnight dry lights Sat Eve
    # then Sun AM with the dead hours simply absent.
    assert cells_between(
        datetime.datetime(2026, 8, 8, 22, 0), datetime.datetime(2026, 8, 9, 9, 0)
    ) == ["5-eve", "6-am"]


def test_a_load_with_no_eta_lights_only_where_it_started() -> None:
    # "The washer is on now" is worth drawing; how long it runs is the guess
    # this glyph must not make.
    start = datetime.datetime(2026, 8, 8, 18, 0)
    assert cells_between(start, None) == ["5-pm"]
    assert cells_between(start, "junk") == ["5-pm"]
    assert cells_between(start, start) == ["5-pm"]
    # An ETA in the past is a stale ETA, not a negative-length load.
    assert cells_between(start, datetime.datetime(2026, 8, 8, 9, 0)) == ["5-pm"]


def test_a_load_starting_in_the_dead_hours_lights_nothing() -> None:
    # Nothing while still in the dead hours, and nothing with no ETA to say it
    # has left them - how long a load runs is the guess this glyph must not make.
    assert cells_between(datetime.datetime(2026, 8, 8, 3, 0), None) == []
    assert cells_between(
        datetime.datetime(2026, 8, 8, 2, 0), datetime.datetime(2026, 8, 8, 5, 0)
    ) == []
    assert cells_between(None, None) == []
    assert cells_between("junk", "junk") == []


def test_a_load_that_starts_before_dawn_still_lights_the_morning() -> None:
    # The scan used to bail out entirely when the START hour fell in the dead
    # gap, drawing nothing for the load's whole life even once it was clearly
    # mid-slot.
    assert cells_between(
        datetime.datetime(2026, 8, 6, 5, 0), datetime.datetime(2026, 8, 6, 9, 0)
    ) == ["3-am"]
    # Every hour of the gap, not just the one next to dawn.
    for hour in range(0, 6):
        assert cells_between(
            datetime.datetime(2026, 8, 6, hour, 0),
            datetime.datetime(2026, 8, 6, 13, 0),
        ) == ["3-am", "3-mid"], hour


def test_a_wedged_session_cannot_black_out_the_grid() -> None:
    # A stuck tracker would paint the running glyph across days - unfalsifiable
    # from the outside - so the count is capped. Clamped, not dropped: a real
    # load still shows its first slots.
    cells = cells_between(
        datetime.datetime(2026, 8, 8, 7, 0), datetime.datetime(2026, 8, 11, 7, 0)
    )
    assert len(cells) == MAX_RUNNING_CELLS
    assert cells == ["5-am", "5-mid", "5-pm", "5-eve"]


def test_live_occupancy_never_outranks_a_booking() -> None:
    # A booking outlives the load, so the claim is what the cell draws.
    week = effective_week({"1": {"slots": [THU_EVE]}}, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    assert cell_char(week, THU_EVE, 1, None, [THU_EVE]) == CELL_MINE
    assert cell_char(week, THU_EVE, 9, None, [THU_EVE]) == CELL_TAKEN_EVERY_WEEK
    assert cell_char(week, SUN_AM, 9, None, [SUN_AM]) == CELL_TAKEN
    assert is_taken_by_other(week, "0-am", 9) is False  # a running cell is not taken
    # But it does outrank a guess: nothing merely predicted covers something real.
    assert cell_char(week, "0-am", 1, ["0-am"], ["0-am"]) == CELL_RUNNING


# --- the time axis -----------------------------------------------------------


def test_today_gets_a_marker_that_costs_no_width() -> None:
    week = effective_week({"1": {"slots": [THU_EVE]}}, {}, WEEK)
    plain = render_grid(week, 1)
    marked = render_grid(week, 1, today=3)
    assert plain.count("\n") + 1 == marked.count("\n")  # exactly one row added
    for line in marked.split("\n"):
        assert len(line) == GRID_WIDTH
    # Over the second letter of the abbreviation - the column the cells line
    # up on - or the marker would point at the gap between two days.
    rows = marked.split("\n")
    column = rows[0].index("▾")
    assert rows[1][column] == "h"  # the 'h' of 'Th'
    # marker, header, AM, Mid, PM, Eve — the cells really do line up under it.
    assert rows[5][column] == CELL_MINE  # Thursday Eve
    # No clock, no marker — and the grid is then byte-identical to before.
    assert render_grid(week, 1, today=None) == plain
    assert render_grid(week, 1, today=9) == plain


def test_the_marker_lands_on_every_day_of_the_week() -> None:
    for weekday in range(7):
        rows = render_grid({}, today=weekday).split("\n")
        assert rows[0].index("▾") == rows[1].index(_plan.DAY_ABBRS[weekday]) + 1


def test_days_ahead_counts_forward_not_backward() -> None:
    # The grid repeats weekly, so a cell earlier than today is next week's -
    # modular arithmetic, not a subtraction.
    assert days_ahead(THU_EVE, 3) == 0
    assert days_ahead(THU_EVE, 4) == 6
    assert days_ahead(THU_EVE, 2) == 1
    assert days_ahead("junk", 3) == 99
    assert days_ahead(THU_EVE, None) == 99


def test_a_slot_that_already_ended_today_sorts_round_to_next_week() -> None:
    # Today's AM slot at 21:00 has gone; calling it "today" would rank an
    # unusable slot above tomorrow's.
    assert days_ahead("3-am", 3, 7) == 0  # 06:00-12:00, still open at 07:00
    assert days_ahead("3-am", 3, 12) == 7  # ...closed at 12:00
    assert days_ahead("3-eve", 3, 21) == 0  # 20:00-24:00, open at 21:00
    # Only ever applied to today; other days are unaffected by the hour.
    assert days_ahead("4-am", 3, 23) == 1
    assert days_ahead("3-am", 3, "junk") == 0


def test_your_cells_read_back_soonest_first_when_the_clock_is_known() -> None:
    # Monday-first order would open with a slot four days gone and bury
    # tonight's at the end - the one actionable entry, hardest to find.
    week = effective_week(
        {}, {WEEK: {"0-am": ["1"], "4-eve": ["1"], "6-pm": ["1"]}}, WEEK
    )
    assert describe_cells(week, 1) == "Mo AM · Fr Eve · Su PM"
    assert describe_cells(week, 1, today=4) == "Fr Eve · Su PM · Mo AM"
    # At 22:00 Friday Eve is still on; by 06:00 Saturday it moves to the back.
    assert describe_cells(week, 1, today=4, hour=22) == "Fr Eve · Su PM · Mo AM"
    assert describe_cells(week, 1, today=5, hour=6) == "Su PM · Mo AM · Fr Eve"


def test_soonest_first_ordering_is_stable_and_survives_junk() -> None:
    cells = ["6-pm", "junk", None, "0-am", "0-eve"]
    assert cells_soonest_first(cells, 6) == ["6-pm", "0-am", "0-eve"]
    # Same day, ordered by time of day rather than by luck.
    assert cells_soonest_first(["0-eve", "0-am"], 0) == ["0-am", "0-eve"]
    # No clock is plain Monday-first, what the stored form expects.
    assert cells_soonest_first(cells, None) == ["0-am", "0-eve", "6-pm"]
    assert cells_soonest_first(None, 3) == []


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
