"""Pure, dependency-free decisions for the reminder DMs (design doc §10).

Kept free of Home Assistant / discord imports so the one question this phase
turns on — *may this person be messaged, about this slot, right now?* — can be
unit-tested without the HA test harness, the same discipline that made
:mod:`detect`, :mod:`queue`, :mod:`people`, :mod:`plan` and :mod:`habit`
reliable. :mod:`reminders` owns the scheduling and :mod:`assistant` owns the
``Store`` and every Discord call; this module only decides, and writes the
sentence that gets sent.

This is the **first code in the integration that contacts a person on its own
schedule**, so the gates are all here, in one readable list, rather than spread
across an HA callback where a stray ``return`` is invisible:

* **Nothing is opt-out.** A reminder DM goes only to somebody whose delivery
  preference is a DM — which is a thing they had to tap 📬 in the 🤖 panel to
  choose, since :data:`people.DEFAULT_REMINDERS` is the channel. Somebody who
  has never opened the panel is not "unset", they are *not opted in*, and they
  get nothing (design doc P1).
* **Every preference gates independently**, and each has its own reason string,
  so "why was nothing sent" is answerable without a log line per evaluation.
* **The budget is claimed, never merely checked** (P2). :func:`claim_plan_dm`
  and :func:`claim_day_nudge` return the *new* accounting alongside the verdict,
  exactly as :func:`habit.claim_nudge_for` does and for the same reason: two
  calls is one early ``return`` away from a bot that checks the budget and then
  sends anyway. The caller persists what comes back **before** it sends, which
  is what makes a DM that bounces still cost a nudge — otherwise somebody with
  closed DMs is retried at every single trigger, forever.
* **Silence is the default (P6).** No confident prediction and no booking means
  no message at all — not "I don't know your days yet", which is a notification
  that tells you nothing and trains you to ignore the next one.
* **The caller passes the clock.** Nothing here reads the time, exactly like
  :mod:`habit` and :mod:`detect`, which is what makes "the slot had already
  ended" a test rather than something you discover at 11pm.
* **Nothing is mutated.** Every function returns new data, so a rejected store
  write can't half-apply.

The moment passed in must be **timezone-aware local time** (HA's
``dt_util.now()``): a slot is a household wall-clock fact, and :mod:`habit`
rejects a naive datetime outright rather than guessing which evening it meant.
"""

from __future__ import annotations

try:  # the normal path — sibling modules inside the integration package
    from . import habit
    from . import people
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path, as the tests do
    # There is no package when this is exec'd from a file path, which is how the
    # pure suite runs it. The budget, the slot windows and the preference record
    # are *not* reimplemented here: a second copy of any of them is how the
    # reminder loop and the panel start disagreeing about what somebody chose.
    import habit  # type: ignore[no-redef]
    import people  # type: ignore[no-redef]
    import plan  # type: ignore[no-redef]

# --- why a reminder was or wasn't sent ---------------------------------------
# One reason per gate, and they are deliberately distinguishable: "they turned
# predictions off" and "they're over budget" are the same silence to the user
# and completely different answers to "is this working".
REASON_OK = "ok"
REASON_MOMENT = "moment"  # an unreadable clock — never send on a guess
REASON_NOT_OPTED_IN = "not_opted_in"  # no record, or never answered the panel
REASON_REMINDERS_OFF = "reminders_off"  # 🚫 in the panel
REASON_NOT_DM = "not_dm"  # they chose the channel; see the module docstring
REASON_DM_CLOSED = "dm_closed"  # a previous DM bounced (50007)
REASON_PREDICT_OFF = "predict_off"  # 🔕 Stop asking / 🚫 Stop guessing
REASON_MONITOR_OFF = "monitor_off"  # 👁 off — we aren't watching them at all
REASON_PAUSED = "paused"  # ⏸, or ⏭ Skip this week
REASON_NO_PREDICTION = "no_prediction"  # P6: thin data says nothing
REASON_NOTHING_TODAY = "nothing_today"  # not their day
REASON_OUTSIDE_SLOT = "outside_slot"  # their day, wrong part of it
REASON_WASHER_BUSY = "washer_busy"  # somebody else is mid-load
REASON_ALREADY_WASHED = "already_washed"  # they've done it — nothing to nudge
REASON_ALREADY = "already"  # one nudge per slot, whichever trigger won
REASON_BUDGET_DAY = "budget_day"
REASON_BUDGET_WEEK = "budget_week"

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
    REASON_NO_PREDICTION,
    REASON_NOTHING_TODAY,
    REASON_OUTSIDE_SLOT,
    REASON_WASHER_BUSY,
    REASON_ALREADY_WASHED,
    REASON_ALREADY,
    REASON_BUDGET_DAY,
    REASON_BUDGET_WEEK,
)

