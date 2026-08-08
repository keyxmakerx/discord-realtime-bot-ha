"""Tests for the pure trade-broker rules (design doc §9).

Runnable with plain ``python3 tests/test_trade.py`` — no pytest / Home
Assistant, mirroring ``tests/test_reminders.py``, ``tests/test_habit.py``,
``tests/test_plan.py``, ``tests/test_people.py`` and ``tests/test_queue.py``.
``trade.py`` is loaded by file path so importing it does not pull in the
package ``__init__`` (which imports Home Assistant).

Two kinds of thing are under test, and they fail in very different ways.

The **guardrails** are the feature: a trade request is the only message in this
integration that one housemate causes to arrive on another's phone, so every
rule is asserted to block *on its own*, from a fixture that is otherwise
allowed. A guardrail that only works because another one happens to be true
alongside it is a guardrail that disappears the day somebody relaxes the other.

The **anonymity** invariant is the whole point, and it fails silently: a leak
does not raise, it just quietly tells six people something they were promised
they'd never learn. So it is tested as a property over every string the module
can produce before an accept, against a corpus of names and ids, rather than by
eyeballing the ones that looked risky.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys


def _load(name: str, filename: str):
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "custom_components",
        "laundry_discord",
        filename,
    )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Loaded by file path, so the relative imports inside each module fall back to
# bare ones — put each module where that fallback will find it, in dependency
# order (plan, then people/habit, then nudge, then trade, which needs all four).
_plan = _load("ld_plan", "plan.py")
sys.modules["plan"] = _plan
_people = _load("ld_people", "people.py")
sys.modules["people"] = _people
_habit = _load("ld_habit", "habit.py")
sys.modules["habit"] = _habit
_nudge = _load("ld_nudge", "nudge.py")
sys.modules["nudge"] = _nudge
_trade = _load("ld_trade", "trade.py")

ACTION_ACCEPT = _trade.ACTION_ACCEPT
ACTION_BLOCK = _trade.ACTION_BLOCK
ACTION_PASS = _trade.ACTION_PASS
ASK_PROMPT = _trade.ASK_PROMPT
HOLDER_REASONS = _trade.HOLDER_REASONS
MATCH_WINDOW_SECONDS = _trade.MATCH_WINDOW_SECONDS
MAX_OPEN_PER_HOLDER = _trade.MAX_OPEN_PER_HOLDER
MAX_OPEN_PER_REQUESTER = _trade.MAX_OPEN_PER_REQUESTER
MAX_REQUESTS = _trade.MAX_REQUESTS
REASON_ALREADY_ASKED = _trade.REASON_ALREADY_ASKED
REASON_BAD_CELL = _trade.REASON_BAD_CELL
REASON_BAD_WEEK = _trade.REASON_BAD_WEEK
REASON_BLOCKED = _trade.REASON_BLOCKED
REASON_BUDGET_DAY = _trade.REASON_BUDGET_DAY
REASON_DM_CLOSED = _trade.REASON_DM_CLOSED
REASON_HOLDER_BUSY = _trade.REASON_HOLDER_BUSY
REASON_MOMENT = _trade.REASON_MOMENT
REASON_NOT_DM = _trade.REASON_NOT_DM
REASON_NOT_OPTED_IN = _trade.REASON_NOT_OPTED_IN
REASON_NOT_THEIRS = _trade.REASON_NOT_THEIRS
REASON_NOT_YOURS = _trade.REASON_NOT_YOURS
REASON_NO_REPLY_PATH = _trade.REASON_NO_REPLY_PATH
REASON_UNDELIVERED = _trade.REASON_UNDELIVERED
REASON_OK = _trade.REASON_OK
REASON_PAUSED = _trade.REASON_PAUSED
REASON_REMINDERS_OFF = _trade.REASON_REMINDERS_OFF
REASON_SELF = _trade.REASON_SELF
REASON_SILENT = _trade.REASON_SILENT
REASON_SLOT_REFUSED = _trade.REASON_SLOT_REFUSED
REASON_TOO_MANY_OPEN = _trade.REASON_TOO_MANY_OPEN
REASON_TRADES_OFF = _trade.REASON_TRADES_OFF
REASONS = _trade.REASONS
REQUEST_TTL_HOURS = _trade.REQUEST_TTL_HOURS
STATE_ACCEPTED = _trade.STATE_ACCEPTED
STATE_BLOCKED = _trade.STATE_BLOCKED
STATE_DECLINED = _trade.STATE_DECLINED
STATE_EXPIRED = _trade.STATE_EXPIRED
STATE_OPEN = _trade.STATE_OPEN

add_request = _trade.add_request
answer = _trade.answer
apply_swap = _trade.apply_swap
ask_panel_text = _trade.ask_panel_text
asked_this_week = _trade.asked_this_week
accepted_text_for_holder = _trade.accepted_text_for_holder
accepted_text_for_requester = _trade.accepted_text_for_requester
block_ack_text = _trade.block_ack_text
block_list = _trade.block_list
check_holder = _trade.check_holder
check_request = _trade.check_request
claim_request = _trade.claim_request
describe_cell = _trade.describe_cell
find_request = _trade.find_request
is_blocked = _trade.is_blocked
is_expired = _trade.is_expired
is_open = _trade.is_open
dm_sent_ts = _trade.dm_sent_ts
match_any_request = _trade.match_any_request
match_request = _trade.match_request
may_ask = _trade.may_ask
new_request = _trade.new_request
normalise_request = _trade.normalise_request
normalise_requests = _trade.normalise_requests
open_from = _trade.open_from
open_to = _trade.open_to
pass_ack_text = _trade.pass_ack_text
passed_text = _trade.passed_text
pick_holder = _trade.pick_holder
prune_requests = _trade.prune_requests
reachable = _trade.reachable
refusal_text = _trade.refusal_text
request_dm_text = _trade.request_dm_text
request_id = _trade.request_id
sent_text = _trade.sent_text
slot_refused = _trade.slot_refused
state_of = _trade.state_of
with_block = _trade.with_block
withdraw = _trade.withdraw

# A fixed offset rather than a named zone: nothing here reads the clock, so the
# tests never need a DST transition, and exact arithmetic is what makes the TTL
# assertable to the second.
TZ = datetime.timezone(datetime.timedelta(hours=-5))

WANT = "3-eve"  # Thursday Eve — what the requester is after
OFFER = "2-eve"  # Wednesday Eve — what they'd give up
OTHER = "0-am"  # Monday AM


def at(year, month, day, hour=21, minute=0) -> datetime.datetime:
    """A timezone-aware local moment. 21:00 (the Eve slot) by default."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


