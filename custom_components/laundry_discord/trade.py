"""Rules for the anonymous slot-swap broker.

Pure: no Home Assistant or discord imports, so it is unit-tested directly.
:mod:`assistant` owns the Store and Discord calls; this module owns every
rule and string shown before an accept, using a timezone-aware moment.
"""

from __future__ import annotations

try:  # normal path: sibling modules inside the integration package
    from . import habit
    from . import nudge
    from . import people
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path in tests
    # No package when exec'd by file path. Don't reimplement habit/plan/people
    # logic here or this module and the panel will disagree.
    import habit  # type: ignore[no-redef]
    import nudge  # type: ignore[no-redef]
    import people  # type: ignore[no-redef]
    import plan  # type: ignore[no-redef]

SECONDS_PER_HOUR = 3600

# --- guardrail numbers -------------------------------------------------------
# How long an unanswered ask lives before it lapses.
REQUEST_TTL_HOURS = 48

# Max asks one person can have in flight at once.
MAX_OPEN_PER_REQUESTER = 2

# Max asks waiting on one person; kept at 1 so a tap on a trade DM is unambiguous.
MAX_OPEN_PER_HOLDER = 1

# Ceiling on the stored request list, so it can't grow without bound.
MAX_REQUESTS = 250

# Max drift between a DM's own timestamp and its request, so a stale DM
# can't be tapped into answering a newer ask.
MATCH_WINDOW_SECONDS = 300

# --- states ------------------------------------------------------------------
# Only the first four are stored; "expired" is computed at read time
# (:func:`state_of`), so ageing costs no write.
STATE_OPEN = "open"
STATE_ACCEPTED = "accepted"
STATE_DECLINED = "declined"
STATE_BLOCKED = "blocked"  # pass, or permanent "don't ask me again"
STATE_EXPIRED = "expired"
STORED_STATES = (STATE_OPEN, STATE_ACCEPTED, STATE_DECLINED, STATE_BLOCKED)
STATES = (*STORED_STATES, STATE_EXPIRED)

# Both are a refusal, and both close the slot to everybody for the week.
# A block also adds a permanent per-pair refusal on the holder's record.
REFUSED_STATES = (STATE_DECLINED, STATE_BLOCKED)

# The three answers a trade DM carries.
ACTION_ACCEPT = "accept"
ACTION_PASS = "pass"
ACTION_BLOCK = "block"
ACTIONS = (ACTION_ACCEPT, ACTION_PASS, ACTION_BLOCK)

_ACTION_STATES = {
    ACTION_ACCEPT: STATE_ACCEPTED,
    ACTION_PASS: STATE_DECLINED,
    ACTION_BLOCK: STATE_BLOCKED,
}

# --- why an ask was or wasn't allowed ---------------------------------------
# One reason per gate. :func:`refusal_text` collapses the holder-side ones
# into a single sentence for the requester.
REASON_OK = "ok"
REASON_MOMENT = "moment"  # an unreadable clock — never act on a guess
REASON_SILENT = "silent"  # ask recorded but not delivered; see claim_request
REASON_TRADES_OFF = "trades_off"  # the house switch, checked by the caller
REASON_BAD_CELL = "bad_cell"  # unusable want/offer, or the same cell twice
REASON_BAD_WEEK = "bad_week"
REASON_SELF = "self"  # asking yourself for your own slot
REASON_NOT_YOURS = "not_yours"  # you don't hold what you're offering
REASON_NOT_THEIRS = "not_theirs"  # nobody else holds what you're asking for
REASON_ALREADY_ASKED = "already_asked"  # you, this slot, this week
REASON_SLOT_REFUSED = "slot_refused"  # somebody said no to this slot this week
REASON_TOO_MANY_OPEN = "too_many_open"  # your own outstanding asks
REASON_NO_REPLY_PATH = "no_reply_path"  # *you* can't be DMed the answer
# Below here: facts about the holder. All render as one sentence so the
# requester can't tell them apart.
REASON_BLOCKED = "blocked"  # 🚫 don't ask me again, permanently
REASON_NOT_OPTED_IN = "not_opted_in"  # no record, or never answered the panel
REASON_REMINDERS_OFF = "reminders_off"  # 🚫 in the panel
REASON_NOT_DM = "not_dm"  # they chose the channel; a trade can't go there
REASON_DM_CLOSED = "dm_closed"  # a previous DM bounced (50007)
REASON_PAUSED = "paused"  # ⏸, or ⏭ Skip this week
REASON_SWAPS_OFF = "swaps_off"  # 🔁 off on *their* record — not the house switch
REASON_QUIET = "quiet"  # inside their overnight quiet window
REASON_HOLDER_BUSY = "holder_busy"  # they already have an ask waiting
REASON_BUDGET_DAY = "budget_day"  # their 1-DM-a-day cap
# DM never left the building. Nothing renders this: the caller withdraws the
# ask silently. Kept in :data:`HOLDER_REASONS` so the sentence stays uniform.
REASON_UNDELIVERED = "undelivered"

