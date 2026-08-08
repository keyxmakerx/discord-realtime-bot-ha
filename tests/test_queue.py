"""Tests for the pure "I'm next" queue helpers.

Runnable with plain ``python3 tests/test_queue.py`` — no pytest / Home
Assistant, mirroring ``tests/test_energy_detector.py``. ``queue.py`` is loaded
by file path so importing it does not pull in the package ``__init__`` (which
imports Home Assistant), and under a module name that can't collide with the
standard library's ``queue``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_QUEUE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "queue.py",
)
_spec = importlib.util.spec_from_file_location("ld_queue", _QUEUE_PATH)
_queue = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _queue
_spec.loader.exec_module(_queue)

QUEUE_CAP = _queue.QUEUE_CAP
TOGGLE_ADDED = _queue.TOGGLE_ADDED
TOGGLE_REMOVED = _queue.TOGGLE_REMOVED
TOGGLE_FULL = _queue.TOGGLE_FULL
TOGGLE_STALE = _queue.TOGGLE_STALE
attributes = _queue.attributes
carry_forward = _queue.carry_forward
find = _queue.find
format_queue = _queue.format_queue
handoff_line = _queue.handoff_line
names = _queue.names
next_in_line = _queue.next_in_line
ordinal = _queue.ordinal
position = _queue.position
prune = _queue.prune
remove_user = _queue.remove_user
same_user = _queue.same_user
select_handoff = _queue.select_handoff
tap_notice = _queue.tap_notice
toggle_member = _queue.toggle_member

HOUR = 3600.0
EXPIRY = 12 * HOUR  # the shipped default


def _names(queue):
    return [e["name"] for e in queue]


# --- toggling ---------------------------------------------------------------


def test_toggle_adds_to_the_back() -> None:
    q, res = toggle_member([], 1, "Sam", 0.0)
    assert res == TOGGLE_ADDED
    assert _names(q) == ["Sam"]
    q, res = toggle_member(q, 2, "Ty", 10.0)
    assert res == TOGGLE_ADDED
    assert _names(q) == ["Sam", "Ty"]  # FIFO: Sam stays at the head


def test_double_tap_removes_rather_than_duplicating() -> None:
    q, _ = toggle_member([], 1, "Sam", 0.0)
    q, res = toggle_member(q, 1, "Sam", 5.0)
    assert res == TOGGLE_REMOVED
    assert q == []


def test_toggle_removes_from_the_middle_without_reordering() -> None:
    q = []
    for i, name in enumerate(["Sam", "Ty", "Jo"]):
        q, _ = toggle_member(q, i, name, float(i))
    q, res = toggle_member(q, 1, "Ty", 99.0)
    assert res == TOGGLE_REMOVED
    assert _names(q) == ["Sam", "Jo"]


def test_toggle_does_not_mutate_the_input() -> None:
    # The coordinator assigns the result; a rejected tap must not half-apply.
    original = [{"id": 1, "name": "Sam", "ts": 0.0}]
    snapshot = json.dumps(original)
    toggle_member(original, 2, "Ty", 1.0)
    toggle_member(original, 1, "Sam", 1.0)
    assert json.dumps(original) == snapshot


def test_cap_is_enforced_at_exactly_the_limit() -> None:
    q = []
    for i in range(QUEUE_CAP):
        q, res = toggle_member(q, i, f"P{i}", float(i))
        assert res == TOGGLE_ADDED
    assert len(q) == QUEUE_CAP
    full_q, res = toggle_member(q, 999, "Late", 100.0)
    assert res == TOGGLE_FULL
    assert full_q == q  # unchanged
    # Somebody already in the line can still leave when it's full.
    q, res = toggle_member(q, 0, "P0", 101.0)
    assert res == TOGGLE_REMOVED
    assert len(q) == QUEUE_CAP - 1


# --- expiry -----------------------------------------------------------------


def test_prune_drops_entries_at_or_past_the_expiry() -> None:
    q = [
        {"id": 1, "name": "Old", "ts": 0.0},
        {"id": 2, "name": "Fresh", "ts": EXPIRY - 1},
    ]
    # Exactly at the boundary counts as expired (age >= expiry).
    assert _names(prune(q, EXPIRY, EXPIRY)) == ["Fresh"]
    # One second earlier, both survive.
    assert _names(prune(q, EXPIRY - 1, EXPIRY)) == ["Old", "Fresh"]


def test_prune_disabled_when_expiry_is_zero() -> None:
    q = [{"id": 1, "name": "Ancient", "ts": 0.0}]
    assert prune(q, 10 * EXPIRY, 0) == q


def test_prune_drops_entries_with_no_usable_timestamp() -> None:
    # A corrupt entry can never age out, so keeping it would strand the line.
    q = [
        {"id": 1, "name": "NoTs"},
        {"id": 2, "name": "BadTs", "ts": "soon"},
        {"id": 3, "name": "Good", "ts": 0.0},
    ]
    assert _names(prune(q, 1.0, EXPIRY)) == ["Good"]


# --- carry-forward ----------------------------------------------------------


def test_carry_forward_drops_the_claimant_and_keeps_the_rest() -> None:
    # A's load finishes, B takes the machine, C should still be next.
    q = [
        {"id": 2, "name": "B", "ts": 0.0},
        {"id": 3, "name": "C", "ts": 1.0},
    ]
    rolled = carry_forward(q, 2, 10.0, EXPIRY)
    assert _names(rolled) == ["C"]


def test_carry_forward_keeps_everyone_when_nobody_claimed() -> None:
    q = [{"id": 2, "name": "B", "ts": 0.0}, {"id": 3, "name": "C", "ts": 1.0}]
    assert _names(carry_forward(q, None, 10.0, EXPIRY)) == ["B", "C"]


def test_carry_forward_also_prunes() -> None:
    q = [
        {"id": 2, "name": "Stale", "ts": 0.0},
        {"id": 3, "name": "Fresh", "ts": EXPIRY},
    ]
    assert _names(carry_forward(q, None, EXPIRY, EXPIRY)) == ["Fresh"]


# --- the handoff selection --------------------------------------------------


def test_select_handoff_returns_the_head_and_pops_it() -> None:
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 1.0}]
    head, rest = select_handoff(q, 10.0, EXPIRY, None)
    assert head["name"] == "Sam"
    assert _names(rest) == ["Ty"]  # popped, so the fallback can't re-ping Sam


def test_select_handoff_on_an_empty_line_pops_nothing() -> None:
    # The normal case: a load finishes and nobody is waiting.
    assert select_handoff([], 10.0, EXPIRY, None) == (None, [])


def test_select_handoff_never_hands_the_washer_to_its_own_claimant() -> None:
    # Sam queued during Alex's load, then took the machine themselves. Sam must
    # not get "washer's free" for the load Sam is running — Ty is next.
    q = [{"id": 2, "name": "Sam", "ts": 0.0}, {"id": 3, "name": "Ty", "ts": 1.0}]
    head, rest = select_handoff(q, 10.0, EXPIRY, 2)
    assert head["name"] == "Ty"
    assert rest == []
    # ...and with Sam alone in the line, nobody is pinged at all.
    assert select_handoff(q[:1], 10.0, EXPIRY, 2) == (None, [])


def test_select_handoff_excludes_a_claimant_across_id_types() -> None:
    # Ids come back off the Store as strings; the claimant id is an int.
    q = [{"id": "2", "name": "Sam", "ts": 0.0}]
    assert select_handoff(q, 10.0, EXPIRY, 2) == (None, [])


def test_select_handoff_skips_a_stale_head_rather_than_pinging_it() -> None:
    # An entry past expiry must not absorb the handoff and strand the person
    # who is actually standing there with a basket.
    q = [
        {"id": 1, "name": "Yesterday", "ts": 0.0},
        {"id": 2, "name": "Fresh", "ts": EXPIRY},
    ]
    head, rest = select_handoff(q, EXPIRY + 1, EXPIRY, None)
    assert head["name"] == "Fresh"
    assert rest == []


def test_select_handoff_does_not_mutate_the_input() -> None:
    original = [{"id": 1, "name": "Sam", "ts": 0.0}]
    snapshot = json.dumps(original)
    select_handoff(original, 10.0, EXPIRY, None)
    select_handoff(original, 10.0, EXPIRY, 1)
    assert json.dumps(original) == snapshot


def test_remove_user_drops_the_claimer_and_tolerates_none() -> None:
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 1.0}]
    assert _names(remove_user(q, 1)) == ["Ty"]
    assert _names(remove_user(q, "1")) == ["Ty"]
    assert _names(remove_user(q, 99)) == ["Sam", "Ty"]
    # An unclaimed load passes None; that must remove nobody, including an
    # entry whose id failed to persist.
    assert remove_user(q, None) == q
    assert remove_user([{"id": None, "name": "Odd", "ts": 0.0}], None) != []


# --- store round-trip -------------------------------------------------------


def test_ids_still_match_after_a_json_round_trip() -> None:
    # HA's Store serialises to JSON. Whatever the ids come back as, a tap from
    # interaction.user.id (an int) must still find the existing entry.
    q, _ = toggle_member([], 12345, "Sam", 0.0)
    restored = json.loads(json.dumps(q))
    assert find(restored, 12345) is not None
    assert find(restored, "12345") is not None
    q2, res = toggle_member(restored, 12345, "Sam", 5.0)
    assert res == TOGGLE_REMOVED
    assert q2 == []


def test_same_user_compares_across_types() -> None:
    assert same_user({"id": 7}, 7)
    assert same_user({"id": "7"}, 7)
    assert same_user({"id": 7}, "7")
    assert not same_user({"id": 7}, 70)
    assert not same_user({}, 7)


def test_carry_forward_drops_a_string_id_claimant() -> None:
    # The persisted entry is a string; the claimant id is an int.
    q = json.loads(json.dumps([{"id": 2, "name": "B", "ts": 0.0}]))
    q[0]["id"] = "2"
    assert carry_forward(q, 2, 1.0, EXPIRY) == []


# --- accessors / formatting -------------------------------------------------


def test_next_in_line_and_position() -> None:
    assert next_in_line([]) is None
    assert position([], 1) is None
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 1.0}]
    assert next_in_line(q)["name"] == "Sam"
    assert position(q, 1) == 1
    assert position(q, 2) == 2
    assert position(q, 3) is None


def test_format_queue() -> None:
    assert format_queue([]) is None
    q = [{"id": i, "name": n, "ts": 0.0} for i, n in enumerate(["Sam", "Ty", "Jo"])]
    assert format_queue(q[:1]) == "Sam"
    assert format_queue(q[:2]) == "Sam, then Ty"
    assert format_queue(q) == "Sam, then Ty, then Jo"
    big = q + [{"id": 9, "name": "Kim", "ts": 0.0}, {"id": 10, "name": "Ash", "ts": 0.0}]
    assert format_queue(big) == "Sam, then Ty, then Jo (+2 more)"


def test_format_queue_survives_a_nameless_entry() -> None:
    assert format_queue([{"id": 1, "ts": 0.0}]) == "someone"


# --- the HA attribute -------------------------------------------------------


def test_names_is_order_preserving() -> None:
    q = [{"id": i, "name": n, "ts": 0.0} for i, n in enumerate(["Sam", "Ty", "Jo"])]
    assert names(q) == ["Sam", "Ty", "Jo"]
    assert names([]) == []
    assert names([{"id": 1, "ts": 0.0}]) == ["someone"]


def test_names_carries_nothing_derived_from_the_clock() -> None:
    """The recorder-churn guard: the same line must render identically later.

    An attribute that changed on the 5-minute health tick would write ~288
    history rows a day forever — the bug the connection-health sensor was
    already fixed for. Names cannot drift; timestamps and anything computed
    from them can, so none of them are here.
    """
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 1.0}]
    assert names(q) == names(q)
    aged = [dict(e) for e in q]  # same people, read a very long time later
    assert names(aged) == names(q)
    assert all(isinstance(n, str) for n in names(q))


# --- what the tapper is told ------------------------------------------------


def test_ordinal() -> None:
    assert [ordinal(n) for n in (1, 2, 3, 4, 5)] == ["1st", "2nd", "3rd", "4th", "5th"]
    # The 11/12/13 exception, in case the cap is ever raised.
    assert [ordinal(n) for n in (11, 12, 13)] == ["11th", "12th", "13th"]
    assert [ordinal(n) for n in (21, 22, 23, 101)] == ["21st", "22nd", "23rd", "101st"]


def test_tap_notice_joining_names_the_place() -> None:
    assert "**2nd**" in tap_notice(TOGGLE_ADDED, 2)
    assert "**3rd**" in tap_notice(TOGGLE_ADDED, 3)
    # First in line is told "next", not "1st" — and warned that a finished
    # washer is not an empty one, which is the whole reason for the ✅ tap.
    first = tap_notice(TOGGLE_ADDED, 1)
    assert "**next**" in first
    assert "1st" not in first


def test_tap_notice_tells_joining_and_leaving_apart() -> None:
    """The actual bug: the shared card looks the same either way."""
    joined = tap_notice(TOGGLE_ADDED, 2)
    left = tap_notice(TOGGLE_REMOVED, None)
    assert joined and left
    assert joined != left
    assert "out of the line" in left


def test_tap_notice_is_silent_where_the_response_already_spoke() -> None:
    # TOGGLE_FULL / TOGGLE_STALE answer the interaction with their own
    # ephemeral; a followup here would say it twice.
    assert tap_notice(TOGGLE_FULL, None) is None
    assert tap_notice(TOGGLE_STALE, None) is None
    assert tap_notice("something else entirely", 1) is None


def test_tap_notice_still_confirms_without_a_place() -> None:
    notice = tap_notice(TOGGLE_ADDED, None)
    assert notice and "line" in notice


def test_position_feeds_tap_notice() -> None:
    """The two halves of the fix, joined up as the button joins them."""
    q, _ = toggle_member([], 1, "Sam", 0.0)
    q, res = toggle_member(q, 2, "Ty", 10.0)
    assert "**2nd**" in tap_notice(res, position(q, 2))
    # ...and leaving really does leave.
    q, res = toggle_member(q, 2, "Ty", 20.0)
    assert res == TOGGLE_REMOVED
    assert position(q, 2) is None


# --- the done card's record of the handoff ----------------------------------


def test_handoff_line_keeps_the_hedge_distinct() -> None:
    confirmed = handoff_line("Sam", hedged=False)
    backstop = handoff_line("Sam", hedged=True)
    assert "Sam" in confirmed and "Sam" in backstop
    assert confirmed != backstop
    # The backstop must never claim somebody confirmed the drum was clear.
    assert "nobody confirmed" in backstop
    assert "told the washer's free" in confirmed


def test_handoff_line_survives_a_nameless_entry() -> None:
    assert "someone" in handoff_line(None, hedged=False)
    assert "someone" in handoff_line("", hedged=True)


def test_handoff_line_names_whoever_select_handoff_popped() -> None:
    """The vanishing act: the head is gone from the line by design."""
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 1.0}]
    head, rest = select_handoff(q, 100.0, EXPIRY, claimant_id=None)
    assert position(rest, 1) is None  # Sam is off the line entirely...
    assert "Sam" in handoff_line(_queue.entry_name(head), hedged=False)  # ...but named


def test_attributes_publish_the_line_that_would_actually_act() -> None:
    """The plain case: everyone fresh, nobody claiming."""
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 0.0}]
    assert attributes(q, 100.0, EXPIRY, None) == {
        "queue_count": 2,
        "queue": ["Sam", "Ty"],
        "next_up": "Sam",
    }
    assert attributes([], 100.0, EXPIRY, None) == {
        "queue_count": 0,
        "queue": [],
        "next_up": None,
    }


def test_attributes_never_name_somebody_the_handoff_would_skip() -> None:
    """The stored line is pruned on tap/start/handoff — never on read.

    Sam taps 🔜 in the evening and goes away. Nothing touches the line
    overnight, so by morning ``coordinator.queue`` still holds an entry that
    :func:`select_handoff` would drop. Reading it raw had the dashboard
    announcing a person who is provably not getting the machine.
    """
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 11 * HOUR}]
    aged = attributes(q, 13 * HOUR, EXPIRY, None)  # EXPIRY is 12h
    assert aged == {"queue_count": 1, "queue": ["Ty"], "next_up": "Ty"}
    # Everybody aged out: an empty line, not a stale head.
    assert attributes(q, 40 * HOUR, EXPIRY, None)["next_up"] is None
    # ...and expiry disabled keeps them, exactly as the handoff would.
    assert attributes(q, 40 * HOUR, 0.0, None)["next_up"] == "Sam"


def test_attributes_never_name_the_claimant_as_next_up() -> None:
    """Claiming takes you out of the line; tapping 🔜 after can put you back.

    ``select_handoff`` refuses to hand the machine to whoever is using it, so
    the attribute must not promise it either — otherwise the card says the
    claimant is up next and the ping goes to the person behind them.
    """
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 0.0}]
    attrs = attributes(q, 100.0, EXPIRY, claimant_id=1)
    assert attrs["next_up"] == "Ty"
    # The line itself still shows them, matching the Discord card's rendering.
    assert attrs["queue"] == ["Sam", "Ty"]
    # A claimant alone in the line means nobody is up next, not "Sam".
    assert attributes(q[:1], 100.0, EXPIRY, claimant_id=1)["next_up"] is None
    # String/int ids round-trip through the Store; both must match.
    assert attributes(q, 100.0, EXPIRY, claimant_id="1")["next_up"] == "Ty"


def test_attributes_agree_with_select_handoff_on_who_is_up() -> None:
    """Guards the two from drifting apart — the whole point of the fix."""
    q = [
        {"id": 1, "name": "Sam", "ts": 0.0},  # stale
        {"id": 2, "name": "Ty", "ts": 11 * HOUR},  # the claimant
        {"id": 3, "name": "Jo", "ts": 11 * HOUR},
    ]
    now = 13 * HOUR
    head, _rest = select_handoff(q, now, EXPIRY, claimant_id=2)
    assert attributes(q, now, EXPIRY, claimant_id=2)["next_up"] == _queue.entry_name(
        head
    )


def test_attributes_carry_nothing_derived_from_the_clock() -> None:
    """The recorder-churn guard, now that ``now`` is an input.

    Pruning needs the wall clock, which is exactly the shape of thing that
    wrote ~288 rows a day on the connection-health sensor. It is safe here
    because the clock only decides *membership*: between two ticks with nobody
    expiring, the attribute dict must be identical, so HA suppresses the
    state_changed event and no row is written.
    """
    q = [{"id": 1, "name": "Sam", "ts": 0.0}, {"id": 2, "name": "Ty", "ts": 0.0}]
    ticks = [attributes(q, 100.0 + 300.0 * i, EXPIRY, None) for i in range(12)]
    assert all(t == ticks[0] for t in ticks)  # a full hour of health ticks
    assert set(ticks[0]) == {"queue_count", "queue", "next_up"}
    assert not any(isinstance(v, float) for v in ticks[0].values())


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
