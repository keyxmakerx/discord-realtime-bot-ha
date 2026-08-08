"""Pure, dependency-free helpers for the week grid.

Kept free of Home Assistant / discord imports so the slot maths, the
recurring-vs-override reconciliation and the exact rendered grid can be
unit-tested without the HA test harness — the same discipline that made
:mod:`detect`, :mod:`queue` and :mod:`people` reliable. :mod:`assistant` owns
the ``Store`` and :mod:`grid` owns every Discord call; this module only decides
what a week looks like, and what it looks like *to one person*.

Five things here are load-bearing and every one of them is easy to get subtly
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
  them. A cell is free, taken (this week, or every week), yours, running, or —
  only ever for the person looking at it — expected. That is the whole
  vocabulary, and :data:`CELL_STATES` is the whole of it in one place.
* **Provenance survives reconciliation.** :func:`effective_week` returns, for
  every held cell, *both* who holds it and which of those hold it as a standing
  weekly slot. Flattening the two into one list — which is what it used to do —
  makes a standing Thursday and a one-off tap byte-identical downstream, and
  ``║`` is then impossible to draw at all.
* **Nothing is mutated.** Every function returns new data, like the other pure
  modules, so a rejected store write can't half-apply.

The rendered grid is deliberately **one function's return value** (§6.5), so a
``render_png`` can be dropped in beside :func:`render_grid` later without the
data layer noticing. :func:`render_week` bundles that string with the flags a
caller needs to describe it — which states are *actually on the block* — so
nobody has to ask the rendered text what is in it.
"""

from __future__ import annotations

from typing import NamedTuple

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

# --- the cell alphabet -------------------------------------------------------
# **Shape encodes KIND. Weight encodes WHOSE.** (live-use design §5.)
#
# This started life as ``· ░ ▓ █`` — a dot plus three steps of one shading ramp,
# faint to solid. A density ramp encodes *magnitude*, and none of these are
# magnitudes: a guess, somebody else's booking and your own booking are
# different **kinds** of thing, and "how dark is this square?" answers a
# question nobody was asking. That mismatch is why the grid could not be read at
# a glance, and it had nowhere to put a fifth state — the ramp was already at
# full black.
#
# So the ramp is gone. Exactly two block weights survive, and between them they
# now mean one thing and one thing only — ``▒`` somebody else, ``█`` you. Every
# other state is a different *shape* rather than a different darkness:
#
#   ·   free
#   ?   the model's guess — the viewer's own, never on a shared view
#   ▒   somebody else's, this week only
#   ║   somebody else's, every week (a standing booking)
#   █   yours
#   *   running right now
#
# **Width.** All six are single-width. ``▒`` (U+2592) and ``║`` (U+2551) share
# an East Asian Width class with ``·``/``█``, which already render correctly on
# the household's clients, so they behave identically; ``?`` and ``*`` are
# narrow ASCII. None of them collide with the day (``Mo Tu ...``) or slot
# (``AM Mid PM Eve``) labels.
#
# **ASCII and block characters only.** Emoji break monospace alignment inside a
# Discord code block (§6.3) — they are fine in the legend *outside* it, which is
# why the legend is a separate function. ANSI colour was considered and rejected:
# clients that don't support it render the escape sequences as visible garbage,
# and seven people means mixed devices.
CELL_FREE = "·"  # U+00B7
CELL_TAKEN = "▒"  # U+2592 — somebody else's, this week only
CELL_TAKEN_EVERY_WEEK = "║"  # U+2551 — somebody else's, every week
CELL_MINE = "█"  # U+2588
# The habit model's guess: the days :mod:`habit` thinks *the person looking*
# usually washes. It is the only cell state that is not a fact — see
# :func:`cell_state` for the precedence rule that keeps it off every real
# booking, and :func:`expected_cells` for why it can only ever be the viewer's
# own. It stays out of :func:`render_legend` unless a guess is actually in play:
# a legend entry for a state that cannot appear reads as a renderer bug.
CELL_EXPECTED = "?"  # ASCII
# Live occupancy: the machine is running in this slot *right now*. Defined,
# rendered and tested here, but **nothing produces it yet** — the coordinator
# wiring is Phase 4. It is deliberately kept out of :func:`render_legend` until
# then, exactly the discipline ``?`` got when it shipped a phase ahead of the
# habit model: a legend that promises a character the grid cannot draw is
# indistinguishable from a renderer that has stopped drawing it.
CELL_RUNNING = "*"  # ASCII

