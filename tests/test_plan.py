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
CELL_EXPECTED = _plan.CELL_EXPECTED
GRID_WIDTH = _plan.GRID_WIDTH
SLOTS = _plan.SLOTS
SLOT_AM = _plan.SLOT_AM
SLOT_EVE = _plan.SLOT_EVE
SLOT_MID = _plan.SLOT_MID
SLOT_PM = _plan.SLOT_PM
cell_char = _plan.cell_char
cell_key = _plan.cell_key
describe_cells = _plan.describe_cells
effective_week = _plan.effective_week
holders = _plan.holders
is_mine = _plan.is_mine
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
render_grid = _plan.render_grid
render_legend = _plan.render_legend
slot_for_hour = _plan.slot_for_hour
slot_window_text = _plan.slot_window_text
toggle_booking = _plan.toggle_booking
toggle_holder = _plan.toggle_holder
week_overrides = _plan.week_overrides
weekday_of = _plan.weekday_of

WEEK = "2026-W32"
THU_EVE = "3-eve"
SUN_AM = "6-am"


def _person(*cells) -> dict:
    """A stored person record holding recurring slots, as §12 shapes them."""
    return {"name": "x", "slots": [list(parse_cell(c)) for c in cells]}


# --- slots and cell keys ----------------------------------------------------


def test_cell_keys_round_trip() -> None:
    for weekday in range(7):
        for slot in SLOTS:
            key = cell_key(weekday, slot)
            assert key == f"{weekday}-{slot}"
            assert parse_cell(key) == (weekday, slot)


def test_cell_keys_reject_nonsense() -> None:
    # These come off disk and out of a select menu; a bad one must return None
    # rather than raise inside a button callback.
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
    # The UI works in cell keys; §12 stores recurring slots as pairs. Either
    # must land on the same key.
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
    # §12 fixes the stored form; cell keys are accepted on the way in.
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
    # The boundary that bites: 2027-01-01 is a Friday of ISO week 2026-W53.
    # Keying it under 2027 would move that day's plans into a week that hasn't
    # happened yet.
    assert iso_week_key(datetime.date(2026, 12, 28)) == "2026-W53"  # Mon
    assert iso_week_key(datetime.date(2027, 1, 1)) == "2026-W53"  # Fri
    assert iso_week_key(datetime.date(2027, 1, 3)) == "2026-W53"  # Sun
    assert iso_week_key(datetime.date(2027, 1, 4)) == "2027-W01"  # Mon: rolls
    # ...and the same the other way: 2025-12-29 is already 2026-W01.
    assert iso_week_key(datetime.date(2025, 12, 28)) == "2025-W52"
    assert iso_week_key(datetime.date(2025, 12, 29)) == "2026-W01"


def test_iso_week_keys_sort_chronologically_as_strings() -> None:
    # prune_overrides is a string comparison; zero-padding is what makes that
    # legal.
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
    assert week == {THU_EVE: ["1", "2"], SUN_AM: ["1"]}


def test_an_override_replaces_the_cell_for_that_week_only() -> None:
    people = {"1": _person(THU_EVE)}
    overrides = {WEEK: {THU_EVE: ["2"]}}
    assert effective_week(people, overrides, WEEK) == {THU_EVE: ["2"]}
    # Next week is untouched — the standing slot survives.
    assert effective_week(people, overrides, "2026-W33") == {THU_EVE: ["1"]}


def test_an_empty_override_frees_a_recurring_cell_for_one_week() -> None:
    # "I normally wash Thursday evening but not this week." The empty list is a
    # tombstone, not an absence, which is why normalise_overrides keeps it.
    people = {"1": _person(THU_EVE, SUN_AM)}
    overrides = {WEEK: {THU_EVE: []}}
    assert effective_week(people, overrides, WEEK) == {SUN_AM: ["1"]}
    assert effective_week(people, overrides, "2026-W33") == {
        THU_EVE: ["1"],
        SUN_AM: ["1"],
    }


