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
* **Two of those gates have to know which message it is.** The 🔔 per-kind
  switches and the quiet window are facts about *this message*, not about the
  person, so :func:`eligible` applies them only when it is handed a kind — and
  :func:`select` cannot hand it one until it has chosen between the heads-up and
  the opportunity. Every caller that predates them, the reminder loop's own
  cheap pre-filter included, keeps exactly the answer it always had.
* **The budget is claimed, never merely checked** (P2). :func:`claim_plan_dm`
  and :func:`claim_select` return the *new* accounting alongside the verdict,
  exactly as :func:`habit.claim_nudge_for` does and for the same reason: two
  calls is one early ``return`` away from a bot that checks the budget and then
  sends anyway. The caller persists what comes back **before** it sends, which
  is what makes a DM that bounces still cost a nudge — otherwise somebody with
  closed DMs is retried at every single trigger, forever.
* **Silence is the default (P6).** No confident prediction and no booking means
  no message at all — not "I don't know your days yet", which is a notification
  that tells you nothing and trains you to ignore the next one.
* **At most one message, chosen for the moment.** :func:`select` picks between
  the slot heads-up and the opportunity, or picks nothing, so the triggers
  cannot race each other into four separate DMs about one evening. The bot may
  only speak when *its own private information is the point*: it knows the
  washer is free and that nobody has booked your usual slot; it does not know
  whether you have dirty clothes, and it never assumes.
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
REASON_KIND_OFF = "kind_off"  # 🔔 this *kind* of message is switched off
REASON_QUIET = "quiet"  # inside their overnight quiet window
REASON_NO_PREDICTION = "no_prediction"  # P6: thin data says nothing
REASON_NOTHING_TODAY = "nothing_today"  # not their day
REASON_OUTSIDE_SLOT = "outside_slot"  # their day, wrong part of it
REASON_WASHER_BUSY = "washer_busy"  # somebody else is mid-load
REASON_ALREADY_WASHED = "already_washed"  # they've done it — nothing to nudge
REASON_ALREADY = "already"  # one nudge per slot, whichever trigger won
REASON_NOT_DUE = "not_due"  # they washed inside their own usual gap
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


def in_quiet_hours(person, moment) -> bool:
    """Whether ``moment`` falls inside this person's overnight quiet window.

    **The window wraps midnight**, and that is the one bit of arithmetic in this
    file worth its own test. ``22 -> 8`` is not an interval on the number line,
    it is two arcs of a clock face: everything from 22:00 to the end of the day
    *and* everything from midnight to 08:00. So the containment test flips
    depending on which end is larger, and getting it backwards would silence
    somebody all day and message them all night — the exact inverse of what they
    asked for, which is a bug that reads as malice.

    Read through :func:`people.quiet_hours`, so "they have no window", "they have
    half a window" and "both ends are the same hour" are decided in one place
    rather than here as well (the way :func:`is_paused` is the one definition of
    paused for this module and :mod:`trade` alike).

    An unreadable moment counts as quiet, and only ever for somebody who *set* a
    window: :func:`is_paused` takes the same direction for the same reason — if
    we cannot tell what time it is, the safe answer about a message somebody
    asked us to hold is to hold it. Nobody without a window is affected, so one
    bad datetime cannot silence the house.
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

    ``kind`` is one of :data:`people.KINDS`, and passing one adds the two gates
    that can only be answered once you know *which* message this is: the 🔔
    switch for that kind, and their quiet hours. Leaving it out asks the older,
    broader question — "may this person be messaged at all" — and gets exactly
    the answer it always did, which is what lets the reminder loop keep using
    this as a cheap pre-filter before it knows what it would say.
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
    # Everything above is a fact about the person; everything below depends on
    # which message is being weighed. A caller that names no kind is asking the
    # question this function has always answered, so it gets the old answer
    # untouched — no existing caller can start suppressing anything by upgrading.
    if kind is None:
        return REASON_OK
    if not people.wants_kind(person, kind):
        return REASON_KIND_OFF
    if in_quiet_hours(person, moment):
        return REASON_QUIET
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


def in_slot_now(cell, moment) -> bool:
    """Whether ``moment`` falls inside this cell's own window, today.

    What a *reply* is checked against: a tap on a DM has to land while the slot
    it was about is still running, or there is nothing useful left to write.
    :func:`slot_ended` is the same question asked from the other side, and it
    also answers it for a cell on another day.
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