# The two reasons worth a debug line, because they are decisions taken about a
# message that was otherwise going to be sent. Everything else in REASONS is an
# evaluation that concluded "no", and logging those would put a line per person
# per trigger in the log of a household that has opted into nothing.
BUDGET_REASONS = (REASON_BUDGET_DAY, REASON_BUDGET_WEEK)

_BUDGET_REASONS = {
    habit.BUDGET_DAY: REASON_BUDGET_DAY,
    habit.BUDGET_WEEK: REASON_BUDGET_WEEK,
    habit.BUDGET_UNREADABLE: REASON_MOMENT,
}

# Second person and *today*-relative, because that is the only day this message
# is ever about. Deliberately not :data:`habit.SLOT_PHRASES`, which says
# "Thursday evenings" — a habit — where this says "tonight". PM and Eve stay
# distinguishable ("this evening" vs "tonight") so somebody down for 16:00-20:00
# isn't told to wash at ten.
TODAY_PHRASES = {
    plan.SLOT_AM: "this morning",
    plan.SLOT_MID: "this afternoon",
    plan.SLOT_PM: "this evening",
    plan.SLOT_EVE: "tonight",
}


# --- the clock, passed in ----------------------------------------------------
def _ts(moment) -> float | None:
    """The moment as a unix timestamp, or None if it isn't a usable moment.

    :func:`habit.moment_ts` rather than a second implementation, so a naive
    datetime is refused here for exactly the reason it is refused there.
    """
    return habit.moment_ts(moment)


def slot_window_ts(cell, moment) -> tuple[float, float] | None:
    """This cell's slot as ``(start, end)`` timestamps **on the moment's own
    day**, or None.

    Only meaningful once the cell's weekday is known to be the moment's — every
    caller checks that first. It exists to answer "have we already messaged them
    inside this window", which is what makes §10.4's *whichever comes first*
    structural rather than a side effect of the day cap happening to be 1.

    The end is the start plus the window's length rather than another
    ``replace``: the Eve slot ends at hour 24, which is not an hour a datetime
    has. Adding the span also keeps the two ends of one window consistent
    through a DST shift, where the arithmetic can be an hour out but is never
    inside-out.
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


def fallback_clock(slot, lead_minutes) -> tuple[int, int] | None:
    """The wall-clock time the day-of backstop fires for one slot, or None.

    §10.4's trigger (b): "a fallback time near the end of that slot". *That*
    slot — so this is one time per slot, derived from the slot's own end and a
    single configured lead, rather than one clock time for the whole day. A
    single 18:00 reminder cannot be near the end of a slot that ended at noon,
    and the person who washes on Saturday mornings is exactly the person a fixed
    evening reminder is useless to.

    The lead is clamped to the shortest slot (4 h) so the answer always lands
    *inside* the window it belongs to — which is what lets :func:`day_nudge`
    apply one rule to both triggers instead of special-casing this one.
    """
    if not plan.is_slot(slot):
        return None
    try:
        lead = int(lead_minutes)
    except (TypeError, ValueError):
        return None
    start, end = plan.SLOT_WINDOWS[slot]
    span = (end - start) * 60
    lead = max(1, min(lead, span - 1))
    minutes = end * 60 - lead
    return (minutes // 60, minutes % 60)


def parse_clock(value) -> tuple[int, int] | None:
    """``"18:00:00"`` / ``"18:00"`` -> ``(18, 0)``; None for anything else.

    HA's time selector stores ``"HH:MM:SS"``; a hand-edited options file can
    hold anything at all, and an unparseable one must leave the trigger
    unregistered rather than registering it at some invented hour.
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
    """Whether ⏸ / ⏭ Skip is still in force at this moment.

    An unreadable moment counts as paused: if we can't tell what time it is we
    can't tell whether a pause has expired, and the safe direction for a message
    somebody asked us to stop sending is always "keep quiet".
    """
    until = paused_until(person)
    if until is None:
        return False
    now = _ts(moment)
    return now is None or now < until


