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
carry_forward = _queue.carry_forward
find = _queue.find
format_queue = _queue.format_queue
next_in_line = _queue.next_in_line
position = _queue.position
prune = _queue.prune
same_user = _queue.same_user
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


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