REASONS = (
    REASON_OK,
    REASON_MOMENT,
    REASON_SILENT,
    REASON_TRADES_OFF,
    REASON_BAD_CELL,
    REASON_BAD_WEEK,
    REASON_SELF,
    REASON_NOT_YOURS,
    REASON_NOT_THEIRS,
    REASON_ALREADY_ASKED,
    REASON_SLOT_REFUSED,
    REASON_TOO_MANY_OPEN,
    REASON_NO_REPLY_PATH,
    REASON_BLOCKED,
    REASON_NOT_OPTED_IN,
    REASON_REMINDERS_OFF,
    REASON_NOT_DM,
    REASON_DM_CLOSED,
    REASON_PAUSED,
    REASON_SWAPS_OFF,
    REASON_QUIET,
    REASON_HOLDER_BUSY,
    REASON_BUDGET_DAY,
    REASON_UNDELIVERED,
)

# Facts about the holder; grouped so the tests can check they all render alike.
HOLDER_REASONS = (
    REASON_BLOCKED,
    REASON_NOT_OPTED_IN,
    REASON_REMINDERS_OFF,
    REASON_NOT_DM,
    REASON_DM_CLOSED,
    REASON_PAUSED,
    REASON_SWAPS_OFF,
    REASON_QUIET,
    REASON_HOLDER_BUSY,
    REASON_BUDGET_DAY,
    REASON_UNDELIVERED,
)


# --- small defensive readers -------------------------------------------------
def _id(value) -> str | None:
    """A user id as a string, or None if unusable. `interaction.user.id`
    is an int but HA's Store round-trips it as JSON, coming back as
    `"123"`, so ids must be compared as strings."""
    if value is None or isinstance(value, bool):
        return None
    key = str(value)
    return key or None


def _ts(value) -> float | None:
    """A unix timestamp field, or None when missing/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _week(value) -> str | None:
    """An ISO week key, or None. Not parsed — only shape-checked."""
    return value if isinstance(value, str) and value else None


# --- request records ---------------------------------------------------------
def request_id(week, requester, want) -> str | None:
    """The stable id for one ask: ``"2026-W32:123:3-eve"``. Deterministic,
    not random: one request per slot per requester per week is the key, so
    a duplicate ask can't be written even by mistake."""
    key = plan.normalise_cell(want)
    who = _id(requester)
    stamp = _week(week)
    if key is None or who is None or stamp is None:
        return None
    return f"{stamp}:{who}:{key}"


def new_request(requester, holder, want, offer, week, moment) -> dict | None:
    """One request row, or None if it could not be a usable one. Fields:
    id, from, to, want, offer, week, ts, made, state. ``made`` is when the
    ask was made; ``ts`` is liveness, aged past TTL to lapse it."""
    ident = request_id(week, requester, want)
    who = _id(requester)
    them = _id(holder)
    wanted = plan.normalise_cell(want)
    offered = plan.normalise_cell(offer)
    ts = habit.moment_ts(moment)
    if ident is None or who is None or them is None or ts is None:
        return None
    if offered is None or wanted == offered or who == them:
        return None
    return {
        "id": ident,
        "from": who,
        "to": them,
        "want": wanted,
        "offer": offered,
        "week": _week(week),
        "ts": ts,
        "made": ts,
        "state": STATE_OPEN,
    }