def test_the_single_holder_shape_from_the_design_doc_still_loads() -> None:
    # §12 sketches "3-eve": "123"; the stored form is a list. A store written
    # to the sketch must not vanish.
    assert normalise_holders("123") == ["123"]
    assert effective_week({}, {WEEK: {THU_EVE: "123"}}, WEEK) == {THU_EVE: ["123"]}


def test_two_people_can_hold_the_same_cell() -> None:
    # Nothing here can refuse a slot — it's information, not permission (§8) —
    # so a shape that could only remember one of them would drop a plan.
    people = {}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is True
    overrides, booked = toggle_booking(people, overrides, WEEK, THU_EVE, 2)
    assert booked is True
    assert effective_week(people, overrides, WEEK) == {THU_EVE: ["1", "2"]}


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
    # HA's Store serialises to JSON: object keys and our holder ids are written
    # as strings, and the next tap arrives as interaction.user.id, an int. A
    # mismatch here would show up as the grid quietly disowning your own cells.
    overrides, _ = toggle_booking({}, {}, WEEK, THU_EVE, 12345)
    restored = json.loads(json.dumps(overrides))
    week = effective_week({}, restored, WEEK)
    assert week == {THU_EVE: ["12345"]}
    assert is_mine(week, THU_EVE, 12345) is True  # int in, string stored
    assert is_mine(week, THU_EVE, "12345") is True
    assert is_taken_by_other(week, THU_EVE, 12345) is False
    assert cell_char(week, THU_EVE, 12345) == CELL_MINE
    # ...and toggling again removes it rather than adding a second copy.
    overrides, booked = toggle_booking({}, restored, WEEK, THU_EVE, 12345)
    assert booked is False
    assert effective_week({}, overrides, WEEK) == {}


def test_an_int_keyed_people_mapping_still_reconciles() -> None:
    # A mapping that never round-tripped can be int-keyed, exactly as in
    # people.py's own test.
    people = {123: _person(THU_EVE)}
    week = effective_week(people, {}, WEEK)
    assert week == {THU_EVE: ["123"]}
    assert is_mine(week, THU_EVE, 123) is True


def test_holders_are_deduped_and_stringified() -> None:
    assert normalise_holders([1, "1", None, 2]) == ["1", "2"]
    assert normalise_holders({"1": True}) == []
    assert holders({THU_EVE: [1, 1]}, THU_EVE) == ["1"]
    assert holders(None, THU_EVE) == []


# --- toggling ---------------------------------------------------------------


def test_toggling_books_then_frees_the_same_cell() -> None:
    overrides, booked = toggle_booking({}, {}, WEEK, THU_EVE, 1)
    assert booked is True
    assert effective_week({}, overrides, WEEK) == {THU_EVE: ["1"]}
    overrides, booked = toggle_booking({}, overrides, WEEK, THU_EVE, 1)
    assert booked is False
    assert effective_week({}, overrides, WEEK) == {}
    # The cell is still written — as an empty list, the tombstone that can
    # cancel a recurring slot.
    assert overrides[WEEK][THU_EVE] == []


def test_toggling_off_a_recurring_slot_leaves_next_week_alone() -> None:
    people = {"1": _person(THU_EVE)}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is False
    assert effective_week(people, overrides, WEEK) == {}
    assert effective_week(people, overrides, "2026-W33") == {THU_EVE: ["1"]}