# 2026-08-06 is a Thursday.
NOW = at(2026, 8, 6, 21, 0)
WEEK = _plan.iso_week_key(NOW)
NEXT_WEEK = _plan.iso_week_key(NOW + datetime.timedelta(days=7))
LAST_WEEK = _plan.iso_week_key(NOW - datetime.timedelta(days=7))

# Distinctive so a leak of one into a rendered string cannot hide in ordinary
# prose. The ids are the other half of the corpus.
ASKER = "111"
HOLDER = "222"
THIRD = "333"
NAMES = ("Zebediah", "Perpetua", "Quillon")


def _prefs(*, holder_changes=None, asker_changes=None) -> dict:
    """A house where the asker and the holder have both opted into DMs.

    Built through :mod:`people` rather than by hand, so every record is exactly
    the shape the 🤖 panel writes — including the defaults, which is where the
    interesting cases are.
    """
    prefs: dict = {}
    for index, uid in enumerate((ASKER, HOLDER, THIRD)):
        prefs = _people.set_reminders(
            prefs, uid, _people.REMIND_DM, name=NAMES[index]
        )
    if holder_changes:
        prefs = _people.set_person(prefs, HOLDER, **holder_changes)
    if asker_changes:
        prefs = _people.set_person(prefs, ASKER, **asker_changes)
    return prefs


def _budget(moment, *, today=0, this_week=0) -> dict:
    """One person's nudge accounting, built to order.

    ``last_nudge_ts`` is put safely in the past: a stored timestamp *ahead* of
    the moment is read as a backwards clock and denies everything, which would
    make several of these tests pass for the wrong reason.
    """
    return {
        "last_nudge_ts": _habit.moment_ts(moment) - 600,
        "nudge_day": _habit.day_key(moment),
        "nudges_today": today,
        "nudge_week": _plan.iso_week_key(moment),
        "nudges_this_week": this_week,
    }


def _ask(prefs=None, requests=(), budgets=None, moment=NOW, **kwargs) -> str:
    """:func:`may_ask` with the ordinary arguments filled in."""
    return may_ask(
        prefs if prefs is not None else _prefs(),
        requests,
        budgets if budgets is not None else {},
        kwargs.pop("requester", ASKER),
        kwargs.pop("holder", HOLDER),
        kwargs.pop("want", WANT),
        kwargs.pop("offer", OFFER),
        kwargs.pop("week", WEEK),
        moment,
        mine=kwargs.pop("mine", (OFFER,)),
    )


def _request(state=STATE_OPEN, *, moment=NOW, requester=ASKER, holder=HOLDER,
             want=WANT, offer=OFFER, week=WEEK) -> dict:
    row = new_request(requester, holder, want, offer, week, moment)
    return {**row, "state": state}


# --- the record ---------------------------------------------------------------


def test_the_request_id_is_the_once_a_week_rule_written_down() -> None:
    # One ask per requester per slot per week IS the identity of a request, so
    # two rows for one ask cannot be written even by a careless caller.
    assert request_id(WEEK, ASKER, WANT) == f"{WEEK}:{ASKER}:{WANT}"
    assert request_id(WEEK, 111, WANT) == request_id(WEEK, "111", WANT)
    assert request_id(WEEK, ASKER, WANT) != request_id(NEXT_WEEK, ASKER, WANT)
    assert request_id(WEEK, ASKER, WANT) != request_id(WEEK, HOLDER, WANT)
    assert request_id(None, ASKER, WANT) is None
    assert request_id(WEEK, None, WANT) is None
    assert request_id(WEEK, ASKER, "nonsense") is None


def test_a_request_needs_two_people_two_slots_and_a_real_moment() -> None:
    assert new_request(ASKER, HOLDER, WANT, OFFER, WEEK, NOW) is not None
    # Trading a slot for itself is not a trade.
    assert new_request(ASKER, HOLDER, WANT, WANT, WEEK, NOW) is None
    assert new_request(ASKER, ASKER, WANT, OFFER, WEEK, NOW) is None
    assert new_request(ASKER, HOLDER, WANT, None, WEEK, NOW) is None
    # A naive datetime is refused by habit.moment_ts, which is what stops a
    # request being written with a timestamp from the wrong timezone.
    naive = datetime.datetime(2026, 8, 6, 21, 0)
    assert new_request(ASKER, HOLDER, WANT, OFFER, WEEK, naive) is None


def test_stored_rows_survive_json_with_int_ids() -> None:
    # The hazard every module here has hit: ids are ints at the button and
    # strings off disk. A request whose ends were spelled differently would
    # silently never match anybody.
    row = new_request(111, 222, WANT, OFFER, WEEK, NOW)
    assert row["from"] == "111" and row["to"] == "222"
    reloaded = json.loads(json.dumps([row]))
    assert normalise_requests(reloaded) == [row]
    assert open_to(reloaded, 222, NOW) == [row]
    assert open_from(reloaded, 111, NOW) == [row]
    assert asked_this_week(reloaded, 111, WANT, WEEK) is True


def test_junk_rows_are_dropped_rather_than_carried() -> None:
    assert normalise_request(None) is None
    assert normalise_request({"from": ASKER}) is None
    # No timestamp means it can never expire, so it is not a usable row.
    assert normalise_request({**_request(), "ts": None}) is None
    assert normalise_request({**_request(), "week": ""}) is None
    # An unrecognised state reads as open rather than as some fourth thing.
    assert normalise_request({**_request(), "state": "??"})["state"] == STATE_OPEN
    assert normalise_requests("nope") == []
    assert normalise_requests([None, 7, _request()]) == [_request()]


# --- expiry -------------------------------------------------------------------


