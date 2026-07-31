"""Pure, dependency-free helpers for the week grid.

Kept free of Home Assistant / discord imports so the slot maths, the
recurring-vs-override reconciliation and the exact rendered grid can be
unit-tested without the HA test harness — the same discipline that made
:mod:`detect`, :mod:`queue` and :mod:`people` reliable. :mod:`assistant` owns
the ``Store`` and :mod:`grid` owns every Discord call; this module only decides
what a week looks like, and what it looks like *to one person*.

Four things here are load-bearing and every one of them is easy to get subtly
wrong:

* **The caller passes the clock.** Nothing in here calls ``datetime.now()``.
  The ISO week and the weekday are derived from a moment handed in, exactly as
  :mod:`detect` takes ``now`` — which is what makes "the week rolled over"
  testable by passing a date rather than by waiting for Sunday night.
* **JSON object keys are always strings.** A holder written while the id was
  ``interaction.user.id`` (an int) comes back off disk as ``"123"``, so every
  comparison in here is done on the string form. :mod:`people` and :mod:`queue`
  both hit this; the grid would fail *silently and anonymously*, which is worse
  — you'd just never see your own cell as yours.
* **The grid is anonymous** (design doc P5 / §11). Nothing rendered here can
  identify anybody: not a name, and not a count either, because in a house of
  seven "3 people want Thursday night" is one conversation away from naming
  them. A cell is free, taken, or yours. That is the whole vocabulary.
* **Nothing is mutated.** Every function returns new data, like the other pure
  modules, so a rejected store write can't half-apply.

The rendered grid is deliberately **one function's return value** (§6.5), so a
``render_png`` can be dropped in beside :func:`render_grid` later without the
data layer noticing.
"""

from __future__ import annotations

# --- slots (design doc §6.1) -------------------------------------------------
# At 4-5 hours a cycle, one slot IS one load: a cell is not a time range you
# reserve, it's "I'm doing a wash then". Four of them cover the usable day.
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

# Half-open [start, end) hour windows, local time. 00:00-06:00 belongs to no
# slot on purpose: nobody runs a wash at 4am in a shared house, and inventing a
# fifth slot for it would cost a column of grid width for a row of dots.
SLOT_WINDOWS = {
    SLOT_AM: (6, 12),
    SLOT_MID: (12, 16),
    SLOT_PM: (16, 20),
    SLOT_EVE: (20, 24),
}

# Two letters, because "M T W T F S S" is ambiguous and full names don't fit in
# the 30-character budget below.
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

# The density ramp, ASCII/block characters only. Emoji break monospace
# alignment inside a Discord code block (§6.3) — they are fine in the legend
# *outside* it, which is why the legend is a separate function.
CELL_FREE = "·"
CELL_TAKEN = "▓"
CELL_MINE = "█"
# Reserved for the Phase 4 model guess ("expected"). Nothing produces it yet,
# so it is deliberately absent from :func:`render_legend`: a legend entry for a
# state that can never appear reads as a bug in the renderer.
CELL_EXPECTED = "░"

# Every rendered line is exactly this wide. The cap that matters is ~30
# characters, past which the block wraps on a phone and the alignment — the
# only reason to draw a grid in text at all — is destroyed.
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
    """The slot an hour falls in, or None when no slot covers it.

    The windows are otherwise inert data; this is what gives them a meaning
    that can be asserted. None for 00:00-06:00 is the honest answer — see
    :data:`SLOT_WINDOWS`.
    """
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
    """The stable key for one cell — ``"3-eve"`` for Thursday evening.

    A single string rather than a pair because it is also an object key in the
    override store (design doc §12), and JSON has no tuples.
    """
    if not is_weekday(weekday) or not is_slot(slot):
        return None
    return f"{weekday}-{slot}"


