"""Pure, dependency-free per-person preferences for the 🤖 assistant.

Kept free of Home Assistant / discord imports so the record rules — the
defaults, the JSON key hazard, the "I couldn't DM you" latch — can be
unit-tested without the HA test harness, the same discipline that made
:mod:`detect` and :mod:`queue` reliable. :mod:`assistant` owns the ``Store``
and every Discord call; this module only decides what a person's record should
look like.

Records live under a ``{"<discord_user_id>": {...}}`` mapping so they
round-trip through HA's ``Store`` as JSON, and every function returns new data
rather than mutating what it was handed — the caller assigns the result, so a
rejected write can't half-apply.

The one hazard worth stating up front: **JSON object keys are always strings.**
A record written while the key was ``interaction.user.id`` (an int) comes back
off disk under ``"123"``, and looking it up with the int again would miss and
silently create a second, empty record for somebody who already has one.
:mod:`queue` hit the same thing with entry ids. Every read and write here goes
through :func:`person_key`.
"""

from __future__ import annotations

# How somebody wants to be reached for the messages that are *about them* —
# their load finishing, or the washer coming free for them.
REMIND_DM = "dm"
REMIND_CHANNEL = "channel"
REMIND_OFF = "off"
REMIND_MODES = (REMIND_DM, REMIND_CHANNEL, REMIND_OFF)

# The default for somebody who has never touched the panel is the CHANNEL, not
# a DM: that is byte-identical to how the bot behaved before the assistant
# existed. Opting in is a deliberate act; nobody's phone starts buzzing in a
# new place because they upgraded the integration.
DEFAULT_REMINDERS = REMIND_CHANNEL


def _defaults() -> dict:
    """A fresh record for somebody the bot has never seen.

    Built per call rather than shared as a module constant: a shared dict would
    be one accidental in-place write away from redefining "default" for the
    whole house.
    """
    return {
        "name": "",
        # None = never tried, True = a DM went through, False = Forbidden
        # (50007). Tri-state on purpose: "untested" and "refused" want very
        # different behaviour, and collapsing them into a bool loses the
        # difference between "we don't know" and "we know it doesn't work".
        "dm_ok": None,
        "reminders": DEFAULT_REMINDERS,
        "predict": True,
        "monitor": True,
        "onboarded": False,
        # Set when a DM was refused, cleared the next time the panel is opened.
        # This is the whole self-heal path: Discord never tells the recipient
        # their privacy settings ate a message, so the only way they find out
        # is the next thing they tap.
        "dm_notice_pending": False,
        # Stored and normalised so an upgrade doesn't lose them, but nothing in
        # this phase reads them: recurring slots and the trade blocklist belong
        # to the week grid and the broker (design doc §6 / §9), and
        # ``paused_until`` to the reminder schedule (§10). Defining them now
        # costs one line each and saves a migration later.
        "slots": [],
        "paused_until": None,
        "no_trade_from": [],
    }


def person_key(user_id) -> str:
    """The mapping key for a Discord user id.

    Always the string form — see the module docstring. Callers hand us either
    the int from ``interaction.user.id`` or whatever came back off disk, and
    both must land on the same record.
    """
    return str(user_id)


def _flag(record: dict, field: str, default: bool) -> bool:
    """A boolean field, falling back to ``default`` for anything unexpected.

    Deliberately not ``bool(value)``: a half-written record can hold the string
    ``"false"`` (truthy) or ``None``, and reading either as True would flip a
    consent — monitoring, say — that the person never gave.
    """
    value = record.get(field, default)
    return value if isinstance(value, bool) else default


def _timestamp(value) -> float | None:
    """A unix timestamp field, or None when it is missing/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_list(value) -> list:
    """A list field, defensively emptied when the stored value isn't one."""
    return list(value) if isinstance(value, list) else []


def normalise_person(record: dict | None, *, name: str | None = None) -> dict:
    """A complete, sane record built from whatever was stored.

    Rebuilt field by field from the defaults rather than merged over them, so a
    record written by an older version (missing keys), a partially-written one
    (a crash mid-save) or an outright corrupt one all load with usable values
    instead of raising ``KeyError`` at the worst possible moment — inside a
    button callback, where the user just sees "interaction failed".

    ``name`` overrides the stored display name when the caller has a fresher
    one (people rename themselves).
    """
    source = record if isinstance(record, dict) else {}
    person = _defaults()
    stored_name = name if name is not None else source.get("name")
    person["name"] = str(stored_name) if stored_name else ""
    dm_ok = source.get("dm_ok")
    person["dm_ok"] = dm_ok if isinstance(dm_ok, bool) else None
    reminders = source.get("reminders")
    person["reminders"] = (
        reminders if reminders in REMIND_MODES else DEFAULT_REMINDERS
    )
    person["predict"] = _flag(source, "predict", True)
    person["monitor"] = _flag(source, "monitor", True)
    person["onboarded"] = _flag(source, "onboarded", False)
    person["dm_notice_pending"] = _flag(source, "dm_notice_pending", False)
    person["slots"] = _string_list(source.get("slots"))
    person["paused_until"] = _timestamp(source.get("paused_until"))
    person["no_trade_from"] = _string_list(source.get("no_trade_from"))
    return person