def normalise_request(record) -> dict | None:
    """A stored request rebuilt field by field, or None if unusable. Rows
    missing a timestamp or either end are dropped rather than kept."""
    if not isinstance(record, dict):
        return None
    who = _id(record.get("from"))
    them = _id(record.get("to"))
    wanted = plan.normalise_cell(record.get("want"))
    offered = plan.normalise_cell(record.get("offer"))
    week = _week(record.get("week"))
    ts = _ts(record.get("ts"))
    if who is None or them is None or wanted is None or week is None:
        return None
    if ts is None or offered is None:
        return None
    state = record.get("state")
    ident = record.get("id")
    # Rows from before `made` existed fall back to `ts`.
    made = _ts(record.get("made"))
    return {
        "id": ident if isinstance(ident, str) and ident else request_id(
            week, who, wanted
        ),
        "from": who,
        "to": them,
        "want": wanted,
        "offer": offered,
        "week": week,
        "ts": ts,
        "made": ts if made is None else made,
        "state": state if state in STORED_STATES else STATE_OPEN,
    }


def normalise_requests(requests) -> list[dict]:
    """Every usable request, oldest first. ``[]`` for a junk store."""
    if not isinstance(requests, (list, tuple)):
        return []
    rows = [
        row
        for row in (normalise_request(r) for r in requests)
        if row is not None
    ]
    rows.sort(key=lambda row: row["ts"])
    return rows


def prune_requests(requests, week) -> list[dict]:
    """Drop requests from past weeks, and cap the rest. Current-week rows
    are kept regardless of state, so an expired ask can't be retried early.
    Week keys sort as strings, so "older" is a plain comparison."""
    rows = normalise_requests(requests)
    current = _week(week)
    if current is not None:
        rows = [row for row in rows if row["week"] >= current]
    if MAX_REQUESTS > 0 and len(rows) > MAX_REQUESTS:
        rows = rows[-MAX_REQUESTS:]
    return rows


# --- state, computed rather than swept ---------------------------------------
def expires_at(request) -> float | None:
    """When this request stops being answerable, or None."""
    row = normalise_request(request)
    if row is None:
        return None
    return row["ts"] + REQUEST_TTL_HOURS * SECONDS_PER_HOUR


def is_expired(request, moment) -> bool:
    """Whether this request has aged out at ``moment``. An unreadable
    moment reads as expired: refusing to act costs a tap, acting on a
    stale request costs more."""
    deadline = expires_at(request)
    now = habit.moment_ts(moment)
    if deadline is None:
        return True
    if now is None:
        return True
    return now >= deadline


def state_of(request, moment) -> str | None:
    """This request's state right now: the stored one, or expired. Only
    an open row can become expired, computed at read time; nothing ever
    writes that transition to the store."""
    row = normalise_request(request)
    if row is None:
        return None
    if row["state"] != STATE_OPEN:
        return row["state"]
    return STATE_EXPIRED if is_expired(row, moment) else STATE_OPEN


def is_open(request, moment) -> bool:
    """Whether this request is still awaiting an answer."""
    return state_of(request, moment) == STATE_OPEN


# --- reading the list --------------------------------------------------------
def open_from(requests, requester, moment) -> list[dict]:
    """This person's own outstanding asks."""
    key = _id(requester)
    return [
        row
        for row in normalise_requests(requests)
        if key is not None and row["from"] == key and is_open(row, moment)
    ]


def pending_from(requests, requester, moment) -> list[dict]:
    """The asks this person has spent, for :data:`MAX_OPEN_PER_REQUESTER`.
    Ages by ``made`` rather than ``ts`` (unlike :func:`open_from`), so a
    silently-refused or undelivered ask still spends a slot for the full
    TTL instead of leaking whether it was delivered."""
    key = _id(requester)
    now = habit.moment_ts(moment)
    if key is None:
        return []
    ttl = REQUEST_TTL_HOURS * SECONDS_PER_HOUR
    return [
        row
        for row in normalise_requests(requests)
        if row["from"] == key
        and row["state"] == STATE_OPEN
        # Unreadable clock reads as still pending (same direction as is_expired).
        and (now is None or now < row["made"] + ttl)
    ]


def open_to(requests, holder, moment) -> list[dict]:
    """The asks waiting on this person. At most one, by :data:`MAX_OPEN_PER_HOLDER`."""
    key = _id(holder)
    return [
        row
        for row in normalise_requests(requests)
        if key is not None and row["to"] == key and is_open(row, moment)
    ]


def find_request(requests, ident) -> dict | None:
    """One request by id, or None."""
    if not isinstance(ident, str) or not ident:
        return None
    for row in normalise_requests(requests):
        if row["id"] == ident:
            return row
    return None


