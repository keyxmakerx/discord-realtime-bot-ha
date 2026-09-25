"""Pure, dependency-free per-person preferences for the assistant.

No Home Assistant or discord imports, so it is unit-tested directly.
:mod:`assistant` owns the Store and every Discord call; this module only
decides what a person's record should look like.
"""

from __future__ import annotations

# Reach preference for messages about this person (load done, washer free).
REMIND_DM = "dm"
REMIND_CHANNEL = "channel"
REMIND_OFF = "off"
REMIND_MODES = (REMIND_DM, REMIND_CHANNEL, REMIND_OFF)

# Defaults to the channel, matching pre-assistant behavior; DMs are opt-in.
DEFAULT_REMINDERS = REMIND_CHANNEL

# --- which unprompted messages somebody will take ---------------------------
# One opt-out per kind, so refusing one doesn't silence the rest. Defined
# here (not nudge/trade) since both already import this module.
KIND_CHECKIN = "checkin"  # the Sunday plan DM
KIND_SLOT = "slot"  # ⏰ heads-up before a slot you booked
KIND_OPPORTUNITY = "opportunity"  # 💡 "tonight's wide open and you're overdue"
KIND_TRADES = "trades"  # 🔁 a housemate asking for your slot
KIND_EMPTY = "empty"  # 🧺 your finished load is still in the washer
KIND_TAKEN = "taken"  # 🏃 someone else is using the washer in your booked slot
KINDS = (
    KIND_CHECKIN,
    KIND_SLOT,
    KIND_OPPORTUNITY,
    KIND_TRADES,
    KIND_EMPTY,
    KIND_TAKEN,
)

# Which stored field each kind reads. A kind added here without a matching
# default in :func:`_defaults` would silently never persist a change.
KIND_FIELDS = {
    KIND_CHECKIN: "dm_checkin",
    KIND_SLOT: "dm_headsup",
    KIND_OPPORTUNITY: "dm_opportunity",
    KIND_TRADES: "dm_trades",
    KIND_EMPTY: "dm_empty",
    KIND_TAKEN: "dm_taken",
}


def _defaults() -> dict:
    """A fresh record for somebody the bot has never seen. Built per call,
    not shared as a module constant, so an in-place write can't redefine
    "default" for the whole house."""
    return {
        "name": "",
        # Tri-state: unknown / delivered / refused (Forbidden 50007), not
        # a bool, so "we don't know" stays distinct from "it doesn't work".
        "dm_ok": None,
        "reminders": DEFAULT_REMINDERS,
        "predict": True,
        "monitor": True,
        "onboarded": False,
        # Discord never tells the recipient a DM was blocked; this flag is
        # how the panel finds out and shows the notice once.
        "dm_notice_pending": False,
        # All on by default: these messages predate the switch, and a
        # setting may only subtract, never surprise.
        "dm_checkin": True,
        "dm_headsup": True,
        "dm_opportunity": True,
        "dm_trades": True,
        "dm_empty": True,
        "dm_taken": True,
        # Local hours, wraps midnight (22 -> 8); None/None means no window.
        "quiet_start": None,
        "quiet_end": None,
        # Not read yet; normalised now to avoid a migration later.
        "slots": [],
        "paused_until": None,
        "no_trade_from": [],
    }


def person_key(user_id) -> str:
    """The mapping key for a Discord user id: always the string form.
    JSON object keys are always strings, so a record written with the int
    from `interaction.user.id` comes back as `"123"`; both forms must land
    on the same record or a lookup creates a duplicate."""
    return str(user_id)


def _flag(record: dict, field: str, default: bool) -> bool:
    """A boolean field, falling back to ``default`` for anything unexpected.
    Not `bool(value)`: a stored string like `"false"` is truthy, and
    reading it as True would flip a consent the person never gave."""
    value = record.get(field, default)
    return value if isinstance(value, bool) else default


def _hour(value) -> int | None:
    """A local hour, 0-23, or None for anything that isn't one. Bools are
    refused explicitly: `isinstance(True, int)` is True in Python, so a
    stored `True` would otherwise read as 01:00."""
    if value is None or isinstance(value, bool):
        return None
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


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
    """A complete, sane record built from whatever was stored. Rebuilt
    field by field from the defaults, not merged over them, so a missing,
    partial or corrupt record still loads with usable values instead of
    raising `KeyError` inside a button callback. ``name`` overrides the
    stored display name when the caller has a fresher one."""
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
    for field in KIND_FIELDS.values():
        person[field] = _flag(source, field, True)
    # One pair, not two fields: a half-set window can exist on disk but
    # never in memory; see :func:`quiet_hours`.
    window = quiet_hours(source)
    person["quiet_start"], person["quiet_end"] = window or (None, None)
    person["slots"] = _string_list(source.get("slots"))
    person["paused_until"] = _timestamp(source.get("paused_until"))
    person["no_trade_from"] = _string_list(source.get("no_trade_from"))
    return person


def normalise_people(people) -> dict[str, dict]:
    """Every record, re-keyed by :func:`person_key` and normalised. If a
    person ended up under both ``123`` and ``"123"``, the later one wins
    and the duplicate disappears."""
    if not isinstance(people, dict):
        return {}
    normalised: dict[str, dict] = {}
    for key, record in people.items():
        normalised[person_key(key)] = normalise_person(record)
    return normalised


def _lookup(people, user_id) -> dict:
    """The raw stored record for a user, or ``{}``. Tolerates an int key
    as well as the string one, for a mapping that hasn't been through
    :func:`normalise_people` or JSON yet."""
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
    """Whether the bot has any record for this person at all. Not the
    same as onboarded: a record exists once anyone answers the panel;
    :func:`is_onboarded` decides which panel they see."""
    if not isinstance(people, dict):
        return False
    key = person_key(user_id)
    return any(
        person_key(k) == key and isinstance(v, dict) for k, v in people.items()
    )