def test_an_open_request_expires_on_its_own_arithmetic() -> None:
    row = _request()
    just_inside = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS - 1)
    exactly = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS)
    assert state_of(row, NOW) == STATE_OPEN
    assert state_of(row, just_inside) == STATE_OPEN
    # Half-open, like every other window in this integration.
    assert state_of(row, exactly) == STATE_EXPIRED
    assert is_expired(row, exactly) is True
    assert is_open(row, exactly) is False


def test_an_unreadable_moment_reads_as_expired() -> None:
    # The safe direction: refusing to act on an old request costs a tap, acting
    # on a week-old plan does not.
    assert is_expired(_request(), None) is True
    assert is_expired(_request(), datetime.datetime(2026, 8, 6, 21, 0)) is True


def test_an_answered_request_never_expires() -> None:
    later = NOW + datetime.timedelta(days=30)
    assert state_of(_request(STATE_DECLINED), later) == STATE_DECLINED
    assert state_of(_request(STATE_ACCEPTED), later) == STATE_ACCEPTED
    assert state_of(_request(STATE_BLOCKED), later) == STATE_BLOCKED


def test_an_expired_request_cannot_be_answered() -> None:
    rows = [_request()]
    ident = rows[0]["id"]
    late = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 1)
    reason, answered, updated = answer(rows, ident, ACTION_ACCEPT, late)
    assert reason == REASON_MOMENT
    assert answered is None
    assert updated == rows  # nothing recorded


def test_a_request_cannot_be_answered_twice() -> None:
    rows = [_request()]
    ident = rows[0]["id"]
    reason, answered, rows = answer(rows, ident, ACTION_PASS, NOW)
    assert reason == REASON_OK and answered["state"] == STATE_DECLINED
    again, second, rows = answer(rows, ident, ACTION_ACCEPT, NOW)
    assert again == REASON_MOMENT and second is None
    assert find_request(rows, ident)["state"] == STATE_DECLINED


def test_answering_records_the_action_that_was_taken() -> None:
    for action, state in (
        (ACTION_ACCEPT, STATE_ACCEPTED),
        (ACTION_PASS, STATE_DECLINED),
        (ACTION_BLOCK, STATE_BLOCKED),
    ):
        reason, answered, rows = answer([_request()], _request()["id"], action, NOW)
        assert reason == REASON_OK
        assert answered["state"] == state
        assert find_request(rows, answered["id"])["state"] == state
    assert answer([_request()], _request()["id"], "shrug", NOW)[0] == REASON_MOMENT
    assert answer([], "nope", ACTION_PASS, NOW)[0] == REASON_MOMENT


# --- pruning ------------------------------------------------------------------


def test_pruning_drops_weeks_that_have_happened_and_keeps_this_one() -> None:
    rows = [
        _request(STATE_DECLINED, week=LAST_WEEK),
        _request(STATE_DECLINED),
        _request(week=NEXT_WEEK),
    ]
    kept = prune_requests(rows, WEEK)
    assert [row["week"] for row in kept] == [WEEK, NEXT_WEEK]


def test_pruning_keeps_a_refusal_so_the_slot_stays_shut_all_week() -> None:
    # An expired *open* row also stays: dropping it would hand its author a
    # second go at the same person for the same slot.
    rows = prune_requests([_request(STATE_DECLINED), _request(want=OTHER)], WEEK)
    assert len(rows) == 2
    assert slot_refused(rows, WANT, WEEK) is True
    assert asked_this_week(rows, ASKER, OTHER, WEEK) is True


def test_the_stored_list_is_capped() -> None:
    many = [
        _request(moment=NOW + datetime.timedelta(seconds=i), want=WANT,
                 requester=str(1000 + i))
        for i in range(MAX_REQUESTS + 20)
    ]
    assert len(prune_requests(many, WEEK)) == MAX_REQUESTS
    assert len(add_request(many, _request(requester="99999"))) == MAX_REQUESTS


# --- the guardrails, each on its own ------------------------------------------


def test_the_fixture_allows_an_ask() -> None:
    # Everything below asserts that ONE change to this turns it into a refusal.
    # Without this case they could all be passing for some shared reason.
    assert _ask() == REASON_OK


def test_one_ask_per_slot_per_requester_per_week() -> None:
    rows = [_request()]
    assert _ask(requests=rows) == REASON_ALREADY_ASKED
    # ...whatever became of it. An ask nobody answered still used up the ask.
    for state in (STATE_ACCEPTED, STATE_DECLINED, STATE_BLOCKED):
        assert _ask(requests=[_request(state)]) == REASON_ALREADY_ASKED
    late = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 1)
    assert state_of(rows[0], late) == STATE_EXPIRED
    assert _ask(requests=rows, moment=late) == REASON_ALREADY_ASKED
    # A different slot is a different ask (asked of somebody who isn't already
    # fielding one — that is a separate guardrail, tested separately).
    assert _ask(requests=rows, want=OTHER, offer=OFFER, holder=THIRD) == REASON_OK


def test_the_once_a_week_rule_rolls_with_the_iso_week() -> None:
    rows = [_request()]
    assert asked_this_week(rows, ASKER, WANT, WEEK) is True
    assert asked_this_week(rows, ASKER, WANT, NEXT_WEEK) is False
    assert _ask(requests=rows, week=NEXT_WEEK,
                moment=NOW + datetime.timedelta(days=7)) == REASON_OK


def test_a_refused_slot_is_shut_to_everybody_for_the_week() -> None:
    for state in (STATE_DECLINED, STATE_BLOCKED):
        rows = [_request(state, requester=THIRD)]
        assert slot_refused(rows, WANT, WEEK) is True
        # Not the person who was refused — anybody.
        assert _ask(requests=rows) == REASON_SLOT_REFUSED
    # An accept does not shut the slot; nor does an unanswered ask.
    assert slot_refused([_request(STATE_ACCEPTED, requester=THIRD)], WANT, WEEK) is False
    assert slot_refused([_request(requester=THIRD)], WANT, WEEK) is False


def test_a_refused_slot_reopens_next_week() -> None:
    rows = [_request(STATE_DECLINED, requester=THIRD)]
    assert slot_refused(rows, WANT, NEXT_WEEK) is False
    assert _ask(requests=rows, week=NEXT_WEEK,
                moment=NOW + datetime.timedelta(days=7)) == REASON_OK