def match_request(requests, holder, moment, sent_ts=None) -> dict | None:
    """The request a trade DM in front of us is about, or None. Buttons
    can't carry the request id, so this resolves it from the recipient
    plus the DM's own timestamp, when available, so a stale DM can't
    answer a newer request. Falls back to the single open request
    otherwise."""
    waiting = open_to(requests, holder, moment)
    if not waiting:
        return None
    stamp = _ts(sent_ts)
    if stamp is None:
        return waiting[0]
    for row in waiting:
        if abs(row["ts"] - stamp) <= MATCH_WINDOW_SECONDS:
            return row
    return None


def match_any_request(requests, holder, sent_ts=None) -> dict | None:
    """The request a trade DM refers to, whatever state it is in now.
    Unlike :func:`match_request`, also matches lapsed/answered requests:
    "don't ask me again" is a standing decision about a person, so it
    works on old, expired DMs too. Matches by recipient plus timestamp,
    or the most recent ask to this person if none is given."""
    key = _id(holder)
    if key is None:
        return None
    rows = [row for row in normalise_requests(requests) if row["to"] == key]
    if not rows:
        return None
    stamp = _ts(sent_ts)
    if stamp is None:
        return rows[-1]
    for row in rows:
        if abs(row["ts"] - stamp) <= MATCH_WINDOW_SECONDS:
            return row
    return None


def dm_sent_ts(moment, dm_ts, tapped_ts) -> float | None:
    """Where a Discord message sits on our clock, or None if it can't be
    told. Measures the DM's age on Discord's clock (tap time minus send
    time) and subtracts that from our own now, since the two clocks can
    drift. A tap that predates its own message reads as unknown."""
    now = habit.moment_ts(moment)
    sent = _ts(dm_ts)
    tapped = _ts(tapped_ts)
    if now is None or sent is None or tapped is None:
        return None
    age = tapped - sent
    if age < 0:
        return None
    return now - age


def asked_this_week(requests, requester, want, week) -> bool:
    """Whether this person has already asked for this slot this week.
    Counts the ask, not the answer: an expired, unanswered ask still
    counts, so re-asking someone who didn't reply is blocked too."""
    key = plan.normalise_cell(want)
    who = _id(requester)
    stamp = _week(week)
    if key is None or who is None or stamp is None:
        return False
    return any(
        row["from"] == who and row["want"] == key and row["week"] == stamp
        for row in normalise_requests(requests)
    )


def slot_refused(requests, want, week) -> bool:
    """Whether anybody was told no about this slot this week. A refusal
    closes the slot to the whole house for the week, not just to whoever
    asked, so "no" to one person can't be retried by the next five."""
    key = plan.normalise_cell(want)
    stamp = _week(week)
    if key is None or stamp is None:
        return False
    return any(
        row["want"] == key
        and row["week"] == stamp
        and row["state"] in REFUSED_STATES
        for row in normalise_requests(requests)
    )


# --- "never ask me again", per requester-pair --------------------------------
def block_list(people_map, holder) -> list[str]:
    """Who this person has permanently refused, as string ids. Stored on
    the holder's own record (`no_trade_from`) rather than in the request
    list, since it must outlive every request and every week."""
    stored = people.get_person(people_map, holder)["no_trade_from"]
    blocked: list[str] = []
    for item in stored if isinstance(stored, list) else []:
        key = _id(item)
        if key is not None and key not in blocked:
            blocked.append(key)
    return blocked


def is_blocked(people_map, requester, holder) -> bool:
    """Whether this holder has told this requester never again. Per pair:
    blocking one housemate doesn't affect asks to or from anyone else,
    and reads as an ordinary refusal (:func:`refusal_text`)."""
    key = _id(requester)
    return key is not None and key in block_list(people_map, holder)


def with_block(people_map, requester, holder) -> list[str]:
    """The holder's new ``no_trade_from`` list, with this requester added.
    Returns the list, not the mapping; the caller writes it through
    :func:`people.set_person`. Permanent by design: there is no unblock."""
    current = block_list(people_map, holder)
    key = _id(requester)
    if key is None or key in current:
        return current
    return [*current, key]


# --- can this person be reached at all? --------------------------------------
def _delivery_gate(people_map, user_id, moment) -> str:
    """Whether a DM can reach this person at all (the route, not this
    message). Returns :data:`REASON_OK` or the first gate that said no:
    not opted in, reminders off, channel-only (a trade can't post there),
    DMs closed (`Forbidden`/50007), or paused. Split from :func:`reachable`
    so the reply to your own ask still passes even when its extra gates
    wouldn't."""
    if habit.moment_ts(moment) is None:
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
    if nudge.is_paused(person, moment):
        return REASON_PAUSED
    return REASON_OK


