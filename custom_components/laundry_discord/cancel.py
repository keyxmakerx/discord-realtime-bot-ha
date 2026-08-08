"""Pure, dependency-free rules for "somebody stopped the wash on the machine".

Kept free of Home Assistant / discord imports so the rules can be replayed with
plain ``python3 tests/test_cancel.py``, the same discipline that made
:mod:`detect`, :mod:`queue`, :mod:`people` and :mod:`plan` reliable. This module
decides *what a stop signal means*; :mod:`coordinator` owns the wall clock, the
debounce timer and every side effect.

**The gap this exists to close.** :mod:`detect` has exactly three routes to a
finish — ``job_state == 'finish'``, the ETA gate, and the flat-energy backstop —
and a human pressing stop on the front panel trips none of them. ``job_state``
goes to ``none`` rather than ``finish``; ``completion_time`` keeps the *planned*
finish, which is still in the future, so the ETA gate stays shut; and because an
estimate exists at all the third route sits behind an ``elif`` and cannot run.
The load then sits there until the 12-hour max-session net. The missing signal
is the one the washer reports plainly and the detector is never told about: the
machine says it is **stopped**.

That signal is added here rather than in :mod:`detect` on purpose. The ETA gate
is what stops a frozen or flat meter firing a false "done" mid-cycle, it was
hard-won, and it is not weakened by a path keyed off a *different* signal —
:mod:`detect` is untouched.

Two rules do the load-bearing work:

* **A stop must be positively reported, never inferred from silence.** This
  washer's cloud drops to ``unavailable`` on a ~51-minute timer; "no longer says
  run" is precisely what that outage looks like, so treating an absence as a
  stop would kill a live wash roughly every hour. Something has to actually read
  ``stop`` (or the running sensor actually read off), and nothing may contradict
  it. Absence of evidence is :data:`VERDICT_IGNORE`, and the coordinator's
  offline path already knows what to do with an offline load.
* **"Stopped early" and "finished" are different claims, and the washer's own
  estimate tells them apart.** If the estimate says this cycle should still be
  running, somebody ended it; if it has passed — or ``job_state`` reached
  ``finish`` — the cycle is simply over and the normal wording applies. The bot
  must not tell the house a load is done when it knows it is not.
"""

from __future__ import annotations

try:  # the normal path — a sibling module inside the integration package
    from .const import MACHINE_PAUSE, MACHINE_RUN, MACHINE_STOP, UNAVAILABLE_STATES
except ImportError:  # pragma: no cover - loaded by file path, as the tests do
    # There is no package when this is exec'd from a file path, which is how the
    # pure suite runs it. The machine_state vocabulary is *not* re-spelled here:
    # two copies of the strings "run"/"pause"/"stop" is exactly how a rename in
    # const.py becomes a cancel path that quietly stops firing.
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
    """Whether a transition *out of* ``old_state`` is a flap, not an event.

    The prior art is ``coordinator._on_job_state``: a value arriving from
    ``unavailable``/``unknown`` is the cloud reconnecting, not the machine
    doing something. ``None`` (no previous state at all) is the same case at
    startup. On a washer that drops hourly this single rule is the difference
    between a cancel path and an hourly false completion.
    """
    return old_state is None or old_state in UNAVAILABLE_STATES


def machine_stop_signalled(old_state, new_state) -> bool:
    """Whether a ``machine_state`` transition is worth debouncing as a stop.

    Only a real value settling on ``stop``. ``pause`` is deliberately not a
    stop — a paused load is on hold, not over — and neither is a reconnect that
    happens to land on ``stop`` (:func:`is_flap`).
    """
    if is_flap(old_state):
        return False
    return new_state == MACHINE_STOP