def parse_cell(key) -> tuple[int, str] | None:
    """``"3-eve"`` -> ``(3, "eve")``; None for anything malformed.

    Round-trips with :func:`cell_key`. Never raises: these keys come off disk,
    and a corrupt one must not take a button callback down with it.
    """
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
    """A cell key from either stored form, or None.

    Accepts the key itself (``"3-eve"``) and the ``[weekday, slot]`` pair the
    data model uses for recurring slots, so callers never have to care which
    end of the store a cell came from.
    """
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
    """``"2026-W32"`` for a ``date``/``datetime``, or None.

    The ISO year, **not** the calendar year: 2027-01-01 belongs to 2026-W53,
    and keying it under 2027 would silently move that Friday's plans into a
    week that hasn't happened. Zero-padded so the keys sort chronologically as
    plain strings, which is what lets :func:`prune_overrides` be a comparison.
    """
    try:
        calendar = moment.isocalendar()
        year, week = int(calendar[0]), int(calendar[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return f"{year}-W{week:02d}"


# --- stored shapes -----------------------------------------------------------
def normalise_slots(value) -> list[list]:
    """Recurring slots as the data model stores them: ``[[3, "eve"], ...]``.

    Design doc §12 fixes the stored form as ``[weekday, slot]`` pairs, so that
    is what comes back — but cell keys are accepted on the way in, because the
    UI works in cell keys and a value that round-tripped through one of them
    must not be lost. Deduped and ordered so two equal weeks always serialise
    identically.
    """
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
    """The cell keys somebody has down every week.

    Reads the record's ``slots`` defensively rather than importing
    :mod:`people`: this module has to stay loadable on its own, and a record
    that never went through ``normalise_person`` (straight off disk, or from a
    version that stored something else there) must not raise.
    """
    source = person.get("slots") if isinstance(person, dict) else None
    return [f"{weekday}-{slot}" for weekday, slot in normalise_slots(source)]


def normalise_holders(value) -> list[str]:
    """Who holds a cell, as a deduped list of string ids.

    Design doc §12 sketches one holder per overridden cell (``"3-eve": "123"``)
    and that form still loads, but the stored form here is a **list**. Nothing
    in this design can refuse a second person a slot — it is information, not
    permission (§8) — so a shape that can only remember one of them would drop
    somebody's plan on the floor the moment two people aimed for the same
    Thursday, which is precisely the case the grid exists to make visible.
    """
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
    """The per-ISO-week override store, cleaned up.

    ``{"2026-W32": {"3-eve": ["123"]}}``. Junk weeks and unparseable cells are
    dropped; an empty holder list is **kept**, because that is how "not this
    week" is recorded against a recurring slot (see :func:`toggle_booking`).
    """
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
    """Drop weeks that have already happened.

    Plans are not history — the habit model (Phase 4) learns from actual claims,
    never from what somebody wrote down — so a past week is dead weight in a
    store that is rewritten on every tap. Week keys are zero-padded and
    ISO-year-first, so "older than now" is a string comparison.
    """
    weeks = normalise_overrides(overrides)
    if not isinstance(current_week, str) or not current_week:
        return weeks
    return {week: cells for week, cells in weeks.items() if week >= current_week}


# --- reconciliation ----------------------------------------------------------
def effective_week(people, overrides, week) -> dict[str, list[str]]:
    """One week's occupancy: ``{cell: [holder ids]}``, recurring + overrides.

    Two layers, and the order between them is the whole point:

    1. **Recurring** — everybody's standing slots, the ones the Sunday check-in
       (§10.2) maintains.
    2. **The week's overrides** — a one-off, which *replaces* the cell rather
       than adding to it. Replacing is what lets a cell be emptied: somebody
       who normally washes Thursday evening but can't this week leaves an empty
       list behind, and their standing slot survives into next week untouched.

    Cells nobody holds are left out entirely, so an empty week is an empty dict
    and the renderer's default is "free".
    """
    occupancy: dict[str, list[str]] = {}
    if isinstance(people, dict):
        # Sorted so the holder order in a cell is the same on every run — the
        # output feeds a rendered string that tests assert character for
        # character.
        for stored_key, record in sorted(people.items(), key=lambda kv: str(kv[0])):
            person_id = str(stored_key)
            for cell in recurring_cells(record):
                holders_list = occupancy.setdefault(cell, [])
                if person_id not in holders_list:
                    holders_list.append(person_id)
    for cell, held in week_overrides(overrides, week).items():
        if held:
            occupancy[cell] = list(held)
        else:
            occupancy.pop(cell, None)
    return {cell: list(held) for cell, held in occupancy.items() if held}


def holders(occupancy, cell) -> list[str]:
    """Who holds one cell, as string ids. Never the stored list itself."""
    if not isinstance(occupancy, dict):
        return []
    return normalise_holders(occupancy.get(cell))


def is_taken(occupancy, cell) -> bool:
    """Whether anybody at all has this cell."""
    return bool(holders(occupancy, cell))


def is_mine(occupancy, cell, viewer_id) -> bool:
    """Whether this cell is one of the viewer's own."""
    if viewer_id is None:
        return False
    return str(viewer_id) in holders(occupancy, cell)


def is_taken_by_other(occupancy, cell, viewer_id) -> bool:
    """Whether somebody *else* has this cell.

    The question the UI actually asks — "can I still take this, and if not is
    that because I already did?" — and the one place a wrong id comparison
    would show up as the grid quietly disowning your own bookings.
    """
    held = holders(occupancy, cell)
    if not held:
        return False
    if viewer_id is None:
        return True
    return any(person_id != str(viewer_id) for person_id in held)


def toggle_holder(held, user_id) -> tuple[list[str], bool]:
    """Add or remove one person from a holder list.

    Returns ``(new_holders, booked)``. The list is rebuilt rather than mutated,
    so a store write that fails leaves the in-memory week alone.
    """
    person_id = str(user_id)
    current = normalise_holders(held)
    if person_id in current:
        return ([h for h in current if h != person_id], False)
    return ([*current, person_id], True)


def toggle_booking(people, overrides, week, cell, user_id):
    """Book or free one cell for one person, for one ISO week.

    Returns ``(new_overrides, booked)``. The write is always an **override**,
    never a change to a recurring slot: a tap on the grid means "this week",
    which is the only thing somebody can actually know on a Tuesday. Recurring
    slots are the habit model's business (§10.2, Phase 4).

    The override records the cell's *whole* holder list, snapshotted at the
    moment of the tap. That is what makes an empty list meaningful — "nobody
    this week", including whoever normally recurs here — and it is why touching
    a cell pins it for that week: a later change to somebody's standing slots
    won't reach back into a week that has already been edited by hand. Pinning
    the cell somebody deliberately edited is the right way round.
    """
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
def cell_char(occupancy, cell, viewer_id=None) -> str:
    """The one character this cell renders as, for this viewer.

    ``viewer_id=None`` is the shared board: no viewer, so no "yours" state, so
    the same string for everybody. That is the *only* difference between the
    private grid and the pinned one, which is what keeps them one renderer.
    """
    held = holders(occupancy, cell)
    if not held:
        return CELL_FREE
    if viewer_id is not None and str(viewer_id) in held:
        return CELL_MINE
    return CELL_TAKEN


def render_grid(occupancy, viewer_id=None) -> str:
    """The week, as a monospace block. Deterministic for a given input.

    Renders **per viewer** — your cells are ``█``, everybody else's are ``▓``,
    and that is all anyone learns. No name, and no count: "taken" is taken,
    whether one person wants Thursday night or four (design doc P5 / §11).
    Rendering differently for each viewer is exactly why the interactive grid
    has to be ephemeral — one shared message can only have one rendering.

    ASCII and block characters only, and every line exactly
    :data:`GRID_WIDTH` (26) characters, comfortably inside the ~30 a phone
    shows before it wraps. The legend lives in :func:`render_legend` because it
    belongs *outside* the code block, where emoji would be legal.
    """
    lines = [" " * (_LABEL_WIDTH + 1) + " ".join(DAY_ABBRS)]
    for slot in SLOTS:
        row = SLOT_LABELS[slot].ljust(_LABEL_WIDTH)
        for weekday in range(7):
            char = cell_char(occupancy, f"{weekday}-{slot}", viewer_id)
            row += char.rjust(_CELL_WIDTH)
        lines.append(row)
    return "\n".join(lines)


def render_legend(personal: bool = True) -> str:
    """The key to the grid, for the line under the block.

    ``personal=False`` drops "yours", because a shared message has no single
    viewer and can never contain that state. ``░ expected`` is missing from
    both on purpose — nothing produces it until the habit model lands, and a
    legend entry for a state you can never see is just confusing.
    """
    if personal:
        return f"{CELL_MINE} yours  {CELL_TAKEN} taken  {CELL_FREE} free"
    return f"{CELL_TAKEN} taken  {CELL_FREE} free"


def render_windows() -> str:
    """The slot windows on one line, for under the legend."""
    return " · ".join(
        f"{SLOT_LABELS[slot]} {slot_window_text(slot)}" for slot in SLOTS
    )


def describe_cells(occupancy, viewer_id) -> str | None:
    """One person's own cells this week — "Thu Eve · Sun AM", or None.

    Only ever called with the viewer's own id, and only ever rendered back to
    that same person: it names *cells*, never people, so it cannot leak. None
    when they have nothing down, so the caller can drop the line entirely.
    """
    if viewer_id is None:
        return None
    mine = sorted(
        (cell for cell in occupancy if is_mine(occupancy, cell, viewer_id)),
        key=_cell_order,
    )
    parts = []
    for cell in mine:
        parsed = parse_cell(cell)
        if parsed is None:
            continue
        weekday, slot = parsed
        parts.append(f"{DAY_ABBRS[weekday]} {SLOT_LABELS[slot]}")
    return " · ".join(parts) if parts else None
