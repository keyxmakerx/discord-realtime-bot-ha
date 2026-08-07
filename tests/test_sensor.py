"""Tests for the wiring in ``sensor.py`` that no pure test can reach.

``sensor.py`` imports Home Assistant, so it cannot be loaded here the way
``queue.py`` is (see ``tests/test_queue.py``). What matters about it, though,
is declarative — which attributes are kept out of recorder history, and that
the 🔜 line is read through the pruning helper rather than raw — so it is read
with :mod:`ast` instead. Runnable with plain ``python3 tests/test_sensor.py``.

The behaviour of the attribute itself is covered by ``test_queue.py``.
"""

from __future__ import annotations

import ast
import os

STAGE_SENSOR = "LaundryStageSensor"
_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "sensor.py",
)

with open(_PATH, encoding="utf-8") as _fh:
    _TREE = ast.parse(_fh.read(), filename=_PATH)


def _class(name: str) -> ast.ClassDef:
    for node in _TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from sensor.py")


def _class_attr(cls: ast.ClassDef, name: str):
    """The literal value assigned to ``name`` in the class body, or None."""
    for node in cls.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return _literal(node.value)
    return None


def _literal(node: ast.expr):
    """``literal_eval``, plus the ``frozenset({...})`` HA asks these to be."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("frozenset", "set")
    ):
        return frozenset(_literal(node.args[0]) if node.args else ())
    return ast.literal_eval(node)


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{cls.name}.{name} is gone")


def test_the_waiters_names_are_kept_out_of_recorder_history() -> None:
    """Design doc §11: no per-person tally is ever surfaced to the household.

    The recorder writes a row whenever a state *or an attribute* changes, and
    keeps it for the retention window. With the names persisted, every 🔜 tap
    leaves a timestamped record of who joined and who left, out of which "Sam
    queued 14 times this week" falls directly — a stat the Discord card does
    not leave behind, because it is edited in place. The names still ride on
    the live state; only their history is dropped.
    """
    unrecorded = _class_attr(_class(STAGE_SENSOR), "_unrecorded_attributes")
    assert unrecorded is not None, "the queue names are being written to history"
    assert {"queue", "next_up"} <= set(unrecorded)


def test_the_count_stays_recorded() -> None:
    """It names nobody, and it is the half of this a dashboard graphs."""
    unrecorded = _class_attr(_class(STAGE_SENSOR), "_unrecorded_attributes")
    assert "queue_count" not in set(unrecorded or ())


def test_the_attribute_is_read_through_the_pruning_helper() -> None:
    """Not ``names(coordinator.queue)`` — that publishes expired entries.

    The stored line is pruned only on a tap, a session start or a handoff, so
    a raw read can name somebody ``select_handoff`` would drop.
    """
    body = _method(_class(STAGE_SENSOR), "extra_state_attributes")
    calls = {
        node.func.attr
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "attributes" in calls, "the queue attribute is not being pruned on read"


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