def test_never_ask_me_again_is_permanent_and_per_pair() -> None:
    prefs = _prefs(holder_changes={"no_trade_from": [ASKER]})
    assert is_blocked(prefs, ASKER, HOLDER) is True
    assert _ask(prefs=prefs) == REASON_BLOCKED
    # Only that pair: the third housemate is unaffected, and so is the same
    # asker approaching anybody else.
    assert is_blocked(prefs, THIRD, HOLDER) is False
    assert _ask(prefs=prefs, requester=THIRD) == REASON_OK
    assert _ask(prefs=prefs, holder=THIRD) == REASON_OK
    # ...and it survives the JSON round trip that stores it.
    reloaded = json.loads(json.dumps(prefs))
    assert is_blocked(reloaded, ASKER, HOLDER) is True


def test_a_block_is_added_once_and_leaves_the_others_alone() -> None:
    prefs = _prefs()
    assert with_block(prefs, ASKER, HOLDER) == [ASKER]
    prefs = _people.set_person(prefs, HOLDER, no_trade_from=[THIRD])
    assert with_block(prefs, ASKER, HOLDER) == [THIRD, ASKER]
    prefs = _people.set_person(prefs, HOLDER, no_trade_from=[THIRD, ASKER])
    assert with_block(prefs, ASKER, HOLDER) == [THIRD, ASKER]  # no duplicate
    assert block_list(prefs, ASKER) == []


def test_somebody_who_never_opted_in_is_not_reachable() -> None:
    assert reachable({}, HOLDER, NOW) == REASON_NOT_OPTED_IN
    stranger = {k: v for k, v in _prefs().items() if k != HOLDER}
    assert _ask(prefs=stranger) == REASON_NOT_OPTED_IN
    # A record exists but the panel was never answered — booking a slot creates
    # one of these, and it is still not consent to be messaged.
    prefs = _people.set_person(_prefs(), HOLDER, onboarded=False)
    assert _ask(prefs=prefs) == REASON_NOT_OPTED_IN


def test_reminders_off_is_not_reachable() -> None:
    prefs = _people.set_reminders(_prefs(), HOLDER, _people.REMIND_OFF)
    assert reachable(prefs, HOLDER, NOW) == REASON_REMINDERS_OFF
    assert _ask(prefs=prefs) == REASON_REMINDERS_OFF


def test_the_channel_preference_is_not_reachable_either() -> None:
    # A trade ask cannot be posted in the channel — "someone wants your
    # Thursday" in front of six people is neither anonymous nor quiet — so the
    # default answer means not reachable, not reachable loudly.
    prefs = _people.set_reminders(_prefs(), HOLDER, _people.REMIND_CHANNEL)
    assert reachable(prefs, HOLDER, NOW) == REASON_NOT_DM
    assert _ask(prefs=prefs) == REASON_NOT_DM


def test_closed_dms_are_not_reachable() -> None:
    prefs = _people.mark_dm_failed(_prefs(), HOLDER)
    assert reachable(prefs, HOLDER, NOW) == REASON_DM_CLOSED
    assert _ask(prefs=prefs) == REASON_DM_CLOSED


def test_a_paused_person_is_not_reachable_until_the_pause_ends() -> None:
    until = _habit.moment_ts(NOW) + 3600
    prefs = _people.set_person(_prefs(), HOLDER, paused_until=until)
    assert reachable(prefs, HOLDER, NOW) == REASON_PAUSED
    assert _ask(prefs=prefs) == REASON_PAUSED
    assert _ask(prefs=prefs, moment=NOW + datetime.timedelta(hours=2)) == REASON_OK


def test_guessing_and_monitoring_do_not_gate_a_trade() -> None:
    # They are consents about the habit model, and a housemate asking about
    # Thursday is not the model talking.
    prefs = _people.set_person(_prefs(), HOLDER, predict=False, monitor=False)
    assert reachable(prefs, HOLDER, NOW) == REASON_OK
    assert _ask(prefs=prefs) == REASON_OK


def test_only_one_ask_can_be_waiting_on_one_person() -> None:
    waiting = [_request(requester=THIRD)]
    assert len(open_to(waiting, HOLDER, NOW)) == MAX_OPEN_PER_HOLDER
    assert _ask(requests=waiting) == REASON_HOLDER_BUSY
    # Once it is answered, or once it lapses, they are askable again.
    answered = [_request(STATE_ACCEPTED, requester=THIRD)]
    assert _ask(requests=answered) == REASON_OK
    late = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 1)
    assert _ask(requests=waiting, moment=late, week=WEEK) == REASON_OK


def test_a_requester_can_only_have_so_many_asks_out() -> None:
    rows = [
        _request(want=f"{index}-am", holder=str(500 + index))
        for index in range(MAX_OPEN_PER_REQUESTER)
    ]
    assert len(open_from(rows, ASKER, NOW)) == MAX_OPEN_PER_REQUESTER
    assert _ask(requests=rows, want=WANT) == REASON_TOO_MANY_OPEN
    # Answered ones don't count against it; nor do lapsed ones.
    answered = [{**row, "state": STATE_DECLINED} for row in rows]
    assert _ask(requests=answered, want=WANT) == REASON_OK


def test_withdrawing_frees_the_cap_without_claiming_they_said_no() -> None:
    rows = [_request(want=OTHER, holder=THIRD), _request()]
    lapsed = withdraw(rows, rows[0]["id"], NOW)
    assert [row["id"] for row in open_from(lapsed, ASKER, NOW)] == [rows[1]["id"]]
    assert state_of(lapsed[0], NOW) == STATE_EXPIRED
    # ...but the ask still counts as this week's ask for that slot, and it is
    # emphatically NOT recorded as a refusal — nobody said no.
    assert asked_this_week(lapsed, ASKER, OTHER, WEEK) is True
    assert slot_refused(lapsed, OTHER, WEEK) is False
    assert withdraw(rows, "no such id", NOW) == normalise_requests(rows)