def reachable(people_map, user_id, moment) -> str:
    """Whether an unprompted trade ask may be delivered to this person.
    :func:`_delivery_gate` plus swap requests off and quiet hours
    (:func:`nudge.in_quiet_hours`); both render as the same holder-side
    refusal, so neither leaks which one applied."""
    verdict = _delivery_gate(people_map, user_id, moment)
    if verdict != REASON_OK:
        return verdict
    person = people.get_person(people_map, user_id)
    if not people.wants_kind(person, people.KIND_TRADES):
        return REASON_SWAPS_OFF
    if nudge.in_quiet_hours(person, moment):
        return REASON_QUIET
    return REASON_OK


# --- the two halves of "may I ask" ------------------------------------------
def check_request(
    people_map, requests, requester, want, offer, week, moment, *, mine=()
) -> str:
    """The rules about the ask itself, checked before any holder is
    considered. In order: usable, distinct want/offer cells and week; you
    must hold what you're offering (``mine``, passed in since this module
    has no store); you must be reachable for the reply; one ask per slot
    per week; the slot isn't already refused; and your open-ask cap."""
    if habit.moment_ts(moment) is None:
        return REASON_MOMENT
    stamp = _week(week)
    if stamp is None:
        return REASON_BAD_WEEK
    wanted = plan.normalise_cell(want)
    if wanted is None:
        return REASON_BAD_CELL
    offered = plan.normalise_cell(offer)
    # No offer reads as "nothing to put up", not a parse failure.
    if offered is None:
        return REASON_NOT_YOURS
    if wanted == offered:
        return REASON_BAD_CELL
    if _id(requester) is None:
        return REASON_SELF
    held = [plan.normalise_cell(cell) for cell in mine or ()]
    if offered not in held:
        return REASON_NOT_YOURS
    # _delivery_gate, not reachable: swaps-off and quiet hours govern
    # messages started at you, not the reply to one you sent.
    if _delivery_gate(people_map, requester, moment) != REASON_OK:
        return REASON_NO_REPLY_PATH
    if asked_this_week(requests, requester, wanted, stamp):
        return REASON_ALREADY_ASKED
    if slot_refused(requests, wanted, stamp):
        return REASON_SLOT_REFUSED
    # pending_from, not open_from: counts asks spent, not what happened to them.
    if len(pending_from(requests, requester, moment)) >= MAX_OPEN_PER_REQUESTER:
        return REASON_TOO_MANY_OPEN
    return REASON_OK


def check_holder(people_map, requests, budgets, requester, holder, moment) -> str:
    """The rules about the person who would get the DM. In order: not
    yourself; not permanently blocked; reachable (:func:`reachable`);
    nothing else waiting; daily DM budget has room
    (:func:`habit.check_daily_cap`, not the weekly cap). Every reason
    renders as the same sentence (:func:`refusal_text`)."""
    who = _id(requester)
    them = _id(holder)
    if who is None or them is None or who == them:
        return REASON_SELF
    if is_blocked(people_map, who, them):
        return REASON_BLOCKED
    verdict = reachable(people_map, them, moment)
    if verdict != REASON_OK:
        return verdict
    if len(open_to(requests, them, moment)) >= MAX_OPEN_PER_HOLDER:
        return REASON_HOLDER_BUSY
    if habit.check_daily_cap(habit.budget_for(budgets, them), moment) != (
        habit.BUDGET_OK
    ):
        return REASON_BUDGET_DAY
    return REASON_OK


def may_ask(
    people_map, requests, budgets, requester, holder, want, offer, week, moment,
    *, mine=(),
) -> str:
    """Both halves, for one named holder. :data:`REASON_OK` to go ahead."""
    verdict = check_request(
        people_map, requests, requester, want, offer, week, moment, mine=mine
    )
    if verdict != REASON_OK:
        return verdict
    return check_holder(
        people_map, requests, budgets, requester, holder, moment
    )


