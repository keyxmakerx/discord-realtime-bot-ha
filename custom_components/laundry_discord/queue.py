"""Pure, dependency-free helpers for the "I'm next" line.

Kept free of Home Assistant / discord imports so the ordering, expiry and
carry-forward rules can be unit-tested without the HA test harness — the same
discipline that made :mod:`detect` reliable. The coordinator owns the wall
clock (it passes ``now``) and the persistence; this module only decides what
the line should look like.

Entries are plain dicts ``{"id", "name", "ts"}`` so they round-trip through
HA's ``Store`` as JSON.
"""

from __future__ import annotations

# Most people who can be waiting at once. Past this the button says so rather
# than silently dropping the tap — a no-op button reads as a bug.
QUEUE_CAP = 5

# Results of :func:`toggle_member` (and the coordinator's stale-tap case).
TOGGLE_ADDED = "added"
TOGGLE_REMOVED = "removed"
TOGGLE_FULL = "full"
TOGGLE_STALE = "stale"


def same_user(entry: dict, user_id) -> bool:
    """Whether a stored entry belongs to ``user_id``.

    Entries survive a restart by way of HA's ``Store`` (JSON), and callers hand
    us either the int from ``interaction.user.id`` or whatever came back off
    disk — so the comparison is done on the string form rather than trusting
    both sides to still be the same type.
    """
    return str(entry.get("id")) == str(user_id)


def entry_ts(entry: dict) -> float | None:
    """The entry's queued-at timestamp, or None if it is missing/unparseable."""
    try:
        return float(entry["ts"])
    except (KeyError, TypeError, ValueError):
        return None


def find(queue: list[dict], user_id) -> dict | None:
    """The caller's entry in the line, or None."""
    for entry in queue:
        if same_user(entry, user_id):
            return entry
    return None


def position(queue: list[dict], user_id) -> int | None:
    """1-based place in line, or None if the user isn't waiting."""
    for i, entry in enumerate(queue):
        if same_user(entry, user_id):
            return i + 1
    return None


def prune(queue: list[dict], now: float, expiry: float) -> list[dict]:
    """Drop entries that have gone stale.

    Somebody who tapped "I'm next" yesterday and went to bed should not be
    pinged at 6am for a load they've long forgotten about, and a line that
    never empties would strand every future handoff. An entry with no usable
    timestamp is dropped too: it can never age out, so keeping it is the worse
    failure. ``expiry <= 0`` disables ageing entirely.
    """
    if expiry <= 0:
        return list(queue)
    kept: list[dict] = []
    for entry in queue:
        ts = entry_ts(entry)
        if ts is None:
            continue
        if (now - ts) < expiry:
            kept.append(entry)
    return kept


def toggle_member(
    queue: list[dict], user_id, name: str, now: float
) -> tuple[list[dict], str]:
    """Join the back of the line, or leave it if already waiting.

    Returns ``(new_queue, result)`` where result is one of ``TOGGLE_ADDED`` /
    ``TOGGLE_REMOVED`` / ``TOGGLE_FULL``. The queue is never mutated in place —
    the caller assigns the result — so a rejected tap can't half-apply.
    """
    if find(queue, user_id) is not None:
        return ([e for e in queue if not same_user(e, user_id)], TOGGLE_REMOVED)
    if len(queue) >= QUEUE_CAP:
        return (list(queue), TOGGLE_FULL)
    return (
        [*queue, {"id": user_id, "name": name, "ts": now}],
        TOGGLE_ADDED,
    )


def carry_forward(
    queue: list[dict], claimant_id, now: float, expiry: float
) -> list[dict]:
    """Roll the line into the next load, minus whoever claimed that load.

    A three-deep line is a real thing in a shared house: A's load finishes, B
    takes the machine, and C should still be next. So the queue survives into
    the next session — but B, who is now running a load, is obviously not
    waiting for it, so they come out of the line.
    """
    rolled = prune(queue, now, expiry)
    if claimant_id is None:
        return rolled
    return [e for e in rolled if not same_user(e, claimant_id)]


def next_in_line(queue: list[dict]) -> dict | None:
    """Whoever is up next, or None when nobody's waiting."""
    return queue[0] if queue else None


def entry_name(entry: dict | None) -> str:
    """Display name for an entry, with a fallback for a malformed one."""
    if not entry:
        return "someone"
    return str(entry.get("name") or "someone")


def format_queue(queue: list[dict], *, limit: int = 3) -> str | None:
    """Render the line for the embed — "Sam", "Sam, then Ty", "… +2 more".

    Returns None for an empty line so the caller can skip the field entirely
    rather than showing an empty one.
    """
    if not queue:
        return None
    shown = [entry_name(e) for e in queue[:limit]]
    text = ", then ".join(shown)
    extra = len(queue) - len(shown)
    if extra > 0:
        text += f" (+{extra} more)"
    return text
