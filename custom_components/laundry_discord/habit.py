"""Pure, dependency-free habit model — what somebody's own history implies. No Home
Assistant or discord imports, so it is unit-tested directly. :mod:`assistant` owns
the ``Store`` and Discord calls; this module only decides what one person's history
means and whether they may be messaged. Pass timezone-aware local time (HA's
``dt_util.now()``); nothing is mutated."""

from __future__ import annotations

try:  # sibling module inside the integration package
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path in tests
    # plan.py owns the week/slot maths; not duplicated here.
    import plan  # type: ignore[no-redef]

SECONDS_PER_DAY = 86400

# --- retention: history capped at 90 days -------------------------------------
# Long enough for a fortnightly washer, short enough that an old habit (a
# different job, different housemates) stops voting on this week.
HISTORY_DAYS = 90

# Absolute ceiling, in case timestamps aren't sane: bounds history even when a
# future-dated row (unsynced clock) would never age out of the 90-day window.
HISTORY_MAX = 1000

# Dedupe window for unclaim/reclaim on one load; two genuine loads by the same
# person inside an hour don't happen (a cycle is 4-5 hours).
LOAD_DEDUPE_SECONDS = 3600

# --- the confidence gate: all three must pass, or stay silent -----------------
# observations: too few sightings. share: washes whenever. weeks: not watched
# long enough to tell.
MIN_OBSERVATIONS = 3
MIN_SHARE_PERCENT = 30
MIN_SHARE = MIN_SHARE_PERCENT / 100  # display only; the gate itself uses integer maths
MIN_WEEKS = 4
MIN_HISTORY_DAYS = MIN_WEEKS * 7

GATE_OBSERVATIONS = "observations"
GATE_SHARE = "share"
GATE_WEEKS = "weeks"
GATES = (GATE_OBSERVATIONS, GATE_SHARE, GATE_WEEKS)

# --- corrections ---------------------------------------------------------------
# "wrong": the guess was wrong. "pushed" (Push to tomorrow) is NOT a wrongness
# signal — the day was right, they just aren't doing it tonight.
CORRECTION_WRONG = "wrong"
CORRECTION_PUSHED = "pushed"
CORRECTION_KINDS = (CORRECTION_WRONG, CORRECTION_PUSHED)

# --- the nudge budget: over budget means dropped, never queued ----------------
MAX_NUDGES_PER_DAY = 1
MAX_NUDGES_PER_WEEK = 2

BUDGET_OK = "ok"
BUDGET_DAY = "day"
BUDGET_WEEK = "week"
BUDGET_UNREADABLE = "unreadable"

# Prose for a sentence ("I think you wash Thursday evenings"), not
# :data:`plan.SLOT_LABELS` — those are grid column headers, kept short for the
# 26-character grid.
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
    """A non-negative counter field, or 0 for anything unexpected (``bool`` included)."""
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
    """Whether this is a moment we are allowed to read at all. Rejects naive datetimes:
    one written with ``datetime.utcnow()`` instead of ``dt_util.now()`` would
    silently bucket into the wrong evening."""
    try:
        return moment.utcoffset() is not None
    except (AttributeError, TypeError, ValueError):
        return False


def moment_ts(moment) -> float | None:
    """Unix timestamp for a moment, or None if it isn't a usable one. A ``date``
    returns None rather than midnight; a naive datetime is refused (see
    :func:`_aware`)."""
    if not _aware(moment):
        return None
    try:
        return float(moment.timestamp())
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def day_key(moment) -> str | None:
    """The local calendar day as ``"2026-08-03"``, or None. The day half of the nudge budget."""
    if not _aware(moment):
        return None
    try:
        return moment.strftime("%Y-%m-%d")
    except (AttributeError, TypeError, ValueError):
        return None


def bucket_for(moment) -> str | None:
    """The ``(weekday, slot)`` bucket a moment falls in — ``"3-eve"`` — or None. Uses
    :mod:`plan`'s slot windows so the model and the grid can't drift apart. None for
    00:00-06:00, which is in no slot."""
    if not _aware(moment):
        return None
    weekday = plan.weekday_of(moment)
    slot = plan.slot_for_hour(getattr(moment, "hour", None))
    if weekday is None or slot is None:
        return None
    return plan.cell_key(weekday, slot)


# --- history records ---------------------------------------------------------
def load_record(user_id, moment) -> dict | None:
    """One history row for a real Claim tap, or None if it can't be used. ``{"ts": ...,
    "user_id": "123", "cell": "3-eve"}``. The id is normalised to a string; the
    bucket is computed and frozen at write time, since the household's timezone
    could change later. No name is ever stored."""
    ts = moment_ts(moment)
    if ts is None or user_id is None or isinstance(user_id, bool):
        return None
    key = str(user_id)
    if not key:
        return None
    return {"ts": ts, "user_id": key, "cell": bucket_for(moment)}


