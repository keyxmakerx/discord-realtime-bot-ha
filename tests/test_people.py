"""Tests for the pure per-person preference helpers.

Runnable with plain ``python3 tests/test_people.py`` — no pytest / Home
Assistant, mirroring ``tests/test_queue.py``. ``people.py`` is loaded by file
path so importing it does not pull in the package ``__init__`` (which imports
Home Assistant).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_PEOPLE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "laundry_discord",
    "people.py",
)
_spec = importlib.util.spec_from_file_location("ld_people", _PEOPLE_PATH)
_people = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _people
_spec.loader.exec_module(_people)

KINDS = _people.KINDS
KIND_CHECKIN = _people.KIND_CHECKIN
KIND_FIELDS = _people.KIND_FIELDS
KIND_OPPORTUNITY = _people.KIND_OPPORTUNITY
KIND_SLOT = _people.KIND_SLOT
KIND_TRADES = _people.KIND_TRADES
REMIND_CHANNEL = _people.REMIND_CHANNEL
REMIND_DM = _people.REMIND_DM
REMIND_OFF = _people.REMIND_OFF
delivery = _people.delivery
get_person = _people.get_person
is_known = _people.is_known
is_onboarded = _people.is_onboarded
mark_dm_failed = _people.mark_dm_failed
mark_dm_ok = _people.mark_dm_ok
normalise_people = _people.normalise_people
normalise_person = _people.normalise_person
person_key = _people.person_key
quiet_hours = _people.quiet_hours
set_dm_kind = _people.set_dm_kind
set_monitor = _people.set_monitor
set_person = _people.set_person
set_quiet_hours = _people.set_quiet_hours
set_reminders = _people.set_reminders
take_pending_dm_notice = _people.take_pending_dm_notice
wants_dm = _people.wants_dm
wants_kind = _people.wants_kind


# --- defaults ---------------------------------------------------------------


def test_unknown_person_gets_usable_defaults() -> None:
    person = get_person({}, 123)
    # Channel, not DM: identical to how the bot behaved before the panel
    # existed, so an upgrade changes nobody's notifications.
    assert person["reminders"] == REMIND_CHANNEL
    assert person["dm_ok"] is None  # untested, not "refused"
    assert person["onboarded"] is False
    assert person["dm_notice_pending"] is False
    assert person["monitor"] is True
    assert person["predict"] is True
    assert person["name"] == ""
    assert person["slots"] == []
    assert person["paused_until"] is None
    assert person["no_trade_from"] == []


def test_unknown_person_is_not_known_or_onboarded() -> None:
    assert is_known({}, 123) is False
    assert is_known(None, 123) is False
    assert is_onboarded({}, 123) is False
    # A record exists as soon as they answer, and answering onboards them.
    people = set_reminders({}, 123, REMIND_OFF)
    assert is_known(people, 123) is True
    assert is_onboarded(people, 123) is True


def test_defaults_are_not_shared_between_people() -> None:
    # A caller poking at one record must not redefine "default" for the house.
    a = get_person({}, 1)
    a["slots"].append([3, "eve"])
    a["reminders"] = REMIND_DM
    assert get_person({}, 2)["slots"] == []
    assert get_person({}, 2)["reminders"] == REMIND_CHANNEL


# --- the JSON round trip ----------------------------------------------------


def test_ids_still_match_after_a_json_round_trip() -> None:
    # HA's Store serialises to JSON, and JSON object keys are ALWAYS strings.
    # A tap arrives as interaction.user.id, an int — it must find the record
    # written before the restart rather than silently creating a second one.
    people = set_reminders({}, 12345, REMIND_DM, name="Sam")
    restored = json.loads(json.dumps(people))
    assert list(restored) == ["12345"]
    assert is_known(restored, 12345) is True
    assert get_person(restored, 12345)["reminders"] == REMIND_DM
    assert get_person(restored, "12345")["name"] == "Sam"
    # ...and writing again updates that record instead of adding another.
    updated = set_monitor(restored, 12345, False)
    assert list(updated) == ["12345"]
    assert get_person(updated, 12345)["monitor"] is False


def test_an_int_keyed_mapping_is_collapsed_not_duplicated() -> None:
    # An in-memory mapping that never round-tripped can be int-keyed. Reading
    # must find it, and writing must leave exactly one record behind.
    people = {123: {"reminders": REMIND_DM, "onboarded": True}}
    assert is_known(people, 123) is True
    assert get_person(people, "123")["reminders"] == REMIND_DM
    updated = set_monitor(people, 123, False)
    assert list(updated) == ["123"]
    assert updated["123"]["reminders"] == REMIND_DM  # merged, not reset
    assert normalise_people(people) == {"123": get_person(people, 123)}


def test_person_key_is_always_the_string_form() -> None:
    assert person_key(7) == "7"
    assert person_key("7") == "7"


# --- normalising a partial / corrupt record ---------------------------------


def test_normalise_fills_in_a_record_from_an_older_version() -> None:
    # Written before dm_notice_pending / monitor existed: no KeyError, and the
    # fields it *did* have survive.
    person = normalise_person({"name": "Alex", "reminders": REMIND_DM})
    assert person["name"] == "Alex"
    assert person["reminders"] == REMIND_DM
    assert person["dm_notice_pending"] is False
    assert person["monitor"] is True


def test_normalise_replaces_nonsense_with_defaults() -> None:
    person = normalise_person(
        {
            "name": None,
            "dm_ok": "yes",  # not a bool -> "untested"
            "reminders": "carrier-pigeon",  # not a mode -> the default
            "monitor": "false",  # truthy string must NOT read as True
            "onboarded": 1,  # not a bool
            "slots": "thu-eve",  # not a list
            "paused_until": "tomorrow",  # not a timestamp
        }
    )
    assert person["name"] == ""
    assert person["dm_ok"] is None
    assert person["reminders"] == REMIND_CHANNEL
    assert person["monitor"] is True
    assert person["onboarded"] is False
    assert person["slots"] == []
    assert person["paused_until"] is None


def test_normalise_survives_a_record_that_is_not_a_dict() -> None:
    assert normalise_person(None)["reminders"] == REMIND_CHANNEL
    assert normalise_person("junk")["onboarded"] is False
    assert normalise_people(None) == {}
    assert normalise_people({"1": "junk"})["1"]["reminders"] == REMIND_CHANNEL
    assert get_person({"1": "junk"}, 1)["monitor"] is True


def test_normalise_keeps_a_real_paused_until() -> None:
    assert normalise_person({"paused_until": 1754000000})["paused_until"] == (
        1754000000.0
    )


# --- 🔔 what the bot may send you -------------------------------------------


def test_every_dm_kind_starts_on_and_no_quiet_hours_are_set() -> None:
    # THE regression this whole feature must not cause: somebody who upgrades
    # gets exactly the messages they got yesterday. Every default here is
    # "carry on", so the settings can only ever subtract from what 📬 already
    # opted them into — a default of False would silence a house on upgrade,
    # and nobody would report it, because the symptom is silence.
    person = get_person({}, 123)
    assert person["dm_checkin"] is True
    assert person["dm_headsup"] is True
    assert person["dm_opportunity"] is True
    assert person["dm_trades"] is True
    assert person["quiet_start"] is None
    assert person["quiet_end"] is None
    assert quiet_hours(person) is None
    # ...and asked the way the two callers ask it.
    assert [wants_kind(person, kind) for kind in KINDS] == [True] * 4
    # Every kind names a field that actually exists on the record, so a kind
    # cannot be added to the table without a default to read.
    assert set(KIND_FIELDS.values()) <= set(person)


def test_one_kind_switched_off_leaves_the_other_three_alone() -> None:
    # The whole point of four switches instead of one: the unit somebody opts
    # out of is the message that annoyed them, not the feature it arrived in.
    # A setter that wrote all four would be the single 📬 switch again.
    people = set_reminders({}, 1, REMIND_DM, name="Sam")
    people = set_dm_kind(people, 1, KIND_OPPORTUNITY, False)
    person = get_person(people, 1)
    assert wants_kind(person, KIND_OPPORTUNITY) is False
    assert person["dm_opportunity"] is False
    assert [person[KIND_FIELDS[k]] for k in (
        KIND_CHECKIN, KIND_SLOT, KIND_TRADES
    )] == [True, True, True]
    # It is a toggle, not a one-way door (P7).
    people = set_dm_kind(people, 1, KIND_OPPORTUNITY, True)
    assert get_person(people, 1)["dm_opportunity"] is True


def test_an_unknown_kind_is_never_written_and_never_gates() -> None:
    # Two failures, opposite directions. A setter that wrote dm_typo would look
    # like it had saved the setting while the person went on being messaged;
    # and a reader that treated an unrecognised kind as "off" would silently
    # mute somebody over a typo, which is the failure nobody finds.
    people = set_reminders({}, 1, REMIND_DM)
    after = set_dm_kind(people, 1, "dm_nonsense", False)
    assert after is people  # untouched, so a bad id costs no store write
    assert set_dm_kind(None, 1, "dm_nonsense", False) == {}
    person = get_person(people, 1)
    assert wants_kind(person, "dm_nonsense") is True
    assert wants_kind(person, None) is True
    assert wants_kind("junk", KIND_CHECKIN) is True


def test_a_half_set_quiet_pair_is_not_a_window() -> None:
    # A store write interrupted between the two keys, or a hand-edited options
    # file. "Quiet from 22:00" with no morning to switch back on would silence
    # somebody permanently, so half a pair reads as no pair at all.
    assert normalise_person({"quiet_start": 22})["quiet_start"] is None
    assert normalise_person({"quiet_start": 22})["quiet_end"] is None
    assert normalise_person({"quiet_end": 8})["quiet_end"] is None
    assert quiet_hours({"quiet_start": 22, "quiet_end": None}) is None
    # Same hour at both ends is no window either, not a 24-hour one: no single
    # tap is allowed to collapse into total silence.
    assert quiet_hours({"quiet_start": 22, "quiet_end": 22}) is None
    same = normalise_person({"quiet_start": 22, "quiet_end": 22})
    assert (same["quiet_start"], same["quiet_end"]) == (None, None)


def test_quiet_hours_refuse_anything_that_is_not_an_hour() -> None:
    # isinstance(True, int) is True in Python, so a stored bool would read as
    # 01:00 — a quiet window nobody chose, eating messages they asked for.
    assert quiet_hours({"quiet_start": True, "quiet_end": 8}) is None
    assert quiet_hours({"quiet_start": 22, "quiet_end": False}) is None
    # ...and an hour that cannot exist came from a hand-edited file. Guessing
    # what it meant is worse than having no window.
    assert quiet_hours({"quiet_start": 24, "quiet_end": 8}) is None
    assert quiet_hours({"quiet_start": -1, "quiet_end": 8}) is None
    assert quiet_hours({"quiet_start": "10pm", "quiet_end": 8}) is None
    assert quiet_hours({"quiet_start": None, "quiet_end": None}) is None
    assert quiet_hours("junk") is None
    # The forms JSON actually hands back are kept: a whole-number float and the
    # string an options flow can store are both a real hour.
    assert quiet_hours({"quiet_start": 22.0, "quiet_end": "8"}) == (22, 8)
    assert quiet_hours({"quiet_start": 0, "quiet_end": 7}) == (0, 7)  # midnight


def test_setting_quiet_hours_stores_both_ends_or_neither() -> None:
    people = set_quiet_hours(set_reminders({}, 1, REMIND_DM), 1, 23, 9)
    assert quiet_hours(get_person(people, 1)) == (23, 9)
    # "No quiet hours" is the same setter, and it clears both ends.
    cleared = set_quiet_hours(people, 1, None, None)
    assert quiet_hours(get_person(cleared, 1)) is None
    # An unusable end clears the window rather than leaving half of one behind.
    assert quiet_hours(get_person(set_quiet_hours(people, 1, 22, "eight"), 1)) is None


def test_the_dm_settings_survive_the_json_round_trip() -> None:
    # HA's Store is JSON, so these read back through it on every restart: the
    # booleans must not come back as strings, the hours must not come back as
    # None, and the int id must still find the record (see the module docstring).
    people = set_reminders({}, 12345, REMIND_DM, name="Sam")
    people = set_dm_kind(people, 12345, KIND_SLOT, False)
    people = set_quiet_hours(people, 12345, 22, 8)
    restored = json.loads(json.dumps(people))
    person = get_person(restored, 12345)
    assert wants_kind(person, KIND_SLOT) is False
    assert wants_kind(person, KIND_CHECKIN) is True
    assert quiet_hours(person) == (22, 8)
    # ...and a later write through the string key keeps them.
    updated = set_monitor(restored, "12345", False)
    assert quiet_hours(get_person(updated, 12345)) == (22, 8)
    assert get_person(updated, 12345)["dm_headsup"] is False


def test_a_junk_record_reads_as_every_message_on_and_no_quiet_hours() -> None:
    # §12: a half-written or hand-edited record must never raise inside a
    # button callback, and must never *invent* a refusal either. A stored
    # "false" is truthy, so reading these with bool() would flip three consents
    # the person still has switched on.
    person = normalise_person(
        {
            "dm_checkin": "false",  # truthy string must NOT read as True...
            "dm_headsup": 0,  # ...and a non-bool must not read as off
            "dm_opportunity": None,
            "dm_trades": "yes",
            "quiet_start": "22:00",
            "quiet_end": [8],
        }
    )
    assert person["dm_checkin"] is True
    assert person["dm_headsup"] is True
    assert person["dm_opportunity"] is True
    assert person["dm_trades"] is True
    assert person["quiet_start"] is None and person["quiet_end"] is None
    # Only a real stored False is a refusal.
    assert normalise_person({"dm_trades": False})["dm_trades"] is False


# --- dm_ok transitions ------------------------------------------------------


def test_dm_failure_records_the_refusal_and_owes_a_notice() -> None:
    people = set_reminders({}, 1, REMIND_DM, name="Kim")
    assert wants_dm(people, 1) is True
    people = mark_dm_failed(people, 1)
    person = get_person(people, 1)
    assert person["dm_ok"] is False
    assert person["dm_notice_pending"] is True
    assert person["name"] == "Kim"  # the refusal doesn't wipe the record
    # Their stated preference is untouched — but delivery routes to the channel
    # so the reminder isn't silently lost.
    assert person["reminders"] == REMIND_DM
    assert delivery(people, 1) == REMIND_CHANNEL
    assert wants_dm(people, 1) is False


def test_a_successful_dm_clears_the_refusal_and_the_notice() -> None:
    people = mark_dm_failed(set_reminders({}, 1, REMIND_DM), 1)
    people = mark_dm_ok(people, 1)
    person = get_person(people, 1)
    assert person["dm_ok"] is True
    assert person["dm_notice_pending"] is False
    assert delivery(people, 1) == REMIND_DM


def test_choosing_dm_again_re_arms_a_refused_dm() -> None:
    # The self-heal: without this a single Forbidden would pin somebody to the
    # channel forever, even after they fix their privacy settings.
    people = mark_dm_failed(set_reminders({}, 1, REMIND_DM), 1)
    assert delivery(people, 1) == REMIND_CHANNEL
    people = set_reminders(people, 1, REMIND_DM)
    assert get_person(people, 1)["dm_ok"] is None
    assert delivery(people, 1) == REMIND_DM


def test_delivery_reflects_the_stated_preference() -> None:
    assert delivery({}, 1) == REMIND_CHANNEL  # never opted in
    assert delivery(set_reminders({}, 1, REMIND_OFF), 1) == REMIND_OFF
    assert delivery(set_reminders({}, 1, REMIND_CHANNEL), 1) == REMIND_CHANNEL
    assert delivery(set_reminders({}, 1, "nonsense"), 1) == REMIND_CHANNEL


def test_off_is_not_downgraded_by_a_closed_dm() -> None:
    # Only a DM preference falls back; "off" means off.
    people = set_person(set_reminders({}, 1, REMIND_OFF), 1, dm_ok=False)
    assert delivery(people, 1) == REMIND_OFF


# --- the pending notice is consumed exactly once ----------------------------


def test_pending_notice_is_shown_once_then_cleared() -> None:
    people = mark_dm_failed({}, 1)
    owed, people = take_pending_dm_notice(people, 1)
    assert owed is True
    assert get_person(people, 1)["dm_notice_pending"] is False
    owed, people = take_pending_dm_notice(people, 1)
    assert owed is False  # a second visit doesn't nag


def test_taking_a_notice_nobody_is_owed_changes_nothing() -> None:
    people = set_reminders({}, 1, REMIND_CHANNEL)
    owed, after = take_pending_dm_notice(people, 1)
    assert owed is False
    assert after is people  # untouched, so opening the panel costs no save
    owed, after = take_pending_dm_notice({}, 999)
    assert (owed, after) == (False, {})


def test_a_second_refusal_re_arms_the_notice() -> None:
    people = mark_dm_failed({}, 1)
    _owed, people = take_pending_dm_notice(people, 1)
    people = mark_dm_failed(people, 1)
    assert take_pending_dm_notice(people, 1)[0] is True


def test_a_refused_dm_cannot_re_arm_its_own_notice() -> None:
    # Why the assistant may only clear the flag once the panel has *landed*:
    # after a refusal, delivery() routes this person to the channel, so no
    # further DM is attempted and mark_dm_failed can never fire again on its
    # own. A notice cleared before it was seen is therefore gone for good.
    people = set_reminders({}, 1, REMIND_DM)
    people = mark_dm_failed(people, 1)
    assert delivery(people, 1) == REMIND_CHANNEL
    assert wants_dm(people, 1) is False
    # Only an explicit "DM me" re-arms the route (and it is the panel that
    # offers that button — the very thing they'd never have seen).
    people = set_reminders(people, 1, REMIND_DM)
    assert wants_dm(people, 1) is True


def test_reading_the_pending_flag_does_not_clear_it() -> None:
    # The read and the clear are separate on purpose: the panel renders from
    # the flag, and only a confirmed delivery is allowed to retire it.
    people = mark_dm_failed({}, 1)
    assert get_person(people, 1)["dm_notice_pending"] is True
    assert get_person(people, 1)["dm_notice_pending"] is True  # still owed
    owed, people = take_pending_dm_notice(people, 1)
    assert owed is True
    assert get_person(people, 1)["dm_notice_pending"] is False


def test_a_null_id_still_routes_to_the_channel() -> None:
    # queue.py tolerates an entry whose id never persisted ({"id": None}), and
    # select_handoff can hand one back as the head. There is nobody to DM, but
    # the entry has already been popped off the line, so the handoff must still
    # reach the channel rather than being swallowed.
    people = set_reminders({}, 1, REMIND_DM)
    assert delivery(people, None) == REMIND_CHANNEL
    assert wants_dm(people, None) is False


# --- non-mutation -----------------------------------------------------------


def test_writers_never_mutate_the_mapping_they_are_given() -> None:
    # The assistant assigns the result; a failed save must not have already
    # changed the in-memory prefs.
    people = set_reminders({}, 1, REMIND_DM, name="Sam")
    snapshot = json.dumps(people, sort_keys=True)
    set_reminders(people, 1, REMIND_OFF)
    set_monitor(people, 1, False)
    set_person(people, 2, name="Ty")
    mark_dm_failed(people, 1)
    mark_dm_ok(people, 1)
    take_pending_dm_notice(people, 1)
    assert json.dumps(people, sort_keys=True) == snapshot


def test_get_person_never_hands_back_the_stored_record() -> None:
    people = set_reminders({}, 1, REMIND_DM)
    person = get_person(people, 1)
    person["reminders"] = REMIND_OFF
    person["slots"].append([3, "eve"])
    assert get_person(people, 1)["reminders"] == REMIND_DM
    assert get_person(people, 1)["slots"] == []


def test_setting_one_person_leaves_the_others_alone() -> None:
    people = set_reminders({}, 1, REMIND_DM, name="Sam")
    people = set_reminders(people, 2, REMIND_OFF, name="Ty")
    people = set_monitor(people, 1, False)
    assert get_person(people, 2)["reminders"] == REMIND_OFF
    assert get_person(people, 2)["name"] == "Ty"
    assert get_person(people, 1)["reminders"] == REMIND_DM  # merged, not reset


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