def already_nudged_in_slot(budgets, user_id, cell, moment, lead_minutes=0) -> bool:
    """Whether a DM about this slot already went to this person.

    §10.4's *whichever comes first*, made structural. The day cap alone would
    deliver the same behaviour today — it is 1 — but only by coincidence: raise
    :data:`habit.MAX_NUDGES_PER_DAY` to 2 and two triggers an hour apart would
    be two DMs about one evening. The rule is "one message per slot", so that is
    what is checked.

    **The window starts at the lead, not at the slot.** A heads-up is sent
    *before* its slot opens, so measuring from the slot's own start would put
    the message outside the window it was about, and the second trigger would
    find nothing and send again. That is not a hypothetical: the two triggers
    for an evening slot are the 19:00 tick and a washer freeing at 19:40, and
    both land before 20:00.

    Bounded at *both* ends, not just the start. In practice a trigger only ever
    asks about a slot that is running or about to, so a message sent after the
    window cannot exist — but a function whose answer is only correct because of
    how its callers are ordered is one refactor from being wrong, and the wrong
    direction here is silently suppressing somebody's message.
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


# --- the heads-up and the opportunity (live-use design §3) -------------------
# How long before a booked slot *starts* the heads-up goes out. An hour is the
# smallest useful number: less and there is no time to put a load on before the
# window opens, more and "in a while" is not news you can act on. Note this is a
# lead before the **start**, where the retired day-of nudge used a lead before
# the *end* — which is why booking Thursday Eve used to do nothing until you
# were already inside it.
HEADS_UP_LEAD_MINUTES = 60

# What a message, if any, should be about. At most one of these is ever chosen:
# the whole point of routing them through one decision is that four independent
# triggers cannot race each other into four DMs.
MSG_NONE = "none"
# Taken from :data:`people.KINDS` rather than spelled again, because a message
# kind and the switch that governs it are one thing named twice: ⏰ Slot heads-up
# *is* the setting for MSG_SLOT. Sharing the string means :func:`select` can hand
# the kind it just worked out straight to :func:`eligible` with no lookup table
# in between — and a table between two constants is a table that can fall out of
# step, which here would silently gate the wrong message.
MSG_SLOT = people.KIND_SLOT  # you booked this, it starts within the hour
MSG_OPPORTUNITY = people.KIND_OPPORTUNITY  # you're overdue and your slot is clear
MESSAGES = (MSG_NONE, MSG_SLOT, MSG_OPPORTUNITY)


def slot_soon(booked, moment, lead_minutes=HEADS_UP_LEAD_MINUTES) -> str | None:
    """A cell of theirs whose window opens within ``lead_minutes``, or None.

    Looks only at the moment's own day, and only *forward*: a slot already open
    is not "soon", it is now, and telling somebody their evening is starting at
    22:00 is the timing failure the day-of nudge was retired for.

    Ties go to the earliest slot, which is the one they can act on first.
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


