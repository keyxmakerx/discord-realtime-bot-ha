"""Pure, dependency-free rules for the double-blind trade broker (§9).

Kept free of Home Assistant / discord imports so the only question that matters
here — *may this person ask that person for that slot, right now?* — can be
unit-tested without the HA test harness, the same discipline that made
:mod:`detect`, :mod:`queue`, :mod:`people`, :mod:`plan`, :mod:`habit` and
:mod:`nudge` reliable. :mod:`assistant` owns the ``Store``, the Discord calls
and the delivery; this module owns every rule, every state and every string
that is written before an accept.

A trade request is the only message in this integration that one housemate
causes to arrive on another housemate's phone. In a house of seven that is one
step away from being a way to pester somebody, so the guardrails are not
decoration — they *are* the feature, and they all live here, in one readable
list, rather than spread across button callbacks where a stray ``return`` is
invisible:

* **Anonymity until an accept, and it is structural.** Nothing this module
  renders before :data:`STATE_ACCEPTED` takes a name or an id — the pre-accept
  text functions have nowhere to put one, because they do not accept one as an
  argument. "Someone" is the only correct word (§9 step 2), and the two
  functions that *are* allowed a name say so in their own names.
* **Refusals are told back flat.** Several different refusals share one
  sentence on purpose (:func:`refusal_text`): a requester who cannot see who
  holds a cell must not be able to learn, from *how* the bot says no, that a
  particular person has blocked them, has DMs closed, or is already being asked
  by somebody else. The reason codes stay distinct so the behaviour is
  testable; the rendering does not.
* **Every guardrail refuses on its own.** :func:`check_request` and
  :func:`check_holder` return the **first** rule that said no, and each one is
  sufficient — no rule needs another to be true to bite.
* **Expiry is read, never swept.** A request's liveness is computed from its
  timestamp at the moment somebody looks (:func:`state_of`), so nothing has to
  run on a tick to age one out and nothing writes to the store to make an old
  request stop working. An expired request cannot be answered
  (:func:`answer`), and requests from a week that has passed are dropped the
  next time the list is written (:func:`prune_requests`).
* **The caller passes the clock.** Nothing here reads the time, exactly like
  :mod:`habit` and :mod:`nudge`.
* **Nothing is mutated.** Every function returns new data, so a rejected store
  write can't half-apply.

The moment passed in must be **timezone-aware local time** (HA's
``dt_util.now()``); :mod:`habit` refuses a naive one outright rather than
guessing which evening it meant, and every read of the clock here goes through
it.
"""

from __future__ import annotations

try:  # the normal path — sibling modules inside the integration package
    from . import habit
    from . import nudge
    from . import people
    from . import plan
except ImportError:  # pragma: no cover - loaded by file path, as the tests do
    # There is no package when this is exec'd from a file path, which is how the
    # pure suite runs it. The budget, the week maths, the preference record and
    # "is this person paused" are *not* reimplemented here: a second copy of any
    # of them is how the broker and the panel start disagreeing about what
    # somebody chose.
    import habit  # type: ignore[no-redef]
    import nudge  # type: ignore[no-redef]
    import people  # type: ignore[no-redef]
    import plan  # type: ignore[no-redef]

SECONDS_PER_HOUR = 3600

# --- the guardrail numbers (design doc §9) ----------------------------------
# How long an unanswered ask lives. A week's plan is worthless a week later, and
# an ask that is still sitting there on Friday is about a Tuesday that has
# happened. Two days is long enough for somebody who only opens Discord in the
# evening and short enough that the answer still means something.
REQUEST_TTL_HOURS = 48

# How many asks one person can have in flight at once. Two is enough to have
# more than one iron in the fire; more than that and one person's week becomes
# everybody else's inbox.
MAX_OPEN_PER_REQUESTER = 2

# ...and how many can be waiting on one person. Exactly one, for two reasons:
# nobody should open Discord to a queue of people wanting their Thursday, and it
# is what makes a tap on a trade DM unambiguous — the buttons cannot carry a
# request id (a per-request ``custom_id`` cannot be a persistent view, so it
# would die at the next restart), so "the open request addressed to you" has to
# be a single thing. :func:`match_request` still checks the DM's own timestamp
# on top of that.
MAX_OPEN_PER_HOLDER = 1

