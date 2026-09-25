"""Pure, dependency-free decisions for the reminder DMs: whether a person may
be messaged right now, which message (if any) to send, its budget claim,
and its wording. No Home Assistant or discord imports, so this is
unit-tested directly; `reminders` owns scheduling and Discord I/O.

The caller always passes the clock as timezone-aware local time (naive is
rejected). Every function returns new data rather than mutating in place.
"""

from __future__ import annotations

try:  # normal path: sibling modules inside the package
    from . import habit
    from . import people
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path, as tests do
    # No package when exec'd by file path. Import directly rather than
    # reimplementing the budget/slot/preference logic here.
    import habit  # type: ignore[no-redef]
    import people  # type: ignore[no-redef]
    import plan  # type: ignore[no-redef]

# --- why a reminder was or wasn't sent ---------------------------------------
# One reason per gate, so "why didn't this send" is answerable without a log
# line per evaluation.
REASON_OK = "ok"
REASON_MOMENT = "moment"  # unreadable clock — never send on a guess
REASON_NOT_OPTED_IN = "not_opted_in"  # no record, or never answered the panel
REASON_REMINDERS_OFF = "reminders_off"  # 🚫 in the panel
REASON_NOT_DM = "not_dm"  # they chose the channel, not DMs
REASON_DM_CLOSED = "dm_closed"  # a previous DM bounced (50007)
REASON_PREDICT_OFF = "predict_off"  # 🔕 Stop asking / 🚫 Stop guessing
REASON_MONITOR_OFF = "monitor_off"  # 👁 off — not watching them at all
REASON_PAUSED = "paused"  # ⏸, or ⏭ Skip this week
REASON_KIND_OFF = "kind_off"  # 🔔 this *kind* of message is switched off
REASON_QUIET = "quiet"  # inside their overnight quiet window
REASON_NO_PREDICTION = "no_prediction"  # thin data says nothing, not a guess
REASON_NOTHING_TODAY = "nothing_today"  # not their day
REASON_OUTSIDE_SLOT = "outside_slot"  # their day, wrong part of it
REASON_WASHER_BUSY = "washer_busy"  # somebody else is mid-load
REASON_ALREADY_WASHED = "already_washed"  # they've done it — nothing to nudge
REASON_ALREADY = "already"  # one nudge per slot, whichever trigger won
REASON_NOT_DUE = "not_due"  # washed within their own usual gap
REASON_BUDGET_DAY = "budget_day"  # over the daily DM cap (1/day)
REASON_BUDGET_WEEK = "budget_week"  # over the weekly DM cap (2/week)

REASONS = (
    REASON_OK,
    REASON_MOMENT,
    REASON_NOT_OPTED_IN,
    REASON_REMINDERS_OFF,
    REASON_NOT_DM,
    REASON_DM_CLOSED,
    REASON_PREDICT_OFF,
    REASON_MONITOR_OFF,
    REASON_PAUSED,
    REASON_KIND_OFF,
    REASON_QUIET,
    REASON_NO_PREDICTION,
    REASON_NOTHING_TODAY,
    REASON_OUTSIDE_SLOT,
    REASON_WASHER_BUSY,
    REASON_ALREADY_WASHED,
    REASON_ALREADY,
    REASON_NOT_DUE,
    REASON_BUDGET_DAY,
    REASON_BUDGET_WEEK,
)

# Worth a debug line: these are messages that were otherwise going to send.
# Every other reason is a routine "no" and would flood the log if logged too.
BUDGET_REASONS = (REASON_BUDGET_DAY, REASON_BUDGET_WEEK)

_BUDGET_REASONS = {
    habit.BUDGET_DAY: REASON_BUDGET_DAY,
    habit.BUDGET_WEEK: REASON_BUDGET_WEEK,
    habit.BUDGET_UNREADABLE: REASON_MOMENT,
}

# Today-relative wording ("tonight"), unlike `habit.SLOT_PHRASES` ("Thursday
# evenings"). PM and Eve stay distinct so a 16:00-20:00 booking isn't worded
# as if it were at ten.
TODAY_PHRASES = {
    plan.SLOT_AM: "this morning",
    plan.SLOT_MID: "this afternoon",
    plan.SLOT_PM: "this evening",
    plan.SLOT_EVE: "tonight",
}


# --- the clock, passed in ----------------------------------------------------
def _ts(moment) -> float | None:
    """The moment as a unix timestamp, or None if unusable. Delegates to
    `habit.moment_ts`, which also rejects a naive datetime.
    """
    return habit.moment_ts(moment)