def slot_ended(cell, moment) -> bool:
    """Whether this cell's window has already closed, relative to ``moment``.

    True for a cell on another weekday too, which is what a reply tapped the
    next morning needs: "yesterday evening" is over, whatever the clock says
    now. Unreadable input is *not* treated as ended — the caller's fallback is
    better than silently refusing a tap.
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
    """The wall-clock time the heads-up fires for one slot, or None.

    One trigger per slot, derived from that slot's own **start** — 05:00, 11:00,
    15:00 and 19:00 at the default hour's lead. The retired day-of nudge took
    its lead from the slot's *end*, which is the single line that made a
    reservation worthless: booking Thursday Eve bought nothing until you were
    already standing inside it.

    Clamped so the answer stays on the same day as the slot it belongs to. The
    earliest slot opens at 06:00, so a lead beyond that would wrap into
    yesterday and fire a heads-up for a window that had already opened.
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
    prediction, occupancy, user_id, moment, lead_minutes=HEADS_UP_LEAD_MINUTES
) -> str | None:
    """The cell an opportunity nudge would be about, or None.

    Their predicted usual slot, but only when it is **starting soon and nobody
    else has it**. Both halves matter, and they are what make this message the
    bot's private information rather than an opinion about somebody's laundry:
    it knows the slot is unbooked and they cannot see that from their bedroom.

    Deliberately *not* "the machine is free right now" — the caller checks that
    separately. A slot somebody else has booked is not an opportunity even with
    the drum standing empty, because the whole point of the grid is that the
    booking is the thing to respect.
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
    # Open now, or opening within the same lead the heads-up uses — and the
    # lower bound is the half of this that matters.
    #
    # Without it "not over yet" was the only test, so from midnight onwards the
    # 06:00 AM slot qualified: a housemate's load finishing at 02:00 would fire
    # SIGNAL_WASHER_FREE and DM somebody "this morning is wide open", four hours
    # early, in the dead hours :data:`plan.SLOT_WINDOWS` exists to declare
    # nobody does laundry in. That is the exact nagging this message was
    # designed not to be, and it is worse here than for the heads-up: a
    # heads-up at least concerns a slot the person deliberately booked, where
    # this one is the bot volunteering.
    #
    # Sharing the heads-up's lead rather than inventing a second number keeps
    # one answer to "how far ahead is this bot allowed to talk about a slot",
    # which is also the number the house can already tune.
    if now >= (window[1] if window else start):
        return None
    try:
        lead = max(0.0, float(lead_minutes) * 60)
    except (TypeError, ValueError):
        lead = HEADS_UP_LEAD_MINUTES * 60
    if now < start - lead:
        return None
    if plan.is_taken_by_other(occupancy or {}, cell, user_id):
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

    Returns ``(kind, cell, reason)``. ``kind`` is :data:`MSG_NONE` unless
    ``reason`` is :data:`REASON_OK`, and **none is the overwhelmingly common
    answer** — that is the design working, not the design failing.

    The rule this whole function exists to enforce: *the bot may only speak when
    its own private information is the point*. It knows the washer is free, it
    knows nobody has booked your usual slot, and it knows your booked slot is
    about to pass unused. It does **not** know whether you have dirty clothes or
    a free evening, so it never assumes either.

    Every suppression below drops the message rather than queueing it (P2), and
    they are checked before the budget so that a silenced message costs nothing:

    - the machine is busy (nothing useful to say about a slot you can't use)
    - they already washed today, or the caller knows the load that just finished
      was theirs
    - they switched this *kind* of message off, or it would land inside their
      quiet hours — both of which can only be asked once the branch below has
      chosen which message this would be
    - they have already heard from the bot today (the 1/day budget, but checked
      as a rule rather than as an accident of the number)

    **Precedence: a booking beats a guess**, the same rule the grid draws with
    (:func:`plan.cell_state`) and for the same reason. A slot heads-up is about
    something the person actually said; an opportunity is arithmetic about their
    past. If they have both, saying the second would be answering a question
    they didn't ask while ignoring one they did.
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
        # No booking of theirs coming up. An opportunity needs them to be
        # overdue *by their own learned cadence* — a fixed number of days would
        # be wrong for the twice-a-week washer and the fortnightly one alike.
        if not due:
            return (MSG_NONE, None, REASON_NOT_DUE)
        cell = opportunity_cell(
            prediction, occupancy, user_id, moment, lead_minutes
        )
        kind = MSG_OPPORTUNITY
    if cell is None:
        return (MSG_NONE, None, REASON_NO_PREDICTION)
    # The 🔔 gate goes **here**, not in the call at the top, because until this
    # point there was no kind to gate on. ⏰ and 💡 are separate switches, and
    # asking about them before the branch above would have to ask about both —
    # which is precisely how somebody who switched the heads-up off would stop
    # getting the opportunity they left on.
    #
    # A heads-up refused here deliberately does *not* fall through to the
    # opportunity, even when one is available. These settings may only ever
    # subtract: handing somebody a differently-worded message about the same
    # evening because they switched the first one off is routing around the tap
    # they just made.
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
    """:func:`select`, spending the budget. ``(kind, cell, reason, budgets)``.

    Charged before the send rather than after, for the reason
    :func:`claim_plan_dm` gives: a DM refused by somebody's privacy settings is
    gone, and refunding it would retry them at every trigger forever.
    """
    kind, cell, reason = select(people_map, budgets, user_id, moment, **kwargs)
    spent, updated = _claim(reason, budgets, user_id, moment)
    if spent != REASON_OK:
        return (MSG_NONE, None, spent, updated)
    return (kind, cell, REASON_OK, updated)


# --- the two decisions -------------------------------------------------------
def plan_dm(people_map, budgets, user_id, prediction, moment) -> str:
    """Whether to send the Sunday plan DM (§10.2). :data:`REASON_OK` to send.

    **No confident prediction means no DM at all** (P6). The alternative — "I
    don't know your days yet" — is a push notification whose entire content is
    that the bot has nothing to say, and the first month is nothing but those.

    The kind is known before anything else here — there is only one message this
    function can produce — so 📅 and the quiet window are checked in the first
    call rather than after the prediction, and somebody who has switched the
    check-in off costs the loop no history scan at all.
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


def heads_up_text(cell, minutes=None) -> str | None:
    """The slot heads-up's body, or None when the cell isn't renderable.

    The message a reservation has never had. Until now, booking Thursday Eve
    bought you nothing until you were already standing inside it — the reminder
    fired on a lead before the slot *ended*, so the strongest signal anybody can
    give the bot produced the weakest response it has.

    Worded as a **question about the booking**, not an instruction about the
    laundry. "Still want it?" is answerable by somebody who has changed their
    mind, and that is the point: the reply that helps the house most is the one
    that gives the slot back.
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


def opportunity_text(cell, prediction=None, gap_days=None) -> str | None:
    """The opportunity nudge's body, or None when the cell isn't renderable.

    The one message that is *not* about something the person said, so it has to
    earn its place by carrying only what they cannot see: the machine is free
    and **nobody has booked** the slot they usually use. It says both, and it
    says how it worked the timing out, because a message that just said "fancy
    doing some laundry?" would be exactly the nagging this design refuses.

    ``gap_days`` is their own learned cadence, quoted back so the timing is
    arguable rather than mysterious — the same principle as ``habit.explain``
    behind a day guess.
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