def normalise_record(record) -> dict | None:
    """A stored history row, cleaned up — or None if it is unusable. Dropped rather
    than kept: a row with no timestamp can never age out, and a row with no id
    belongs to nobody."""
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
    # No stored cell still counts toward the total but votes for no weekday.
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
    """Drop rows older than the retention window, and cap what is left. Half-open: a
    row exactly :data:`HISTORY_DAYS` old is gone. Ageing is one-sided — a
    future-dated row (clock skew) is kept, not dropped, so a backwards clock jump
    can't wipe history. An unreadable moment prunes nothing."""
    rows = normalise_history(history)
    now = moment_ts(moment)
    if now is not None and HISTORY_DAYS > 0:
        oldest = now - HISTORY_DAYS * SECONDS_PER_DAY
        rows = [row for row in rows if row["ts"] > oldest]
    return _cap(rows)


def record_load(history, user_id, moment, *, monitor: bool) -> list[dict]:
    """Log one real load and prune in the same breath. The only history writer.
    ``monitor`` is the person's consent: must be exactly ``True``, not merely
    truthy, so a stray value can never log someone who opted out."""
    rows = prune_history(history, moment)
    if monitor is not True:
        return rows
    record = load_record(user_id, moment)
    if record is None:
        return rows
    # Check every row, not just the newest: a future-dated row (never aged
    # out) could otherwise sit last and defeat dedupe for every load after it.
    for prior in rows:
        if prior["user_id"] != record["user_id"]:
            continue
        if abs(record["ts"] - prior["ts"]) < LOAD_DEDUPE_SECONDS:
            return rows  # same load, claimed twice
    return _cap([*rows, record])


def forget_load(history, user_id, since, until) -> list[dict]:
    """Drop one person's rows in one window — retracts a load that wasn't a wash.
    ``since``/``until`` bound the retracted load (inclusive); an earlier real load
    that evening survives. Unreadable input (no id, a bad bound, a backwards window)
    removes nothing rather than guessing."""
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
    """Every row that is **not** this person's, for history or corrections. Purges a
    person's data when monitoring is turned off; rows are removed, never normalised,
    so it can't rewrite anyone else's."""
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
    """One person's rows, oldest first, inside the retention window. Every read in this
    module goes through here, so nothing ever looks at the household's history as a
    whole. ``moment`` re-applies the 90-day window; ``None`` means no age filter,
    not a verdict."""
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
    """How long the bot has been watching this person, in days. From their first
    retained load to ``moment``, not their last or a count of active weeks."""
    return _days_from_rows(history_for(history, user_id, moment), moment)


def history_weeks(history, user_id, moment) -> float:
    """The same span in weeks — the third gate's input."""
    return history_days(history, user_id, moment) / 7


# --- how often somebody washes ----------------------------------------------
# Below this a gap is a wash+dry pair claimed separately, not a real interval
# (a cycle is 4-5 hours); counting it would drag the median down.
MIN_GAP_DAYS = 0.5

# Two gaps (three loads) is the same evidence bar as the day prediction.
MIN_GAPS = 2


def gaps_for(history, user_id, moment=None) -> list[float]:
    """The intervals between this person's loads, in days, oldest first."""
    rows = history_for(history, user_id, moment)
    gaps: list[float] = []
    for older, newer in zip(rows, rows[1:]):
        days = (newer["ts"] - older["ts"]) / SECONDS_PER_DAY
        if days >= MIN_GAP_DAYS:
            gaps.append(days)
    return gaps


def typical_gap(history, user_id, moment=None) -> float | None:
    """How many days this person usually leaves between loads, or None. The median, not
    the mean, so one long gap (a holiday) doesn't silence the nudge for weeks. None
    below :data:`MIN_GAPS`."""
    gaps = sorted(gaps_for(history, user_id, moment))
    if len(gaps) < MIN_GAPS:
        return None
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[middle]
    return (gaps[middle - 1] + gaps[middle]) / 2


def days_since_load(history, user_id, moment) -> float | None:
    """Days since this person's last load, or None if they have none."""
    rows = history_for(history, user_id, moment)
    now = moment_ts(moment)
    if not rows or now is None:
        return None
    return max(0.0, (now - rows[-1]["ts"]) / SECONDS_PER_DAY)


