"""Pure, dependency-free habit model — what somebody's own history implies.

Kept free of Home Assistant / discord imports so the confidence gate, the
90-day window and the nudge budget can be unit-tested without the HA test
harness — the same discipline that made :mod:`detect`, :mod:`queue`,
:mod:`people` and :mod:`plan` reliable. :mod:`assistant` owns the ``Store`` and
every Discord call; this module only decides what one person's history means,
and whether they are allowed to be messaged about it.

Six things here are load-bearing, and every one of them is a way this feature
becomes the thing everyone mutes:

* **Silence is the default (design doc P6).** A prediction has to clear three
  independent gates (§7.2). Any one of them failing means no prediction, no
  nudge, nothing rendered — not a hedged guess. Thin data is the normal state
  of this model for the first month and it must be quiet the whole time.
* **The model learns from real claims and explicit corrections only (§7.3),**
  structurally rather than by convention: :func:`record_load` is the only
  function in this module that can add to history, it takes the id and the
  moment of an actual Claim tap, and *nothing* it writes comes from
  :func:`predict`. Sending a nudge touches :func:`record_nudge`, which cannot
  reach history at all. There is no path by which a guess becomes evidence.
* **Nothing here can mention another person (§11).** Every read takes a
  ``user_id`` and filters to that person first, so the counts and the strings
  are self-referential by construction — a history row carries an id and a
  timestamp and has nowhere to put a name. No leaderboards, no household
  aggregates, no "3 people wash on Thursday": in a house of seven that is one
  conversation away from naming them.
* **The nudge budget is arithmetic (P2).** 1 DM a day, 2 a week, and over
  budget means *dropped* — there is deliberately no deferral, no pending list
  and no "send it tomorrow instead", because a queued nudge is just a late
  nudge and the point of the cap is that the phone stays quiet.
* **The caller passes the clock.** This module does not import ``datetime`` and
  never asks what time it is; a moment is handed in, exactly as :mod:`detect`
  takes ``now``. That is what makes "the week rolled over the new year" a test
  rather than a thing you find out in January.
* **Nothing is mutated.** Every function returns new data, so a rejected store
  write can't half-apply.

The moment passed in should be **timezone-aware local time** (HA's
``dt_util.now()``), because a bucket is a household wall-clock fact: a wash at
21:00 is a Thursday evening wash to the people living there whatever UTC
thinks.
"""

from __future__ import annotations

try:  # the normal path — a sibling module inside the integration package
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path, as the tests do
    # There is no package when habit.py is exec'd from a file path, which is
    # how the pure suite runs it. The slot windows, the cell keys and the ISO
    # week live in plan.py and are *not* reimplemented here: two copies of the
    # week maths is exactly how the grid and the model start disagreeing about
    # what Thursday evening means.
    import plan  # type: ignore[no-redef]

SECONDS_PER_DAY = 86400

# --- retention (design doc §12: history is "capped at 90 days") --------------
# Long enough that a fortnightly washer still has 6 observations, short enough
# that a habit from three months ago (different job, different housemates)
# stops voting on this week.
HISTORY_DAYS = 90

# A second, absolute ceiling. The 90-day window only bounds history if the
# timestamps are sane, and a row dated in the *future* — one write from a
# device whose clock hadn't synced yet — can never age out at all. 7 people
# washing daily for 90 days is ~630 rows, so this cannot be reached by ordinary
# use: it exists so "it can never grow without bound" is true of the code
# rather than of the happy path.
HISTORY_MAX = 1000

# Claim can be tapped twice for one load — unclaim then reclaim is a real pair
# of buttons on the live card — and each tap would otherwise be a separate data
# point, inflating a bucket toward a habit nobody has. A cycle is 4-5 hours, so
# two *genuine* loads by the same person inside an hour do not happen.
LOAD_DEDUPE_SECONDS = 3600

# --- the confidence gate (design doc §7.2) ----------------------------------
# All three must pass. They fail for different reasons on purpose: too few
# observations means "I have barely seen you", too small a share means "you
# wash whenever", too little history means "I have not watched you long enough
# to know the difference".
MIN_OBSERVATIONS = 3
MIN_SHARE_PERCENT = 30
MIN_SHARE = MIN_SHARE_PERCENT / 100  # for display; the test is integer maths
MIN_WEEKS = 4
MIN_HISTORY_DAYS = MIN_WEEKS * 7

GATE_OBSERVATIONS = "observations"
GATE_SHARE = "share"
GATE_WEEKS = "weeks"
GATES = (GATE_OBSERVATIONS, GATE_SHARE, GATE_WEEKS)

# --- corrections (design doc §7.3) ------------------------------------------
# "Wrong" is the model being told it is wrong. "Pushed" is ⏭ Push to tomorrow,
# which explicitly does NOT count as the model being wrong — the day was right,
# the person just isn't doing it tonight.
CORRECTION_WRONG = "wrong"
CORRECTION_PUSHED = "pushed"
CORRECTION_KINDS = (CORRECTION_WRONG, CORRECTION_PUSHED)

