"""Pure, dependency-free rate limiting for the anomaly log.

Kept free of Home Assistant / discord imports so the one question it answers —
*should this be said out loud right now, and what should it say?* — is unit
testable, the same discipline that made :mod:`detect` and :mod:`nudge`
reliable. The caller owns the clock and the logger; this module only decides.

**Why this exists at all.** Every line this integration logs is ``debug``, on
purpose: a household that has opted into nothing should produce no log traffic,
and the inventory is held at 0 ``info`` and 1 ``warning``. That was the right
instinct and it was taken too far. The bot misbehaved six times in one morning
and left *nothing* at the default log level, so the only way to find out what
happened was to read a storage file by hand. Quiet in normal operation and
silent during a fault are not the same requirement, and one setting was serving
both.

So anomalies — and only anomalies — get a real log level. The thing that makes
that safe is here: **at most one line per kind per window**, however often the
underlying event fires. That matters precisely because these faults arrive on a
cadence. This washer's cloud drops every ~51 minutes; a fault wired to it would
otherwise write a line an hour, for ever, which is the log flooding the debug
discipline was protecting against in the first place.

Repeats are not thrown away, they are *counted*: the next line out after the
window says how many were swallowed. A fault firing 40 times an hour and one
firing twice must not read the same, or the log tells you something is wrong
without telling you how wrong.
"""

from __future__ import annotations

# How long one kind of anomaly stays quiet after speaking, in seconds. Fifteen
# minutes is chosen against the failure cadence rather than as a round number:
# the known faults here are driven by a ~51-minute reconnect, so this collapses
# a burst (a flap storm, a retry loop) to one line while never hiding a second
# genuine occurrence of a slow fault behind the first.
DEFAULT_WINDOW = 900

# The anomaly kinds. Strings rather than an enum so a record of them round-trips
# through JSON if this ever needs persisting, and so an unknown one from a newer
# version cannot raise on an older one.
PHANTOM_LOAD = "phantom_load"  # a load completed and the meter never moved
START_POST_FAILED = "start_post_failed"  # the card could not be posted
STATE_ROLLED_BACK = "state_rolled_back"  # a failed start discarded a session
STOP_MISREAD = "stop_misread"  # a stop verdict on a load with no evidence
GATEWAY_UNREADY = "gateway_unready"  # a send gave up waiting for Discord
SESSION_FORCED = "session_forced"  # reset_session, or a safety-net completion

KINDS = (
    PHANTOM_LOAD,
    START_POST_FAILED,
    STATE_ROLLED_BACK,
    STOP_MISREAD,
    GATEWAY_UNREADY,
    SESSION_FORCED,
)


def note(state, kind, now, *, window=DEFAULT_WINDOW):
    """Decide whether to speak. Returns ``(emit, new_state, suppressed)``.

    ``state`` is an opaque mapping the caller holds and reassigns — never
    mutated here, like everything else in this codebase, so a caller that drops
    the result silently keeps the old accounting rather than half-applying a
    new one.

    ``emit`` is True on the first occurrence of a kind and on the first one
    after its window has expired. ``suppressed`` is how many were swallowed
    since the last line, which the caller appends to the message — it is 0 on a
    first occurrence, so the ordinary line stays uncluttered.

    An unreadable clock emits and counts nothing: a rate limiter that cannot
    tell the time would otherwise either flood or go permanently silent, and
    both are worse than the event simply not being logged.
    """
    try:
        stamp = float(now)
    except (TypeError, ValueError):
        return (False, dict(state) if isinstance(state, dict) else {}, 0)
    current = dict(state) if isinstance(state, dict) else {}
    try:
        span = max(0.0, float(window))
    except (TypeError, ValueError):
        span = DEFAULT_WINDOW
    entry = current.get(kind)
    last, held = None, 0
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        try:
            last, held = float(entry[0]), int(entry[1])
        except (TypeError, ValueError):
            last, held = None, 0
    # A last-seen in the future is a clock that jumped backwards. Treat it as
    # due rather than muting the kind until the clock catches up, which on a
    # box whose time drifts could be hours.
    if last is None or stamp < last or (stamp - last) >= span:
        current[kind] = (stamp, 0)
        return (True, current, held)
    current[kind] = (last, held + 1)
    return (False, current, 0)


def suffix(suppressed, *, window=DEFAULT_WINDOW) -> str:
    """The trailing clause naming what was swallowed, or an empty string.

    Written as a separate function so the caller's log call stays one readable
    format string, and so the wording is asserted in one place rather than
    spelled out at every site.
    """
    try:
        count = int(suppressed)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    minutes = max(1, int(float(window) // 60))
    times = "time" if count == 1 else "times"
    return f" (plus {count} more {times} in the last {minutes} min)"