def test_you_have_to_be_reachable_yourself_to_ask() -> None:
    # The answer comes back as a DM, hours later, and it is the only way the
    # asker ever finds out — so asking without one would spend somebody else's
    # DM on a question whose answer goes nowhere.
    prefs = _people.set_reminders(_prefs(), ASKER, _people.REMIND_CHANNEL)
    assert _ask(prefs=prefs) == REASON_NO_REPLY_PATH
    prefs = _people.mark_dm_failed(_prefs(), ASKER)
    assert _ask(prefs=prefs) == REASON_NO_REPLY_PATH
    paused = _people.set_person(
        _prefs(), ASKER, paused_until=_habit.moment_ts(NOW) + 3600
    )
    assert _ask(prefs=paused) == REASON_NO_REPLY_PATH
    # ...and the sentence names both things that can cause it, because being
    # told to switch on a setting that is already on reads as a broken bot.
    assert "DM me" in refusal_text(REASON_NO_REPLY_PATH)
    assert "paused" in refusal_text(REASON_NO_REPLY_PATH)
    # ...and that refusal IS allowed to be specific: it is a fact about the
    # asker's own settings, not about anybody they cannot see.
    assert refusal_text(REASON_NO_REPLY_PATH) != refusal_text(REASON_BLOCKED)


def test_the_daily_dm_budget_blocks_a_trade() -> None:
    spent = {HOLDER: _budget(NOW, today=_habit.MAX_NUDGES_PER_DAY)}
    assert _ask(budgets=spent) == REASON_BUDGET_DAY
    # Tomorrow the day window has rolled and they are askable again.
    assert _ask(budgets=spent, moment=NOW + datetime.timedelta(days=1)) == REASON_OK


def test_the_weekly_budget_deliberately_does_not_block_a_trade() -> None:
    # The documented decision: the weekly cap bounds how often the BOT's own
    # arithmetic starts a conversation. A trade is a housemate asking, and
    # charging it would silence the reminders somebody actually opted into. The
    # daily cap is what keeps the ceiling at one unprompted DM a day.
    spent = {HOLDER: _budget(NOW, today=0, this_week=_habit.MAX_NUDGES_PER_WEEK)}
    assert _habit.check_nudge(spent[HOLDER], NOW) == _habit.BUDGET_WEEK
    assert _habit.check_daily_cap(spent[HOLDER], NOW) == _habit.BUDGET_OK
    assert _ask(budgets=spent) == REASON_OK


def test_a_trade_dm_spends_the_day_and_leaves_the_week_alone() -> None:
    _reason, _request_row, _rows, budgets = claim_request(
        _prefs(), [], {}, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
    )
    account = _habit.budget_for(budgets, HOLDER)
    assert account["nudges_today"] == 1
    assert account["nudges_this_week"] == 0
    # The reminder loop's own budget still sees the day as spent, which is the
    # honest answer: this person's phone has already buzzed today.
    assert _habit.check_nudge(account, NOW) == _habit.BUDGET_DAY


def test_you_must_hold_the_slot_you_are_offering() -> None:
    assert _ask(mine=()) == REASON_NOT_YOURS
    assert _ask(offer=OTHER, mine=(OFFER,)) == REASON_NOT_YOURS
    assert _ask(offer=OTHER, mine=(OFFER, OTHER)) == REASON_OK
    # Nothing offered at all is "you've nothing to put up", not "I couldn't
    # parse that": it is exactly what somebody with no bookings of their own
    # has, and the sentence has to send them to the grid, not to a bug report.
    assert _ask(offer=None, mine=()) == REASON_NOT_YOURS
    assert "offer in return" in refusal_text(REASON_NOT_YOURS)


def test_you_cannot_trade_a_slot_for_itself_or_ask_yourself() -> None:
    assert _ask(offer=WANT, mine=(WANT,)) == REASON_BAD_CELL
    assert _ask(want="nonsense") == REASON_BAD_CELL
    assert _ask(week="") == REASON_BAD_WEEK
    assert _ask(moment=None) == REASON_MOMENT
    assert _ask(holder=ASKER) == REASON_SELF


def test_every_reason_this_module_can_return_is_declared() -> None:
    # A reason that isn't in REASONS is one refusal_text has never been asked
    # about, and an undeclared reason is how a new gate quietly gets its own
    # sentence — which is the leak this feature cannot have.
    seen = {
        _ask(),
        _ask(requests=[_request()]),
        _ask(requests=[_request(STATE_DECLINED, requester=THIRD)]),
        _ask(prefs=_prefs(holder_changes={"no_trade_from": [ASKER]})),
        _ask(prefs={}),
        _ask(mine=()),
        _ask(offer=WANT, mine=(WANT,)),
        _ask(week=""),
        _ask(moment=None),
        _ask(holder=ASKER),
        _ask(budgets={HOLDER: _budget(NOW, today=1)}),
    }
    assert seen <= set(REASONS)


# --- choosing who to ask ------------------------------------------------------


def test_the_ask_goes_to_one_holder_not_all_of_them() -> None:
    holder, reason = pick_holder(
        _prefs(), [], {}, ASKER, [HOLDER, THIRD], WANT, OFFER, WEEK, NOW,
        mine=(OFFER,),
    )
    assert reason == REASON_OK
    assert holder == HOLDER


def test_an_unreachable_holder_is_skipped_for_a_reachable_one() -> None:
    prefs = _people.set_reminders(_prefs(), HOLDER, _people.REMIND_OFF)
    holder, reason = pick_holder(
        prefs, [], {}, ASKER, [HOLDER, THIRD], WANT, OFFER, WEEK, NOW,
        mine=(OFFER,),
    )
    assert (holder, reason) == (THIRD, REASON_OK)


def test_the_requester_is_never_a_candidate_for_their_own_ask() -> None:
    # Tapping a taken slot books you in alongside its holder (§8), so the
    # requester is very often one of the holders of the cell they're asking
    # about. That must not resolve to asking themselves.
    holder, reason = pick_holder(
        _prefs(), [], {}, ASKER, [ASKER], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
    )
    assert (holder, reason) == (None, REASON_NOT_THEIRS)
    holder, reason = pick_holder(
        _prefs(), [], {}, ASKER, [ASKER, HOLDER], WANT, OFFER, WEEK, NOW,
        mine=(OFFER,),
    )
    assert (holder, reason) == (HOLDER, REASON_OK)