# The states a cell can be in, as ids rather than characters, so that anything
# choosing a *non-character* rendering for a cell — a Discord button style, a
# future PNG — asks the same question the grid does and gets the same answer.
# One precedence rule, in :func:`cell_state`, and everything else is a lookup.
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
# The key names inside one cell's entry. Constants rather than bare strings
# because a typo in a dict lookup is not an error, it is an empty answer — a
# cell that quietly reports nobody on it. Consumers should reach for
# :func:`holders` and :func:`recurring_holders` instead of indexing; these exist
# for the tests and for anybody building the mapping by hand.
OCC_HOLDERS = "holders"
OCC_RECURRING = "recurring"


def effective_week(people, overrides, week) -> dict[str, dict[str, list[str]]]:
    """One week's occupancy, with **provenance**, recurring + overrides.

    ``{cell: {"holders": [ids], "recurring": [ids]}}``, where ``recurring`` is
    the subset of ``holders`` who hold that cell as a *standing weekly slot*
    rather than as a one-off for this week alone. Cells nobody holds are left
    out entirely, so an empty week is an empty dict and the renderer's default
    is "free".

    Two layers, and the order between them is the whole point:

    1. **Recurring** — everybody's standing slots, the ones the Sunday check-in
       (§10.2) maintains.
    2. **The week's overrides** — a one-off, which *replaces* the cell rather
       than adding to it. Replacing is what lets a cell be emptied: somebody
       who normally washes Thursday evening but can't this week leaves an empty
       list behind, and their standing slot survives into next week untouched.

    **Why the shape changed.** It used to return ``{cell: [ids]}``, and because
    an override replaces the whole list, a standing Thursday and a tap made two
    minutes ago came out byte-identical. ``║`` — "somebody has this *every*
    week", which is the single fact that decides whether asking for a trade is
    worth the message — was not merely unrendered, it was unrecoverable.

    **Why per holder rather than per cell.** "Did this cell come from an
    override?" is the cheaper question and it is the wrong one. Overrides
    snapshot the cell's *whole* holder list (see :func:`toggle_booking`), so the
    moment a second person taps a cell somebody stands on every week, that
    standing booking is inside an override too. A per-cell source flag would
    quietly demote it to a one-off and tell the house a slot is easy to move
    when it is the least movable one on the grid. Asking of each holder "is this
    cell in *their* standing slots?" is both correct and no more work.
    """
    standing: dict[str, list[str]] = {}
    if isinstance(people, dict):
        # Sorted so the holder order in a cell is the same on every run — the
        # output feeds a rendered string that tests assert character for
        # character.
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
            # Order follows ``held`` rather than ``standing`` so the two lists
            # read in step, and the membership test is against the *person's*
            # slots, so an override that happens to contain a standing holder
            # keeps their cadence.
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
    """``(holders, recurring)`` for one cell, from either occupancy shape.

    The bare ``{cell: [ids]}`` form is still accepted — a week that came
    straight out of an override store, or a literal in a test — and reports no
    recurring holders, which is the honest answer for a mapping that never knew
    anybody's standing slots. :func:`effective_week`'s own output is the shape
    with provenance in it; everything else degrades to "this week only" rather
    than raising in a button callback.
    """
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
    """Which of a cell's holders have it **every week**, as string ids.

    Always a subset of :func:`holders`. Empty for a cell held only this week,
    and empty for an occupancy mapping that carries no provenance at all.
    """
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
    """Whether the viewer holds this cell **every week**.

    The viewer's own cadence deliberately gets no glyph of its own — a seventh
    character would be a third thing to learn for information that is only ever
    about one person, and ``█`` already says "yours". It goes in the "Yours this
    week" text instead (:func:`describe_cells`), which is where somebody looks
    to check what they have actually committed to.
    """
    if viewer_id is None:
        return False
    return str(viewer_id) in recurring_holders(occupancy, cell)


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


def is_recurring_for_other(occupancy, cell, viewer_id) -> bool:
    """Whether somebody *else* has this cell **every week**.

    What ``║`` draws, and it is shown for other people precisely because it
    changes what you'd do about it: a standing commitment is much less likely to
    move than a one-off, so it is the difference between "worth asking for a
    swap" and "pick another evening". It leaks nothing a booking doesn't
    already — it is a fact about a *cell*, with no name and no count attached,
    the same vocabulary as ``▒``.
    """
    standing = recurring_holders(occupancy, cell)
    if not standing:
        return False
    if viewer_id is None:
        return True
    return any(person_id != str(viewer_id) for person_id in standing)


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


