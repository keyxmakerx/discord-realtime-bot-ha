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


def remove_user(queue: list[dict], user_id) -> list[dict]:
    """The line without ``user_id``. A None id removes nobody.

    The None guard is load-bearing: an unclaimed load has ``claimed_by_id is
    None``, and :func:`same_user` stringifies both sides, so without it every
    entry whose id failed to persist (``{"id": None}``) would be silently
    dropped from an unclaimed load's line.
    """
    if user_id is None:
        return list(queue)
    return [e for e in queue if not same_user(e, user_id)]


def carry_forward(
    queue: list[dict], claimant_id, now: float, expiry: float
) -> list[dict]:
    """Roll the line into the next load, minus whoever claimed that load.

    A three-deep line is a real thing in a shared house: A's load finishes, B
    takes the machine, and C should still be next. So the queue survives into
    the next session — but B, who is now running a load, is obviously not
    waiting for it, so they come out of the line.
    """
    return remove_user(prune(queue, now, expiry), claimant_id)


def select_handoff(
    queue: list[dict], now: float, expiry: float, claimant_id
) -> tuple[dict | None, list[dict]]:
    """Pick who gets the washer, and return the line with them taken off it.

    Returns ``(entry_or_None, new_queue)``. Three rules, all of which have a
    way of going wrong in a live channel and none of which are obvious from the
    call site, which is why they live here rather than in the coordinator:

    - **Stale entries never get the handoff.** Somebody who tapped 🔜 at
      breakfast and left the house would otherwise absorb the ping and leave
      the person actually standing there unnotified.
    - **The claimant is never handed the machine they are using.** They can
      still be in the line — they tapped 🔜 during someone else's load and then
      took the washer themselves — and pinging them would both break the
      one-push-per-load rule and silently consume the real next person's turn.
    - **The chosen entry comes out of the line**, so the fallback timer can't
      ping them a second time for the same load.
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

    Deliberately *only* the names: no timestamps, and nothing derived from
    them. An attribute that answers "how long have they waited" would change
    on every refresh tick, and the recorder writes a history row each time an
    attribute changes — the bug the connection-health sensor already had to be
    fixed for. This changes when somebody taps 🔜, and not otherwise.
    """
    return [entry_name(e) for e in queue]


def attributes(
    queue: list[dict], now: float, expiry: float, claimant_id
) -> dict:
    """The state-attribute view of the line — the line that would *act*.

    The stored line is only ever pruned when something happens to it (a tap,
    a session start, a handoff), so read at any other moment it can still hold
    entries that have aged past ``expiry``. Publishing that raw list would have
    a dashboard naming somebody the handoff would provably skip, so the same
    rules the handoff uses are applied here on read.

    ``queue``/``queue_count`` are the pruned line, matching what the Discord
    card shows; ``next_up`` additionally drops the claimant, because it is a
    claim about who gets the machine and :func:`select_handoff` never hands it
    to whoever is running it. (The two differ only in the odd case of someone
    tapping 🔜 *after* claiming — a claim already takes them out of the line.)

    Still churn-safe: nothing here is a clock-derived *value*. ``now`` only
    decides membership, so an expiry produces one attribute change per entry —
    bounded by taps, not by the 5-minute refresh tick.
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

    The line caps at :data:`QUEUE_CAP`, so this only ever sees small numbers —
    but the 11/12/13 exception is written in anyway rather than left as a trap
    for whoever raises the cap.
    """
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def tap_notice(result: str, place: int | None) -> str | None:
    """The private word owed to whoever just tapped 🔜, or None.

    A successful tap spends its one interaction *response* on the shared card,
    which is right — the whole house reads it — but it means the person who
    tapped gets nothing addressed to them, and on a phone scrolled away from
    the card they see nothing at all. Worse, joining and leaving look
    identical, which is how a working button reads as a broken one.

    Returns None for the two failure results: those answer the tap directly
    with their own ephemeral, and a second one would say it twice.
    """
    if result == TOGGLE_ADDED:
        if place is None:
            # Shouldn't happen — we were just added — but a confirmation that
            # can't name a place is still far better than silence.
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

    The handoff *pops* the head off the queue, so "Next up" loses them at the
    exact moment they were told — to the rest of the house it reads as though
    they were never waiting at all. Naming them here is consistent with the
    live card, which already names the claimant and the line; the anonymity
    rule (design doc §11 P5) is about the forward plan, not this card.

    The two paths stay worded apart. At the backstop nobody confirmed
    anything, and a card that claims otherwise is how the ping stops being
    trusted.
    """
    who = name or "someone"
    if hedged:
        return f"🔜 {who} — nudged that it's probably free (nobody confirmed)."
    return f"🔜 {who} — told the washer's free."