def is_due(history, user_id, moment) -> bool:
    """Whether this person is overdue by **their own** usual gap, not a fixed one.
    False whenever the cadence is unknown, which is the common early answer."""
    gap = typical_gap(history, user_id, moment)
    if gap is None:
        return False
    since = days_since_load(history, user_id, moment)
    return since is not None and since >= gap


# --- corrections ---------------------------------------------------------------
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
    """The same retention window as history: an older correction is inert anyway."""
    rows = normalise_corrections(corrections)
    now = moment_ts(moment)
    if now is not None and HISTORY_DAYS > 0:
        oldest = now - HISTORY_DAYS * SECONDS_PER_DAY
        rows = [row for row in rows if row["ts"] > oldest]
    return _cap(rows)


def record_correction(corrections, user_id, cell, kind, moment) -> list[dict]:
    """Log an explicit correction, pruned on write like history. Returns the new list.
    Only a person's own button tap writes here — never the model's own guess."""
    rows = prune_corrections(corrections, moment)
    record = correction_record(user_id, cell, kind, moment)
    if record is None:
        return rows
    return _cap([*rows, record])


def mark_prediction_wrong(corrections, user_id, cell, moment) -> list[dict]:
    """Wrong — retires the guess for this cell by resetting its evidence clock
    (:func:`corrected_since`), not blacklisting it forever."""
    return record_correction(corrections, user_id, cell, CORRECTION_WRONG, moment)


def mark_nudge_pushed(corrections, user_id, cell, moment) -> list[dict]:
    """Push to tomorrow — recorded, and explicitly not a wrongness signal. The day was
    right; they just aren't doing it tonight."""
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
    """When this person last said a guess for this cell was wrong, or None. Loads at or
    before that moment stop counting toward the bucket."""
    key = plan.normalise_cell(cell)
    if key is None:
        return None
    return _veto_map(corrections, user_id).get(key)


def next_day_cell(cell) -> str | None:
    """The same slot, one day later — where "push to tomorrow" lands. Wraps Sunday to
    Monday."""
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    weekday, slot = parsed
    return plan.cell_key((weekday + 1) % 7, slot)


# --- prediction ------------------------------------------------------------
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
    """This person's ``cell -> loads`` histogram, corrections applied. Cells never
    washed in are absent rather than zero."""
    return _counts_from_rows(
        history_for(history, user_id, moment), _veto_map(corrections, user_id)
    )