def test_nobody_reachable_reports_one_reason_and_asks_nobody() -> None:
    prefs = _people.set_reminders(
        _people.set_reminders(_prefs(), HOLDER, _people.REMIND_OFF),
        THIRD,
        _people.REMIND_OFF,
    )
    holder, reason = pick_holder(
        prefs, [], {}, ASKER, [HOLDER, THIRD], WANT, OFFER, WEEK, NOW,
        mine=(OFFER,),
    )
    assert holder is None
    assert reason in HOLDER_REASONS


def test_an_empty_holder_list_is_not_a_trade() -> None:
    assert pick_holder(
        _prefs(), [], {}, ASKER, [], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
    ) == (None, REASON_NOT_THEIRS)


# --- claiming -----------------------------------------------------------------


def test_a_claim_writes_the_row_and_charges_the_dm() -> None:
    reason, request, rows, budgets = claim_request(
        _prefs(), [], {}, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
    )
    assert reason == REASON_OK
    assert request["from"] == ASKER and request["to"] == HOLDER
    assert request["want"] == WANT and request["offer"] == OFFER
    assert [row["id"] for row in rows] == [request["id"]]
    assert _habit.budget_for(budgets, HOLDER)["nudges_today"] == 1
    # ...and the same ask a second time is refused, having changed nothing.
    again, second, rows2, budgets2 = claim_request(
        _prefs(), rows, budgets, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW,
        mine=(OFFER,),
    )
    assert (again, second) == (REASON_ALREADY_ASKED, None)
    assert rows2 == rows
    assert _habit.budget_for(budgets2, HOLDER)["nudges_today"] == 1


def test_a_refused_claim_costs_nothing() -> None:
    for kwargs in (
        {"mine": ()},
        {"holders": [ASKER]},
        {"prefs": {}},
    ):
        prefs = kwargs.pop("prefs", _prefs())
        reason, request, rows, budgets = claim_request(
            prefs, [], {}, ASKER, kwargs.pop("holders", [HOLDER]), WANT, OFFER,
            WEEK, NOW, mine=kwargs.pop("mine", (OFFER,)),
        )
        assert reason != REASON_OK and request is None
        assert rows == []
        assert _habit.budget_for(budgets, HOLDER)["nudges_today"] == 0


# --- matching a tap to the DM it came from ------------------------------------


def test_a_trade_dm_resolves_to_the_request_it_was_sent_for() -> None:
    rows = [_request()]
    sent = _habit.moment_ts(NOW)
    assert match_request(rows, HOLDER, NOW, sent)["id"] == rows[0]["id"]
    # No timestamp available: fall back to the one open ask, which is bounded
    # to exactly one by MAX_OPEN_PER_HOLDER.
    assert match_request(rows, HOLDER, NOW)["id"] == rows[0]["id"]


def test_a_stale_trade_dm_cannot_answer_somebody_elses_ask() -> None:
    # The old ask has lapsed and a different housemate's has taken its place.
    later = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 1)
    fresh = _request(requester=THIRD, want=OTHER, moment=later)
    old_dm = _habit.moment_ts(NOW)
    assert match_request([fresh], HOLDER, later, old_dm) is None
    assert match_request([fresh], HOLDER, later,
                         _habit.moment_ts(later))["id"] == fresh["id"]


def test_nothing_waiting_matches_nothing() -> None:
    assert match_request([], HOLDER, NOW, _habit.moment_ts(NOW)) is None
    assert match_request([_request(STATE_DECLINED)], HOLDER, NOW) is None


# --- the swap -----------------------------------------------------------------


def test_an_accept_actually_exchanges_the_two_slots() -> None:
    prefs = _prefs()
    overrides = {WEEK: {WANT: [HOLDER], OFFER: [ASKER]}}
    swapped = apply_swap(prefs, overrides, _request(STATE_ACCEPTED))
    week = _plan.effective_week(prefs, swapped, WEEK)
    assert _plan.holders(week, WANT) == [ASKER]
    assert _plan.holders(week, OFFER) == [HOLDER]


def test_the_swap_works_when_the_asker_was_already_booked_alongside() -> None:
    # The ordinary case: tapping a taken slot books you in next to its holder,
    # and that is the tap that offers the trade.
    prefs = _prefs()
    overrides = {WEEK: {WANT: [HOLDER, ASKER], OFFER: [ASKER]}}
    swapped = apply_swap(prefs, overrides, _request(STATE_ACCEPTED))
    week = _plan.effective_week(prefs, swapped, WEEK)
    assert _plan.holders(week, WANT) == [ASKER]
    assert _plan.holders(week, OFFER) == [HOLDER]


def test_the_swap_is_idempotent() -> None:
    prefs = _prefs()
    overrides = {WEEK: {WANT: [HOLDER], OFFER: [ASKER]}}
    once = apply_swap(prefs, overrides, _request(STATE_ACCEPTED))
    twice = apply_swap(prefs, once, _request(STATE_ACCEPTED))
    assert twice == once


def test_the_swap_leaves_everybody_else_where_they_were() -> None:
    prefs = _prefs()
    overrides = {WEEK: {WANT: [HOLDER, THIRD], OFFER: [ASKER], OTHER: [THIRD]}}
    swapped = apply_swap(prefs, overrides, _request(STATE_ACCEPTED))
    week = _plan.effective_week(prefs, swapped, WEEK)
    # Order matters as much as membership: the third party keeps their place.
    assert _plan.holders(week, WANT) == [THIRD, ASKER]
    assert _plan.holders(week, OFFER) == [HOLDER]
    assert _plan.holders(week, OTHER) == [THIRD]


def test_a_junk_request_swaps_nothing() -> None:
    overrides = {WEEK: {WANT: [HOLDER]}}
    assert apply_swap(_prefs(), overrides, None) == _plan.normalise_overrides(
        overrides
    )


# --- anonymity ----------------------------------------------------------------


