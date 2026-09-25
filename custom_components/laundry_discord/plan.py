"""Pure helpers for the week grid: what a week looks like to one person.

No Home Assistant or discord imports, so it is unit-tested directly;
:mod:`assistant` owns the ``Store`` and the Discord calls, and nothing here
mutates state. The grid is anonymous: no name, no count. JSON keys always come
back as strings, so ids are compared as strings throughout.
"""

from __future__ import annotations

from typing import NamedTuple

# --- slots ---------------------------------------------------------------------
# One slot IS one load (a cycle is 4-5 hours), not a time range you reserve.
SLOT_AM = "am"
SLOT_MID = "mid"
SLOT_PM = "pm"
SLOT_EVE = "eve"
SLOTS = (SLOT_AM, SLOT_MID, SLOT_PM, SLOT_EVE)

SLOT_LABELS = {
    SLOT_AM: "AM",
    SLOT_MID: "Mid",
    SLOT_PM: "PM",
    SLOT_EVE: "Eve",
}

# Half-open [start, end) hour windows, local time. 00:00-06:00 is in no slot
# on purpose: not worth a fifth grid column for a row of dots.
SLOT_WINDOWS = {
    SLOT_AM: (6, 12),
    SLOT_MID: (12, 16),
    SLOT_PM: (16, 20),
    SLOT_EVE: (20, 24),
}

# Two letters: "M T W T F S S" is ambiguous, full names don't fit the grid width.
DAY_ABBRS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# --- the cell alphabet -----------------------------------------------------
# Shape encodes KIND, weight encodes WHOSE: ``▒`` somebody else, ``█`` you.
#
#   ·   free
#   ?   the model's guess — the viewer's own, never on a shared view
#   ▒   somebody else's, this week only
#   ║   somebody else's, every week (a standing booking)
#   █   yours
#   *   running right now
#
# All six are single-width and never collide with the day/slot labels. ASCII
# and block characters only: emoji break monospace alignment inside a Discord
# code block, and ANSI colour renders as garbage on clients that don't support it.
CELL_FREE = "·"  # U+00B7
CELL_TAKEN = "▒"  # U+2592 — somebody else's, this week only
CELL_TAKEN_EVERY_WEEK = "║"  # U+2551 — somebody else's, every week
CELL_MINE = "█"  # U+2588
# The habit model's guess for the viewer's own usual day. The only cell state
# that isn't a fact; see :func:`cell_state` for precedence and
# :func:`expected_cells` for why it's only ever the viewer's own.
CELL_EXPECTED = "?"  # ASCII
# Live occupancy: the machine is running in this slot right now. Defined and
# tested here, but nothing produces it yet (coordinator wiring is later);
# kept out of :func:`render_legend` until it does.
CELL_RUNNING = "*"  # ASCII

# States as ids rather than characters, so a non-character rendering (a
# Discord button style, a future PNG) asks the same question and gets the
# same answer. One precedence rule lives in :func:`cell_state`.
STATE_FREE = "free"
STATE_EXPECTED = "expected"
STATE_RUNNING = "running"
STATE_TAKEN = "taken"
STATE_TAKEN_EVERY_WEEK = "taken_every_week"
STATE_MINE = "mine"

CELL_STATES = {
    STATE_FREE: CELL_FREE,
    STATE_EXPECTED: CELL_EXPECTED,
    STATE_RUNNING: CELL_RUNNING,
    STATE_TAKEN: CELL_TAKEN,
    STATE_TAKEN_EVERY_WEEK: CELL_TAKEN_EVERY_WEEK,
    STATE_MINE: CELL_MINE,
}

# Every rendered line is exactly this wide. Past ~30 characters a phone wraps
# the block and destroys the column alignment the grid depends on.
GRID_WIDTH = 26
_LABEL_WIDTH = 5  # "Mid"/"Eve" + the gutter before Monday
_CELL_WIDTH = 3  # a 2-char day header + one space


# --- slots and cell keys -----------------------------------------------------
def is_slot(value) -> bool:
    """Whether ``value`` is one of the four slot ids."""
    return value in SLOTS


def is_weekday(value) -> bool:
    """Whether ``value`` is a weekday index, 0 (Monday) - 6 (Sunday)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6


def slot_label(slot) -> str:
    """"AM" / "Mid" / "PM" / "Eve", or "?" for something unrecognised."""
    return SLOT_LABELS.get(slot, "?")


def slot_window_text(slot) -> str:
    """The slot's hours as "06:00-12:00", for a legend outside the grid."""
    window = SLOT_WINDOWS.get(slot)
    if window is None:
        return ""
    start, end = window
    return f"{start:02d}:00-{end % 24:02d}:00"


