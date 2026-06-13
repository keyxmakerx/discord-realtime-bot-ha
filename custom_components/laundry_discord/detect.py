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
