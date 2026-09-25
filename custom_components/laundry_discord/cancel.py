"""Pure rules for "somebody stopped the wash on the machine".

Decides what a stop signal means; `coordinator` owns the timers and side
effects. No Home Assistant or discord imports (tested by tests/test_cancel.py).

A stop must be positively reported, never inferred from silence (the cloud
drops hourly), and "stopped early" vs "finished" is decided by the washer's
own estimate.
"""

from __future__ import annotations

try:  # the normal path — a sibling module inside the integration package
    from .const import MACHINE_PAUSE, MACHINE_RUN, MACHINE_STOP, UNAVAILABLE_STATES
except ImportError:  # pragma: no cover - loaded by file path
    # No package when exec'd by file path (how the pure suite runs this).
    # Constants aren't re-spelled here — a duplicate copy is how a rename in
    # const.py quietly breaks this path.
    from const import (  # type: ignore[no-redef]
        MACHINE_PAUSE,
        MACHINE_RUN,
        MACHINE_STOP,
        UNAVAILABLE_STATES,
    )

# What a confirmed stop signal turns out to mean.
VERDICT_IGNORE = "ignore"      # not a stop, or not one we can stand behind
VERDICT_STOPPED = "stopped"    # the cycle was ended before it was due to end
VERDICT_FINISHED = "finished"  # it stopped because it was over


def is_flap(old_state) -> bool:
    """Whether a transition out of `old_state` is a flap, not an event.

    Same rule as `coordinator._on_job_state`: a value arriving from
    `unavailable`/`unknown` is the cloud reconnecting, not the machine
    acting. `None` (no prior state) is the same case, at startup.
    """
    return old_state is None or old_state in UNAVAILABLE_STATES


def machine_stop_signalled(old_state, new_state) -> bool:
    """Whether a `machine_state` transition is worth debouncing as a stop.

    `pause` is deliberately not a stop (on hold, not over), and neither is a
    reconnect landing on `stop` (see `is_flap`).
    """
    if is_flap(old_state):
        return False
    return new_state == MACHINE_STOP


def running_off_signalled(
    old_state, new_state, *, machine_state_configured: bool = True
) -> bool:
    """Whether a `running` binary_sensor transition is worth debouncing.

    A second route to the same fact; same flap rule as `is_flap`. Needs
    `machine_state_configured` because this sensor reports `off` for a
    pause exactly as for a stop, and only `machine_state` (live, or the
    session's `paused` flag) can veto that — unconfigured, this sensor
    alone never arms a stop; it's a second trigger for a signal
    `machine_state` can check.
    """
    if is_flap(old_state):
        return False
    if not machine_state_configured:
        return False
    return new_state == "off"


def stop_verdict(
    *,
    tracked: bool,
    machine_state: str | None,
    running_on: bool | None,
    paused: bool,
    has_eta: bool,
    eta_passed: bool,
    job_finished: bool,
) -> str:
    """What a debounced stop signal means for the load being tracked.

    `machine_state`/`running_on` are current readings after the debounce,
    None meaning unreadable (offline); `paused` is the session's pause
    flag; `has_eta`/`eta_passed` the washer's own estimate; `job_finished`
    whether job_state reached 'finish'. Every IGNORE below is a case where
    ending the load would be a guess, and a wrong guess kills a live wash.
    """
    if not tracked:
        return VERDICT_IGNORE  # nothing to end (a stale timer, or an idle machine)
    if machine_state in (MACHINE_RUN, MACHINE_PAUSE):
        return VERDICT_IGNORE  # it came back / it is only on hold
    if running_on is True:
        return VERDICT_IGNORE  # the two sensors disagree; the wash wins the tie
    if paused and machine_state != MACHINE_STOP:
        # Not a blanket `if paused`: cancelling a combo unit is usually
        # run -> pause -> stop with the flag still set, so that would
        # discard a live stop. `paused` is a cached, possibly-stale belief;
        # `machine_state` is read live and wins when it says stop.
        return VERDICT_IGNORE
    if machine_state != MACHINE_STOP and running_on is not False:
        # Nothing positively says stopped — this is what an outage looks like.
        return VERDICT_IGNORE
    if has_eta and not eta_passed and not job_finished:
        # The washer itself expected to still be running. Somebody ended it.
        return VERDICT_STOPPED
    # Either the estimate has passed, or the washer says the job finished, or
    # there is no estimate to contradict a machine that reports itself stopped.
    return VERDICT_FINISHED


# How far short of the estimate a stop must land to delete a history row.
# Longer than a normal early finish (estimates routinely run a few minutes
# out), shorter than any real cancel.
HISTORY_RETRACT_MARGIN_S = 600


def retracts_history(
    *, verdict: str, machine_state: str | None, eta_remaining_s: float | None
) -> bool:
    """Whether a stop is unambiguous enough to un-log the load from history.

    Stricter than the wording check: a wrong word self-corrects next load,
    but `habit.forget_load` deletes a real row that nothing restores.

    Needs both: `machine_state` positively reads `stop` (not just the
    running sensor), and the cycle ended materially before its estimate —
    `not eta_passed` alone also matches a wash finishing a few minutes early.
    """
    if verdict != VERDICT_STOPPED:
        return False
    if machine_state != MACHINE_STOP:
        return False
    if eta_remaining_s is None:
        return False
    return eta_remaining_s > HISTORY_RETRACT_MARGIN_S