def pick_holder(
    people_map, requests, budgets, requester, holders, want, offer, week, moment,
    *, mine=(),
) -> tuple[str | None, str]:
    """Who to ask, out of everybody holding the slot. ``(holder, reason)``.
    The ask goes to exactly one holder (never all), skipping the
    requester. When nobody qualifies, the first holder's reason is
    reported, which (holder-side reasons all render alike) tells the
    requester nothing."""
    verdict = check_request(
        people_map, requests, requester, want, offer, week, moment, mine=mine
    )
    if verdict != REASON_OK:
        return (None, verdict)
    who = _id(requester)
    candidates = [
        key
        for key in (_id(h) for h in holders or ())
        if key is not None and key != who
    ]
    if not candidates:
        return (None, REASON_NOT_THEIRS)
    first = REASON_OK
    for holder in candidates:
        reason = check_holder(
            people_map, requests, budgets, requester, holder, moment
        )
        if reason == REASON_OK:
            return (holder, REASON_OK)
        if first == REASON_OK:
            first = reason
    return (None, first)


# --- making one ---------------------------------------------------------------
def add_request(requests, request) -> list[dict]:
    """The list with this request in it, replacing any row of the same
    id. Ids are deterministic (:func:`request_id`), so a replacement only
    happens if a caller skipped :func:`check_request`."""
    row = normalise_request(request)
    rows = normalise_requests(requests)
    if row is None:
        return rows
    rows = [r for r in rows if r["id"] != row["id"]]
    rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    if MAX_REQUESTS > 0 and len(rows) > MAX_REQUESTS:
        rows = rows[-MAX_REQUESTS:]
    return rows


def _lapsed_request(requester, holders, want, offer, week, moment) -> dict | None:
    """The row a silently-refused ask leaves behind, or None if
    unbuildable. Born already past its TTL, so it's inert to everyone
    else, but still costs its author one open-ask slot and this week's
    ask for this slot, same as a delivered ask."""
    who = _id(requester)
    holder = next(
        (
            key
            for key in (_id(h) for h in holders or ())
            if key is not None and key != who
        ),
        None,
    )
    row = new_request(requester, holder, want, offer, week, moment)
    if row is None:
        return None
    return {**row, "ts": row["ts"] - REQUEST_TTL_HOURS * SECONDS_PER_HOUR}


def claim_request(
    people_map, requests, budgets, requester, holders, want, offer, week, moment,
    *, mine=(),
):
    """Decide, pick a holder, spend the DM budget and write the row, in
    one call. Returns ``(reason, request_or_None, new_requests,
    new_budgets)``. A holder-side refusal returns :data:`REASON_SILENT`
    with an already-lapsed row and no DM sent, rendered the same as a
    successful send. Charges the daily DM cap but not the weekly nudge
    cap: a trade is a housemate asking, not the bot's own arithmetic, and
    it's already bounded by the per-slot, per-pair and per-holder rules
    above."""
    holder, reason = pick_holder(
        people_map, requests, budgets, requester, holders, want, offer, week,
        moment, mine=mine,
    )
    if reason in HOLDER_REASONS and holder is None:
        # Still writes a lapsed row and reports sent_text: a free refusal
        # would let a requester probe who blocks or refuses them.
        silent = _lapsed_request(requester, holders, want, offer, week, moment)
        if silent is not None:
            return (
                REASON_SILENT,
                silent,
                add_request(requests, silent),
                habit.normalise_budgets(budgets),
            )
    if reason != REASON_OK or holder is None:
        return (
            reason if reason != REASON_OK else REASON_NOT_THEIRS,
            None,
            normalise_requests(requests),
            habit.normalise_budgets(budgets),
        )
    request = new_request(requester, holder, want, offer, week, moment)
    if request is None:
        return (
            REASON_BAD_CELL,
            None,
            normalise_requests(requests),
            habit.normalise_budgets(budgets),
        )
    # Claimed before send succeeds, like nudge.claim_plan_dm: refunding a
    # bounced DM would retry someone with closed DMs on every tap.
    allowed, updated_budgets = habit.claim_daily_nudge_for(budgets, holder, moment)
    if not allowed:
        return (
            REASON_BUDGET_DAY,
            None,
            normalise_requests(requests),
            updated_budgets,
        )
    return (REASON_OK, request, add_request(requests, request), updated_budgets)


