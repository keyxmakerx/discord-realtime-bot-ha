"""Pure, dependency-free detection helpers.

Kept free of Home Assistant / discord imports so the tricky energy-sample math
can be unit-tested without the HA test harness. The coordinator delegates load
liveness to :class:`EnergyDetector`; see
:meth:`coordinator.LaundryCoordinator._feed_detector`.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def session_too_long(
    session_started_ts: float | None, now: float, max_session: float
) -> bool:
    """Absolute safety net: a tracked load that has run ``max_session`` is forced
    done so a stuck session (estimate frozen in the future, no 'finish') can
    never live forever. Pure so it's unit-tested without the HA harness.
    """
    return (
        session_started_ts is not None
        and (now - session_started_ts) >= max_session
    )


# --------------------------------------------------------------------------- #
# Energy-primary liveness state machine (the permanent detection core).
#
# One source of truth for "is a load running": the energy meter. job_state is an
# optional *accelerant* (fast start on an early phase, fast finish on 'finish')
# and is used for enrichment elsewhere — it can never block or override the meter.
# This collapses the old job/energy/self-clean guards into a single tested unit.
# --------------------------------------------------------------------------- #

# Liveness phases (the detection layer; distinct from the Discord session stage).
RUN_IDLE = "idle"
RUN_ACTIVE = "active"

# Events emitted for the coordinator to act on.
EV_STARTED = "started"
EV_FINISHED = "finished"


@dataclass
class EnergyDetector:
    """Decide load start/finish from a stream of meter samples.

    Feed it observations via :meth:`observe`; it returns ``EV_STARTED`` /
    ``EV_FINISHED`` / ``None``. Pure and deterministic — no clock, no I/O — so
    it replays against captured traces in tests. The caller owns the wall clock
    (passes ``ts``) and the Discord session/claim state.

    Real-world cases it is built to handle (see the tests):
      * **Back-to-back loads** — after a finish it returns to idle, so the next
        load re-fires ``EV_STARTED`` (the caller supersedes the lingering done
        message). This is the case the old completion-energy guard broke.
      * **Unreliable meter (this washer)** — the energy meter can freeze, reset,
        or read flat for an entire load, so it cannot time completion on its own.
        When the washer's own estimate is available (``has_eta``) completion is
        gated on it: the load finishes only once that estimate has passed
        (``eta_passed``) AND the meter has settled for ``eta_grace``. This both
        (a) lets a frozen/flat meter never fire early — it must wait for the
        estimate — and (b) does not depend on job_state advancing, so a job_state
        frozen mid-cycle can't block completion. The plain ``idle_timeout`` is
        used ONLY when there's no usable estimate (``has_eta`` False, a truly
        offline load).
      * **Removed early / abandoned (offline)** — with no estimate, energy goes
        flat and the load finishes after ``idle_timeout``.
      * **Pause to add a sock, then resume** — a gap shorter than
        ``idle_timeout`` does not finish the load; the meter resuming just
        re-arms the timer.
      * **Offline batch** — a single jump while job_state is dark still starts.
      * **Wrinkle-prevent / standby creep** — tiny single steps never reach the
        start jump; with ``wrinkle_active`` wired in, post-cycle tumbling does
        not keep a finished load "alive".
      * **Meter reset** — a decrease rebaselines and never spuriously
        starts/finishes.
    """

    start_jump: float = 0.3   # kWh rise in one sample => a load (offline/batch)
    idle_timeout: float = 3600.0  # s flat => finished, ONLY when no usable ETA
    eta_grace: float = 1200.0  # s the meter must settle after the ETA passes

    phase: str = RUN_IDLE
    last_energy: float | None = None
    last_rise_ts: float | None = None
    idle_energy: float | None = None  # meter reading when we last went idle

    def observe(
        self,
        ts: float,
        energy: float | None,
        *,
        job_is_early: bool = False,
        job_is_real: bool = False,
        job_is_finish: bool = False,
        wrinkle_active: bool = False,
        has_eta: bool = False,
        eta_passed: bool = False,
    ) -> str | None:
        """Process one sample; return an event or ``None``.

        ``energy`` is ``None`` when the meter is unavailable (held, never a
        sample). ``job_is_early`` marks a fresh early phase (weight_sensing /
        wash); ``job_is_real`` marks any real wash phase (for a mid-cycle
        catch-up); ``job_is_finish`` marks job_state == 'finish'.
        ``wrinkle_active`` is the optional wrinkle-prevent sensor — when true, a
        rise is attributed to tumbling, not the load.

        Completion gating: ``has_eta`` is true when the washer's own completion
        estimate (for this cycle) is available; ``eta_passed`` is true once that
        estimate has elapsed. With an estimate, the load finishes only when it
        has passed AND the meter has been flat for ``eta_grace`` — so a frozen or
        dead meter can't fire early, and a stalled job_state can't block it.
        Without an estimate (offline), the plain ``idle_timeout`` flat-energy
        backstop applies instead.
        """
        rose = False
        jumped = False
        if energy is not None:
            if self.last_energy is not None and energy > self.last_energy:
                rose = (energy - self.last_energy) > _EPS
            jumped = (
                not wrinkle_active
                and energy_jumped(self.last_energy, energy, self.start_jump)
            )
            self.last_energy = energy  # advance (a decrease rebaselines on reset)

        if self.phase != RUN_ACTIVE:
            # A mid/late phase counts as a catch-up only if the meter has moved
            # since we went idle — a phase frozen at the completion reading is a
            # stale leftover, not a running load.
            catchup = (
                job_is_real
                and energy is not None
                and self.idle_energy is not None
                and energy > self.idle_energy + _EPS
            )
            # Start on: an energy jump (offline batch), the early-phase accelerant
            # (online, even while the meter lags), or a corroborated catch-up.
            if jumped or job_is_early or catchup:
                self.phase = RUN_ACTIVE
                self.last_rise_ts = ts
                return EV_STARTED
            return None

        # ACTIVE -------------------------------------------------------------
        if job_is_finish:  # fast-path completion when the cloud reports it
            self.reset()
            return EV_FINISHED
        if rose and not wrinkle_active:
            self.last_rise_ts = ts  # load is still drawing => re-arm the timer
        if self.last_rise_ts is not None:
            flat_for = ts - self.last_rise_ts
            if has_eta:
                # The washer's estimate is the gate: finish only once it has
                # passed and the (unreliable) meter has settled. A frozen/flat
                # meter can't fire early, and a stalled job_state can't block it.
                if eta_passed and flat_for >= self.eta_grace:
                    self.reset()
                    return EV_FINISHED
            elif flat_for >= self.idle_timeout:
                # No usable estimate (offline): energy-only flat backstop.
                self.reset()
                return EV_FINISHED
        return None

    def reset(self) -> None:
        """Return to idle after a finish; record the meter baseline for catch-up."""
        self.phase = RUN_IDLE
        self.last_rise_ts = None
        self.idle_energy = self.last_energy
