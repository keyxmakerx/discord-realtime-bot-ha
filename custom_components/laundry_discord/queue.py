"""Pure helpers for the "I'm next" line.

No Home Assistant / discord imports, so this is unit-tested directly. The
coordinator owns the clock (`now`) and persistence; entries are plain
dicts `{"id", "name", "ts"}`, JSON-safe for HA's `Store`.
"""

from __future__ import annotations

# Max people waiting at once. Past this the button says so, rather than
# silently no-op'ing.
QUEUE_CAP = 5

# Results of :func:`toggle_member` (and the coordinator's stale-tap case).
TOGGLE_ADDED = "added"
TOGGLE_REMOVED = "removed"
TOGGLE_FULL = "full"
TOGGLE_STALE = "stale"
TOGGLE_ALREADY = "already"  # join-only: already waiting


def same_user(entry: dict, user_id) -> bool:
    """Whether a stored entry belongs to `user_id`.

    Compares as strings: an int from the caller vs whatever JSON restored.
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

    An entry with no usable timestamp is dropped too — it could never age
    out otherwise.
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

    Returns `(new_queue, result)`; never mutates in place, so a rejected
    tap can't half-apply.
    """
    if find(queue, user_id) is not None:
        return ([e for e in queue if not same_user(e, user_id)], TOGGLE_REMOVED)
    if len(queue) >= QUEUE_CAP:
        return (list(queue), TOGGLE_FULL)
    return (
        [*queue, {"id": user_id, "name": name, "ts": now}],
        TOGGLE_ADDED,
    )


def join_member(
    queue: list[dict], user_id, name: str, now: float
) -> tuple[list[dict], str]:
    """Join the back of the line without the toggle's leave (for DM replies).

    Returns `(new_queue, result)`: `TOGGLE_ADDED`, `TOGGLE_FULL`, or
    `TOGGLE_ALREADY` when they're already waiting.
    """
    if find(queue, user_id) is not None:
        return (list(queue), TOGGLE_ALREADY)
    return toggle_member(queue, user_id, name, now)


def remove_user(queue: list[dict], user_id) -> list[dict]:
    """The line without `user_id`. A None id removes nobody.

    Load-bearing: `same_user` compares as strings, so without this an
    unclaimed load (`claimed_by_id is None`) would strip every id-less entry.
    """
    if user_id is None:
        return list(queue)
    return [e for e in queue if not same_user(e, user_id)]


def carry_forward(
    queue: list[dict], claimant_id, now: float, expiry: float
) -> list[dict]:
    """Roll the line into the next load, minus whoever claimed it.

    The queue survives into the next session, but the new claimant can't
    also be waiting for the machine they're now using.
    """
    return remove_user(prune(queue, now, expiry), claimant_id)


def select_handoff(
    queue: list[dict], now: float, expiry: float, claimant_id
) -> tuple[dict | None, list[dict]]:
    """Pick who gets the washer; return the line with them taken off it.

    Returns `(entry_or_None, new_queue)`. Three rules:

    - Stale entries never get the handoff.
    - The claimant is never handed the machine they're already using (they
      can still be in line from before they claimed it).
    - The chosen entry comes off the line, so the fallback timer can't ping
      them twice.
    """
    remaining = remove_user(prune(queue, now, expiry), claimant_id)
    if not remaining:
        return (None, remaining)
    return (remaining[0], remaining[1:])


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


def names(queue: list[dict]) -> list[str]:
    """Every display name, in order — the shape a state attribute wants.

    Only names, deliberately — a derived time value would churn every
    refresh tick and spam the recorder's per-change history.
    """
    return [entry_name(e) for e in queue]


def attributes(
    queue: list[dict], now: float, expiry: float, claimant_id
) -> dict:
    """The state-attribute view of the line — the line that would *act*.

    Pruned like the handoff: the stored line is pruned only on an event
    (tap/start/handoff), so read at other times it can hold entries already
    past `expiry`. `queue`/`queue_count` are that pruned line; `next_up`
    also drops the claimant. Churn-safe: `now` only affects membership, so
    expiry costs one attribute change per entry, not one per refresh tick.
    """
    pruned = prune(queue, now, expiry)
    head, _ = select_handoff(pruned, now, expiry, claimant_id)
    return {
        "queue_count": len(pruned),
        "queue": names(pruned),
        "next_up": entry_name(head) if head is not None else None,
    }


def ordinal(n: int) -> str:
    """1 → "1st", 2 → "2nd", 3 → "3rd", 4 → "4th".

    Only sees small numbers (capped by `QUEUE_CAP`), but the 11/12/13
    exception is handled anyway, in case the cap is raised later.
    """
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def tap_notice(result: str, place: int | None) -> str | None:
    """The private word owed to whoever just tapped 🔜, or None.

    The card's response is shared with the house, so without this the
    tapper sees nothing addressed to them and join/leave look identical.
    Returns None for the two failure results, which already sent their own
    reply.
    """
    if result == TOGGLE_ADDED:
        if place is None:
            # Shouldn't happen (just added); a vague reply still beats silence.
            return "You're in the line — I'll ping you when the washer's free."
        if place == 1:
            return (
                "You're **next** — I'll ping you when the washer's actually "
                "free. Done isn't empty: somebody still has to clear the drum."
            )
        return (
            f"You're **{ordinal(place)}** in line — I'll ping you when the "
            "washer's actually free."
        )
    if result == TOGGLE_REMOVED:
        return "You're **out of the line** — no ping coming. Tap 🔜 to rejoin."
    return None


def handoff_line(name: str | None, *, hedged: bool) -> str:
    """The done card's record that the line moved, for the person told.

    Pops the head off the queue, so this is the only place their name still
    appears (consistent with the live card; the anonymity rule is about the
    forward plan, not this one). Worded apart from the backstop, where
    nobody confirmed anything and a card claiming otherwise would lose trust.
    """
    who = name or "someone"
    if hedged:
        return f"🔜 {who} — nudged that it's probably free (nobody confirmed)."
    return f"🔜 {who} — told the washer's free."


def free_announcement(name: str | None, *, hedged: bool) -> str | None:
    """The channel line posted when the washer comes free, or None.

    ``name`` is whoever was just handed the washer (None when nobody was
    waiting). A hedged backstop with nobody waiting says nothing: done isn't
    empty, and the house would be told "free" on no evidence.
    """
    if name:
        if hedged:
            return (
                f"🔜 The washer's probably free — nobody's confirmed. "
                f"{name}'s up next."
            )
        return f"🔜 Washer's free — {name}'s up next."
    if hedged:
        return None
    return "🧺 Washer's free."


def empty_reminder_text(*, waiting: bool, name: str | None = None) -> str:
    """The reminder for a claimant who hasn't tapped ✅ Emptied it.

    ``name`` gives the push-free form used when they set 🌙 Quiet on the card.
    """
    tail = " — someone's waiting for it." if waiting else "."
    if name:
        return f"🌙 {name}, your laundry's still in the washer{tail}"
    if waiting:
        return f"🧺 Your laundry's still in the washer{tail}"
    return "🧺 Reminder: your laundry's done and still in the washer."
