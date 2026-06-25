"""Unit tests for the pure offline-load detection math.

Runnable with plain ``python3 tests/test_detect.py`` — no pytest or Home
Assistant required. ``detect.py`` is loaded by file path so importing it does
not pull in the package ``__init__`` (which imports Home Assistant).
"""

from __future__ import annotations

import importlib.util
import os
import sys

_DETECT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "detect.py",
)
_spec = importlib.util.spec_from_file_location("ld_detect", _DETECT_PATH)
_detect = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _detect  # dataclass needs the module registered
_spec.loader.exec_module(_detect)
energy_jumped = _detect.energy_jumped
load_is_active = _detect.load_is_active
session_too_long = _detect.session_too_long
offline_completion_due = _detect.offline_completion_due

# Load const by path too (no Home Assistant imports) so the phase sets the test
# uses stay in lockstep with the integration.
_CONST_PATH = os.path.join(os.path.dirname(_DETECT_PATH), "const.py")
_cspec = importlib.util.spec_from_file_location("ld_const", _CONST_PATH)
_const = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(_const)
REAL_PHASES = _const.REAL_PHASES
MIDCYCLE_PHASES = _const.MIDCYCLE_PHASES
FINISH = _const.JOB_STATE_FINISH

THRESHOLD = 0.3


def _active(phase, energy, completion):
    return load_is_active(phase, REAL_PHASES, FINISH, MIDCYCLE_PHASES, energy, completion)


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


def test_fresh_early_phase_starts_despite_flat_meter() -> None:
    # THE REGRESSION: a new load's job goes weight_sensing/wash while the energy
    # meter still reads the previous completion (it lags 15-45 min). Must START.
    assert _active("weight_sensing", energy=14.8, completion=14.8) is True
    assert _active("wash", energy=14.8, completion=14.8) is True
    # ...and obviously when the meter has nothing to compare against.
    assert _active("weight_sensing", energy=None, completion=None) is True


def test_finish_and_non_phases_never_start() -> None:
    assert _active(FINISH, energy=15.0, completion=14.8) is False
    assert _active("none", energy=15.0, completion=14.8) is False
    assert _active(None, energy=15.0, completion=14.8) is False


def test_frozen_late_phase_does_not_restart() -> None:
    # A mid/late phase stuck at exactly the completion reading = stale leftover.
    assert _active("drying", energy=14.8, completion=14.8) is False
    assert _active("spin", energy=14.8, completion=14.8) is False


def test_real_midcycle_catchup_starts() -> None:
    # Same late phase but the meter has moved past completion = a real catch-up.
    assert _active("drying", energy=15.2, completion=14.8) is True


MAXS = 720 * 60     # max_session safety net (s)


def test_session_too_long_is_the_final_safety_net() -> None:
    # Force-finish a load that has run max_session (e.g. estimate frozen in the
    # future, no 'finish'): not before, yes at/after the cap.
    assert session_too_long(0, MAXS - 1, MAXS) is False
    assert session_too_long(0, MAXS, MAXS) is True
    assert session_too_long(0, MAXS + 10_000, MAXS) is True


def test_session_too_long_inert_when_not_tracking() -> None:
    # No session start => never fires (nothing is being tracked).
    assert session_too_long(None, MAXS * 5, MAXS) is False


OFF = 60 * 60       # offline_after (s)
EGRACE = 30 * 60    # offline completion grace past the last ETA (s)


def _offdue(offline_since, last_eta_ts, now):
    return offline_completion_due(
        offline_since=offline_since,
        last_eta_ts=last_eta_ts,
        now=now,
        offline_after=OFF,
        eta_grace=EGRACE,
    )


def test_offline_completion_needs_long_offline_and_passed_eta() -> None:
    # Offline 70 min, ETA was 40 min ago (> 30 grace) => complete (unverified).
    assert _offdue(offline_since=0, last_eta_ts=30 * 60, now=70 * 60) is True


def test_offline_completion_waits_for_eta_plus_grace() -> None:
    # Offline long enough, but only 10 min past the ETA (< 30 grace) => not yet.
    assert _offdue(offline_since=0, last_eta_ts=60 * 60, now=70 * 60) is False


def test_offline_completion_needs_sustained_offline() -> None:
    # ETA long passed, but offline only 20 min (< 60) => don't fire on a blip.
    assert _offdue(offline_since=50 * 60, last_eta_ts=0, now=70 * 60) is False


def test_offline_completion_inert_without_offline_or_eta() -> None:
    # Online (no offline_since) or no known ETA => never fires.
    assert _offdue(offline_since=None, last_eta_ts=0, now=10 * 60 * 60) is False
    assert _offdue(offline_since=0, last_eta_ts=None, now=10 * 60 * 60) is False


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