# --- the nudge budget (design doc P2) ---------------------------------------
MAX_NUDGES_PER_DAY = 1
MAX_NUDGES_PER_WEEK = 2

BUDGET_OK = "ok"
BUDGET_DAY = "day"
BUDGET_WEEK = "week"
BUDGET_UNREADABLE = "unreadable"

# Prose for a sentence, as §7.3 writes it ("I think you wash Thursday
# evenings"). Deliberately not :data:`plan.SLOT_LABELS` — "you wash Thursday
# Eve" reads like a column header, and the labels have to stay short because
# they are drawn inside a 26-character grid. PM and Eve are distinguished
# rather than both being "evenings", so a person can tell which one the bot
# means without being shown the hours.
SLOT_PHRASES = {
    plan.SLOT_AM: "mornings",
    plan.SLOT_MID: "afternoons",
    plan.SLOT_PM: "early evenings",
    plan.SLOT_EVE: "evenings",
}


# --- small defensive readers -------------------------------------------------
def _timestamp(value) -> float | None:
    """A unix timestamp field, or None when missing/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count(value) -> int:
    """A non-negative counter field, or 0 for anything unexpected.

    ``bool`` is rejected rather than counted as 1: ``True`` in a counter is a
    half-written record, not one nudge.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _cell_order(cell) -> tuple[int, int]:
    """Sort cells by day then slot, so ties resolve the same way every run."""
    parsed = plan.parse_cell(cell)
    if parsed is None:
        return (99, 99)
    weekday, slot = parsed
    return (weekday, plan.SLOTS.index(slot))


# --- the clock, passed in ----------------------------------------------------
def _aware(moment) -> bool:
    """Whether this is a moment we are allowed to read at all.

    A *naive* datetime is rejected rather than guessed at. It is the one bad
    input that does not look bad: :func:`moment_ts` would read it in whatever
    timezone the process happens to be in while :func:`bucket_for` reads its
    bare wall-clock fields, so a caller reaching for ``datetime.utcnow()``
    instead of ``dt_util.now()`` writes a *plausible* bucket for the wrong
    evening — and three of those clear the gate. Rejecting here routes it down
    the existing "unusable moment" paths (nothing written, ``BUDGET_UNREADABLE``
    on the nudge side), which is the direction this module is wrong in.
    """
    try:
        return moment.utcoffset() is not None
    except (AttributeError, TypeError, ValueError):
        return False


def moment_ts(moment) -> float | None:
    """Unix timestamp for a moment, or None if it isn't a usable moment.

    A ``date`` has no time of day, so it cannot be a load: it comes back None
    rather than being silently read as midnight, which is in no slot anyway. A
    naive datetime is refused for the reason in :func:`_aware`.
    """
    if not _aware(moment):
        return None
    try:
        return float(moment.timestamp())
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def day_key(moment) -> str | None:
    """The local calendar day as ``"2026-08-03"``, or None.

    The day half of the nudge budget. Derived from the moment the caller hands
    in rather than from a stored timestamp, because turning a unix timestamp
    back into "which day was that for the household" needs a timezone, and the
    moment already carries one.
    """
    if not _aware(moment):
        return None
    try:
        return moment.strftime("%Y-%m-%d")
    except (AttributeError, TypeError, ValueError):
        return None


def bucket_for(moment) -> str | None:
    """The ``(weekday, slot)`` bucket a moment falls in — ``"3-eve"`` — or None.

    This is the whole mapping from "a load happened" to "a cell of the week",
    and it is :mod:`plan`'s slot windows and weekday doing the work so the model
    and the grid can never drift apart. None for 00:00-06:00, which belongs to
    no slot: a 3am wash is real but it is not a habit the grid can show.
    """
    if not _aware(moment):
        return None
    weekday = plan.weekday_of(moment)
    slot = plan.slot_for_hour(getattr(moment, "hour", None))
    if weekday is None or slot is None:
        return None
    return plan.cell_key(weekday, slot)


# --- history records ---------------------------------------------------------
def load_record(user_id, moment) -> dict | None:
    """One history row for a real Claim tap, or None if it can't be used.

    ``{"ts": ..., "user_id": "123", "cell": "3-eve"}``. Two deliberate choices:

    * **The id is stored as a string.** ``interaction.user.id`` is an int and
      HA's ``Store`` is JSON, so it comes back as ``"123"`` — :mod:`queue`,
      :mod:`people` and :mod:`plan` all hit this. Normalising on the way in
      means the histogram cannot end up split across two spellings of one
      human, which would show up as a model that never gets confident.
    * **The bucket is frozen at write time.** §12 sketches ``{ts, user_id}``
      alone, but recovering "which weekday and slot was that, locally?" from a
      bare timestamp needs a timezone at *read* time — and the household's
      timezone is exactly the thing that can change under a stored history. The
      moment is already local and aware here, so the answer is recorded while
      it is still known. ``None`` when the load was in no slot at all.

    There is nowhere in this shape to put a name, which is what makes §11 a
    property of the data rather than a rule to remember.
    """
    ts = moment_ts(moment)
    if ts is None or user_id is None or isinstance(user_id, bool):
        return None
    key = str(user_id)
    if not key:
        return None
    return {"ts": ts, "user_id": key, "cell": bucket_for(moment)}