def _pre_accept_strings() -> list[str]:
    """Every string this module can produce **before** an accept.

    Collected in one place so the invariant is asserted over the module's whole
    pre-accept surface rather than over the two or three that looked risky. The
    two post-accept functions are excluded by name, because a reveal is exactly
    what they are for (§9 step 3).
    """
    strings = [
        ASK_PROMPT,
        ask_panel_text(WANT, OFFER),
        ask_panel_text(WANT, None),
        request_dm_text(WANT, OFFER),
        sent_text(WANT),
        passed_text(WANT),
        pass_ack_text(),
        block_ack_text(),
        describe_cell(WANT),
    ]
    strings += [refusal_text(reason) for reason in REASONS]
    strings += [refusal_text("something new nobody declared")]
    return [s for s in strings if s]


def test_nothing_said_before_an_accept_can_name_anybody() -> None:
    haystack = "\n".join(_pre_accept_strings()).lower()
    for name in NAMES:
        assert name.lower() not in haystack
    for uid in (ASKER, HOLDER, THIRD, "<@111>", "<@222>"):
        assert uid not in haystack
    # And "someone"/"they" is the vocabulary that replaces them.
    assert "someone" in request_dm_text(WANT, OFFER).lower()
    assert "they" in passed_text(WANT).lower()


def test_no_pre_accept_string_takes_an_identity_at_all() -> None:
    # Structural, not textual: these functions cannot leak a name because there
    # is no argument to pass one in. The two that can are named for it.
    import inspect

    for func in (
        ask_panel_text, request_dm_text, sent_text, passed_text, pass_ack_text,
        block_ack_text, refusal_text, describe_cell,
    ):
        params = set(inspect.signature(func).parameters)
        assert not (params & {"name", "user_id", "requester", "holder", "who"})
    for func in (accepted_text_for_holder, accepted_text_for_requester):
        assert "ref" in "".join(inspect.signature(func).parameters)


def test_every_holder_side_refusal_reads_identically() -> None:
    # If "they blocked you" looked different from "their DMs are closed", a
    # requester who cannot see who holds a cell could still learn something
    # about the person holding it.
    rendered = {refusal_text(reason) for reason in HOLDER_REASONS}
    assert len(rendered) == 1
    # ...and an undeclared reason lands on the same sentence rather than on a
    # new one, so a gate added later cannot invent its own tell.
    assert refusal_text("a brand new gate") in rendered
    # The request-side refusals are allowed to differ: they are facts about the
    # asker's own behaviour, not about anybody else.
    assert refusal_text(REASON_ALREADY_ASKED) not in rendered
    assert refusal_text(REASON_TRADES_OFF) not in rendered


def test_the_reveal_names_both_sides_and_only_then() -> None:
    holder_text = accepted_text_for_holder("<@111>", WANT, OFFER)
    asker_text = accepted_text_for_requester("<@222>", WANT, OFFER)
    assert "<@111>" in holder_text and "Thursday Eve" in holder_text
    assert "<@222>" in asker_text and "Wednesday Eve" in asker_text
    assert accepted_text_for_holder("", WANT, OFFER) is None
    assert accepted_text_for_requester("<@222>", WANT, None) is None


def test_a_cell_is_described_by_slot_and_never_by_holder() -> None:
    assert describe_cell(WANT) == "Thursday Eve"
    assert describe_cell([3, "eve"]) == "Thursday Eve"
    assert describe_cell("9-eve") is None
    assert describe_cell(None) is None


def test_the_texts_refuse_a_cell_they_cannot_render() -> None:
    assert request_dm_text("nope", OFFER) is None
    assert request_dm_text(WANT, "nope") is None
    assert sent_text(None) is None
    assert passed_text("9-am") is None
    assert ask_panel_text(None) is None


# --- non-mutation -------------------------------------------------------------


def test_nothing_mutates_the_data_it_is_given() -> None:
    prefs = _prefs(holder_changes={"no_trade_from": [THIRD]})
    rows = [_request(), _request(want=OTHER, holder=THIRD)]
    budgets = {HOLDER: _budget(NOW)}
    overrides = {WEEK: {WANT: [HOLDER], OFFER: [ASKER]}}
    snapshot = json.dumps([prefs, rows, budgets, overrides], sort_keys=True)
    may_ask(prefs, rows, budgets, ASKER, HOLDER, WANT, OFFER, WEEK, NOW,
            mine=(OFFER,))
    pick_holder(prefs, rows, budgets, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW,
                mine=(OFFER,))
    claim_request(prefs, rows, budgets, ASKER, [HOLDER], OTHER, OFFER, WEEK,
                  NOW, mine=(OFFER,))
    answer(rows, rows[0]["id"], ACTION_ACCEPT, NOW)
    apply_swap(prefs, overrides, rows[0])
    with_block(prefs, ASKER, HOLDER)
    withdraw(rows, rows[0]["id"], NOW)
    prune_requests(rows, WEEK)
    assert json.dumps([prefs, rows, budgets, overrides], sort_keys=True) == snapshot


def test_the_request_list_is_not_a_window_onto_the_store() -> None:
    rows = [_request()]
    updated = add_request(rows, _request(want=OTHER, holder=THIRD))
    updated[0]["state"] = STATE_ACCEPTED
    assert rows[0]["state"] == STATE_OPEN
    assert state_of(rows[0], NOW) == STATE_OPEN


# --- the guardrails that have to survive a lapsed ask -------------------------


def test_a_block_still_lands_after_the_ask_it_answers_has_lapsed() -> None:
    # 🚫 "Don't ask me again" is the only opt-out a pestered housemate has, and
    # the DMs most likely to still be sitting unread are the *old* ones. If it
    # needed the request to still be answerable, somebody who opens Discord on
    # Monday and taps 🚫 on Friday's DM would record nothing at all, and the
    # same requester could ask again the following week.
    rows = [_request()]
    late = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 6)
    sent = _habit.moment_ts(NOW)
    # Not answerable: no accept, no pass, no swap — an ask nobody answered must
    # not become a decline that shuts the slot to the whole house.
    assert match_request(rows, HOLDER, late, sent) is None
    assert answer(rows, rows[0]["id"], ACTION_BLOCK, late)[1] is None
    # ...but the person it is about is still resolvable, which is what lets the
    # permanent per-pair block be written.
    found = match_any_request(rows, HOLDER, sent)
    assert found is not None and found["from"] == ASKER
    prefs = _people.set_person(
        _prefs(), HOLDER, no_trade_from=with_block(_prefs(), found["from"], HOLDER)
    )
    assert is_blocked(prefs, ASKER, HOLDER) is True
    # And the request itself is untouched by that: still expired, never refused.
    assert state_of(rows[0], late) == STATE_EXPIRED
    assert slot_refused(rows, WANT, WEEK) is False
    # A stale DM still cannot pin the block on the wrong housemate.
    other = _request(requester=THIRD, want=OTHER, moment=late)
    assert match_any_request([other], HOLDER, sent) is None
    assert match_any_request([rows[0], other], HOLDER, sent)["from"] == ASKER
    assert match_any_request([], HOLDER, sent) is None