def slot_window_ts(cell, moment) -> tuple[float, float] | None:
    """This cell's slot as `(start, end)` timestamps on the moment's own day,
    or None (assumes the weekday already matches; callers check that
    first). End is start plus the window's length, not a second `replace`
    — the Eve slot ends at hour 24, which `datetime.replace` can't express.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None or _ts(moment) is None:
        return None
    _weekday, slot = parsed
    start, end = plan.SLOT_WINDOWS[slot]
    try:
        opened = float(
            moment.replace(
                hour=start, minute=0, second=0, microsecond=0
            ).timestamp()
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return (opened, opened + (end - start) * 3600)


def slot_start_ts(cell, moment) -> float | None:
    """When this cell's slot began on the moment's own day, or None."""
    window = slot_window_ts(cell, moment)
    return window[0] if window else None


def parse_clock(value) -> tuple[int, int] | None:
    """`"18:00:00"` / `"18:00"` -> `(18, 0)`; None for anything unparseable, so
    a bad value leaves the trigger unregistered rather than firing at a
    guess.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return (hour, minute)


# --- eligibility -------------------------------------------------------------
def paused_until(person) -> float | None:
    """When this person's pause ends, or None if they aren't paused."""
    if not isinstance(person, dict):
        return None
    value = person.get("paused_until")
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_paused(person, moment) -> bool:
    """Whether ⏸ / ⏭ Skip is still in force. An unreadable moment counts as
    paused: if we can't tell whether it's expired, the safe default is
    silence.
    """
    until = paused_until(person)
    if until is None:
        return False
    now = _ts(moment)
    return now is None or now < until


def in_quiet_hours(person, moment) -> bool:
    """Whether `moment` falls in this person's overnight quiet window (from
    `people.quiet_hours`, which can wrap midnight — 22 -> 8 is two arcs, not
    one interval; the direction matters, since getting it backwards would
    silence someone all day and message them all night). An unreadable
    moment counts as quiet, same fail-safe reason as `is_paused`.
    """
    window = people.quiet_hours(person)
    if window is None:
        return False
    hour = getattr(moment, "hour", None)
    if _ts(moment) is None or not isinstance(hour, int) or isinstance(hour, bool):
        return True
    start, end = window
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def eligible(people_map, user_id, moment, kind=None) -> str:
    """Whether this person may be sent a reminder DM at all right now.
    Returns `REASON_OK` or the first gate that says no, in order: opted in,
    🚫 reminders off, channel is DM, DMs not closed, 🔕 predict on, 👁 monitor
    on, not ⏸/⏭ paused. `kind` (one of `people.KINDS`) adds two more checks
    that need to know which message this is: its 🔔 switch and quiet hours;
    omitting it asks the broader question and answers as before.
    """
    if _ts(moment) is None:
        return REASON_MOMENT
    if not people.is_known(people_map, user_id):
        return REASON_NOT_OPTED_IN
    person = people.get_person(people_map, user_id)
    if not person["onboarded"]:
        return REASON_NOT_OPTED_IN
    mode = person["reminders"]
    if mode == people.REMIND_OFF:
        return REASON_REMINDERS_OFF
    if mode != people.REMIND_DM:
        return REASON_NOT_DM
    if person["dm_ok"] is False:
        return REASON_DM_CLOSED
    if not person["predict"]:
        return REASON_PREDICT_OFF
    if not person["monitor"]:
        return REASON_MONITOR_OFF
    if is_paused(person, moment):
        return REASON_PAUSED
    # Below here depends on which message is being weighed; no kind means the
    # broader question, answered as before.
    if kind is None:
        return REASON_OK
    if not people.wants_kind(person, kind):
        return REASON_KIND_OFF
    if in_quiet_hours(person, moment):
        return REASON_QUIET
    return REASON_OK


# --- what somebody is down for today ----------------------------------------
def is_booked(booked, cell) -> bool:
    """Whether this cell is one the person actually booked, as opposed to
    guessed. Only the DM wording turns on this.
    """
    key = plan.normalise_cell(cell)
    if key is None or not isinstance(booked, (list, tuple, set, frozenset)):
        return False
    return any(plan.normalise_cell(item) == key for item in booked)