def normalise_record(record) -> dict | None:
    """A stored history row, cleaned up — or None if it is unusable.

    Rebuilt field by field rather than trusted, because these come off disk:
    a row with no timestamp can never age out, and a row with no id belongs to
    nobody, so both are dropped rather than carried forever.
    """
    if not isinstance(record, dict):
        return None
    ts = _timestamp(record.get("ts"))
    if ts is None:
        return None
    user_id = record.get("user_id")
    if user_id is None or isinstance(user_id, bool):
        return None
    key = str(user_id)
    if not key:
        return None
    # A row written before the bucket was stored simply has no cell: it still
    # counts toward the person's total (they did wash) but votes for no
    # weekday, which biases toward silence. That is the right way to be wrong.
    cell = plan.normalise_cell(record.get("cell"))
    return {"ts": ts, "user_id": key, "cell": cell}


def normalise_history(history) -> list[dict]:
    """Every usable row, oldest first. ``[]`` for a junk store."""
    if not isinstance(history, (list, tuple)):
        return []
    rows = [row for row in (normalise_record(r) for r in history) if row is not None]
    rows.sort(key=lambda row: row["ts"])
    return rows


def _cap(rows: list[dict]) -> list[dict]:
    """Keep the newest :data:`HISTORY_MAX` rows."""
    if HISTORY_MAX > 0 and len(rows) > HISTORY_MAX:
        return rows[-HISTORY_MAX:]
    return rows


def prune_history(history, moment) -> list[dict]:
    """Drop rows older than the retention window, and cap what is left.

    The window is half-open, exactly like :func:`queue.prune`: a row *exactly*
    :data:`HISTORY_DAYS` old is gone.

    Ageing is deliberately **one-sided** — a row dated ahead of ``moment`` is
    kept, not dropped. It looks like junk, and sometimes is, but the same
    condition fires when a clock jumps *backwards* (an unsynced RTC on a Pi,
    corrected minutes after boot), and that write would then delete every real
    row in one go. :data:`HISTORY_MAX` is what bounds junk; nothing here is
    allowed to be the thing that loses somebody's history.

    An unreadable moment prunes nothing by age, for the same reason.
    """
    rows = normalise_history(history)
    now = moment_ts(moment)
    if now is not None and HISTORY_DAYS > 0:
        oldest = now - HISTORY_DAYS * SECONDS_PER_DAY
        rows = [row for row in rows if row["ts"] > oldest]
    return _cap(rows)


def record_load(history, user_id, moment, *, monitor: bool) -> list[dict]:
    """Log one real load — the Claim tap — and prune in the same breath.

    This is the **only** writer of history in this module, and the signal is the
    one the bot has been capturing since v0.x (§7.1): user X ran a load at time
    T. Nothing derived from a prediction can get in here, which is what makes
    "never learns from its own guesses" (§7.3) structural.

    ``monitor`` is required and is the person's consent (§11 / §5.2), passed in
    rather than read from :mod:`people` so this module stays loadable on its
    own and the decision stays visible at the call site. Anything that is not
    exactly ``True`` logs nothing: a half-written record holding the string
    ``"false"`` is truthy, and reading it as consent would log somebody who
    opted out — the one failure here that cannot be undone by a later fix.

    Pruning happens on every write, so history is bounded by the act of using
    it and never needs a sweeper.
    """
    rows = prune_history(history, moment)
    if monitor is not True:
        return rows
    record = load_record(user_id, moment)
    if record is None:
        return rows
    # Every one of this person's rows is checked, not just their newest. Rows
    # are stored oldest-first, but "newest row" and "nearest row in time" are
    # not the same thing once a single future-dated row is in the store — and
    # the deliberately one-sided ageing above means such a row never leaves.
    # Stopping at the newest would let that one row disable this person's
    # dedupe forever, so Unclaim -> Reclaim would count twice every time.
    for prior in rows:
        if prior["user_id"] != record["user_id"]:
            continue
        if abs(record["ts"] - prior["ts"]) < LOAD_DEDUPE_SECONDS:
            return rows  # same load, claimed twice
    return _cap([*rows, record])