# An absolute ceiling on the stored list, the same belt-and-braces
# :data:`habit.HISTORY_MAX` is. The rules already bound it — one request per
# requester per cell per week is 7 people x 28 cells — so this cannot be reached
# by ordinary use; it exists so "it can never grow without bound" is true of the
# code rather than of the happy path.
MAX_REQUESTS = 250

# How far a trade DM's own timestamp may be from the request it is taken to be
# about. The DM goes out immediately after the request is written, so this is
# generous; it exists so a *stale* DM — one whose request has since expired and
# been replaced by somebody else's — cannot be tapped into answering a request
# its reader has never seen.
MATCH_WINDOW_SECONDS = 300

# --- states ------------------------------------------------------------------
# Only the first four are ever *stored*. "Expired" is computed from the row's
# timestamp when somebody looks (:func:`state_of`), so ageing costs no write.
STATE_OPEN = "open"
STATE_ACCEPTED = "accepted"
STATE_DECLINED = "declined"
STATE_BLOCKED = "blocked"  # ❌ Pass, plus 🚫 don't ask me again
STATE_EXPIRED = "expired"
STORED_STATES = (STATE_OPEN, STATE_ACCEPTED, STATE_DECLINED, STATE_BLOCKED)
STATES = (*STORED_STATES, STATE_EXPIRED)

# Both of these are somebody saying no, and both close the slot to *everybody*
# for the rest of the week (§9). The difference is only what else happened: a
# block also writes the permanent per-pair refusal onto the holder's record.
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
# One reason per gate, distinguishable for the tests and for "is this working".
# What the *requester* is shown collapses several of them into one sentence —
# see :func:`refusal_text`, which is the only thing allowed to render these.
REASON_OK = "ok"
REASON_MOMENT = "moment"  # an unreadable clock — never act on a guess
# The ask was recorded but nothing was sent, because the holder-side rules said
# no. The requester is told exactly what a delivered ask is told (:func:`sent_text`)
# and it lapses unanswered, because *whether a DM went out at all* is itself a
# fact about the holder: a refusal that costs nothing is a free, repeatable
# oracle that partitions the grid by who refuses, which is the one thing
# anonymity is for. See :func:`claim_request`.
REASON_SILENT = "silent"
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
# Everything below here is a fact about the *holder*, and all of it renders as
# one identical sentence. Learning which of these applied would be learning
# something about a person the requester cannot even name.
REASON_BLOCKED = "blocked"  # 🚫 don't ask me again, permanently
REASON_NOT_OPTED_IN = "not_opted_in"  # no record, or never answered the panel
REASON_REMINDERS_OFF = "reminders_off"  # 🚫 in the panel
REASON_NOT_DM = "not_dm"  # they chose the channel; a trade can't go there
REASON_DM_CLOSED = "dm_closed"  # a previous DM bounced (50007)
REASON_PAUSED = "paused"  # ⏸, or ⏭ Skip this week
REASON_HOLDER_BUSY = "holder_busy"  # they already have an ask waiting
REASON_BUDGET_DAY = "budget_day"  # their 1-DM-a-day cap
REASON_UNDELIVERED = "undelivered"  # the DM did not leave the building

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
    REASON_HOLDER_BUSY,
    REASON_BUDGET_DAY,
    REASON_UNDELIVERED,
)

# The reasons that are facts about the holder rather than about the request.
# Grouped here so the "these must all read identically" rule is a data
# structure the tests can iterate, not a promise in a docstring.
HOLDER_REASONS = (
    REASON_BLOCKED,
    REASON_NOT_OPTED_IN,
    REASON_REMINDERS_OFF,
    REASON_NOT_DM,
    REASON_DM_CLOSED,
    REASON_PAUSED,
    REASON_HOLDER_BUSY,
    REASON_BUDGET_DAY,
    REASON_UNDELIVERED,
)


# --- small defensive readers -------------------------------------------------
def _id(value) -> str | None:
    """A user id in its string form, or None for something unusable.

    Same hazard as everywhere else in this integration: ``interaction.user.id``
    is an int and HA's ``Store`` is JSON, so it comes back as ``"123"``. A
    request whose two ends were spelled differently would silently never match
    anybody.
    """
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
    """The stable id for one ask: ``"2026-W32:123:3-eve"``.

    Deterministic rather than random, because *one request per slot per
    requester per week* is a hard rule (§9) and this is exactly that rule's
    natural key: two rows for one ask cannot be written even by a caller that
    forgets to check.
    """
    key = plan.normalise_cell(want)
    who = _id(requester)
    stamp = _week(week)
    if key is None or who is None or stamp is None:
        return None
    return f"{stamp}:{who}:{key}"


