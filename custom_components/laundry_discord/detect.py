"""Pure, dependency-free detection helpers.

No Home Assistant / discord imports, so the energy-sample math is
unit-tested directly. The coordinator delegates load liveness to
`EnergyDetector` (see `LaundryCoordinator._feed_detector`).
"""

from __future__ import annotations

from dataclasses import dataclass

# Float-subtraction slack: IEEE-754 rounding could fail a boundary "rose by
# exactly the threshold" check. Energy steps are ~0.1 kWh, well above this.
_EPS = 1e-9


def energy_jumped(
    prev: float | None, cur: float | None, threshold: float
) -> bool:
    """True when energy rose by >= `threshold` within a single sample.

    A single-sample jump is the fingerprint of a batched-offline load. Slow
    creep (standby/wrinkle-prevent) never reaches it in one step, and a
    meter reset reads as a decrease — both stay False, so the offline
    backstop doesn't false-fire.
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

    An early phase (e.g. weight_sensing/wash) is unambiguously a fresh load
    and must NOT be gated on the meter — it lags phases by 15-45 min and
    still reads the last completion value early in a cycle. Only mid/late
    phases need the energy guard, to tell a real catch-up from a stale
    frozen phase.
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


def offline_completion_due(
    *,
    offline_since: float | None,
    last_eta_ts: float | None,
    now: float,
    offline_after: float,
    eta_grace: float,
) -> bool:
    """Whether to complete a load while the washer is offline/unverifiable.

    Fires only once offline for `offline_after` AND the last estimate has
    passed by `eta_grace` (cushion for a long dry). Caller marks it
    unverified. Otherwise False; the max-session net is the last resort.
    """
    if offline_since is None or last_eta_ts is None:
        return False
    if (now - offline_since) < offline_after:
        return False
    return now >= last_eta_ts + eta_grace


def session_too_long(
    session_started_ts: float | None, now: float, max_session: float
) -> bool:
    """Absolute safety net: force done once a tracked load exceeds `max_session`,
    so a stuck session (frozen estimate, no 'finish') can't live forever.
    """
    return (
        session_started_ts is not None
        and (now - session_started_ts) >= max_session
    )


# --------------------------------------------------------------------------- #
# Energy-primary liveness state machine.
#
# The energy meter is the one source of truth for "is a load running".
# job_state is an optional accelerant (fast start/finish) — it can enrich
# but never block or override the meter.
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

    Feed samples via `observe`; returns `EV_STARTED`/`EV_FINISHED`/None. Pure
    and deterministic — no clock, no I/O, so it replays against captured
    traces — with the caller owning the wall clock (`ts`) and session state.

    Cases it handles:
      * Back-to-back loads — re-fires `EV_STARTED` right after a finish.
      * Unreliable meter — energy can freeze, reset or stay flat all load.
        With a washer estimate, finish waits for `eta_passed` AND the
        meter to settle; without one, `idle_timeout` alone applies.
      * Abandoned/offline, no estimate — flat energy finishes after
        `idle_timeout`.
      * Brief pause — a gap under `idle_timeout` just re-arms the timer.
      * Offline batch — a single jump while job_state is dark still starts.
      * Wrinkle-prevent/standby creep — too small to reach the start jump.
      * Meter reset — a decrease rebaselines rather than starting/finishing.
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
        machine_idle: bool = False,
        meter_reporting: bool = True,
    ) -> str | None:
        """Process one sample; return an event or None.

        `energy` is None when the meter is unavailable. `job_is_early` marks
        a fresh early phase; `job_is_real` any real wash phase (catch-up);
        `job_is_finish` marks job_state == 'finish'. `wrinkle_active`
        attributes a rise to tumbling, not the load.

        Completion gating: with `has_eta`, finish needs `eta_passed` AND the
        meter flat for `eta_grace` — neither alone can trigger or block it.
        Without an estimate, `idle_timeout` is the backstop. `machine_idle`
        vetoes an energy-jump start only; `meter_reporting` false vetoes
        the flat-energy backstop.
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
            # A mid/late phase is a catch-up only if the meter moved since we
            # went idle; frozen at the completion reading means stale, not
            # running.
            catchup = (
                job_is_real
                and energy is not None
                and self.idle_energy is not None
                and energy > self.idle_energy + _EPS
            )
            # Starts on: an energy jump (offline batch), the early-phase
            # accelerant, or a corroborated catch-up. A jump is ignored while
            # `machine_idle` — a reconnect catch-up in one step with the
            # machine off isn't a load.
            if (jumped and not machine_idle) or job_is_early or catchup:
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
                # Estimate-gated finish; full rule is in the docstring above.
                if eta_passed and flat_for >= self.eta_grace:
                    self.reset()
                    return EV_FINISHED
            elif energy is not None and meter_reporting and flat_for >= self.idle_timeout:
                # Flat-energy backstop for a load with no usable estimate but
                # a reporting meter (e.g. offline batch). `energy is not
                # None` only means the entity has a value, not that it's
                # live, and `last_rise_ts` is seeded at load start rather
                # than on a real rise — so `meter_reporting` guards against a
                # meter frozen since before the session began looking "flat"
                # the instant the timer runs out.
                self.reset()
                return EV_FINISHED
        return None

    def reset(self) -> None:
        """Return to idle after a finish; record the meter baseline for catch-up."""
        self.phase = RUN_IDLE
        self.last_rise_ts = None
        self.idle_energy = self.last_energy