# --- answering one -----------------------------------------------------------
def answer(requests, ident, action, moment) -> tuple[str, dict | None, list[dict]]:
    """Record an answer to one request. ``(reason, request,
    new_requests)``. The returned request is the answered row, with both
    ids and cells the caller needs to reveal names and swap slots. An
    expired or already-answered request cannot be answered again."""
    rows = normalise_requests(requests)
    row = find_request(rows, ident)
    if row is None or action not in ACTIONS:
        return (REASON_MOMENT, None, rows)
    if state_of(row, moment) != STATE_OPEN:
        return (REASON_MOMENT, None, rows)
    answered = {**row, "state": _ACTION_STATES[action]}
    return (
        REASON_OK,
        answered,
        [answered if r["id"] == row["id"] else r for r in rows],
    )


def withdraw(requests, ident, moment) -> list[dict]:
    """Retire one ask without answering it: it lapses, here and now. Only
    ``ts`` ages past the TTL; the row stays, still counting against the
    asker's limits. Not recorded as a decline, which would wrongly close
    the slot on an answer nobody gave."""
    rows = normalise_requests(requests)
    now = habit.moment_ts(moment)
    if now is None:
        return rows
    lapsed = now - REQUEST_TTL_HOURS * SECONDS_PER_HOUR
    return [
        {**row, "ts": min(row["ts"], lapsed)}
        if row["id"] == ident and is_open(row, moment)
        else row
        for row in rows
    ]


# --- the swap itself ---------------------------------------------------------
def _ensure(people_map, overrides, week, cell, user_id, present: bool):
    """Put one person on (or off) one cell, via :func:`plan.toggle_booking`.
    Reuses the grid's own toggle so a swap and a manual tap produce
    identical overrides."""
    occupancy = plan.effective_week(people_map, overrides, week)
    held = plan.holders(occupancy, cell)
    if (str(user_id) in held) == bool(present):
        return plan.normalise_overrides(overrides)
    updated, _booked = plan.toggle_booking(
        people_map, overrides, week, cell, user_id
    )
    return updated


def apply_swap(people_map, overrides, request) -> dict:
    """The overrides with the two slots actually exchanged. Four moves:
    holder off the wanted cell and onto the offered one, the requester
    the other way. Set-based, not toggled, so a double-tap can't swap
    them back."""
    row = normalise_request(request)
    if row is None:
        return plan.normalise_overrides(overrides)
    week = row["week"]
    updated = plan.normalise_overrides(overrides)
    updated = _ensure(people_map, updated, week, row["want"], row["to"], False)
    updated = _ensure(people_map, updated, week, row["want"], row["from"], True)
    updated = _ensure(people_map, updated, week, row["offer"], row["from"], False)
    updated = _ensure(people_map, updated, week, row["offer"], row["to"], True)
    return updated


# --- what any of it says -----------------------------------------------------
def describe_cell(cell) -> str | None:
    """``"Thursday Eve"`` for a cell key, or None. Not
    :func:`habit.describe_bucket` ("Thursday evenings"): that phrase
    describes a habit, this describes one specific square of one week."""
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    weekday, slot = parsed
    return f"{plan.DAY_NAMES[weekday]} {plan.SLOT_LABELS[slot]}"


# Shown when a tap lands on somebody else's slot; the ask needs a second tap.
ASK_PROMPT = "That one's spoken for. Want me to ask?"


def ask_panel_text(want, offer=None) -> str | None:
    """What the requester is shown before they commit to asking. Shows
    exactly what will be sent. Takes no id or name: this side never
    knows whose slot it is either, so there's nothing to leak."""
    wanted = describe_cell(want)
    if wanted is None:
        return None
    offered = describe_cell(offer)
    lines = [f"🔁 **Ask about {wanted}**"]
    if offered is None:
        lines.append(
            "Pick one of your own slots to offer in return — a swap needs "
            "something on both sides."
        )
    else:
        lines.append(
            f"I'll offer them **{offered}** in return. They'll get a DM that "
            "says *someone* is asking — never your name, and you won't learn "
            "whose slot it is unless they say yes."
        )
    return "\n".join(lines)


def request_dm_text(want, offer) -> str | None:
    """The DM the holder gets. Never takes a name or an id. Names two
    slots and no people, and gives no reason: in a house of seven, a
    reason ("people coming Friday") can itself identify someone."""
    wanted = describe_cell(want)
    offered = describe_cell(offer)
    if wanted is None or offered is None:
        return None
    return (
        "🔁 **Someone's asking about your slot**\n"
        f"Someone's asking about **{wanted}**. They'd offer you **{offered}** "
        "in return.\n"
        "I haven't told them whose slot it is, and I won't tell you whose ask "
        "it is unless you say yes."
    )