def forget_load(history, user_id, since, until) -> list[dict]:
    """Drop one person's rows inside one window — a load that wasn't a wash.

    The counterpart to :func:`record_load`, and the reason the cancel path can
    be *structurally* incapable of feeding the model rather than merely
    documented as not doing so. A Claim tap is logged the moment it happens, so
    by the time the washer reports that somebody stopped the cycle on the
    machine, the row already exists — and a cancel is not a wash. Leaving it in
    would move this person's predicted times toward a load they never ran, and
    those predictions drive every nudge in the system.

    The window is what keeps this from being destructive. ``since`` is the start
    of the load being retracted and ``until`` its end, so the only rows that can
    match are ones written *during* it: an earlier, real load of theirs that
    same evening is outside the window and survives. Inclusive at both ends —
    the claim can land on the same second as the session start on a load claimed
    the instant the card appears.

    Anything unreadable — no id, an unparseable bound, a window that runs
    backwards — removes **nothing**. The dangerous direction here is deleting
    somebody's real history because of a bad argument, so an unusable call is a
    no-op rather than a guess, exactly as :func:`prune_history` refuses to age
    rows off an unreadable clock.
    """
    rows = normalise_history(history)
    if user_id is None or isinstance(user_id, bool):
        return rows
    key = str(user_id)
    start = _timestamp(since)
    end = _timestamp(until)
    if not key or start is None or end is None or end < start:
        return rows
    return [
        row
        for row in rows
        if not (row["user_id"] == key and start <= row["ts"] <= end)
    ]


def forget_person(records, user_id) -> list:
    """Every row that is **not** this person's, for history or corrections.

    §11 lets monitoring be turned off; this is what makes that retroactive if
    the panel wants it to be. Rows are removed rather than normalised, so a
    purge cannot also quietly rewrite everybody else's data.
    """
    if not isinstance(records, (list, tuple)):
        return []
    key = str(user_id)
    return [
        row
        for row in records
        if not (isinstance(row, dict) and str(row.get("user_id")) == key)
    ]


# --- reading one person's history -------------------------------------------
def history_for(history, user_id, moment=None) -> list[dict]:
    """One person's rows, oldest first, inside the retention window.

    Every read in this module starts here. That is the §11 guarantee in one
    line: there is no function that looks at the household's history as a
    whole, so there is nothing to accidentally render.

    ``moment`` applies the same 90-day window :func:`prune_history` writes with.
    Reads have to do this themselves rather than trusting that a write happened
    recently: pruning only runs inside :func:`record_load` and
    :func:`record_correction`, so a household that goes quiet for a season would
    otherwise keep being predicted at from history the design says no longer
    exists — and a *single* stale-clock row is enough to stretch
    :func:`history_days` past the four-week gate. Filtering here keeps the
    documented one-sided ageing (nothing is deleted from the store) while
    making the gates immune to a timestamp that should have aged out.

    ``None`` means "no age filter", matching :func:`prune_history`'s behaviour
    for an unreadable moment: it is a shape read, not a verdict.
    """
    key = str(user_id)
    rows = [row for row in normalise_history(history) if row["user_id"] == key]
    now = moment_ts(moment) if moment is not None else None
    if now is not None and HISTORY_DAYS > 0:
        oldest = now - HISTORY_DAYS * SECONDS_PER_DAY
        rows = [row for row in rows if row["ts"] > oldest]
    return rows


def load_count(history, user_id, moment=None) -> int:
    """How many loads this person has in the retained window."""
    return len(history_for(history, user_id, moment))


def first_load_ts(history, user_id, moment=None) -> float | None:
    """When this person's oldest retained load was, or None."""
    rows = history_for(history, user_id, moment)
    return rows[0]["ts"] if rows else None


def _days_from_rows(rows: list[dict], moment) -> float:
    """:func:`history_days` against rows already read. See :func:`_stats_from_rows`."""
    now = moment_ts(moment)
    if not rows or now is None:
        return 0.0
    return max(0.0, (now - rows[0]["ts"]) / SECONDS_PER_DAY)


def history_days(history, user_id, moment) -> float:
    """How long the bot has been watching this person, in days.

    Measured from their first retained load to the moment handed in — not from
    their *last* one, and not as a count of distinct weeks. "Four weeks of
    history" (§7.2) is a statement about how long we have been looking, and
    counting distinct active weeks instead would quietly require eight weeks of
    a fortnightly washer before it said anything.
    """
    return _days_from_rows(history_for(history, user_id, moment), moment)


def history_weeks(history, user_id, moment) -> float:
    """The same span in weeks — the third gate's input."""
    return history_days(history, user_id, moment) / 7


# --- corrections (design doc §7.3) ------------------------------------------
def correction_record(user_id, cell, kind, moment) -> dict | None:
    """One correction row, or None if it is not a usable one."""
    ts = moment_ts(moment)
    key = plan.normalise_cell(cell)
    if ts is None or key is None or kind not in CORRECTION_KINDS:
        return None
    if user_id is None or isinstance(user_id, bool) or not str(user_id):
        return None
    return {"ts": ts, "user_id": str(user_id), "cell": key, "kind": kind}


def normalise_correction(record) -> dict | None:
    """A stored correction, cleaned up — or None."""
    if not isinstance(record, dict):
        return None
    ts = _timestamp(record.get("ts"))
    kind = record.get("kind")
    cell = plan.normalise_cell(record.get("cell"))
    user_id = record.get("user_id")
    if ts is None or cell is None or kind not in CORRECTION_KINDS:
        return None
    if user_id is None or isinstance(user_id, bool) or not str(user_id):
        return None
    return {"ts": ts, "user_id": str(user_id), "cell": cell, "kind": kind}


