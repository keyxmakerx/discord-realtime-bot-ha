"""Pure, dependency-free detection helpers.

Kept free of Home Assistant / discord imports so the tricky energy-sample math
can be unit-tested without the HA test harness. The coordinator delegates to
these; see :meth:`coordinator.LaundryCoordinator._evaluate_energy_start`.
"""

from __future__ import annotations

# Float-subtraction slack: 11.3 - 11.0 == 0.2999999999999998 in IEEE-754, which
# would spuriously fail a boundary "rose by exactly the threshold" check. Energy
# steps are ~0.1 kWh, so this tolerance never blurs two real samples together.
_EPS = 1e-9


def energy_jumped(
    prev: float | None, cur: float | None, threshold: float
) -> bool:
    """True when energy rose by >= ``threshold`` within a single sample.

    A single-sample jump is the fingerprint of a load whose telemetry was
    batched after the washer's cloud was offline. Slow per-sample creep
    (standby / wrinkle-prevent tumbling) never reaches the threshold in one
    step, and a meter reset shows as a *decrease* — so this stays ``False`` for
    both, which is what keeps the offline-load backstop from false-firing.
    """
    if prev is None or cur is None:
        return False
    return (cur - prev) >= threshold - _EPS


def load_is_active(
    phase: str | None,
    real_phases: frozenset[str] | set[str],
    finish_phase: str,
    midcycle_phases: frozenset[str] | set[str],
    energy: float | None,
    completion_energy: float | None,
) -> bool:
    """Whether a real load is running, given the settled job phase + meter.

    Pure form of :meth:`coordinator.LaundryCoordinator._load_active`. The phase
    sets are passed in (rather than imported) so this module stays importable by
    file path for testing without pulling in the package.

    A fresh cycle begins at an *early* phase (e.g. weight_sensing / wash); the
    washer only ever freezes on the mid/late phase it ended on, so an early phase
    is unambiguously a new load and must NOT be gated on the energy meter — the
    meter lags the phases by 15-45 min and still reads the previous completion
    value during the first part of every load. Only a mid/late phase needs the
    energy guard, to tell a real catch-up from a stale frozen phase.
    """
    if phase is None or phase not in real_phases:
        return False
    if phase == finish_phase:
        return False
    if phase not in midcycle_phases:
        return True  # early phase => a fresh load, regardless of the meter
    if (
        energy is not None
        and completion_energy is not None
        and abs(energy - completion_energy) < 1e-6
    ):
        return False  # mid/late phase stuck at the completion reading => stale
    return True
