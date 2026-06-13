"""Unit tests for the pure offline-load detection math.

Runnable with plain ``python3 tests/test_detect.py`` — no pytest or Home
Assistant required. ``detect.py`` is loaded by file path so importing it does
not pull in the package ``__init__`` (which imports Home Assistant).
"""

from __future__ import annotations

import importlib.util
import os

_DETECT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "detect.py",
)
_spec = importlib.util.spec_from_file_location("ld_detect", _DETECT_PATH)
_detect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_detect)
energy_jumped = _detect.energy_jumped

THRESHOLD = 0.3


def _feed(samples: list[float | None], threshold: float = THRESHOLD) -> list[int]:
    """Replay a meter stream through the coordinator's baseline-advance rule.

    Mirrors ``_evaluate_energy_start``: skip ``None`` (unavailable) and flat
    samples, advance the baseline on every change (so a decrease rebaselines),
    and flag the indices where a single-sample jump trips the threshold.
    """
    last: float | None = None
    triggers: list[int] = []
    for i, cur in enumerate(samples):
        if cur is None:  # unavailable: hold last good, not a sample
            continue
        if cur == last:  # flat: nothing changed
            continue
        prev, last = last, cur
        if energy_jumped(prev, cur, threshold):
            triggers.append(i)
    return triggers


def test_jump_primitive() -> None:
    assert energy_jumped(None, 12.8, THRESHOLD) is False  # first sample
    assert energy_jumped(11.9, None, THRESHOLD) is False  # unavailable
    assert energy_jumped(11.9, 12.8, THRESHOLD) is True  # batch jump
    assert energy_jumped(11.0, 11.1, THRESHOLD) is False  # creep step
    assert energy_jumped(12.8, 11.0, THRESHOLD) is False  # decrease / reset
    # Exact-threshold boundary must trip despite float subtraction slop.
    assert energy_jumped(11.0, 11.3, THRESHOLD) is True


def test_offline_batch_triggers_once() -> None:
    # Flat all day, an unavailable flap, then the cloud dumps the load at once.
    assert _feed([11.9, 11.9, None, 11.9, 12.8]) == [4]


def test_small_steps_never_trigger() -> None:
    # A meter reported in <=0.2 kWh steps never trips on step size alone. (A
    # coarse high-power online sample *can* exceed 0.3 — that live case is
    # handled by the job-dark gate in the coordinator, not by this math.)
    assert _feed([11.0, 11.1, 11.2, 11.4, 11.6, 11.8]) == []


def test_wrinkle_prevent_creep_never_triggers() -> None:
    # Post-cycle tumbling nudges the meter 0.1 at a time for hours; cumulative
    # rise is 0.5 kWh but no *single* step reaches 0.3 — must not false-fire.
    assert _feed([11.9, 12.0, 12.1, 12.2, 12.3, 12.4]) == []


def test_meter_reset_rebaselines_and_still_detects() -> None:
    # total_increasing meter resets mid-stream; the decrease must rebaseline (no
    # trigger) and a later real load must STILL be caught — not made invisible.
    assert _feed([12.8, 5.0, 5.0, 5.9]) == [3]


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