def normalise_corrections(corrections) -> list[dict]:
    """Every usable correction, oldest first."""
    if not isinstance(corrections, (list, tuple)):
        return []
    rows = [
        row
        for row in (normalise_correction(r) for r in corrections)
        if row is not None
    ]
    rows.sort(key=lambda row: row["ts"])
    return rows


def prune_corrections(corrections, moment) -> list[dict]:
    """The same retention window as history, for the same reason.

    A correction older than the window is inert anyway — every load it could
    veto has already been pruned — so keeping it would be pure growth.
    """
    rows = normalise_corrections(corrections)
    now = moment_ts(moment)
    if now is not None and HISTORY_DAYS > 0:
        oldest = now - HISTORY_DAYS * SECONDS_PER_DAY
        rows = [row for row in rows if row["ts"] > oldest]
    return _cap(rows)


def record_correction(corrections, user_id, cell, kind, moment) -> list[dict]:
    """Log an explicit correction. Returns the new list.

    Corrections are the *other* half of the training signal (§7.3), and the
    only half a person can produce deliberately. Like history they are pruned
    on write, and like history nothing the model produces can write here — a
    row exists because somebody tapped a button about their own week.
    """
    rows = prune_corrections(corrections, moment)
    record = correction_record(user_id, cell, kind, moment)
    if record is None:
        return rows
    return _cap([*rows, record])


def mark_prediction_wrong(corrections, user_id, cell, moment) -> list[dict]:
    """📅 Wrong — the guess for this cell is retired.

    It does not blacklist the cell forever: it resets the evidence clock for
    that bucket (see :func:`corrected_since`), so the only thing that can bring
    the prediction back is the person actually washing then again. Being told
    "no" and then arguing from the same data would be the model learning from
    itself with extra steps.
    """
    return record_correction(corrections, user_id, cell, CORRECTION_WRONG, moment)


def mark_nudge_pushed(corrections, user_id, cell, moment) -> list[dict]:
    """⏭ Push to tomorrow — recorded, and explicitly **not** a wrongness signal.

    §7.3 is exact about this: the day was right, the person just isn't doing it
    tonight. Counting it as a miss would train the model out of every correct
    guess anyone was ever too busy to act on.
    """
    return record_correction(corrections, user_id, cell, CORRECTION_PUSHED, moment)


def corrections_for(corrections, user_id) -> list[dict]:
    """One person's corrections, oldest first. Self-referential, like history."""
    key = str(user_id)
    rows = normalise_corrections(corrections)
    return [row for row in rows if row["user_id"] == key]


def _veto_map(corrections, user_id) -> dict[str, float]:
    """Cell -> the timestamp of the most recent "that's wrong" for it."""
    latest: dict[str, float] = {}
    for row in corrections_for(corrections, user_id):
        if row["kind"] != CORRECTION_WRONG:
            continue
        if row["ts"] > latest.get(row["cell"], float("-inf")):
            latest[row["cell"]] = row["ts"]
    return latest


def corrected_since(corrections, user_id, cell) -> float | None:
    """When this person last said a guess for this cell was wrong, or None.

    Loads at or before that moment stop counting toward the bucket: the
    evidence they were told was wrong does not get to keep voting.
    """
    key = plan.normalise_cell(cell)
    if key is None:
        return None
    return _veto_map(corrections, user_id).get(key)