def toggle_recurring(slots, cell) -> tuple[list[list], bool]:
    """Promote one cell to a standing weekly slot, or demote it back.

    Returns ``(new_slots, standing)`` in the stored ``[[weekday, slot], ...]``
    form, never mutating the list it was given — the caller writes the result
    back onto ``person["slots"]``, so a rejected store write cannot half-apply.

    This is the writer the data model has been missing. ``person["slots"]`` was
    defaulted, normalised, read by :func:`recurring_cells` and reconciled by
    :func:`effective_week` since the planner shipped; nothing could ever *set*
    it, so "every week" was a shape the store understood and no button could
    produce.

    **It deliberately does not touch this week's overrides.** The two layers
    answer different questions — a standing slot is "this is my usual", an
    override is "here is what I am doing in the week of the 3rd" — and
    :func:`effective_week` already lays the second over the first. Writing both
    from one tap would make "every week" mean "every week except when somebody
    happened to edit that week", which is the bug the two-layer model exists to
    avoid. The caller books the current week separately if it wants to, and the
    grid does exactly that: promoting a cell you already hold leaves this week
    alone, because you already hold it.
    """
    key = normalise_cell(cell)
    current = normalise_slots(slots)
    if key is None:
        return (current, False)
    kept = [pair for pair in current if cell_key(*pair) != key]
    if len(kept) != len(current):
        return (kept, False)
    return (normalise_slots([*current, key]), True)


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
def expected_cells(expected, viewer_id=None) -> list[str]:
    """The cells that may render as ``?`` for this viewer — normalised, ordered.

    **Empty whenever there is no viewer**, and that is the important line in
    this module. A prediction is a statement about one person's habits, and
    §11's whole point is that nothing about one person is ever shown to the
    house: a booking at least represents something they chose to publish, while
    a guess is the bot telling six other people what it thinks somebody's week
    looks like. Leaking it is strictly worse than leaking a booking.

    Making the viewer-less case return ``[]`` here — rather than trusting every
    call site to pass nothing for the shared board — is what makes that
    structural. :func:`render_grid` with no ``viewer_id`` cannot produce a ``?``
    no matter what it is handed, so the anonymous board has no path to one.

    The caller supplies the cells (:func:`habit.predicted_cells` in practice);
    this module has no history and does no arithmetic. Junk is dropped, dupes
    collapse and the order is fixed, exactly as :func:`normalise_slots` does.
    """
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
    """The cells the machine is actually running in — normalised, ordered.

    **No viewer gate**, unlike :func:`expected_cells`, and the asymmetry is the
    point: "the washer is on" is a fact about the machine that anybody standing
    in the utility room can see, so it is not somebody's private information and
    the anonymous board may show it. A guess is the opposite on both counts.

    Nothing produces this yet — live occupancy is Phase 4, derived from
    coordinator state on each render and never stored, so that it vanishes the
    moment the load ends. It is accepted here now so the renderer is already the
    one that will draw it.
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


# The most cells one live load may black out. A wash is one slot; a wash then a
# dry, started late and crossing midnight, is a plausible three. Past that the
# input is not a load, it is a stuck session — and the failure mode matters: a
# wedged tracker would paint ``*`` across days of everybody's grid, which is
# both wrong and unfalsifiable from the outside, because the one thing ``*``
# claims is that the machine is busy *right now*. Phase 1 fixed the 12-hour hang
# that made this likely; the cap is what stops the next such bug reaching the
# display at all. Clamped rather than dropped: a load that really is running
# should still show its first few slots.
MAX_RUNNING_CELLS = 4


def cells_between(start, end) -> list[str]:
    """The cells a load running from ``start`` to ``end`` actually occupies.

    Both are ``datetime``s in local time, and the caller owns the clock as
    everywhere else in this module. Returns cell keys in chronological order,
    capped at :data:`MAX_RUNNING_CELLS`.

    Half-open at the end, matching :data:`SLOT_WINDOWS`: a load finishing at
    exactly 16:00 occupied PM for no time at all and does not light it up.
    Hours that fall in no slot (00:00-06:00) contribute nothing, so an overnight
    dry lights Eve and then the next morning's AM with the dead hours simply
    absent — which is what the machine was actually doing.

    A missing or unparseable end (no ETA yet) yields **just the starting cell**
    rather than nothing: "the washer is on now" is the fact worth drawing, and
    guessing how long it will run is exactly the guess this glyph must not make.
    """
    first = _cell_at(start)
    if first is None:
        return []
    cells = [first]
    try:
        span = (end - start).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return cells
    if span <= 0:
        return cells
    # Step by the shortest slot (4h) would skip nothing, but stepping hourly is
    # simpler to reason about and costs at most ~24 iterations before the cap.
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

    The grid repeats weekly, so every cell is always "coming up" — the only
    question is how soon. A cell earlier in the week than today is next week's,
    which is why this is modular rather than a subtraction.

    ``hour`` closes the last gap: today's AM slot at 21:00 has already gone, and
    calling it "today" would sort a slot nobody can still use above tomorrow's.
    Given the hour, a slot whose window has already **ended** today is treated as
    next week's — honest for a weekly repeating plan, and it is what stops
    "Yours this week" opening with something that already happened.
    """
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
    """Cell keys ordered by how soon each comes round, then by time of day.

    With ``today=None`` this is plain Monday-first order, which is what the
    stored form and every test that predates a clock expect.
    """
    keys = [key for key in (normalise_cell(c) for c in cells or ()) if key]
    if not is_weekday(today):
        keys.sort(key=_cell_order)
        return keys
    keys.sort(key=lambda k: (days_ahead(k, today, hour), _cell_order(k)[1]))
    return keys