def new_request(requester, holder, want, offer, week, moment) -> dict | None:
    """One request row, or None if it could not be a usable one.

    ``{"id", "from", "to", "want", "offer", "week", "ts", "state"}`` — §12's
    shape plus the ISO week, which the doc's sketch leaves out and every
    once-per-week rule in here needs: a bare timestamp cannot say which week it
    belongs to without redoing the week maths at every read.
    """
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
        "state": STATE_OPEN,
    }


def normalise_request(record) -> dict | None:
    """A stored request, rebuilt field by field — or None if unusable.

    Rebuilt rather than trusted, because these come off disk: a row with no
    timestamp can never expire, and a row missing either end belongs to nobody,
    so both are dropped rather than carried forever.
    """
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
    """Drop requests from weeks that have already happened, and cap the rest.

    Rows from the **current** week are kept whatever state they are in, expired
    ones included: "you already asked for this slot this week" and "somebody was
    refused this slot this week" are both answered by looking at them, and
    dropping an expired ask would quietly hand its author a second go at the
    same person for the same slot. A week later they are inert, and they go.

    Week keys are zero-padded and ISO-year-first (:func:`plan.iso_week_key`), so
    "older than now" is a string comparison — the same trick
    :func:`plan.prune_overrides` uses, for the same reason.
    """
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
    """Whether this request has aged out at ``moment``.

    An unreadable moment reads as **expired**, and that direction is deliberate:
    the two things expiry protects are somebody answering an ask about a slot
    that has passed, and a stale ask blocking a fresh one. If we cannot tell
    what time it is, refusing to act on an old request costs a tap and getting
    it wrong the other way acts on a week-old plan.
    """
    deadline = expires_at(request)
    now = habit.moment_ts(moment)
    if deadline is None:
        return True
    if now is None:
        return True
    return now >= deadline