def in_slot_now(cell, moment) -> bool:
    """Whether `moment` falls inside this cell's own window, today. See
    `slot_ended` for the same question asked from the other side (and across
    days).
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return False
    weekday, slot = parsed
    if plan.weekday_of(moment) != weekday:
        return False
    return plan.slot_for_hour(getattr(moment, "hour", None)) == slot


def day_start_ts(moment) -> float | None:
    """Local midnight at the start of the moment's own day, or None."""
    if _ts(moment) is None:
        return None
    try:
        return float(
            moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def washed_today(loads, moment) -> bool:
    """Whether this person already ran a load today (the washer freeing is
    often their own load finishing). Whole day, not just the slot: a 4-5
    hour cycle started in the afternoon often finishes in the evening.
    """
    start = day_start_ts(moment)
    now = _ts(moment)
    if start is None or now is None or not isinstance(loads, (list, tuple, set)):
        return False
    for value in loads:
        try:
            ts = float(value)
        except (TypeError, ValueError):
            continue
        if start <= ts <= now:
            return True
    return False


def already_nudged_in_slot(budgets, user_id, cell, moment, lead_minutes=0) -> bool:
    """Whether a DM about this slot already went to this person — one
    message per slot, not per day. The window starts at the lead, not the
    slot's own start, since a heads-up fires *before* its slot opens and
    both its triggers land before the slot starts. Bounded at both ends.
    """
    last = habit.budget_for(budgets, user_id)["last_nudge_ts"]
    window = slot_window_ts(cell, moment)
    if last is None or window is None:
        return False
    try:
        lead = max(0.0, float(lead_minutes) * 60)
    except (TypeError, ValueError):
        lead = 0.0
    return (window[0] - lead) <= last < window[1]


# --- the heads-up and the opportunity -----------------------------------------
# Minutes before a booked slot starts that its heads-up goes out. An hour
# leaves time to act without being "news too early to act on."
HEADS_UP_LEAD_MINUTES = 60

# At most one message is ever chosen, so triggers can't race into two DMs.
MSG_NONE = "none"
# Same string as the `people.KINDS` switch that gates it, so `select` can
# hand the kind straight to `eligible` with no lookup table to fall out of
# sync.
MSG_SLOT = people.KIND_SLOT  # booked, starts within the hour
MSG_OPPORTUNITY = people.KIND_OPPORTUNITY  # overdue, slot is clear
MESSAGES = (MSG_NONE, MSG_SLOT, MSG_OPPORTUNITY)


def slot_soon(booked, moment, lead_minutes=HEADS_UP_LEAD_MINUTES) -> str | None:
    """A cell of theirs whose window opens within `lead_minutes`, or None.
    Forward-looking only (an already-open slot isn't "soon"); ties go to the
    earliest slot.
    """
    now = _ts(moment)
    if now is None:
        return None
    try:
        lead = float(lead_minutes) * 60
    except (TypeError, ValueError):
        return None
    if lead <= 0:
        return None
    today = plan.weekday_of(moment)
    best: tuple[float, str] | None = None
    for item in booked or ():
        cell = plan.normalise_cell(item)
        parsed = plan.parse_cell(cell) if cell else None
        if parsed is None or parsed[0] != today:
            continue
        start = slot_start_ts(cell, moment)
        if start is None or start <= now or start - now > lead:
            continue
        if best is None or start < best[0]:
            best = (start, cell)
    return best[1] if best else None


def minutes_until_slot(cell, moment) -> int | None:
    """How many whole minutes until this cell's slot opens, or None. Rounded
    up ("about 1 minute" is the smallest result, never 0); None (already
    open, or unreadable) renders as the unquantified "soon" instead.
    """
    start = slot_start_ts(cell, moment)
    now = _ts(moment)
    if start is None or now is None or start <= now:
        return None
    return int(-(-(start - now) // 60))


def slot_ended(cell, moment) -> bool:
    """Whether this cell's window has already closed, relative to `moment`.
    True for a cell on another weekday too (yesterday's slot is over
    regardless of the current time). Unreadable input is *not* treated as
    ended.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    now = _ts(moment)
    if parsed is None or now is None:
        return False
    if plan.weekday_of(moment) != parsed[0]:
        return True
    window = slot_window_ts(cell, moment)
    return window is not None and now >= window[1]


def heads_up_clock(slot, lead_minutes=HEADS_UP_LEAD_MINUTES) -> tuple[int, int] | None:
    """The wall-clock time the heads-up fires for one slot, or None:
    `lead_minutes` before that slot's own start, clamped to stay on the
    same day (the earliest slot opens at 06:00).
    """
    if not plan.is_slot(slot):
        return None
    try:
        lead = int(lead_minutes)
    except (TypeError, ValueError):
        return None
    start = plan.SLOT_WINDOWS[slot][0] * 60
    lead = max(1, min(lead, start))
    minutes = start - lead
    return (minutes // 60, minutes % 60)


def opportunity_cell(
    prediction, occupancy, moment, lead_minutes=HEADS_UP_LEAD_MINUTES
) -> str | None:
    """The cell an opportunity nudge would be about, or None: the person's
    predicted usual slot, but only when nobody (including the recipient) has
    it and it opens within `lead_minutes`. "Nobody" means `plan.is_taken`,
    not `is_taken_by_other` — the DM says "nobody's booked <slot>" even to
    someone whose own booking may sit on that exact cell, so it must be
    literally true.
    """
    cell = plan.normalise_cell(
        prediction.get("cell") if isinstance(prediction, dict) else None
    )
    if cell is None:
        return None
    parsed = plan.parse_cell(cell)
    if parsed is None or parsed[0] != plan.weekday_of(moment):
        return None
    start = slot_start_ts(cell, moment)
    now = _ts(moment)
    if start is None or now is None:
        return None
    window = slot_window_ts(cell, moment)
    # Open now, or opening within the lead — the lower bound matters: without
    # it, a slot would qualify from midnight onward instead of only shortly
    # before it opens.
    if now >= (window[1] if window else start):
        return None
    try:
        lead = max(0.0, float(lead_minutes) * 60)
    except (TypeError, ValueError):
        lead = HEADS_UP_LEAD_MINUTES * 60
    if now < start - lead:
        return None
    if plan.is_taken(occupancy or {}, cell):
        return None
    return cell


def select(
    people_map,
    budgets,
    user_id,
    moment,
    *,
    booked=(),
    prediction=None,
    occupancy=None,
    washer_free=True,
    due=False,
    loads=(),
    just_washed=False,
    lead_minutes=HEADS_UP_LEAD_MINUTES,
) -> tuple[str, str | None, str]:
    """The one thing worth saying to this person right now, if anything.
    Returns `(kind, cell, reason)`; `kind` is `MSG_NONE` unless `reason` is
    `REASON_OK` (the common, correct answer). Order, each step dropping
    rather than queuing: `eligible` -> washer busy -> already washed/just
    washed -> a due booking, else an opportunity if `due` -> the chosen
    kind's switch and quiet hours -> already nudged this slot -> budget. A
    booking always beats a guess.
    """
    verdict = eligible(people_map, user_id, moment)
    if verdict != REASON_OK:
        return (MSG_NONE, None, verdict)
    if not washer_free:
        return (MSG_NONE, None, REASON_WASHER_BUSY)
    if just_washed or washed_today(loads, moment):
        return (MSG_NONE, None, REASON_ALREADY_WASHED)

    cell = slot_soon(booked, moment, lead_minutes)
    kind = MSG_SLOT
    if cell is None:
        # No booking coming up. An opportunity needs them overdue by their
        # own learned cadence, not a fixed number of days.
        if not due:
            return (MSG_NONE, None, REASON_NOT_DUE)
        cell = opportunity_cell(prediction, occupancy, moment, lead_minutes)
        kind = MSG_OPPORTUNITY
    if cell is None:
        return (MSG_NONE, None, REASON_NO_PREDICTION)
    # The 🔔 gate goes here, not in the call at the top, since there was no
    # kind to check until now. A refusal here does not fall through to the
    # other kind — these switches only ever subtract.
    verdict = eligible(people_map, user_id, moment, kind=kind)
    if verdict != REASON_OK:
        return (MSG_NONE, None, verdict)
    if already_nudged_in_slot(budgets, user_id, cell, moment, lead_minutes):
        return (MSG_NONE, None, REASON_ALREADY)
    spend = _budget_verdict(budgets, user_id, moment)
    if spend != REASON_OK:
        return (MSG_NONE, None, spend)
    return (kind, cell, REASON_OK)


def claim_select(people_map, budgets, user_id, moment, **kwargs):
    """`select`, then spends the budget. Returns `(kind, cell, reason,
    budgets)`. Charged before sending, same reasoning as `claim_plan_dm`.
    """
    kind, cell, reason = select(people_map, budgets, user_id, moment, **kwargs)
    spent, updated = _claim(reason, budgets, user_id, moment)
    if spent != REASON_OK:
        return (MSG_NONE, None, spent, updated)
    return (kind, cell, REASON_OK, updated)


# --- the two decisions -------------------------------------------------------
def plan_dm(people_map, budgets, user_id, prediction, moment) -> str:
    """Whether to send the Sunday plan DM (`REASON_OK` to send). No
    confident prediction means no DM at all, never "I don't know your days
    yet". Eligibility (📅 switch, quiet hours) is checked before reading the
    prediction, so an opted-out person costs no history scan.
    """
    verdict = eligible(people_map, user_id, moment, kind=people.KIND_CHECKIN)
    if verdict != REASON_OK:
        return verdict
    if not isinstance(prediction, dict) or not plan.normalise_cell(
        prediction.get("cell")
    ):
        return REASON_NO_PREDICTION
    return _budget_verdict(budgets, user_id, moment)


def _budget_verdict(budgets, user_id, moment) -> str:
    """The nudge budget's answer, as one of `REASONS`."""
    verdict = habit.check_nudge(habit.budget_for(budgets, user_id), moment)
    if verdict == habit.BUDGET_OK:
        return REASON_OK
    return _BUDGET_REASONS.get(verdict, REASON_MOMENT)


def claim_plan_dm(people_map, budgets, user_id, prediction, moment):
    """Decide and spend in one call. Returns `(reason, new_budgets)`. Claimed
    before the send, so a bounced DM still costs the budget and a
    closed-DM person isn't retried every trigger. Over budget is dropped,
    never queued.
    """
    return _claim(plan_dm(people_map, budgets, user_id, prediction, moment),
                  budgets, user_id, moment)


def _claim(verdict, budgets, user_id, moment):
    """Spend one nudge iff `verdict` is OK, via `habit.claim_nudge_for` (which
    re-checks the budget atomically). `REASON_OK` here means already charged.
    """
    if verdict != REASON_OK:
        return (verdict, habit.normalise_budgets(budgets))
    allowed, updated = habit.claim_nudge_for(budgets, user_id, moment)
    if allowed:
        return (REASON_OK, updated)
    return (_budget_verdict(budgets, user_id, moment), updated)


# --- what the DMs actually say ------------------------------------------------
def plan_dm_text(prediction) -> str | None:
    """The Sunday DM's body, or None when there's nothing to say. Wording
    comes from `habit.describe_prediction` / `habit.explain`, the same as the
    🔮 panel.
    """
    where = habit.describe_prediction(prediction)
    if where is None:
        return None
    why = habit.explain(prediction)
    line = f"I've got you down for **{where}**"
    line += f" — {why}." if why else "."
    return (
        "🗓️ **Next week's laundry**\n"
        f"{line}\nLook right?"
    )


def heads_up_text(cell, minutes=None) -> str | None:
    """The slot heads-up's body, or None when the cell isn't renderable.
    Worded as a question about the booking ("Still want it?"), not an
    instruction — giving the slot back is the most useful reply.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    _weekday, slot = parsed
    when = TODAY_PHRASES[slot]
    try:
        soon = int(minutes) if minutes is not None else None
    except (TypeError, ValueError):
        soon = None
    lead = f"in about {soon} minutes" if soon and soon > 0 else "soon"
    return (
        f"🧺 **You're down for {when}**\n"
        f"Your slot starts {lead} and the washer's free. Still want it?"
    )


def slot_taken_targets(running, occupancy, claimant_id, waiting=()) -> dict[str, str]:
    """Who to tell that someone else is using the washer in their booked slot.

    ``{holder_id: cell}`` for every holder of a cell the live load occupies,
    except the claimant and anyone already in the 🔜 line. A holder of several
    such cells is told about the first. Ids are strings throughout.
    """
    claimant = None if claimant_id is None else str(claimant_id)
    skip = {str(item) for item in waiting if item is not None}
    targets: dict[str, str] = {}
    for cell in plan.running_cells(running):
        for holder in plan.holders(occupancy, cell):
            if holder == claimant or holder in skip or holder in targets:
                continue
            targets[holder] = cell
    return targets


def taken_text(cell) -> str | None:
    """The slot-taken DM's body, or None when the cell isn't renderable.
    Never says who is using the washer: plans stay anonymous.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    _weekday, slot = parsed
    return (
        "🏃 **Someone else got to the washer first**\n"
        f"It's in use during the slot you booked for {TODAY_PHRASES[slot]}. "
        "Want me to put you next in line?"
    )


def opportunity_text(cell, prediction=None, gap_days=None) -> str | None:
    """The opportunity nudge's body, or None when the cell isn't renderable.
    States only what the person can't see themselves: the machine is free
    and nobody's booked their usual slot. `gap_days` is their own cadence,
    quoted back so the timing is arguable, not mysterious.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    _weekday, slot = parsed
    when = TODAY_PHRASES[slot]
    why = habit.explain(prediction) if isinstance(prediction, dict) else None
    since = ""
    try:
        days = int(round(float(gap_days))) if gap_days is not None else None
    except (TypeError, ValueError):
        days = None
    if days and days > 0:
        since = f" It's been about {days} day{'s' if days != 1 else ''}."
    detail = f" ({why})" if why else ""
    return (
        f"🧺 **{when.capitalize()} is wide open**\n"
        f"Nobody's booked {when} and the washer's free{detail}.{since}"
    )