def cell_state(occupancy, cell, viewer_id=None, expected=None, running=None) -> str:
    """Which of the six :data:`CELL_STATES` this cell is in, for this viewer.

    The single precedence rule in the module. :func:`cell_char` is a lookup on
    top of it, and so is the grid's button styling in :mod:`assistant` — one
    question, one answer, and no second implementation to drift.

    ``viewer_id=None`` is the shared board: no viewer, so no "yours" state and
    no "expected" state, so the same string for everybody. That is the *only*
    difference between the private grid and the pinned one, which is what keeps
    them one renderer.

    **Highest wins, in this order:**

    1. ``█`` **yours.** What you came to check.
    2. ``║`` **somebody else's, every week.** Above the one-off because it is
       the more consequential of the two: a standing commitment is the one you
       probably shouldn't plan around moving.
    3. ``▒`` **somebody else's, this week.**
    4. ``*`` **running right now.** Below all three bookings on purpose, and
       this is the ordering worth arguing. Live occupancy is derived, ephemeral
       and about the *machine*; a booking is a stated intention about the
       *week*, it outlives the load, and it is the thing you can act on — you
       can ask somebody to swap a booked slot, and there is nothing to ask of a
       drum that is spinning. A cell that is both is better described by the
       claim than by the noise, so the claim is what it draws.
    5. ``?`` **the model's guess** — the viewer's own, never anybody else's.
    6. ``·`` **free.**

    **A real booking always beats a guess**, which follows from 1-3 sitting
    above 5, and it is not a cosmetic choice. A booking is something a person
    actually said; a prediction is the bot's arithmetic about somebody's past.
    If a guess could hide a booking, the grid would answer "is Thursday evening
    spoken for?" with the bot's opinion instead of the house's plans — and the
    one job this display has is making real contention visible (§8). Being wrong
    the other way costs nothing: a predicted cell that somebody then books
    simply stops being a guess and starts being a fact. Putting ``*`` above
    ``?`` extends the same rule — a guess never covers anything real.
    """
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
    """The one character this cell renders as, for this viewer.

    A lookup of :data:`CELL_STATES` on :func:`cell_state`, which owns the
    precedence rule and the argument for it.
    """
    return CELL_STATES[cell_state(occupancy, cell, viewer_id, expected, running)]