def slot_for_hour(hour) -> str | None:
    """The slot an hour falls in, or None when no slot covers it (00:00-06:00)."""
    try:
        value = int(hour)
    except (TypeError, ValueError):
        return None
    for slot in SLOTS:
        start, end = SLOT_WINDOWS[slot]
        if start <= value < end:
            return slot
    return None


def cell_key(weekday, slot) -> str | None:
    """The stable key for one cell — ``"3-eve"`` for Thursday evening. A string, not a
    pair, since it's also an object key in the override store and JSON has no
    tuples."""
    if not is_weekday(weekday) or not is_slot(slot):
        return None
    return f"{weekday}-{slot}"


def parse_cell(key) -> tuple[int, str] | None:
    """``"3-eve"`` -> ``(3, "eve")``; None for anything malformed. Round-trips with
    :func:`cell_key`. Never raises, since a corrupt stored key must not take a
    button callback down with it."""
    if not isinstance(key, str) or "-" not in key:
        return None
    day_part, _, slot = key.partition("-")
    try:
        weekday = int(day_part)
    except (TypeError, ValueError):
        return None
    if not is_weekday(weekday) or not is_slot(slot):
        return None
    return (weekday, slot)


def normalise_cell(value) -> str | None:
    """A cell key from either stored form, or None. Accepts the key itself
    (``"3-eve"``) or the ``[weekday, slot]`` pair used for recurring slots."""
    if isinstance(value, str):
        return value if parse_cell(value) else None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        day, slot = value
        try:
            day = int(day)
        except (TypeError, ValueError):
            return None
        return cell_key(day, slot)
    return None


def _cell_order(key: str) -> tuple[int, int]:
    """Sort cells by day then by slot, so stored order never depends on luck."""
    parsed = parse_cell(key)
    if parsed is None:
        return (99, 99)
    weekday, slot = parsed
    return (weekday, SLOTS.index(slot))


# --- the clock, passed in ----------------------------------------------------
def weekday_of(moment) -> int | None:
    """Monday-based weekday index for a ``date``/``datetime``, or None."""
    try:
        return int(moment.weekday())
    except (AttributeError, TypeError, ValueError):
        return None