def test_toggling_one_cell_leaves_the_other_holders_of_it_alone() -> None:
    people = {"1": _person(THU_EVE), "2": _person(THU_EVE)}
    overrides, booked = toggle_booking(people, {}, WEEK, THU_EVE, 1)
    assert booked is False
    assert effective_week(people, overrides, WEEK) == {THU_EVE: ["2"]}


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
    # Character for character, because alignment is the only reason to draw a
    # grid in text and it breaks silently.
    people = {"1": _person("2-am", "5-am", "3-mid", "0-pm", "3-pm", "6-pm")}
    overrides = {WEEK: {"1-eve": ["2"], "3-eve": ["7"], "5-eve": ["2"]}}
    week = effective_week(people, overrides, WEEK)
    assert render_grid(week) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ▓  ·  ·  ▓  ·\n"
        "Mid    ·  ·  ·  ▓  ·  ·  ·\n"
        "PM     ▓  ·  ·  ▓  ·  ·  ▓\n"
        "Eve    ·  ▓  ·  ▓  ·  ▓  ·"
    )
    # ...and the same week seen by the person who booked Thursday evening.
    assert render_grid(week, 7) == (
        "      Mo Tu We Th Fr Sa Su\n"
        "AM     ·  ·  ▓  ·  ·  ▓  ·\n"
        "Mid    ·  ·  ·  ▓  ·  ·  ·\n"
        "PM     ▓  ·  ·  ▓  ·  ·  ▓\n"
        "Eve    ·  ▓  ·  █  ·  ▓  ·"
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
    # The day headers and the cells under them must actually line up — the one
    # thing a text grid has to get right, and the one that breaks in silence.
    header = lines[0]
    for weekday in range(7):
        column = header.index(_plan.DAY_ABBRS[weekday]) + 1
        for row in lines[1:]:
            assert row[column] in (CELL_FREE, CELL_TAKEN, CELL_MINE)
    thursday = header.index("Th") + 1
    assert lines[4][thursday] == CELL_MINE  # Eve is the last row


def test_the_grid_is_ascii_and_block_characters_only() -> None:
    # Emoji inside a code block break monospace alignment (§6.3 / §13).
    week = effective_week({"1": _person(THU_EVE)}, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    allowed = set(" \n" + CELL_FREE + CELL_TAKEN + CELL_MINE)
    for char in render_grid(week, 1):
        assert char.isascii() or char in allowed


def test_the_grid_is_deterministic() -> None:
    people = {"2": _person(THU_EVE), "1": _person(THU_EVE, SUN_AM)}
    reordered = {"1": _person(SUN_AM, THU_EVE), "2": _person(THU_EVE)}
    assert render_grid(effective_week(people, {}, WEEK)) == render_grid(
        effective_week(reordered, {}, WEEK)
    )
    week = effective_week(people, {}, WEEK)
    assert render_grid(week, 1) == render_grid(week, 1)


# --- anonymity (design doc P5 / §11) ----------------------------------------


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
    # One message, one rendering: a viewer-less render must never produce █,
    # or the first person to look at the pinned board would see somebody
    # else's cells as their own.
    people = {"1": _person(THU_EVE), "2": _person(SUN_AM)}
    week = effective_week(people, {}, WEEK)
    assert CELL_MINE not in render_grid(week)
    assert CELL_MINE not in render_grid(week, None)
    assert render_legend(personal=False) == f"{CELL_TAKEN} taken  {CELL_FREE} free"


def test_the_legend_does_not_promise_a_state_nothing_produces() -> None:
    # ░ is reserved for the Phase 4 model guess. Until something renders it, a
    # legend entry for it is noise.
    people = {"1": _person(THU_EVE)}
    week = effective_week(people, {WEEK: {SUN_AM: ["2"]}}, WEEK)
    assert CELL_EXPECTED not in render_legend(personal=True)
    assert CELL_EXPECTED not in render_legend(personal=False)
    assert CELL_EXPECTED not in render_grid(week, 1)


def test_your_own_cells_can_be_listed_back_to_you() -> None:
    people = {"1": _person(THU_EVE, SUN_AM), "2": _person("0-am")}
    week = effective_week(people, {}, WEEK)
    assert describe_cells(week, 1) == "Th Eve · Su AM"
    assert describe_cells(week, 9) is None
    assert describe_cells(week, None) is None


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
    overrides = {WEEK: {THU_EVE: ["1"]}}
    week = effective_week({}, overrides, WEEK)
    week[THU_EVE].append("999")
    week["0-am"] = ["999"]
    assert effective_week({}, overrides, WEEK) == {THU_EVE: ["1"]}
    assert holders(overrides[WEEK], THU_EVE) == ["1"]


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