def next_day_cell(cell) -> str | None:
    """The same slot, one day later — where ⏭ "push to tomorrow" lands.

    Wraps Sunday to Monday, which is the one line of this that is easy to get
    wrong and would otherwise strand every Sunday push.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    weekday, slot = parsed
    return plan.cell_key((weekday + 1) % 7, slot)


# --- prediction (design doc §7.2) -------------------------------------------
def _counts_from_rows(rows: list[dict], vetoes: dict[str, float]) -> dict[str, int]:
    """:func:`bucket_counts` against rows already read."""
    counts: dict[str, int] = {}
    for row in rows:
        cell = row["cell"]
        if cell is None:
            continue
        since = vetoes.get(cell)
        if since is not None and row["ts"] <= since:
            continue
        counts[cell] = counts.get(cell, 0) + 1
    return counts


def bucket_counts(history, user_id, corrections=None, moment=None) -> dict[str, int]:
    """This person's ``cell -> loads`` histogram, corrections applied.

    Only their own rows, only rows with a bucket, and only rows a "that's
    wrong" hasn't retired. Cells they have never washed in are absent rather
    than zero, so the caller can iterate the dict.
    """
    return _counts_from_rows(
        history_for(history, user_id, moment), _veto_map(corrections, user_id)
    )


def _stats_from_rows(rows: list[dict], counts: dict[str, int], weeks, cell) -> dict:
    """:func:`bucket_stats` against work already done — the arithmetic only.

    The split exists because :func:`predictions` asks about every candidate
    cell, and the three inputs (this person's rows, their histogram, their span
    in weeks) are identical for all of them. Reading them per cell meant
    :func:`normalise_history` rebuilding and re-sorting the *whole house's*
    history ~84 times for one grid tap — a tenth of a second on x86 and close
    to a second on a Pi, spent inside a Discord interaction callback on HA's
    event loop. Nothing here is cached or remembered between calls: the
    functions stay pure, the work just happens once per answer instead of once
    per cell.
    """
    key = plan.normalise_cell(cell)
    total = len(rows)
    count = counts.get(key, 0) if key else 0
    gates = {
        GATE_OBSERVATIONS: count >= MIN_OBSERVATIONS,
        # Integer maths on purpose: `count / total >= 0.30` is a float
        # comparison at exactly the boundary the tests care about, and 3 of 10
        # must be a habit rather than a coin toss decided by binary rounding.
        GATE_SHARE: total > 0 and count * 100 >= total * MIN_SHARE_PERCENT,
        GATE_WEEKS: weeks >= MIN_WEEKS,
    }
    parsed = plan.parse_cell(key) if key else None
    return {
        "cell": key,
        "weekday": parsed[0] if parsed else None,
        "slot": parsed[1] if parsed else None,
        "count": count,
        "total": total,
        "share": (count / total) if total else 0.0,
        "weeks": weeks,
        "gates": gates,
        "confident": key is not None and all(gates.values()),
    }


def bucket_stats(history, user_id, cell, moment, corrections=None) -> dict:
    """Everything behind one cell's verdict — the numbers *and* the gates.

    P4 says the guesses are visible and correctable, which means the UI has to
    be able to show the arithmetic ("5 of your last 8 loads") and, just as
    usefully, why there is *no* guess: the gates come back individually so
    "you've only washed twice on a Thursday" and "you wash whenever" are
    distinguishable answers rather than one shrug.

    The share is deliberately a percentage of **all** their retained loads, not
    of some recent window: a bucket is a habit when it dominates what they
    actually do, and a denominator that quietly shrank would make anybody look
    regular after a quiet fortnight.
    """
    rows = history_for(history, user_id, moment)
    counts = _counts_from_rows(rows, _veto_map(corrections, user_id))
    return _stats_from_rows(rows, counts, _days_from_rows(rows, moment) / 7, cell)


def predictions(history, user_id, moment, corrections=None) -> list[dict]:
    """Every bucket this person clears the gate on, most-washed first.

    Usually none, sometimes one. Three is the arithmetic maximum — at 30% a
    piece there is no room for a fourth — and that ceiling is what stops a
    "prediction" from being a shrug that covers the whole week. Ties break on
    day-then-slot order so two equal buckets always come back the same way
    round.

    This person's rows, their histogram and their span are read **once** and
    reused across every candidate cell (see :func:`_stats_from_rows`); it is
    the same answer :func:`bucket_stats` gives per cell, computed without
    re-reading the store's whole history once per cell.
    """
    rows = history_for(history, user_id, moment)
    counts = _counts_from_rows(rows, _veto_map(corrections, user_id))
    weeks = _days_from_rows(rows, moment) / 7
    stats = [_stats_from_rows(rows, counts, weeks, cell) for cell in counts]
    confident = [s for s in stats if s["confident"]]
    confident.sort(key=lambda s: (-s["count"], _cell_order(s["cell"])))
    return confident


def predict(history, user_id, moment, corrections=None) -> dict | None:
    """This person's one predicted slot, or **None** — the common answer.

    None is not a failure and must not be rendered as a hedge: no prediction
    means no ``?``, no Sunday sentence and no nudge (P6). The person's
    ``predict`` preference is deliberately not consulted here — this is
    arithmetic about their own history, and whether to *act* on it is the
    panel's call (unlike ``monitor``, which has to be enforced at the write or
    the data exists whatever anyone chooses later).
    """
    found = predictions(history, user_id, moment, corrections)
    return found[0] if found else None


def predicted_cells(history, user_id, moment, corrections=None) -> list[str]:
    """Just the cell keys — what a grid would draw as ``?`` for this viewer.

    Only ever this person's own, so a rendered grid still shows nothing about
    anybody else (P5).
    """
    return [s["cell"] for s in predictions(history, user_id, moment, corrections)]


# --- explanation (design doc §7.3) ------------------------------------------
def explain(stats) -> str | None:
    """"5 of your last 8 loads" — the numbers behind a guess, or None.

    Second person, always, because that is the only person it may be shown to:
    the sentence has no room for another name and no other person's count is in
    scope to put there (§11). None when there is nothing to explain, so the
    caller drops the line rather than printing "0 of your last 0 loads".
    """
    if not isinstance(stats, dict):
        return None
    count = stats.get("count")
    total = stats.get("total")
    if not isinstance(count, int) or not isinstance(total, int):
        return None
    if count <= 0 or total <= 0:
        return None
    return f"{count} of your last {total} {'load' if total == 1 else 'loads'}"


def describe_bucket(cell) -> str | None:
    """"Thursday evenings" for a cell key, or None.

    Prose rather than the grid's "Th Eve": this ends up in a sentence somebody
    reads once in a DM, not in a column that has to line up.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    weekday, slot = parsed
    return f"{plan.DAY_NAMES[weekday]} {SLOT_PHRASES[slot]}"


