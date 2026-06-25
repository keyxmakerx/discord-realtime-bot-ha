"""Energy-primary liveness state-machine tests, replayed against real traces.

Runnable with plain ``python3 tests/test_energy_detector.py`` — no pytest / Home
Assistant. ``detect.py`` is loaded by file path so importing it does not pull in
the package ``__init__`` (which imports Home Assistant).

The meter traces below are real captures from the washer (SmartThings history),
converted to (seconds-from-start, kWh). 'M' = 15 min, the meter's active update
gap on this machine.
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

EnergyDetector = _detect.EnergyDetector
EV_STARTED = _detect.EV_STARTED
EV_FINISHED = _detect.EV_FINISHED
RUN_ACTIVE = _detect.RUN_ACTIVE
RUN_IDLE = _detect.RUN_IDLE

M = 900  # 15 minutes, in seconds
IDLE = 3600  # default idle_timeout used in these tests


def _poll_flat(det, start_ts, energy, *, until, every=300, **kw):
    """Feed flat (unchanged) samples on a polling cadence; return last event."""
    ev = None
    t = start_ts
    while t <= until:
        ev = det.observe(t, energy, **kw) or ev
        t += every
    return ev


# --- real online load (Jun 12 14:06->15:51, 11.0->11.9, then flat) ---------
ONLINE_TRACE = [
    (0 * M, 11.0),
    (2 * M, 11.1),
    (3 * M, 11.2),
    (4 * M, 11.4),
    (5 * M, 11.5),
    (6 * M, 11.6),
    (7 * M, 11.8),
    (8 * M, 11.9),
]


def test_online_load_starts_on_accelerant_then_finishes_on_flat() -> None:
    det = EnergyDetector(idle_timeout=IDLE)
    # job_state shows weight_sensing while the meter still reads the prior
    # value (the real lag) — must START immediately via the accelerant.
    assert det.observe(-30, 11.0, job_is_early=True) == EV_STARTED
    assert det.phase == RUN_ACTIVE
    # Replay the rising meter: no further events while it climbs.
    for ts, e in ONLINE_TRACE:
        assert det.observe(ts, e) is None
    # Flat after the last rise (8*M); finishes once flat exceeds idle_timeout.
    last_rise = 8 * M
    assert _poll_flat(det, last_rise + 300, 11.9, until=last_rise + IDLE - 300) is None
    assert det.observe(last_rise + IDLE, 11.9) == EV_FINISHED
    assert det.phase == RUN_IDLE


def test_offline_batch_starts_then_finishes() -> None:
    # Job dark all cycle; the meter sat at 11.9, then the cloud dumps 12.8.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 11.9) is None  # first sample: just baselines
    assert det.observe(M, 11.9) is None
    assert det.observe(2 * M, 12.8) == EV_STARTED  # +0.9 single jump
    assert det.observe(2 * M + IDLE, 12.8) == EV_FINISHED


def test_back_to_back_loads() -> None:
    # The case the old completion-energy guard broke: load 2 right after load 1.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 14.8, job_is_early=True) == EV_STARTED
    assert det.observe(M, 15.7) is None  # load 1 runs
    assert det.observe(M + IDLE, 15.7) == EV_FINISHED  # load 1 done
    # Load 2 begins moments later — energy still at load 1's completion value.
    assert det.observe(M + IDLE + 60, 15.7, job_is_early=True) == EV_STARTED
    assert det.phase == RUN_ACTIVE


def test_removed_early_finishes_on_flat() -> None:
    # Someone pulls the load mid-cycle: energy just stops. Finishes on timeout.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 20.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 20.2) is None  # a little ran
    # ...then removed; meter flat from 1*M onward.
    assert _poll_flat(det, M + 300, 20.2, until=M + IDLE - 300) is None
    assert det.observe(M + IDLE, 20.2) == EV_FINISHED


def test_pause_to_add_a_sock_does_not_false_finish() -> None:
    # Open door / pause < idle_timeout, then resume — must NOT finish early.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 30.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 30.2) is None
    # Paused for 50 min (< 60 min timeout): flat polls, no finish.
    assert _poll_flat(det, M + 300, 30.2, until=M + 3000) is None
    # Resume: meter climbs again, re-arming the timer.
    assert det.observe(M + 3300, 30.4) is None
    assert det.phase == RUN_ACTIVE


def test_job_finish_completes_immediately() -> None:
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 40.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 40.5) is None
    assert det.observe(2 * M, 40.9, job_is_finish=True) == EV_FINISHED
    assert det.phase == RUN_IDLE


def test_wrinkle_creep_while_idle_never_starts() -> None:
    # Post-cycle tumbling nudges the meter 0.1 at a time; no single jump, and
    # no early phase => never a false start.
    det = EnergyDetector(idle_timeout=IDLE)
    det.observe(0, 12.8)
    for i, e in enumerate([12.9, 13.0, 13.1, 13.2, 13.3], start=1):
        assert det.observe(i * M, e) is None
    assert det.phase == RUN_IDLE


def test_wrinkle_active_does_not_keep_finished_load_alive() -> None:
    # With the wrinkle sensor wired in, tumbling rises do NOT re-arm the timer.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 50.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 50.9) is None  # real load energy
    # Cycle ends; wrinkle-prevent tumbles, nudging energy every 30 min but
    # flagged active — must still finish on the real-energy idle timeout.
    t = M
    fin = None
    for _ in range(6):
        t += 1800
        fin = det.observe(t, det.last_energy + 0.1, wrinkle_active=True) or fin
    assert fin == EV_FINISHED


def test_meter_reset_is_safe() -> None:
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 60.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 0.0) is None  # meter reset mid-load: no false finish
    assert det.phase == RUN_ACTIVE
    # Back to idle, then a reset-from-high while idle must not start anything.
    det2 = EnergyDetector(idle_timeout=IDLE)
    det2.observe(0, 60.0)
    assert det2.observe(M, 0.0) is None
    assert det2.phase == RUN_IDLE


def test_midcycle_catchup_when_energy_moved() -> None:
    # Only a late phase ever appears (early phases were dark). It counts as a
    # catch-up because the meter has risen since we went idle.
    det = EnergyDetector(idle_timeout=IDLE)
    det.observe(0, 18.0)  # baseline
    det.reset()  # idle_energy = 18.0
    assert det.observe(M, 18.5, job_is_real=True) == EV_STARTED


def test_frozen_late_phase_after_finish_does_not_restart() -> None:
    # job_state stuck at a late phase after completion, meter flat at the
    # completion reading => stale leftover, must NOT restart.
    det = EnergyDetector(idle_timeout=IDLE)
    det.observe(0, 20.0, job_is_early=True)  # a load
    det.observe(M, 20.9)
    det.observe(M, 20.9, job_is_finish=True)  # finished; idle_energy = 20.9
    assert det.phase == RUN_IDLE
    # Now job is frozen at 'spin' (real) but energy is unchanged.
    assert det.observe(2 * M, 20.9, job_is_real=True) is None
    assert det.phase == RUN_IDLE


EG = 1200  # default eta_grace used in these tests (20 min settle)


def test_eta_gate_completes_when_estimate_passed_and_meter_settled() -> None:
    # Normal online completion: the washer's estimate has passed and the meter
    # has been flat for eta_grace -> done.
    det = EnergyDetector(idle_timeout=IDLE)  # eta_grace defaults to EG
    assert det.observe(0, 20.0, job_is_early=True) == EV_STARTED
    assert det.observe(M, 20.5) is None  # last real rise at 1*M
    last = M
    # Estimate passed, meter still settling: not done until eta_grace elapses.
    assert _poll_flat(
        det, last + 300, 20.5,
        until=last + EG - 300, has_eta=True, eta_passed=True,
    ) is None
    assert det.phase == RUN_ACTIVE
    assert det.observe(last + EG, 20.5, has_eta=True, eta_passed=True) == EV_FINISHED
    assert det.phase == RUN_IDLE


def test_eta_gate_blocks_completion_until_estimate_passes() -> None:
    # THE false-done fix. The meter is dead-flat from the start (it froze/read
    # flat for a whole load on a real capture) AND job_state is stuck — but the
    # washer's estimate is still in the future, so the load must NOT finish, even
    # after the meter has been flat far longer than idle_timeout.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 14.8, job_is_early=True) == EV_STARTED
    assert _poll_flat(
        det, 300, 14.8, until=3 * IDLE, has_eta=True, eta_passed=False
    ) is None
    assert det.phase == RUN_ACTIVE
    # Estimate finally passes; the meter long settled -> done.
    assert det.observe(3 * IDLE + 1, 14.8, has_eta=True, eta_passed=True) == EV_FINISHED
    assert det.phase == RUN_IDLE


def test_offline_load_uses_idle_timeout_when_no_estimate() -> None:
    # No washer estimate (has_eta False) => the flat-energy idle-timeout backstop
    # closes a genuinely offline load.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 11.9) is None
    assert det.observe(M, 12.8) == EV_STARTED  # offline batch jump
    assert _poll_flat(det, M + 300, 12.8, until=M + IDLE - 300) is None
    assert det.observe(M + IDLE, 12.8) == EV_FINISHED
    assert det.phase == RUN_IDLE


def test_energy_jump_while_idle_does_not_start() -> None:
    # Reconnect after an outage: the meter catches up in one big step (18.0->19.1)
    # while the washer reports stopped/idle. That is NOT a load — must not start.
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 18.0) is None  # recovered value, baselines
    assert det.observe(8, 19.1, machine_idle=True) is None  # +1.1 jump but idle
    assert det.phase == RUN_IDLE
    # A genuine offline load (machine not reported idle) still starts on the jump.
    det2 = EnergyDetector(idle_timeout=IDLE)
    assert det2.observe(0, 18.0) is None
    assert det2.observe(8, 19.1) == EV_STARTED
    # ...and a real wash phase always starts, even if the meter looks idle.
    det3 = EnergyDetector(idle_timeout=IDLE)
    assert det3.observe(0, 18.0, job_is_early=True, machine_idle=True) == EV_STARTED


def test_no_meter_data_does_not_idle_complete() -> None:
    # Device unavailable mid-load: energy reads None (no data). The idle-timeout
    # backstop must NOT guess a finish — the coordinator's offline-aware path owns
    # that. (A real offline-batch load reports a number and still completes.)
    det = EnergyDetector(idle_timeout=IDLE)
    assert det.observe(0, 20.0, job_is_early=True) == EV_STARTED
    assert _poll_flat(det, 300, None, until=3 * IDLE) is None  # None = unavailable
    assert det.phase == RUN_ACTIVE


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