def state_of(request, moment) -> str | None:
    """This request's state right now — the stored one, or expired.

    Answered rows keep their answer forever (an accept does not un-happen, and a
    decline has to keep closing its slot for the week). Only an **open** row can
    become :data:`STATE_EXPIRED`, and it does so by arithmetic at read time:
    nothing runs on a tick to age it out, and nothing is written to the store to
    record that it aged.
    """
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
    """The request a trade DM in front of us is about, or None.

    The buttons cannot carry the id (see :data:`MAX_OPEN_PER_HOLDER`), so the
    request is resolved from the recipient plus, when Discord gives us one, the
    **DM's own timestamp**. A DM sits in an inbox indefinitely; without the
    timestamp check, tapping a week-old message whose request expired long ago
    would answer whatever ask happens to be open now — somebody passing on a
    request they have never read, and the requester being told "they passed" by
    a person who was answering something else entirely.

    With no timestamp available it falls back to the single open request, which
    is the pre-Discord-metadata behaviour and still bounded to one.
    """
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
    """The request a trade DM refers to, **whatever state it is in now**.

    :func:`match_request` deliberately resolves only *answerable* asks, because
    everything it feeds — accept, pass, the swap itself — must not act on a week
    old plan. 🚫 *Don't ask me again* is the exception, and it is the whole
    reason this function exists: it is not an answer to the request at all, it
    is a standing decision about a **person**, and it stays true long after the
    ask it was provoked by has lapsed.

    Somebody who opens Discord on Monday, reads a swap DM that went out on
    Friday and taps 🚫 has done the one thing the guardrail exists to allow. If
    that tap needed the request to still be open, the only opt-out a pestered
    housemate has would quietly do nothing for the 48-hours-old DMs that are
    exactly the ones likely to still be sitting unread in an inbox.

    Matching is by recipient plus the DM's own timestamp, as in
    :func:`match_request`; with no timestamp it falls back to the most recent
    ask addressed to this person, which is the one their newest DM is about.
    """
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
    """Where a Discord message sits on **our** clock. None if it can't be told.

    Both :func:`match_request` and :func:`match_any_request` compare a DM's
    timestamp against a request row's, and those two numbers come off different
    clocks: the row is stamped by the HA host, the message by Discord (its
    snowflake). An HA box whose clock is out by more than
    :data:`MATCH_WINDOW_SECONDS` — an RPi with no RTC that has not re-synced NTP
    after a power cut, the same hazard :mod:`habit` already defends against —
    would otherwise match *nothing*, ever: every swap DM in the house would say
    "that one's lapsed", including 🚫.

    So the DM is never placed by its absolute timestamp. It is placed by its
    **age**, measured entirely on Discord's clock (the tap's own timestamp minus
    the message's) and then subtracted from our own now. Any constant offset
    between the two clocks cancels, and what the window still catches — a DM
    that is genuinely hours older than the request open now — is unaffected,
    because that age is real on either clock.

    A tap that appears to predate its own message is a clock we cannot reason
    about at all, so it reads as unknown rather than as a guess.
    """
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

    Counts the ask, not the answer: a request that expired unanswered still used
    up the one ask this person gets for this slot this week. Re-asking somebody
    who did not reply is the exact behaviour "one request per slot per requester
    per week" exists to stop.
    """
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
    """Whether **anybody** was told no about this slot this week.

    A declined slot is closed to the whole house for the rest of the week, not
    just to whoever was refused (§9). Otherwise "no" to one person is an
    invitation to the next five, which is the same pestering arriving from
    different directions — and the holder cannot even tell it apart, because
    every ask is anonymous.
    """
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
    """Who this person has permanently refused, as string ids.

    Stored on the holder's own record (``no_trade_from``, which :mod:`people`
    has carried since the panel shipped) rather than in the request list,
    because it has to outlive every request and every week.
    """
    stored = people.get_person(people_map, holder)["no_trade_from"]
    blocked: list[str] = []
    for item in stored if isinstance(stored, list) else []:
        key = _id(item)
        if key is not None and key not in blocked:
            blocked.append(key)
    return blocked


def is_blocked(people_map, requester, holder) -> bool:
    """Whether this holder has told this requester never again.

    Per **pair**, deliberately: 🚫 is about one person's asks, not about the
    feature. Somebody who blocks one housemate keeps working normally with the
    other five, and never learns that the block is why they stopped hearing
    from them — the refusal reads exactly like every other holder-side refusal
    (:func:`refusal_text`).
    """
    key = _id(requester)
    return key is not None and key in block_list(people_map, holder)


def with_block(people_map, requester, holder) -> list[str]:
    """The holder's new ``no_trade_from`` list, with this requester added.

    Returns the list rather than the mapping: the caller writes it through
    :func:`people.set_person`, which is the only thing that owns the record's
    shape. Permanent, and there is deliberately no function here that removes
    one — an opt-out somebody chose in order to stop being asked is not
    something the asker gets to argue with.
    """
    current = block_list(people_map, holder)
    key = _id(requester)
    if key is None or key in current:
        return current
    return [*current, key]


# --- can this person be reached at all? --------------------------------------
def reachable(people_map, user_id, moment) -> str:
    """Whether a trade DM may be delivered to this person at all.

    Returns :data:`REASON_OK` or the first gate that said no. The gates are the
    ones that mean *this person can be sent an unprompted DM*, which is a
    strictly narrower set than :func:`nudge.eligible` uses: 🔮 guessing and 👁
    monitoring are consents about the **habit model**, and a housemate asking
    about Thursday is not the model talking. Somebody who turned the guessing
    off is still a person who can be asked whether they want to swap.

    What does gate it is every choice about being messaged at all:

    * **Not opted in** — no record, or one that never answered the panel.
    * **🚫 No pings** — off means off, everywhere.
    * **The channel** — the panel's default, and the answer somebody who never
      opened it has. A trade ask cannot be posted in the channel: "someone wants
      your Thursday" in front of six people is neither anonymous nor quiet, so
      the channel preference means *not reachable*, not "reachable, loudly".
    * **DMs closed** — a previous send raised ``Forbidden`` (50007).
    * **⏸ Paused / ⏭ Skip this week** — quiet until the timestamp passes,
      via :func:`nudge.is_paused` so there is one definition of paused.
    """
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


# --- the two halves of "may I ask" ------------------------------------------
def check_request(
    people_map, requests, requester, want, offer, week, moment, *, mine=()
) -> str:
    """The rules that are about the **ask**, not about who holds the slot.

    Checked once, before any holder is considered, so a refusal that has nothing
    to do with a particular person cannot be mistaken for one that does — and so
    every reason this returns is a fact about the asker's own week, which is why
    these are the refusals allowed to say something specific.

    * **A usable cell, a usable week, and two different cells.** Trading a slot
      for itself is not a trade.
    * **You must hold what you are offering.** ``mine`` is the requester's own
      booked cells this week, handed in rather than derived, because occupancy
      is :mod:`plan`'s business and this module has no store. Offering a slot
      you do not have is a request to be given something for nothing — which is
      a pester with extra steps, and it is also what makes the offer safe to put
      in an anonymous DM: it is a real thing the recipient can take.
    * **You have to be reachable yourself.** The answer comes back as a DM,
      minutes or hours later, and it is the only way you ever find out. Somebody
      who cannot receive it would be asking a question into a void — and worse,
      would have spent one of the holder's DMs to do it.
    * **One ask per slot per requester per week.**
    * **A slot somebody was refused is closed for the week**, to everybody.
    * **A cap on your outstanding asks.**
    """
    if habit.moment_ts(moment) is None:
        return REASON_MOMENT
    stamp = _week(week)
    if stamp is None:
        return REASON_BAD_WEEK
    wanted = plan.normalise_cell(want)
    if wanted is None:
        return REASON_BAD_CELL
    offered = plan.normalise_cell(offer)
    # No offer at all reads as "you have nothing to put up", not as a garbled
    # request: it is what somebody with no bookings of their own has, and
    # telling them the bot couldn't parse their tap would send them looking for
    # a bug instead of for a slot.
    if offered is None:
        return REASON_NOT_YOURS
    if wanted == offered:
        return REASON_BAD_CELL
    if _id(requester) is None:
        return REASON_SELF
    held = [plan.normalise_cell(cell) for cell in mine or ()]
    if offered not in held:
        return REASON_NOT_YOURS
    if reachable(people_map, requester, moment) != REASON_OK:
        return REASON_NO_REPLY_PATH
    if asked_this_week(requests, requester, wanted, stamp):
        return REASON_ALREADY_ASKED
    if slot_refused(requests, wanted, stamp):
        return REASON_SLOT_REFUSED
    if len(open_from(requests, requester, moment)) >= MAX_OPEN_PER_REQUESTER:
        return REASON_TOO_MANY_OPEN
    return REASON_OK


def check_holder(people_map, requests, budgets, requester, holder, moment) -> str:
    """The rules that are about the **person** who would get the DM.

    Every one of these renders as the same sentence (:func:`refusal_text`), so
    the requester — who cannot see who holds the cell — cannot learn from a
    refusal that a particular housemate blocked them, has their DMs shut, or is
    already fielding somebody else's ask.

    * **You are not asking yourself.**
    * **🚫 Don't ask me again**, permanently, for this pair.
    * **They are reachable at all** (:func:`reachable`).
    * **They have nothing else waiting** — one ask at a time, per person.
    * **Their daily DM budget has room.** :func:`habit.check_daily_cap`, not
      :func:`habit.check_nudge`: see :func:`claim_request` for the argument
      about which half of the budget a trade spends.
    """
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

    A cell can be held by more than one person — bookings are information, not
    permission (§8), so two people can both be down for Thursday evening — and
    the ask must go to exactly **one** of them. Asking all of them would turn
    one tap into a broadcast, which is the opposite of what this feature is.

    The requester is skipped (you can already be booked into the cell you are
    asking about, since tapping it books you alongside the holder), and the
    first person the rules allow is chosen. When nobody qualifies, the reason
    reported is the **first holder's**, and since every holder-side reason
    renders identically that tells the requester exactly as much as it should:
    not this one, and nothing about who.
    """
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
    """The list with this request in it, replacing any row of the same id.

    The id is deterministic (:func:`request_id`), so "replacing" only ever
    happens for a caller that skipped :func:`check_request` — which would have
    refused it as an ask this person already made this week.
    """
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
    """The row a silently-refused ask leaves behind, or None if unbuildable.

    Born already past its TTL, so :func:`state_of` reads it as expired the
    moment it is written: it is never open, so it holds nobody's slot and
    occupies neither :data:`MAX_OPEN_PER_HOLDER` nor
    :data:`MAX_OPEN_PER_REQUESTER`, and its state is not one of
    :data:`REFUSED_STATES`, so it does not shut the slot to the rest of the
    house on an answer nobody gave. The single thing it does is count against
    :func:`asked_this_week` — which is the entire point.
    """
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
    """Decide, pick a holder, spend the DM budget and write the row — in one call.

    Returns ``(reason, request_or_None, new_requests, new_budgets)``. On a
    request-side refusal the two collections come back normalised and otherwise
    untouched, so a refusal that is about the asker's own week costs no store
    write. A **holder-side** refusal is the one exception: it comes back as
    :data:`REASON_SILENT` with an already-lapsed row to store and no DM to send
    — see the comment in the body for why the caller must render that exactly
    as it renders a successful send.

    **Which half of the budget a trade spends, and why.** The daily cap
    (:data:`habit.MAX_NUDGES_PER_DAY`, 1) is non-negotiable and is charged here:
    whatever else is true, a person receives at most one unprompted DM a day
    from this integration, trade requests included. The weekly cap
    (:data:`habit.MAX_NUDGES_PER_WEEK`, 2) is deliberately **not** charged, and
    that is a judgement call worth stating both ways.

    *For charging it:* one counter is simpler, and it would guarantee that no
    combination of features can put more than two unprompted DMs on somebody's
    phone in a week.

    *Against, which is what is implemented:* the weekly cap exists to bound how
    often **the bot's own arithmetic** starts a conversation — the Sunday
    check-in and the day-of nudge are the model deciding it has something to say
    about you. A trade request is not the bot's idea; it is a housemate asking
    about a slot you put on a shared board, and it carries an answer only you
    can give. Charging it would mean one swap request silences, for the rest of
    the week, the reminder somebody actually opted into — the guardrail doing
    damage to the feature it is not even about. And the anti-pestering job the
    weekly cap would do here is already done, and done better, by rules that
    know what a trade *is*: one ask per slot per requester per week, a refused
    slot closed to everybody for the week, a permanent per-pair block, one ask
    in flight per person, and the daily cap on top. Those bound *who* may ask
    and *how often* — which a shared counter cannot, because it cannot tell six
    people asking once from one person asking six times.

    The upper bound that remains is therefore: **at most one trade DM per person
    per day**, and only from somebody who has not been refused that slot, is not
    blocked, and has an ask to spare.
    """
    holder, reason = pick_holder(
        people_map, requests, budgets, requester, holders, want, offer, week,
        moment, mine=mine,
    )
    if reason in HOLDER_REASONS and holder is None:
        # A holder-side no still writes the ask, already lapsed, and still
        # reports :func:`sent_text`. That is not politeness, it is the
        # anonymity guarantee: if a refusal cost nothing, a requester could tap
        # the same cell every day forever and read the refuse-vs-send outcome
        # as an oracle. Several of these reasons never change — a 🚫 block is
        # permanent, and 📬/🚫/#channel are standing preferences — so a cell
        # that refuses on every probe while its neighbours go through
        # identifies its holder as a member of a fixed set, and a single
        # blocked requester could watch one housemate's whole week move about
        # the grid. Flattening the *wording* cannot close that, because the
        # leak is in the outcome. Writing the row makes a probe cost exactly
        # what a real ask costs — one per slot per week — and the requester is
        # told the truth about what happens next: nobody answers, and it lapses.
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
    # The budget is claimed here rather than after a successful send, exactly as
    # :func:`nudge.claim_plan_dm` does and for the same reason: a DM that
    # bounces has still been attempted, and refunding it would retry somebody
    # with closed DMs at every tap forever.
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
    """Record an answer to one request. ``(reason, request, new_requests)``.

    The request that comes back is the **answered** row, so the caller has the
    two ids and the two cells it needs to reveal names and swap slots without
    going looking for them again.

    An expired request cannot be answered — :data:`REASON_MOMENT` — and neither
    can one that already was. Both matter: a trade DM sits in an inbox
    indefinitely, and a tap on a week-old one must not move a booking that has
    long since stopped meaning anything.
    """
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
    """Retire one ask without answering it — it lapses, here and now.

    The row stays in the list, so it still counts as the one ask its author gets
    for that slot this week; only its liveness changes, by ageing its timestamp
    past the TTL that :func:`state_of` reads. Recording it as a *decline* would
    be a lie about the holder — it would shut the slot to the whole house for
    the week on the strength of an answer nobody gave.

    The caller for this is a request DM that could not be delivered. Leaving it
    open would block the holder and the slot for a week over a message nobody
    ever saw.
    """
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
    """Put one person on (or off) one cell, via :mod:`plan`'s own toggle.

    Nothing here reimplements booking: :func:`plan.toggle_booking` is what the
    grid writes with, so a swap and a tap produce byte-identical overrides.
    """
    occupancy = plan.effective_week(people_map, overrides, week)
    held = plan.holders(occupancy, cell)
    if (str(user_id) in held) == bool(present):
        return plan.normalise_overrides(overrides)
    updated, _booked = plan.toggle_booking(
        people_map, overrides, week, cell, user_id
    )
    return updated