def render_grid(
    occupancy, viewer_id=None, expected=None, running=None, today=None
) -> str:
    """The week, as a monospace block. Deterministic for a given input.

    Renders **per viewer** — your cells are ``█``, everybody else's are ``▒`` or
    ``║``, and that is all anyone learns. No name, and no count: "taken" is
    taken, whether one person wants Thursday night or four (design doc P5 /
    §11). Rendering differently for each viewer is exactly why the interactive
    grid has to be ephemeral — one shared message can only have one rendering.

    ``expected`` is the habit model's guess at **this viewer's own** usual days
    (design doc §7), drawn as ``?`` on cells that are otherwise free. Passing
    somebody else's cells here would be a §11 leak, so the guard is in
    :func:`expected_cells` rather than in a comment: with no ``viewer_id`` there
    is no prediction, full stop.

    ``running`` is live occupancy, drawn as ``*`` — the cells the machine is
    actually mid-load in (:func:`cells_between`).

    ``today`` adds a ``▾`` over today's column, and it is the one thing that
    turns this from a shape into a calendar: without it every column is equally
    far away, and "is that free evening tonight or six days off?" needs counting
    on fingers from a header two lines up. It is a *marker* rather than a
    seventh cell state on purpose — it is a fact about the week, not about any
    cell, so it must not compete with the alphabet for the reader's attention.
    A weekday out of range, or None, simply draws no marker row.

    ASCII and block characters only, and every line exactly
    :data:`GRID_WIDTH` (26) characters, comfortably inside the ~30 a phone
    shows before it wraps. The legend lives in :func:`render_legend` because it
    belongs *outside* the code block, where emoji would be legal.
    """
    predicted = expected_cells(expected, viewer_id)
    live = running_cells(running)
    lines: list[str] = []
    if is_weekday(today):
        # Over the *second* letter of the abbreviation, which is the column the
        # cells below line up on — a marker over the first letter points
        # convincingly at the gap between two days.
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
    """The key to the grid, for the line under the block.

    ``personal=False`` drops "yours", because a shared message has no single
    viewer and can never contain that state — and for the same reason it drops
    ``? expected`` unconditionally, whatever ``expected`` says. The anonymous
    board cannot render a prediction (:func:`expected_cells`), so a legend
    promising one would be describing a state that board can never show. ``║``
    has no such guard: a standing booking is a fact about a cell, so it can and
    does appear on the shared board.

    ``expected`` and ``standing`` are the *caller's* answer to "is this
    character actually on the grid right now", not "does this feature exist":
    with no confident prediction there is no ``?`` on the block, and a legend
    entry for a character that isn't there reads as a bug in the renderer.
    :func:`render_week` works both flags out from the grid it just drew, so no
    caller has to.

    ``running`` earns its entry the same way and is ungated by ``personal``:
    the washer being mid-load is a fact about the machine that anybody in the
    utility room can see, so the anonymous board may say it too. It shipped
    defined-but-unlegended for exactly one release, while nothing produced it.
    """
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
    """A drawn grid plus what is actually on it.

    The flags exist so that nothing has to ask the rendered string what it
    contains. That used to be done by substring — ``CELL_EXPECTED in grid`` —
    which reads as clever and is merely fragile: it silently couples the legend
    and the explainer to the exact character the renderer happens to use this
    month, and it goes wrong the instant a glyph turns up in a day label, a slot
    label or a note. ``?`` would have done exactly that. Asking the renderer is
    strictly better than reading its output back.
    """

    grid: str
    legend: str
    guessed: bool
    standing: bool
    running: bool


def render_week(
    occupancy, viewer_id=None, expected=None, running=None, today=None
):
    """The grid, its matching legend, and which states are on it.

    One call, so the legend can never describe a different grid from the one
    beside it. ``running`` is reported and, once something produces it, legended
    — see :func:`render_legend`.
    """
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
    """One person's own cells — "Th Eve (every week) · Su AM", or None.

    Only ever called with the viewer's own id, and only ever rendered back to
    that same person: it names *cells*, never people, so it cannot leak. None
    when they have nothing down, so the caller can drop the line entirely.

    **This is where the viewer's own cadence lives.** Their standing slots get
    ``(every week)`` in words rather than a seventh glyph on the grid: ``█``
    already says "yours", a character that distinguishes your own standing slot
    from your own one-off would be a third block weight to learn, and the answer
    matters in a different place — when you are reading back what you have
    actually committed to, not when you are scanning for a free evening.

    **Ordered soonest first** when given the clock. Monday-first is the right
    order for a *stored* list and the wrong one for a line somebody reads: on a
    Friday it opened with Monday — a slot four days gone — and buried tonight's
    at the end, so the one entry that could still be acted on was the hardest to
    find. With ``hour`` as well, a slot whose window has already closed today
    sorts round to next week rather than claiming to be today's news. Without
    either it stays Monday-first, which is what the stored form and every test
    that predates a clock expect.
    """
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