def describe_prediction(stats) -> str | None:
    """"Thursday evenings" for a prediction dict, or None."""
    if not isinstance(stats, dict):
        return None
    return describe_bucket(stats.get("cell"))


# --- the nudge budget (design doc P2) ---------------------------------------
def normalise_budget(value) -> dict:
    """One person's nudge accounting, rebuilt from whatever was stored.

    ``last_nudge_ts`` and ``nudges_this_week`` are §12's fields; the day and
    week *keys* are the addition that makes them work. A bare counter cannot
    know which week it counted, so it can only be reset by somebody else
    remembering to — and "somebody else remembering to" across a New Year is
    precisely the bug this is written to avoid. Storing the key the count
    belongs to makes the reset a comparison instead of a chore.

    A record from before those keys existed simply reads as "no nudges this
    week", which is one extra permitted nudge at upgrade time and no lost
    messages.
    """
    source = value if isinstance(value, dict) else {}
    day = source.get("nudge_day")
    week = source.get("nudge_week")
    return {
        "last_nudge_ts": _timestamp(source.get("last_nudge_ts")),
        "nudge_day": day if isinstance(day, str) and day else None,
        "nudges_today": _count(source.get("nudges_today")),
        "nudge_week": week if isinstance(week, str) and week else None,
        "nudges_this_week": _count(source.get("nudges_this_week")),
    }


def normalise_budgets(budgets) -> dict[str, dict]:
    """Every person's accounting, re-keyed to the string form.

    Same hazard as everywhere else: the mapping round-trips through JSON as
    string keys and the next tap arrives as an int.
    """
    if not isinstance(budgets, dict):
        return {}
    return {str(key): normalise_budget(value) for key, value in budgets.items()}


def budget_for(budgets, user_id) -> dict:
    """One person's accounting — a fresh, empty one if they have none."""
    if not isinstance(budgets, dict):
        return normalise_budget(None)
    key = str(user_id)
    for stored_key, value in budgets.items():
        if str(stored_key) == key:
            return normalise_budget(value)
    return normalise_budget(None)


def _rolled(stored, current) -> bool:
    """Whether ``current`` is genuinely a *later* window than the stored one.

    Both key formats are zero-padded so they sort chronologically as plain
    strings, and the comparison is ``<`` rather than ``!=`` on purpose: a window
    key from the *future* means the clock has gone backwards (an unsynced RTC
    after a restart, the same hazard :func:`prune_history` is one-sided for),
    and treating that as a different window would hand out a fresh allowance —
    a safety valve that fails open. An earlier-reading clock keeps counting
    against the window that was already spent.
    """
    return stored is None or stored < current


def nudges_today(budget, moment) -> int:
    """How many DMs this person has had **today**, 0 once the day has rolled."""
    account = normalise_budget(budget)
    day = day_key(moment)
    if day is None or _rolled(account["nudge_day"], day):
        return 0
    return account["nudges_today"]


def nudges_this_week(budget, moment) -> int:
    """How many DMs this person has had **this ISO week**, 0 once it rolled.

    The week comes from :func:`plan.iso_week_key`, which is ISO-year-first, so
    the window rolls on the actual week boundary: 2027-01-01 is still
    2026-W53 and must *not* hand anybody a fresh allowance just because the
    calendar year ticked over. Reusing that function rather than inventing week
    maths is the whole point — there is one definition of a week here.
    """
    account = normalise_budget(budget)
    week = plan.iso_week_key(moment)
    if week is None or _rolled(account["nudge_week"], week):
        return 0
    return account["nudges_this_week"]


def _clock_verdict(budget, moment) -> str | None:
    """:data:`BUDGET_UNREADABLE` when this moment cannot be counted, else None.

    Shared by both caps so there is one answer to "is this a moment we may
    charge a message against". An unreadable moment denies: if we cannot tell
    what day it is we cannot count, and the safe direction for a message budget
    is always "don't send" (P6 again — a missed nudge costs nothing, an
    uncounted one is how a cap silently stops existing).
    """
    if day_key(moment) is None or plan.iso_week_key(moment) is None:
        return BUDGET_UNREADABLE
    # A moment that predates the last DM we know we sent is not a moment: the
    # clock has gone backwards. Denying keeps the backwards excursion from
    # rewriting the window keys to something in the past, which is what would
    # then let the *correct* time look like a fresh window.
    last = normalise_budget(budget)["last_nudge_ts"]
    ts = moment_ts(moment)
    if last is not None and ts is not None and ts < last:
        return BUDGET_UNREADABLE
    return None


def check_nudge(budget, moment) -> str:
    """Whether a DM is allowed right now, and if not, which cap said no.

    Returns :data:`BUDGET_OK` / :data:`BUDGET_DAY` / :data:`BUDGET_WEEK` /
    :data:`BUDGET_UNREADABLE`.
    """
    verdict = _clock_verdict(budget, moment)
    if verdict is not None:
        return verdict
    if nudges_today(budget, moment) >= MAX_NUDGES_PER_DAY:
        return BUDGET_DAY
    if nudges_this_week(budget, moment) >= MAX_NUDGES_PER_WEEK:
        return BUDGET_WEEK
    return BUDGET_OK