def eligible(people_map, user_id, moment) -> str:
    """Whether this person may be sent a reminder DM at all.

    Returns :data:`REASON_OK` or the **first** gate that said no. Order is
    cheapest-and-most-common first, and every one of these is somebody's
    explicit choice rather than a heuristic:

    * **Not opted in** — no record, or a record that never answered the panel.
      Enrolment is free (§10.1) but it is still enrolment.
    * **🚫 No pings** — off means off, everywhere.
    * **The channel** — the panel's default and the pre-assistant behaviour. A
      *reminder* is not a handoff: broadcasting "you're down for tonight" to six
      other people is both the channel noise §1 exists to prevent and a per-
      person fact §11 says is never surfaced to the household. So the channel
      preference means no reminder, not a reminder in the channel.
    * **DMs closed** — a previous send raised ``Forbidden`` (50007). They stop
      being tried; the 🤖 panel is what tells them why (§10.5).
    * **🔕 Stop asking / 🚫 Stop guessing** — ``predict`` off is the permanent
      opt-out from this whole feature, which is why "Stop asking" sets it.
    * **👁 Monitoring off** — the bot isn't watching them, so it has nothing
      honest to say about their days.
    * **⏸ Paused / ⏭ Skip this week** — quiet until the timestamp passes.
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
    return REASON_OK


# --- what somebody is down for today ----------------------------------------
def is_booked(booked, cell) -> bool:
    """Whether this cell is one the person actually booked, as opposed to guessed.

    Only the wording turns on it (§10.3 quotes a booking back as theirs and a
    guess as a guess), but the distinction is P4's: the bot must never present
    its own arithmetic as something you said.
    """
    key = plan.normalise_cell(cell)
    if key is None or not isinstance(booked, (list, tuple, set, frozenset)):
        return False
    return any(plan.normalise_cell(item) == key for item in booked)


def current_cell(booked, prediction, moment) -> str | None:
    """The cell this person is down for **right now**, or None.

    "Right now" rather than "today" on purpose: a nudge is only ever sent inside
    the window it is about (§10.4), so the only cell that can matter at this
    moment is this moment's own. Asking the narrower question here also means
    somebody down for both Saturday morning and Saturday night gets the right
    one of the two rather than whichever sorted first.

    Two sources, in the same precedence :func:`plan.cell_state` draws them in:

    1. **A booking** — they tapped a slot on their own week. A fact, and how ⏭
       "push to tomorrow" moves a nudge: the push books tomorrow's slot, so
       tomorrow that booking is what they are down for.
    2. **The prediction** — the model's guess at their usual days, and only if
       it cleared the §7.2 confidence gate, which is the caller's business
       (:func:`habit.predict` returns None otherwise).

    None — nothing on right now — is the overwhelmingly common answer, and it is
    the answer that sends nothing at all.
    """
    if _ts(moment) is None:
        return None
    cell = plan.cell_key(
        plan.weekday_of(moment), plan.slot_for_hour(getattr(moment, "hour", None))
    )
    if cell is None:
        return None
    if is_booked(booked, cell):
        return cell
    if isinstance(prediction, dict) and plan.normalise_cell(
        prediction.get("cell")
    ) == cell:
        return cell
    return None


def in_slot_now(cell, moment) -> bool:
    """Whether ``moment`` falls inside this cell's own window, today.

    One rule for both of §10.4's triggers. The washer-freeing path has to check
    it (the machine coming free at 2pm says nothing about somebody's evening),
    and the backstop path passes it by construction because
    :func:`fallback_clock` puts the time inside the window it belongs to.
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
    """Whether this person has already run a load today.

    The one false positive this feature would otherwise produce constantly, and
    the most annoying one available: the washer coming free is very often the
    person's *own* load finishing, and "🧺 Laundry day — you're down for tonight
    and the washer's free" arriving the moment somebody folds their washing is
    exactly how a reminder stops being read.

    Deliberately the whole day rather than the slot. A cycle is 4-5 hours, so a
    load claimed in the afternoon routinely finishes in the evening — checking
    only the slot the nudge is about would miss precisely the case that fires
    most. Somebody who genuinely wants a second load that day does not need to
    be told the machine exists.

    ``loads`` is this person's own claim timestamps and nobody else's, which is
    all :func:`habit.history_for` will hand out anyway.
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


def already_nudged_in_slot(budgets, user_id, cell, moment) -> bool:
    """Whether a DM already went to this person inside this slot's window.

    This is §10.4's *whichever comes first*, made structural. The day cap alone
    would deliver the same behaviour today — it is 1 — but only by coincidence:
    raise :data:`habit.MAX_NUDGES_PER_DAY` to 2 and the washer freeing at 20:30
    plus the backstop at 23:00 would be two DMs about one evening. The rule is
    "one nudge per slot", so that is what is checked.

    Bounded at *both* ends of the window, not just the start. In practice a
    trigger only ever asks about the slot that is running, so a nudge sent after
    the window cannot exist — but a function whose answer is only correct
    because of how its callers are ordered is one refactor from being wrong, and
    the wrong direction here is silently suppressing somebody's nudge.
    """
    last = habit.budget_for(budgets, user_id)["last_nudge_ts"]
    window = slot_window_ts(cell, moment)
    if last is None or window is None:
        return False
    return window[0] <= last < window[1]


# --- the two decisions -------------------------------------------------------
def plan_dm(people_map, budgets, user_id, prediction, moment) -> str:
    """Whether to send the Sunday plan DM (§10.2). :data:`REASON_OK` to send.

    **No confident prediction means no DM at all** (P6). The alternative — "I
    don't know your days yet" — is a push notification whose entire content is
    that the bot has nothing to say, and the first month is nothing but those.
    """
    verdict = eligible(people_map, user_id, moment)
    if verdict != REASON_OK:
        return verdict
    if not isinstance(prediction, dict) or not plan.normalise_cell(
        prediction.get("cell")
    ):
        return REASON_NO_PREDICTION
    return _budget_verdict(budgets, user_id, moment)


def day_nudge(
    people_map,
    budgets,
    user_id,
    cell,
    moment,
    *,
    washer_free,
    loads=(),
    just_washed=False,
) -> str:
    """Whether to send the day-of nudge (§10.3). :data:`REASON_OK` to send.

    Called at both of §10.4's triggers with the same arguments, because they
    are the same decision asked at two different moments — the washer coming
    free during the slot, and the backstop near the slot's end. ``washer_free``
    is the caller's reading of the machine: the message says "the washer's free
    right now", so it is only ever sent when that is true. ``loads`` is this
    person's own claim history, and it is what stops the bot telling somebody to
    do the laundry they have just finished (see :func:`washed_today`).

    ``just_washed`` is the same guard from the other direction, and it exists
    because ``loads`` cannot be trusted to be there: history is written only
    when the *house* has day-learning on and the person has 👁 Monitoring on,
    and it is written only for a load somebody actually tapped Claim on. The
    washer freeing is very often the load of the person about to be nudged, and
    the caller knows whose it was — so it says so, rather than hoping the
    history happens to exist.
    """
    verdict = eligible(people_map, user_id, moment)
    if verdict != REASON_OK:
        return verdict
    if plan.normalise_cell(cell) is None:
        return REASON_NOTHING_TODAY
    if not in_slot_now(cell, moment):
        return REASON_OUTSIDE_SLOT
    if just_washed or washed_today(loads, moment):
        return REASON_ALREADY_WASHED
    if not washer_free:
        return REASON_WASHER_BUSY
    if already_nudged_in_slot(budgets, user_id, cell, moment):
        return REASON_ALREADY
    return _budget_verdict(budgets, user_id, moment)


def _budget_verdict(budgets, user_id, moment) -> str:
    """The nudge budget's answer, as one of :data:`REASONS`."""
    verdict = habit.check_nudge(habit.budget_for(budgets, user_id), moment)
    if verdict == habit.BUDGET_OK:
        return REASON_OK
    return _BUDGET_REASONS.get(verdict, REASON_MOMENT)


def claim_plan_dm(people_map, budgets, user_id, prediction, moment):
    """Decide *and* spend, in one call. Returns ``(reason, new_budgets)``.

    The budget is claimed here rather than after a successful send, and that
    ordering is the whole point: a DM to somebody whose privacy settings refuse
    it raises ``Forbidden`` and is gone, and if the bounce refunded the nudge
    they would be retried at every trigger forever. Over budget is **dropped,
    not queued** (P2) — there is no pending list here, in :mod:`habit`, or
    anywhere else.
    """
    return _claim(plan_dm(people_map, budgets, user_id, prediction, moment),
                  budgets, user_id, moment)


def claim_day_nudge(
    people_map,
    budgets,
    user_id,
    cell,
    moment,
    *,
    washer_free,
    loads=(),
    just_washed=False,
):
    """:func:`day_nudge`, spending the budget. Returns ``(reason, new_budgets)``."""
    return _claim(
        day_nudge(
            people_map,
            budgets,
            user_id,
            cell,
            moment,
            washer_free=washer_free,
            loads=loads,
            just_washed=just_washed,
        ),
        budgets,
        user_id,
        moment,
    )


def _claim(verdict, budgets, user_id, moment):
    """Spend one nudge iff the verdict was OK, via :func:`habit.claim_nudge_for`.

    The claim re-checks the budget atomically, so :data:`REASON_OK` out of here
    always means "the accounting in the returned mapping has already been
    charged for this message".
    """
    if verdict != REASON_OK:
        return (verdict, habit.normalise_budgets(budgets))
    allowed, updated = habit.claim_nudge_for(budgets, user_id, moment)
    if allowed:
        return (REASON_OK, updated)
    return (_budget_verdict(budgets, user_id, moment), updated)


# --- what the DMs actually say (§10.2 / §10.3) -------------------------------
def plan_dm_text(prediction) -> str | None:
    """The Sunday DM's body, or None when there is nothing to say.

    The wording is :func:`habit.describe_prediction` and :func:`habit.explain` —
    the same two functions the 🔮 panel uses — so the sentence somebody reads in
    a DM cannot drift from the one they see when they go to argue with it.
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


def day_nudge_text(cell, prediction=None) -> str | None:
    """The day-of DM's body, or None when the cell isn't renderable.

    Two shapes for the two things a cell can be. A booking is quoted back as
    theirs; a guess is quoted as a guess, with the arithmetic behind it, because
    P4 says the model's guesses are visible and arguable — including in the one
    message that acts on them.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    _weekday, slot = parsed
    when = TODAY_PHRASES[slot]
    guess = (
        isinstance(prediction, dict)
        and plan.normalise_cell(prediction.get("cell")) == plan.normalise_cell(cell)
    )
    why = habit.explain(prediction) if guess else None
    if guess:
        lead = f"I've got you down for **{when}**"
        if why:
            lead += f" ({why})"
    else:
        lead = f"You're down for **{when}**"
    return f"🧺 **Laundry day**\n{lead}, and the washer's free right now."