def sent_text(want) -> str | None:
    """What the requester is told once the ask has gone out."""
    wanted = describe_cell(want)
    if wanted is None:
        return None
    return (
        f"🔁 Asked. Whoever's down for **{wanted}** has a DM about it — no name "
        "attached, yours or theirs. I'll tell you either way, and if they don't "
        "answer it just lapses."
    )


def passed_text(want) -> str | None:
    """What the requester is told when the answer is no. No name, no reason."""
    wanted = describe_cell(want)
    if wanted is None:
        return None
    return (
        f"🔁 They passed on **{wanted}** — no name, no reason, nothing to read "
        "into. That slot's settled for this week."
    )


def pass_ack_text() -> str:
    """What the holder sees after ❌ Pass."""
    return (
        "❌ Passed. They've been told, and that's all they've been told — not "
        "who you are and not why. Nobody can ask about that slot again this "
        "week."
    )


def block_ack_text() -> str:
    """What the holder sees after 🚫 Don't ask me again."""
    return (
        "🚫 Done — that person can never ask you for a slot again. They're told "
        "the same thing everybody else is told, which is nothing: no name, no "
        "reason, and no way to tell this apart from a plain no."
    )


def accepted_text_for_holder(requester_ref, want, offer) -> str | None:
    """What the holder sees after accepting. Names are allowed from here
    on. One of only two functions in this module that takes an identity,
    so "is this string allowed a name" is answerable from the signature
    alone."""
    wanted = describe_cell(want)
    offered = describe_cell(offer)
    if wanted is None or offered is None or not requester_ref:
        return None
    return (
        f"✅ **Swapped.** {requester_ref} has **{wanted}**, and you've got "
        f"**{offered}** instead. You're both named now — you two sort out the "
        "detail."
    )


def accepted_text_for_requester(holder_ref, want, offer) -> str | None:
    """What the requester is told when the answer is yes. The other reveal."""
    wanted = describe_cell(want)
    offered = describe_cell(offer)
    if wanted is None or offered is None or not holder_ref:
        return None
    return (
        f"✅ **Swapped.** {holder_ref} said yes: **{wanted}** is yours, and "
        f"they've taken **{offered}**. You're both named now — you two sort out "
        "the detail."
    )


# Several reasons share one sentence so the requester can't tell, from how
# the bot says no, which one applied to a housemate they can't even name.
_HOLDER_REFUSAL = (
    "I can't ask about that one right now. Nothing to read into it — try "
    "another slot, or ask again another day."
)

_REFUSAL_TEXT = {
    REASON_TRADES_OFF: "Swaps aren't switched on for this channel.",
    REASON_BAD_CELL: "I couldn't work out which slot you meant.",
    REASON_BAD_WEEK: "I couldn't work out which slot you meant.",
    REASON_MOMENT: "I couldn't work out which slot you meant.",
    REASON_SELF: "That one's already yours.",
    REASON_NOT_THEIRS: "Nobody else is down for that one — just tap it.",
    REASON_NOT_YOURS: (
        "You need a slot of your own to offer in return. Book one on your week "
        "first — a swap needs something on both sides."
    ),
    REASON_ALREADY_ASKED: (
        "You've already asked about that slot this week. One ask each, so a no "
        "stays a no."
    ),
    REASON_SLOT_REFUSED: (
        "Somebody's already been told no about that slot this week, so it's "
        "closed until next week. Nothing to do with you."
    ),
    REASON_TOO_MANY_OPEN: (
        f"You've got {MAX_OPEN_PER_REQUESTER} asks waiting already. Give those "
        "a chance to come back first."
    ),
    # A fact about the asker, so this one is allowed to be specific.
    REASON_NO_REPLY_PATH: (
        "I'd have no way to tell you the answer — it comes back as a DM, and "
        "that's the only way you'd hear it. Check 📬 **DM me** is on in 🤖, and "
        "that you haven't paused me."
    ),
}


def refusal_text(reason) -> str:
    """The one sentence a refused ask gets. Never varies by holder.
    Anything not in the table, including every :data:`HOLDER_REASONS`
    entry, falls through to the same flat sentence."""
    return _REFUSAL_TEXT.get(reason, _HOLDER_REFUSAL)