def test_a_holder_side_refusal_costs_exactly_what_a_real_ask_costs() -> None:
    # The leak this closes is not in the wording, it is in the *outcome*: if a
    # refusal cost nothing, a requester could tap the same cell every day
    # forever and read refuse-vs-send as an oracle. Several holder-side reasons
    # never change (a block is permanent; 🚫/#channel are standing choices), so
    # a cell that refuses on every probe while its neighbours go through would
    # identify its holder — and one blocked requester could watch a single
    # housemate's whole week move around the grid.
    stable = (
        {"no_trade_from": [ASKER]},                       # 🚫, permanent
        {"reminders": _people.REMIND_OFF},                # no pings
        {"reminders": _people.REMIND_CHANNEL},            # not DM-reachable
        {"dm_ok": False},                                 # DMs shut
    )
    for changes in stable:
        prefs = _prefs(holder_changes=changes)
        reason, request, rows, budgets = claim_request(
            prefs, [], {}, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
        )
        # Indistinguishable from a delivered ask, on purpose.
        assert reason == REASON_SILENT and request is not None
        assert len(rows) == 1
        # ...and it is inert: never open, so it holds nobody's slot and blocks
        # nobody's inbox, and never refused, so it does not shut the cell to the
        # rest of the house on an answer nobody gave.
        assert is_open(rows[0], NOW) is False
        assert open_to(rows, HOLDER, NOW) == [] and open_from(rows, ASKER, NOW) == []
        assert slot_refused(rows, WANT, WEEK) is False
        # No DM went out, so no DM was charged for.
        assert _habit.budget_for(budgets, HOLDER)["nudges_today"] == 0
        # The one thing it does: the probe is spent. Tomorrow, and every day
        # after it this week, the same tap is refused by the asker's own rule.
        assert asked_this_week(rows, ASKER, WANT, WEEK) is True
        tomorrow = NOW + datetime.timedelta(days=1)
        again = claim_request(
            prefs, rows, {}, ASKER, [HOLDER], WANT, OFFER, WEEK, tomorrow,
            mine=(OFFER,),
        )
        assert again[0] == REASON_ALREADY_ASKED
        assert again[2] == rows
        # A transient reason costs the same as a permanent one — otherwise the
        # difference between "charged" and "free" is itself the tell.
    busy = [_request(requester=THIRD, want=OTHER)]
    reason, _request_row, rows, _budgets = claim_request(
        _prefs(), busy, {}, ASKER, [HOLDER], WANT, OFFER, WEEK, NOW, mine=(OFFER,)
    )
    assert reason == REASON_SILENT and len(rows) == 2
    capped = claim_request(
        _prefs(), [], {HOLDER: _budget(NOW, today=1)}, ASKER, [HOLDER], WANT,
        OFFER, WEEK, NOW, mine=(OFFER,),
    )
    assert capped[0] == REASON_SILENT and len(capped[2]) == 1
    # A refusal that is a fact about the *asker* still costs nothing: there is
    # nothing to hide about your own week, and it would be a booby trap.
    for kwargs in ({"mine": ()}, {"holders": [ASKER]}, {"prefs": {}}):
        prefs = kwargs.pop("prefs", _prefs())
        reason, request, rows, _budgets = claim_request(
            prefs, [], {}, ASKER, kwargs.pop("holders", [HOLDER]), WANT, OFFER,
            WEEK, NOW, mine=kwargs.pop("mine", (OFFER,)),
        )
        assert reason not in (REASON_OK, REASON_SILENT)
        assert (request, rows) == (None, [])


def test_a_skewed_host_clock_cannot_kill_every_trade_in_the_house() -> None:
    # The row is stamped by the HA box and the DM by Discord. An RPi with no
    # RTC that has not re-synced NTP is out by minutes, and comparing those two
    # clocks directly would make every tap in the house — 🚫 included — say
    # "that one's lapsed" forever.
    row = _request()
    skew = 47 * 60  # Discord's clock, as far as this box is concerned
    dm = _habit.moment_ts(NOW) + skew
    tapped = dm + 30  # they read it half a minute later
    thirty_seconds_on = NOW + datetime.timedelta(seconds=30)
    naive = match_request([row], HOLDER, thirty_seconds_on, dm)
    assert naive is None  # what the absolute comparison would do
    sent_ts = dm_sent_ts(thirty_seconds_on, dm, tapped)
    assert match_request([row], HOLDER, thirty_seconds_on, sent_ts)["id"] == row["id"]
    assert match_any_request([row], HOLDER, sent_ts)["id"] == row["id"]
    # The stale-DM protection is untouched: a genuinely old DM is old on
    # Discord's clock too, so it still matches nothing.
    later = NOW + datetime.timedelta(hours=REQUEST_TTL_HOURS + 1)
    fresh = _request(requester=THIRD, want=OTHER, moment=later)
    old_tap = _habit.moment_ts(later) + skew
    stale = dm_sent_ts(later, dm, old_tap)
    assert match_request([fresh], HOLDER, later, stale) is None
    # An unreadable clock is not guessed at.
    assert dm_sent_ts(None, dm, tapped) is None
    assert dm_sent_ts(NOW, None, tapped) is None
    assert dm_sent_ts(NOW, dm, None) is None
    assert dm_sent_ts(NOW, tapped, dm) is None  # a tap before its own message


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