def running_off_signalled(
    old_state, new_state, *, machine_state_configured: bool = True
) -> bool:
    """Whether a ``running`` binary_sensor transition is worth debouncing.

    The second way the same fact reaches us. Same flap rule: ``unavailable ->
    off`` is the cloud dropping, which is not the machine stopping.

    ``machine_state_configured`` is the load-bearing argument. This sensor
    reports ``machineState == run``, so a *pause* takes it to off exactly as a
    stop does — "add a sock", or the washer pausing itself to redistribute an
    unbalanced load. Every defence against reading that as a cancel comes from
    the ``machine_state`` entity: the live ``pause`` reading, and the session's
    ``paused`` flag, which is only ever set from that entity's events. With the
    entity left unconfigured (it is optional) neither exists, nothing can
    contradict this sensor, and a mid-cycle pause would end somebody's live
    wash — the one failure this path must never have. So with no
    ``machine_state`` to veto it, the running sensor does not arm a stop on its
    own; it is a second trigger for a signal that can be checked, not a
    standalone one.
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

    ``machine_state`` and ``running_on`` are the *current* readings taken after
    the debounce, ``None`` meaning unreadable (the washer is offline). ``paused``
    is the session's own pause flag, ``has_eta``/``eta_passed`` the washer's own
    completion estimate for this cycle, and ``job_finished`` whether
    ``job_state`` has reached ``finish``.

    Every ``IGNORE`` below is a case where ending the load would be a guess, and
    a wrong guess here kills somebody's live wash — the failure mode this whole
    path has to be safe against, since it is the one the ETA gate was protecting
    us from before.
    """
    if not tracked:
        return VERDICT_IGNORE  # nothing to end (a stale timer, or an idle machine)
    if machine_state in (MACHINE_RUN, MACHINE_PAUSE):
        return VERDICT_IGNORE  # it came back / it is only on hold
    if running_on is True:
        return VERDICT_IGNORE  # the two sensors disagree; the wash wins the tie
    if paused and machine_state != MACHINE_STOP:
        # Paused, and nothing since has positively reported a stop — typically
        # machine_state has gone unreadable. "Add a sock" can take the running
        # sensor to off, and a pause is not the end of a load.
        #
        # Narrowly conditioned on purpose. ``paused`` is a *cached belief* from
        # the event stream and can be arbitrarily stale by the time this runs;
        # ``machine_state`` is re-read live. Cancelling is a two-press sequence
        # on most combo units — Start/Pause, then hold Stop — so the ordinary
        # cancel arrives as ``run -> pause -> stop`` with the flag still set. A
        # blanket ``if paused`` would discard a live, unambiguous ``stop`` and
        # leave exactly the 12-hour hang this module exists to fix. A live
        # ``pause`` is already caught by the check above, and a live ``run``
        # (or ``running_on is True``) still protects a wash in progress.
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


# How far short of the washer's own estimate a cycle has to end before the stop
# is firm enough to *delete* a history row. Comfortably longer than a washer
# beating its own published ``completion_time`` (these estimates re-extend on a
# rebalance and are routinely a few minutes out), comfortably shorter than any
# real cancel, which happens with the cycle visibly unfinished.
HISTORY_RETRACT_MARGIN_S = 600


def retracts_history(
    *, verdict: str, machine_state: str | None, eta_remaining_s: float | None
) -> bool:
    """Whether a stop is unambiguous enough to un-log the load from history.

    A separate, strictly stronger test than the one that picks the card's
    wording, because the two failures are not comparable. Calling a real
    completion "stopped early" is wrong for one card and self-corrects on the
    next load; ``habit.forget_load`` deletes a person's row for a wash that
    really happened, and nothing puts it back — on somebody with little history
    that moves their predicted times toward never having washed, and every
    nudge in the system reads those predictions.

    The wording may therefore stay hedged and cheap to get wrong. This may not.
    So it demands both halves of the evidence:

    * the washer **positively reads** ``stop`` — a stop inferred from the
      running sensor alone, with ``machine_state`` unreadable, is not enough;
    * the cycle ended **materially** before its own estimate, not merely a
      moment before it. ``VERDICT_STOPPED`` is reached by ``not eta_passed``,
      which a washer finishing a few minutes early also satisfies.

    Anything short of that keeps the row. A stale row is recoverable — it is
    one data point among many, and the next real load outweighs it; a deleted
    one is not.
    """
    if verdict != VERDICT_STOPPED:
        return False
    if machine_state != MACHINE_STOP:
        return False
    if eta_remaining_s is None:
        return False
    return eta_remaining_s > HISTORY_RETRACT_MARGIN_S