def apply_swap(people_map, overrides, request) -> dict:
    """The overrides with the two slots actually exchanged.

    Four moves, not two: the holder comes **off** the wanted cell and **on** to
    the offered one, and the requester the other way round. Written as
    "make sure this is true" rather than "toggle", because the requester may
    already be booked into the wanted cell — tapping a taken slot books you in
    alongside its holder (§8), and that is exactly the tap that offers the
    trade, so it is the ordinary case rather than the odd one.

    Idempotent, so a double-tap on an accept cannot swap the slots back.
    """
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
    """``"Thursday Eve"`` for a cell key, or None.

    The day in full and the slot in the grid's own short label, which is how §9
    writes it. Deliberately not :func:`habit.describe_bucket` ("Thursday
    evenings"): that phrase is about a *habit*, and this is about one specific
    square of one specific week.
    """
    parsed = plan.parse_cell(plan.normalise_cell(cell))
    if parsed is None:
        return None
    weekday, slot = parsed
    return f"{plan.DAY_NAMES[weekday]} {plan.SLOT_LABELS[slot]}"


# The line the grid shows when a tap lands on somebody else's slot. It is a
# question, not a notification: the ask only happens if they tap again.
ASK_PROMPT = "That one's spoken for. Want me to ask?"


def ask_panel_text(want, offer=None) -> str | None:
    """What the requester is shown before they commit to asking.

    Says exactly what will be sent, because the whole feature rests on people
    believing the DM is anonymous — and the way to believe that is to be shown
    the words first. Takes no id and no name: there is nothing here to leak,
    because whose slot it is was never known to this side either.
    """
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
    """The DM the holder gets. **Never** takes a name or an id (§9 step 2).

    This is the sentence the whole feature is built around, so it is worth
    saying why it is shaped this way: it names two slots and no people, it says
    what is being offered rather than what is being asked of them, and it does
    not say why. A reason would be a name in disguise in a house of seven ("I've
    got people coming Friday" is one person), and there is nothing to be
    awkward about at breakfast if there was nothing to overhear.
    """
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
    """What the requester is told when the answer is no. No name, no reason.

    §9 step 4, verbatim in intent: *"Pass → requester hears 'they passed.' No
    name, no reason. Nothing to be awkward about at breakfast."*
    """
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
    """What the holder sees after ✅ Trade. **Names are allowed from here on.**

    §9 step 3: an accept reveals both names to both parties, because at that
    point they have to coordinate and they live together. This is one of exactly
    two functions in this module that takes an identity, and it takes it as an
    argument so that "is this string allowed a name" is answerable by reading
    the signature.
    """
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


# What a refusal says. Several reasons deliberately share one sentence: the
# requester cannot see who holds the cell, and must not be able to work out from
# *how* the bot says no that a particular housemate blocked them, has their DMs
# closed, has already been asked today or is fielding somebody else's ask. The
# reason codes stay distinct so each guardrail is testable on its own; only the
# rendering is flattened.
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
    # About the asker's own settings, so it is allowed to be specific — and it
    # names both things that can cause it, because being told to turn on a
    # setting that is already on is how somebody concludes the bot is broken.
    REASON_NO_REPLY_PATH: (
        "I'd have no way to tell you the answer — it comes back as a DM, and "
        "that's the only way you'd hear it. Check 📬 **DM me** is on in 🤖, and "
        "that you haven't paused me."
    ),
}


def refusal_text(reason) -> str:
    """The one sentence a refused ask gets. Never varies by holder.

    Anything not in the table — including every :data:`HOLDER_REASONS` entry —
    falls through to the same flat sentence, so adding a new holder-side gate
    later cannot accidentally invent a new way to leak one.
    """
    return _REFUSAL_TEXT.get(reason, _HOLDER_REFUSAL)