def iso_week_key(moment) -> str | None:
    """``"2026-W32"`` for a ``date``/``datetime``, or None. ISO year, not calendar
    year: 2027-01-01 is still 2026-W53. Zero-padded so keys sort chronologically as
    plain strings."""
    try:
        calendar = moment.isocalendar()
        year, week = int(calendar[0]), int(calendar[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return f"{year}-W{week:02d}"


# --- stored shapes -----------------------------------------------------------
def normalise_slots(value) -> list[list]:
    """Recurring slots as the data model stores them: ``[[3, "eve"], ...]``. Accepts
    cell keys on the way in too, since the UI works in those. Deduped and ordered so
    two equal weeks always serialise identically."""
    if not isinstance(value, (list, tuple)):
        return []
    keys: list[str] = []
    for item in value:
        key = normalise_cell(item)
        if key is not None and key not in keys:
            keys.append(key)
    keys.sort(key=_cell_order)
    return [[weekday, slot] for weekday, slot in (parse_cell(k) for k in keys)]


def recurring_cells(person) -> list[str]:
    """The cell keys somebody has down every week. Reads ``slots`` defensively rather
    than importing :mod:`people`, so a record that never went through
    ``normalise_person`` can't raise."""
    source = person.get("slots") if isinstance(person, dict) else None
    return [f"{weekday}-{slot}" for weekday, slot in normalise_slots(source)]


def normalise_holders(value) -> list[str]:
    """Who holds a cell, as a deduped list of string ids. A single stored holder still
    loads; the form here is a list since a slot is information, not permission, and
    can't refuse a second person."""
    if value is None:
        return []
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return [str(value)]
    if not isinstance(value, (list, tuple)):
        return []
    holders: list[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        key = str(item)
        if key not in holders:
            holders.append(key)
    return holders


def normalise_overrides(value) -> dict[str, dict[str, list[str]]]:
    """The per-ISO-week override store, cleaned up. ``{"2026-W32": {"3-eve":
    ["123"]}}``. An empty holder list is kept — it's how "not this week" overrides a
    recurring slot (see :func:`toggle_booking`)."""
    if not isinstance(value, dict):
        return {}
    weeks: dict[str, dict[str, list[str]]] = {}
    for week, cells in value.items():
        if not isinstance(week, str) or not isinstance(cells, dict):
            continue
        cleaned: dict[str, list[str]] = {}
        for cell, holders_value in cells.items():
            key = normalise_cell(cell)
            if key is None:
                continue
            cleaned[key] = normalise_holders(holders_value)
        if cleaned:
            weeks[week] = cleaned
    return weeks


def week_overrides(overrides, week) -> dict[str, list[str]]:
    """Just one week's overrides, normalised. ``{}`` when there are none."""
    return normalise_overrides(overrides).get(week, {})


def prune_overrides(overrides, current_week) -> dict[str, dict[str, list[str]]]:
    """Drop weeks that have already happened. Week keys are zero-padded and
    ISO-year-first, so "older than now" is a plain string comparison."""
    weeks = normalise_overrides(overrides)
    if not isinstance(current_week, str) or not current_week:
        return weeks
    return {week: cells for week, cells in weeks.items() if week >= current_week}


# --- reconciliation ----------------------------------------------------------
# Key names inside one cell's entry. Constants, not bare strings, so a typo
# can't silently become an empty answer. Prefer :func:`holders` and
# :func:`recurring_holders` over indexing directly.
OCC_HOLDERS = "holders"
OCC_RECURRING = "recurring"


def effective_week(people, overrides, week) -> dict[str, dict[str, list[str]]]:
    """One week's occupancy, with provenance: recurring slots + this week's overrides.
    ``{cell: {"holders": [ids], "recurring": [ids]}}`` — ``recurring`` is the subset
    of ``holders`` with this as a standing weekly slot. Cells nobody holds are left
    out. An override replaces a cell's whole holder list (rather than adding to it),
    so a cell can be emptied for just this week while the standing slot survives
    into next. Provenance is tracked per holder, not per cell, since an override
    snapshots the whole holder list (:func:`toggle_booking`) and can still contain a
    standing booking."""
    standing: dict[str, list[str]] = {}
    if isinstance(people, dict):
        # Sorted so holder order in a cell is stable — output feeds a
        # rendered string tests assert character for character.
        for stored_key, record in sorted(people.items(), key=lambda kv: str(kv[0])):
            person_id = str(stored_key)
            for cell in recurring_cells(record):
                holders_list = standing.setdefault(cell, [])
                if person_id not in holders_list:
                    holders_list.append(person_id)
    occupancy = {cell: list(held) for cell, held in standing.items()}
    for cell, held in week_overrides(overrides, week).items():
        if held:
            occupancy[cell] = list(held)
        else:
            occupancy.pop(cell, None)
    return {
        cell: {
            OCC_HOLDERS: list(held),
            # Order follows `held` so the two lists read in step.
            OCC_RECURRING: [
                person_id
                for person_id in held
                if person_id in standing.get(cell, ())
            ],
        }
        for cell, held in occupancy.items()
        if held
    }


def _cell_entry(occupancy, cell) -> tuple[list[str], list[str]]:
    """``(holders, recurring)`` for one cell, from either occupancy shape. The bare
    ``{cell: [ids]}`` form is still accepted and reports no recurring holders,
    degrading to "this week only" rather than raising."""
    if not isinstance(occupancy, dict):
        return ([], [])
    value = occupancy.get(cell)
    if isinstance(value, dict):
        held = normalise_holders(value.get(OCC_HOLDERS))
        return (
            held,
            [
                person_id
                for person_id in normalise_holders(value.get(OCC_RECURRING))
                if person_id in held
            ],
        )
    return (normalise_holders(value), [])


def holders(occupancy, cell) -> list[str]:
    """Who holds one cell, as string ids. Never the stored list itself."""
    return _cell_entry(occupancy, cell)[0]


def recurring_holders(occupancy, cell) -> list[str]:
    """Which of a cell's holders have it **every week**, as string ids. Always a subset
    of :func:`holders`; empty if held only this week or if the mapping carries no
    provenance."""
    return _cell_entry(occupancy, cell)[1]


def is_taken(occupancy, cell) -> bool:
    """Whether anybody at all has this cell."""
    return bool(holders(occupancy, cell))


def is_mine(occupancy, cell, viewer_id) -> bool:
    """Whether this cell is one of the viewer's own."""
    if viewer_id is None:
        return False
    return str(viewer_id) in holders(occupancy, cell)


def is_recurring_for_me(occupancy, cell, viewer_id) -> bool:
    """Whether the viewer holds this cell **every week**. No glyph of its own — ``█``
    already says "yours" — this goes in the "Yours this week" text instead
    (:func:`describe_cells`)."""
    if viewer_id is None:
        return False
    return str(viewer_id) in recurring_holders(occupancy, cell)


def is_taken_by_other(occupancy, cell, viewer_id) -> bool:
    """Whether somebody *else* has this cell — can the viewer still take it."""
    held = holders(occupancy, cell)
    if not held:
        return False
    if viewer_id is None:
        return True
    return any(person_id != str(viewer_id) for person_id in held)


def is_recurring_for_other(occupancy, cell, viewer_id) -> bool:
    """Whether somebody *else* has this cell **every week**. What ``║`` draws. A fact
    about the cell only — no name, no count, same as ``▒``."""
    standing = recurring_holders(occupancy, cell)
    if not standing:
        return False
    if viewer_id is None:
        return True
    return any(person_id != str(viewer_id) for person_id in standing)


def toggle_holder(held, user_id) -> tuple[list[str], bool]:
    """Add or remove one person from a holder list. Returns ``(new_holders, booked)``.
    The list is rebuilt rather than mutated, so a store write that fails leaves the
    in-memory week alone."""
    person_id = str(user_id)
    current = normalise_holders(held)
    if person_id in current:
        return ([h for h in current if h != person_id], False)
    return ([*current, person_id], True)


def toggle_recurring(slots, cell) -> tuple[list[list], bool]:
    """Promote one cell to a standing weekly slot, or demote it back. Returns
    ``(new_slots, standing)``; never mutates the list given. Does not touch this
    week's overrides — a standing slot is "my usual", an override is "this week
    specifically" — :func:`effective_week` layers one over the other."""
    key = normalise_cell(cell)
    current = normalise_slots(slots)
    if key is None:
        return (current, False)
    kept = [pair for pair in current if cell_key(*pair) != key]
    if len(kept) != len(current):
        return (kept, False)
    return (normalise_slots([*current, key]), True)


def toggle_booking(people, overrides, week, cell, user_id):
    """Book or free one cell for one person, for one ISO week. Returns
    ``(new_overrides, booked)``. Always an override, never a recurring slot;
    snapshots the cell's whole holder list, so it won't be reached by a later change
    to someone's standing slots."""
    key = normalise_cell(cell)
    updated = normalise_overrides(overrides)
    if key is None or not isinstance(week, str) or not week:
        return (updated, False)
    occupancy = effective_week(people, overrides, week)
    held, booked = toggle_holder(holders(occupancy, key), user_id)
    cells = dict(updated.get(week, {}))
    cells[key] = held
    updated[week] = cells
    return (updated, booked)


# --- rendering ---------------------------------------------------------------
def expected_cells(expected, viewer_id=None) -> list[str]:
    """The cells that may render as ``?`` for this viewer — normalised, ordered. Empty
    whenever there is no viewer: a guess is never shown to the shared board, only
    ever to the one person it's about. This module does no arithmetic; the caller
    supplies the cells (:func:`habit.predicted_cells`)."""
    if viewer_id is None or not isinstance(expected, (list, tuple, set, frozenset)):
        return []
    keys: list[str] = []
    for item in expected:
        key = normalise_cell(item)
        if key is not None and key not in keys:
            keys.append(key)
    keys.sort(key=_cell_order)
    return keys


def running_cells(running) -> list[str]:
    """The cells the machine is running in right now, normalised and ordered.

    Derived by the assistant from the live session window. No viewer gate,
    unlike :func:`expected_cells`: a running washer is not private information.
    """
    if not isinstance(running, (list, tuple, set, frozenset)):
        return []
    keys: list[str] = []
    for item in running:
        key = normalise_cell(item)
        if key is not None and key not in keys:
            keys.append(key)
    keys.sort(key=_cell_order)
    return keys


# The most cells one live load may black out. Past this the input is a stuck
# session, not a load — a wedged tracker must not paint ``*`` across days of
# everybody's grid. Clamped rather than dropped, so a real long load still
# shows its first few slots.
MAX_RUNNING_CELLS = 4


def cells_between(start, end) -> list[str]:
    """The cells a load running from ``start`` to ``end`` actually occupies. Local
    ``datetime``s; caller owns the clock. Chronological order, capped at
    :data:`MAX_RUNNING_CELLS`, half-open at the end. Hours in no slot (00:00-06:00)
    contribute nothing. A missing/unparseable end yields just the starting cell, not
    a guessed duration."""
    first = _cell_at(start)
    # A slotless start (00:00-06:00) doesn't end the scan; it may still run
    # into a real slot later.
    cells = [first] if first is not None else []
    try:
        span = (end - start).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return cells
    if span <= 0:
        return cells
    # Hourly steps are simpler than stepping by the shortest slot (4h) and
    # cost at most ~24 iterations before the cap.
    hours = min(int(span // 3600) + 1, 24 * 7)
    for offset in range(1, hours + 1):
        try:
            moment = start + _hours(offset)
        except (OverflowError, TypeError, ValueError):
            break
        if (moment - start).total_seconds() >= span:
            break
        key = _cell_at(moment)
        if key is not None and key not in cells:
            cells.append(key)
            if len(cells) >= MAX_RUNNING_CELLS:
                break
    return cells


def _hours(count: int):
    """``timedelta(hours=count)``, imported lazily to keep the module light."""
    from datetime import timedelta

    return timedelta(hours=count)


def _cell_at(moment) -> str | None:
    """The cell a moment falls in, or None for the 00:00-06:00 dead hours."""
    weekday = weekday_of(moment)
    if weekday is None:
        return None
    try:
        slot = slot_for_hour(moment.hour)
    except (AttributeError, TypeError, ValueError):
        return None
    if slot is None:
        return None
    return cell_key(weekday, slot)


def days_ahead(cell, today, hour=None) -> int:
    """How many days until this cell next comes round. 0 is today, 7 is a week.
    Modular, since the grid repeats weekly. With ``hour``, a slot whose window has
    already ended today counts as next week's rather than today's."""
    parsed = parse_cell(cell)
    if parsed is None or not is_weekday(today):
        return 99
    weekday, slot = parsed
    delta = (weekday - int(today)) % 7
    if delta != 0 or hour is None:
        return delta
    try:
        current = int(hour)
    except (TypeError, ValueError):
        return delta
    return 7 if SLOT_WINDOWS[slot][1] <= current else 0


def cells_soonest_first(cells, today, hour=None) -> list[str]:
    """Cell keys ordered by how soon each comes round, then by time of day. With
    ``today=None``, plain Monday-first order."""
    keys = [key for key in (normalise_cell(c) for c in cells or ()) if key]
    if not is_weekday(today):
        keys.sort(key=_cell_order)
        return keys
    keys.sort(key=lambda k: (days_ahead(k, today, hour), _cell_order(k)[1]))
    return keys


def cell_state(occupancy, cell, viewer_id=None, expected=None, running=None) -> str:
    """Which of the six :data:`CELL_STATES` this cell is in, for this viewer. The
    single precedence rule in the module; :func:`cell_char` and the grid's button
    styling look it up rather than re-deriving it. ``viewer_id=None`` is the shared
    board: no "yours", no "expected" state. Highest wins: ``█`` yours, ``║`` other's
    every week, ``▒`` other's this week, ``*`` running, ``?`` your own guess, ``·``
    free. A booking always beats a guess — the grid must show real contention, not a
    prediction."""
    held = holders(occupancy, cell)
    if held:
        if viewer_id is not None and str(viewer_id) in held:
            return STATE_MINE
        if is_recurring_for_other(occupancy, cell, viewer_id):
            return STATE_TAKEN_EVERY_WEEK
        return STATE_TAKEN
    if cell in running_cells(running):
        return STATE_RUNNING
    if cell in expected_cells(expected, viewer_id):
        return STATE_EXPECTED
    return STATE_FREE


def cell_char(occupancy, cell, viewer_id=None, expected=None, running=None) -> str:
    """The one character this cell renders as, for this viewer. A lookup of
    :data:`CELL_STATES` on :func:`cell_state`, which owns the precedence rule."""
    return CELL_STATES[cell_state(occupancy, cell, viewer_id, expected, running)]


def render_grid(
    occupancy, viewer_id=None, expected=None, running=None, today=None
) -> str:
    """The week, as a monospace block. Deterministic for a given input. Renders per
    viewer — your cells are ``█``, everybody else's ``▒``/``║``, no name or count
    either way. ``expected`` draws ``?`` on the viewer's own guessed cells only;
    ``running`` draws ``*`` for cells mid-load; ``today`` marks today's column with
    ``▾``. ASCII and block characters only, every line exactly :data:`GRID_WIDTH`
    (26) chars so it doesn't wrap on a phone. Legend is separate since emoji are
    fine outside the code block."""
    predicted = expected_cells(expected, viewer_id)
    live = running_cells(running)
    lines: list[str] = []
    if is_weekday(today):
        # Over the second letter of the day abbreviation, the column the
        # cells below line up on.
        marker = [" "] * GRID_WIDTH
        marker[_LABEL_WIDTH + 1 + 3 * int(today) + 1] = "▾"
        lines.append("".join(marker))
    lines.append(" " * (_LABEL_WIDTH + 1) + " ".join(DAY_ABBRS))
    for slot in SLOTS:
        row = SLOT_LABELS[slot].ljust(_LABEL_WIDTH)
        for weekday in range(7):
            char = cell_char(
                occupancy, f"{weekday}-{slot}", viewer_id, predicted, live
            )
            row += char.rjust(_CELL_WIDTH)
        lines.append(row)
    return "\n".join(lines)


def render_legend(
    personal: bool = True,
    expected: bool = False,
    standing: bool = False,
    running: bool = False,
) -> str:
    """The key to the grid, for the line under the block. ``personal=False`` drops
    "yours" and "expected": the shared board has no viewer and can't show a
    prediction. ``expected``/``standing`` mean "is this actually on the grid", not
    "does the feature exist". ``running`` is ungated by ``personal`` — the washer
    being on is visible to anyone."""
    parts = [f"{CELL_MINE} yours"] if personal else []
    parts.append(f"{CELL_TAKEN} taken")
    if standing:
        parts.append(f"{CELL_TAKEN_EVERY_WEEK} taken, every week")
    if running:
        parts.append(f"{CELL_RUNNING} running now")
    if personal and expected:
        parts.append(f"{CELL_EXPECTED} expected")
    parts.append(f"{CELL_FREE} free")
    return "  ".join(parts)


class RenderedWeek(NamedTuple):
    """A drawn grid plus what is actually on it. The flags mean nothing has to grep the
    rendered string for a character (fragile if one ever turns up in a label instead
    of a cell)."""

    grid: str
    legend: str
    guessed: bool
    standing: bool
    running: bool


def render_week(
    occupancy, viewer_id=None, expected=None, running=None, today=None
):
    """The grid, its matching legend, and which states are on it. One call, so the
    legend can never describe a different grid than the one beside it."""
    predicted = expected_cells(expected, viewer_id)
    live = running_cells(running)
    states = {
        cell_state(occupancy, f"{weekday}-{slot}", viewer_id, predicted, live)
        for slot in SLOTS
        for weekday in range(7)
    }
    guessed = STATE_EXPECTED in states
    standing = STATE_TAKEN_EVERY_WEEK in states
    live_now = STATE_RUNNING in states
    return RenderedWeek(
        grid=render_grid(occupancy, viewer_id, predicted, live, today),
        legend=render_legend(
            personal=viewer_id is not None,
            expected=guessed,
            standing=standing,
            running=live_now,
        ),
        guessed=guessed,
        standing=standing,
        running=live_now,
    )


def render_windows() -> str:
    """The slot windows on one line, for under the legend."""
    return " · ".join(
        f"{SLOT_LABELS[slot]} {slot_window_text(slot)}" for slot in SLOTS
    )


def describe_cells(occupancy, viewer_id, today=None, hour=None) -> str | None:
    """One person's own cells — "Th Eve (every week) · Su AM", or None. Only ever the
    viewer's own id: names cells, never people. Standing slots get "(every week)" in
    words rather than a seventh grid glyph. Ordered soonest first when given the
    clock; without ``today`` it stays Monday-first."""
    if viewer_id is None:
        return None
    mine = cells_soonest_first(
        [cell for cell in occupancy if is_mine(occupancy, cell, viewer_id)],
        today,
        hour,
    )
    parts = []
    for cell in mine:
        parsed = parse_cell(cell)
        if parsed is None:
            continue
        weekday, slot = parsed
        text = f"{DAY_ABBRS[weekday]} {SLOT_LABELS[slot]}"
        if is_recurring_for_me(occupancy, cell, viewer_id):
            text += " (every week)"
        parts.append(text)
    return " · ".join(parts) if parts else None