def _stats_from_rows(rows: list[dict], counts: dict[str, int], weeks, cell) -> dict:
    """:func:`bucket_stats`'s arithmetic, shared across candidates to avoid re-reading history per cell."""
    key = plan.normalise_cell(cell)
    total = len(rows)
    count = counts.get(key, 0) if key else 0
    gates = {
        GATE_OBSERVATIONS: count >= MIN_OBSERVATIONS,
        # Integer maths: avoids a float boundary comparison at exactly 30%.
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
    """Everything behind one cell's verdict — the numbers and the gates. Gates come
    back individually so the UI can explain *why* there's no guess. Share is a
    percentage of all retained loads, not a recent window, so a quiet fortnight
    can't make someone look more regular than they are."""
    rows = history_for(history, user_id, moment)
    counts = _counts_from_rows(rows, _veto_map(corrections, user_id))
    return _stats_from_rows(rows, counts, _days_from_rows(rows, moment) / 7, cell)


def predictions(history, user_id, moment, corrections=None) -> list[dict]:
    """Every bucket this person clears the gate on, most-washed first. Usually none,
    sometimes one; three is the arithmetic max at 30% a bucket. Ties break on
    day-then-slot order."""
    rows = history_for(history, user_id, moment)
    counts = _counts_from_rows(rows, _veto_map(corrections, user_id))
    weeks = _days_from_rows(rows, moment) / 7
    stats = [_stats_from_rows(rows, counts, weeks, cell) for cell in counts]
    confident = [s for s in stats if s["confident"]]
    confident.sort(key=lambda s: (-s["count"], _cell_order(s["cell"])))
    return confident


def predict(history, user_id, moment, corrections=None) -> dict | None:
    """This person's one predicted slot, or None — the common answer. None means no
    ``?``, no Sunday sentence and no nudge — not a hedge."""
    found = predictions(history, user_id, moment, corrections)
    return found[0] if found else None


def predicted_cells(history, user_id, moment, corrections=None) -> list[str]:
    """Just the cell keys a grid would draw as ``?`` for this viewer. Only ever this
    person's own — never shows anything about anybody else."""
    return [s["cell"] for s in predictions(history, user_id, moment, corrections)]


# --- explanation ----------------------------------------------------------
def explain(stats) -> str | None:
    """"5 of your last 8 loads" — the numbers behind a guess, or None. Second person
    always; no other person's count is ever in scope here."""
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
    """"Thursday evenings" for a cell key, or None. Prose, not the grid's "Th Eve"."""
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


# --- the nudge budget -------------------------------------------------------
def normalise_budget(value) -> dict:
    """One person's nudge accounting, rebuilt from whatever was stored. The stored
    day/week key makes rollover a comparison rather than a reset to remember; a
    record with no keys reads as "no nudges yet"."""
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
    """Every person's accounting, re-keyed to the string form (JSON round-trip hazard)."""
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
    """Whether ``current`` is genuinely a *later* window than the stored one. ``<`` not
    ``!=``: a window key from the future (clock skew) must not read as a new window
    and hand out a fresh allowance."""
    return stored is None or stored < current


def nudges_today(budget, moment) -> int:
    """How many DMs this person has had **today**, 0 once the day has rolled."""
    account = normalise_budget(budget)
    day = day_key(moment)
    if day is None or _rolled(account["nudge_day"], day):
        return 0
    return account["nudges_today"]


def nudges_this_week(budget, moment) -> int:
    """How many DMs this person has had **this ISO week**, 0 once it rolled. Uses
    :func:`plan.iso_week_key` so a year boundary mid-week doesn't reset it early."""
    account = normalise_budget(budget)
    week = plan.iso_week_key(moment)
    if week is None or _rolled(account["nudge_week"], week):
        return 0
    return account["nudges_this_week"]


def _clock_verdict(budget, moment) -> str | None:
    """:data:`BUDGET_UNREADABLE` when this moment cannot be counted, else None. Denies
    rather than sends: a missed nudge costs nothing, an uncounted one lets the cap
    silently stop existing."""
    if day_key(moment) is None or plan.iso_week_key(moment) is None:
        return BUDGET_UNREADABLE
    # A moment older than the last known DM means the clock went backwards;
    # deny so it can't rewrite the window keys into the past.
    last = normalise_budget(budget)["last_nudge_ts"]
    ts = moment_ts(moment)
    if last is not None and ts is not None and ts < last:
        return BUDGET_UNREADABLE
    return None


def check_nudge(budget, moment) -> str:
    """Whether a DM is allowed right now, and if not, which cap said no. Returns
    :data:`BUDGET_OK` / :data:`BUDGET_DAY` / :data:`BUDGET_WEEK` /
    :data:`BUDGET_UNREADABLE`."""
    verdict = _clock_verdict(budget, moment)
    if verdict is not None:
        return verdict
    if nudges_today(budget, moment) >= MAX_NUDGES_PER_DAY:
        return BUDGET_DAY
    if nudges_this_week(budget, moment) >= MAX_NUDGES_PER_WEEK:
        return BUDGET_WEEK
    return BUDGET_OK


def check_daily_cap(budget, moment) -> str:
    """The **day** cap alone — for a DM the weekly allowance doesn't fund. A trade
    broker message isn't the bot's own initiative, so it spends only the day cap.
    Callers wanting "a nudge" should use :func:`check_nudge`."""
    verdict = _clock_verdict(budget, moment)
    if verdict is not None:
        return verdict
    if nudges_today(budget, moment) >= MAX_NUDGES_PER_DAY:
        return BUDGET_DAY
    return BUDGET_OK


def record_nudge(budget, moment) -> dict:
    """Count one sent DM against both windows. Returns the new accounting. Cannot touch
    history: the model never learns from its own nudges."""
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
    """Count one sent DM against the **day** window only (see :func:`check_daily_cap`).
    ``last_nudge_ts`` still advances: :func:`nudge.already_nudged_in_slot` reads it
    so a trade DM also stands down that slot's day-of nudge."""
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
    """Spend one nudge if there is one to spend. Returns ``(allowed, new_budget)``.
    Over budget is dropped, never queued; deciding and recording is one call."""
    if check_nudge(budget, moment) != BUDGET_OK:
        return (False, normalise_budget(budget))
    return (True, record_nudge(budget, moment))


def claim_daily_nudge(budget, moment) -> tuple[bool, dict]:
    """Spend one **day**-capped DM if there is one to spend. The trade broker's
    counterpart to :func:`claim_nudge`; same shape."""
    if check_daily_cap(budget, moment) != BUDGET_OK:
        return (False, normalise_budget(budget))
    return (True, record_daily_nudge(budget, moment))


def _claim_for(budgets, user_id, moment, claim) -> tuple[bool, dict]:
    """One person's claim, against a whole mapping. ``(allowed, all)``. Rebuilds with
    exactly one record per person so a leftover int key can't shadow the string one
    and double someone's nudge allowance."""
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