def get_person(people, user_id) -> dict:
    """This person's normalised record, or defaults if unknown. Always a
    full record (no ``.get()`` needed) and never the stored dict itself,
    so a caller can't corrupt the prefs by mutating the result."""
    return normalise_person(_lookup(people, user_id))


def is_onboarded(people, user_id) -> bool:
    """Whether this person has answered the first-time panel."""
    return get_person(people, user_id)["onboarded"]


def quiet_hours(person) -> tuple[int, int] | None:
    """This person's quiet window as ``(start, end)`` local hours, or None.
    Public so :func:`normalise_person`, :func:`nudge.in_quiet_hours` and
    the panel all read the same answer. Both ends or neither: a half-set
    pair reads as no window. ``start == end`` is also no window, not 24
    hours, so one setting can't collapse into total silence."""
    if not isinstance(person, dict):
        return None
    start = _hour(person.get("quiet_start"))
    end = _hour(person.get("quiet_end"))
    if start is None or end is None or start == end:
        return None
    return (start, end)


def wants_kind(person, kind) -> bool:
    """Whether this person takes this kind of unprompted message. Takes a
    record, not the mapping and an id, mirroring :func:`nudge.is_paused`.
    An unknown kind reads as True: these switches may only silence a kind
    they know about, never a typo'd or newer one by accident."""
    field = KIND_FIELDS.get(kind) if isinstance(kind, str) else None
    if field is None:
        return True
    return _flag(person if isinstance(person, dict) else {}, field, True)


def set_person(people, user_id, **changes) -> dict:
    """A new mapping with ``changes`` merged into this person's record.
    Drops any other key for the same person (e.g. an int alongside the
    string form), so a write can't leave two records for one human."""
    key = person_key(user_id)
    merged = {**_lookup(people, user_id), **changes}
    source = people if isinstance(people, dict) else {}
    updated = {k: v for k, v in source.items() if person_key(k) != key}
    updated[key] = normalise_person(merged)
    return updated


def set_reminders(people, user_id, mode: str, *, name: str | None = None) -> dict:
    """Record how somebody wants to be reached, and mark them onboarded.
    Answering the question is the onboarding; there's no separate confirm
    step. Choosing DM again resets ``dm_ok`` to untested, since otherwise
    one ``Forbidden`` would pin them to the channel forever."""
    if mode not in REMIND_MODES:
        mode = DEFAULT_REMINDERS
    changes: dict = {"reminders": mode, "onboarded": True}
    if mode == REMIND_DM:
        changes["dm_ok"] = None
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def set_monitor(people, user_id, enabled: bool, *, name: str | None = None) -> dict:
    """Turn per-person load logging on or off."""
    changes: dict = {"monitor": bool(enabled), "onboarded": True}
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def set_dm_kind(
    people, user_id, kind, enabled: bool, *, name: str | None = None
) -> dict:
    """Turn one kind of unprompted message on or off for one person. An
    unknown kind returns the mapping untouched rather than writing a
    field nothing reads, so a mistyped id fails loudly instead of
    looking like it saved."""
    field = KIND_FIELDS.get(kind) if isinstance(kind, str) else None
    if field is None:
        return people if isinstance(people, dict) else {}
    # Answering any sub-panel counts as onboarding, same as set_monitor.
    changes: dict = {field: bool(enabled), "onboarded": True}
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def set_quiet_hours(people, user_id, start, end, *, name: str | None = None) -> dict:
    """Set, or with ``None, None`` clear, the overnight quiet window. Both
    ends are written and read together (:func:`quiet_hours`), so an
    unreadable end clears the whole window rather than leaving half of
    one."""
    changes: dict = {
        "quiet_start": _hour(start),
        "quiet_end": _hour(end),
        "onboarded": True,
    }
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def mark_dm_ok(people, user_id) -> dict:
    """A DM went through: remember it and drop any notice we owed them.
    Otherwise "I couldn't DM you" would show for someone whose DMs work."""
    return set_person(people, user_id, dm_ok=True, dm_notice_pending=False)


def mark_dm_failed(people, user_id, *, name: str | None = None) -> dict:
    """A DM was refused (`discord.Forbidden`, error 50007). Records that
    DMs don't work (fall back to the channel) and that we owe an
    explainer, since Discord never tells the recipient why it bounced."""
    changes: dict = {"dm_ok": False, "dm_notice_pending": True}
    if name:
        changes["name"] = name
    return set_person(people, user_id, **changes)


def take_pending_dm_notice(people, user_id) -> tuple[bool, dict]:
    """Whether the "I couldn't DM you" notice is owed, and clear it.
    Returns ``(owed, new_people)``. Read-and-clear in one call so the
    notice shows exactly once. Untouched when nothing is owed."""
    if not get_person(people, user_id)["dm_notice_pending"]:
        return (False, people if isinstance(people, dict) else {})
    return (True, set_person(people, user_id, dm_notice_pending=False))


def delivery(people, user_id) -> str:
    """Where a message for this person should actually go. The stored
    preference, except DM-with-DMs-known-closed routes to the channel
    instead. A reminder is never silently dropped."""
    person = get_person(people, user_id)
    mode = person["reminders"]
    if mode == REMIND_DM and person["dm_ok"] is False:
        return REMIND_CHANNEL
    return mode


def wants_dm(people, user_id) -> bool:
    """Whether a DM should be attempted for this person right now."""
    return delivery(people, user_id) == REMIND_DM