def check_daily_cap(budget, moment) -> str:
    """The **day** cap alone — for a DM the weekly allowance does not fund.

    :data:`MAX_NUDGES_PER_DAY` is the ceiling on unprompted DMs of every kind,
    and nothing is allowed past it. :data:`MAX_NUDGES_PER_WEEK` is a different
    promise: it bounds how often *the bot's own arithmetic* starts a
    conversation with you, which is why the Sunday check-in and the day-of
    nudge spend it. A message another housemate asked the bot to pass on
    (:mod:`trade`) is not the bot's initiative, and letting one of those eat the
    week's allowance would silence the reminders somebody actually opted into.

    So this reads and reports only the day window. Callers that mean "a nudge"
    want :func:`check_nudge`; there is no third answer.
    """
    verdict = _clock_verdict(budget, moment)
    if verdict is not None:
        return verdict
    if nudges_today(budget, moment) >= MAX_NUDGES_PER_DAY:
        return BUDGET_DAY
    return BUDGET_OK


def record_nudge(budget, moment) -> dict:
    """Count one sent DM against both windows. Returns the new accounting.

    Cannot touch history: a nudge is the model talking, and §7.3 says the model
    never learns from itself. This function's entire vocabulary is timestamps
    and counters.
    """
    updated = normalise_budget(budget)
    day = day_key(moment)
    week = plan.iso_week_key(moment)
    if day is None or week is None:
        return updated
    ts = moment_ts(moment)
    if ts is not None:
        updated["last_nudge_ts"] = ts
    updated["nudges_today"] = nudges_today(budget, moment) + 1
    updated["nudge_day"] = day
    updated["nudges_this_week"] = nudges_this_week(budget, moment) + 1
    updated["nudge_week"] = week
    return updated


def record_daily_nudge(budget, moment) -> dict:
    """Count one sent DM against the **day** window only.

    The weekly counter is deliberately left exactly as it was — see
    :func:`check_daily_cap` for why. ``last_nudge_ts`` *is* advanced, because it
    is the record of when this person's phone last buzzed on our account, and
    :func:`nudge.already_nudged_in_slot` reads it to keep two triggers from
    talking about one evening twice. A trade DM at 20:30 therefore also stands
    down the day-of nudge for that slot, which is the honest answer: the person
    has already been messaged.
    """
    updated = normalise_budget(budget)
    day = day_key(moment)
    if day is None:
        return updated
    ts = moment_ts(moment)
    if ts is not None:
        updated["last_nudge_ts"] = ts
    updated["nudges_today"] = nudges_today(budget, moment) + 1
    updated["nudge_day"] = day
    return updated


def claim_nudge(budget, moment) -> tuple[bool, dict]:
    """Spend one nudge if there is one to spend.

    Returns ``(allowed, new_budget)``. Over budget means **dropped, not
    queued** (P2): the denial path returns the accounting unchanged, and there
    is deliberately no pending list anywhere in this module for a dropped nudge
    to sit in. A nudge that arrives a day late is worse than one that never
    arrives — it is a reminder about a slot that has already passed.

    Deciding and recording are one call on purpose. Two calls is one early
    ``return`` away from a bot that checks the budget and then sends anyway.
    """
    if check_nudge(budget, moment) != BUDGET_OK:
        return (False, normalise_budget(budget))
    return (True, record_nudge(budget, moment))


def claim_daily_nudge(budget, moment) -> tuple[bool, dict]:
    """Spend one **day**-capped DM if there is one to spend.

    The trade broker's half of :func:`claim_nudge`, and deliberately the same
    shape: deciding and recording are one call, over budget is dropped rather
    than queued, and the denial path returns the accounting unchanged.
    """
    if check_daily_cap(budget, moment) != BUDGET_OK:
        return (False, normalise_budget(budget))
    return (True, record_daily_nudge(budget, moment))


def _claim_for(budgets, user_id, moment, claim) -> tuple[bool, dict]:
    """One person's claim, against a whole mapping. ``(allowed, all)``.

    The mapping comes back rebuilt with exactly one record for this person, so
    an int key left over from an in-memory write can never end up shadowing
    (or being shadowed by) the string one — two accountings for one human is
    two nudges a day.
    """
    key = str(user_id)
    allowed, account = claim(budget_for(budgets, user_id), moment)
    updated = {k: v for k, v in normalise_budgets(budgets).items() if k != key}
    updated[key] = account
    return (allowed, updated)


def claim_nudge_for(budgets, user_id, moment) -> tuple[bool, dict]:
    """:func:`claim_nudge` against a whole mapping. Returns ``(allowed, all)``."""
    return _claim_for(budgets, user_id, moment, claim_nudge)


def claim_daily_nudge_for(budgets, user_id, moment) -> tuple[bool, dict]:
    """:func:`claim_daily_nudge` against a whole mapping. ``(allowed, all)``."""
    return _claim_for(budgets, user_id, moment, claim_daily_nudge)