def normalise_people(people) -> dict[str, dict]:
    """Every record, re-keyed by :func:`person_key` and normalised.

    Called once when the prefs are loaded so the rest of the run works on a
    known shape. If a person somehow ended up under both ``123`` and ``"123"``
    (an in-memory write that never round-tripped), the later one wins and the
    duplicate disappears — better than two half-records for one human.
    """
    if not isinstance(people, dict):
        return {}
    normalised: dict[str, dict] = {}
    for key, record in people.items():
        normalised[person_key(key)] = normalise_person(record)
    return normalised


def _lookup(people, user_id) -> dict:
    """The raw stored record for a user, or ``{}``.

    Tolerates an int key as well as the string one so a mapping that has not
    been through :func:`normalise_people` (or through JSON) still resolves.
    """
    if not isinstance(people, dict):
        return {}
    key = person_key(user_id)
    record = people.get(key)
    if record is None:
        for stored_key, stored in people.items():
            if person_key(stored_key) == key:
                record = stored
                break
    return record if isinstance(record, dict) else {}


def is_known(people, user_id) -> bool:
    """Whether the bot has any record for this person at all.

    Not the same as onboarded: a record exists as soon as somebody answers the
    panel, but :func:`is_onboarded` is what decides which panel they see.
    """
    if not isinstance(people, dict):
        return False
    key = person_key(user_id)
    return any(
        person_key(k) == key and isinstance(v, dict) for k, v in people.items()
    )


def get_person(people, user_id) -> dict:
    """This person's normalised record — defaults for somebody unknown.

    Always returns a full record, so callers can read any field without a
    ``.get()`` dance, and never returns the stored dict itself, so a caller
    poking at the result can't corrupt the prefs.
    """
    return normalise_person(_lookup(people, user_id))


def is_onboarded(people, user_id) -> bool:
    """Whether this person has answered the first-time panel."""
    return get_person(people, user_id)["onboarded"]


def set_person(people, user_id, **changes) -> dict:
    """A new mapping with ``changes`` merged into this person's record.

    Any other key of the same person (an int alongside the string form) is
    dropped in the process, so a write can never leave two records behind for
    one human.
    """
    key = person_key(user_id)
    merged = {**_lookup(people, user_id), **changes}
    source = people if isinstance(people, dict) else {}
    updated = {k: v for k, v in source.items() if person_key(k) != key}
    updated[key] = normalise_person(merged)
    return updated


def set_reminders(people, user_id, mode: str, *, name: str | None = None) -> dict:
    """Record how somebody wants to be reached, and mark them onboarded.

    Answering the question *is* the onboarding — there is no separate "got it"
    step to tap, because a second tap to confirm a preference is exactly the
    kind of friction that makes people close the panel.

    Choosing DM again also resets ``dm_ok`` to untested. It is the one signal
    we ever get that somebody may have fixed their Discord privacy settings,
    and without it a single ``Forbidden`` would pin them to the channel
    forever.
    """
    if mode not in REMIND_MODES:
        mode = DEFAULT_REMINDERS
    changes: dict = {"reminders": mode, "onboarded": True}
    if mode == REMIND_DM:
        changes["dm_ok"] = None
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def set_monitor(people, user_id, enabled: bool, *, name: str | None = None) -> dict:
    """Turn per-person load logging on or off (design doc §11)."""
    changes: dict = {"monitor": bool(enabled), "onboarded": True}
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def mark_dm_ok(people, user_id) -> dict:
    """A DM went through: remember it and drop any notice we owed them.

    Clearing the notice matters — showing "I couldn't DM you" to somebody whose
    DMs demonstrably work reads as the bot being broken.
    """
    return set_person(people, user_id, dm_ok=True, dm_notice_pending=False)


def mark_dm_failed(people, user_id, *, name: str | None = None) -> dict:
    """A DM was refused (``discord.Forbidden``, error 50007).

    Two things get recorded: that DMs don't work for this person (so we stop
    trying and use the channel), and that we owe them the explainer — Discord
    tells the *sender* about a blocked DM and never the recipient, so the next
    panel they open is the only way they'll find out.
    """
    changes: dict = {"dm_ok": False, "dm_notice_pending": True}
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def take_pending_dm_notice(people, user_id) -> tuple[bool, dict]:
    """Whether the "I couldn't DM you" notice is owed — and clear it.

    Returns ``(owed, new_people)``. Read-and-clear in one call so the notice is
    shown exactly once per failure: a panel that repeated it every visit would
    be nagging somebody about a setting they may have already fixed. When
    nothing is owed the mapping comes back untouched, so opening the panel
    doesn't cost a store write.
    """
    if not get_person(people, user_id)["dm_notice_pending"]:
        return (False, people if isinstance(people, dict) else {})
    return (True, set_person(people, user_id, dm_notice_pending=False))


def delivery(people, user_id) -> str:
    """Where a message for this person should actually go.

    The stored preference, except that a person who asked for DMs and whose
    DMs are known to be closed is routed to the channel instead. A reminder is
    never silently dropped (design doc §10.5, rule 2); the panel is what tells
    them why.
    """
    person = get_person(people, user_id)
    mode = person["reminders"]
    if mode == REMIND_DM and person["dm_ok"] is False:
        return REMIND_CHANNEL
    return mode


def wants_dm(people, user_id) -> bool:
    """Whether a DM should be attempted for this person right now."""
    return delivery(people, user_id) == REMIND_DM
