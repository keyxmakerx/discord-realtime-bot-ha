"""Tests for the pure health checks behind the `diagnostics` action.

Runnable with plain ``python3 tests/test_diagnose.py`` — no pytest, no Home
Assistant — mirroring the other pure suites. ``diagnose.py`` is loaded by file
path so importing it does not pull in the package ``__init__``.

The anchor case is a **real incident**: a session captured off the running
install on 2026-08-28, in which the bot reported a wash while the machine was
switched off. Every threshold here is asserted against that data rather than
against invented numbers, because a check that only fires on a hypothetical is
a check nobody can trust the next time this happens.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "laundry_discord",
    "diagnose.py",
)
_spec = importlib.util.spec_from_file_location("ld_diagnose", _PATH)
_d = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _d
_spec.loader.exec_module(_d)

check = _d.check
summarise = _d.summarise
flap_cadence = _d.flap_cadence
PROBLEM, WARNING, NOTE = _d.PROBLEM, _d.WARNING, _d.NOTE

# The incident, verbatim.
FLAPS = [
    1787866080.842309, 1787869167.705905, 1787872254.551392, 1787875341.363642,
    1787878474.392588, 1787881561.251164, 1787884648.025608, 1787887735.021497,
    1787890821.798104, 1787893908.587645, 1787896995.505498, 1787900082.344886,
    1787903187.25417, 1787906274.39207, 1787909622.307071, 1787912708.767186,
    1787915855.379614, 1787918984.985064, 1787922072.291811,
]
STARTED = 1787922106.292044


def _incident(**over):
    session = {
        "stage": "washing", "waiting": False, "claimed_by": "Unclaimed",
        "claimed_by_id": None, "queue": [], "emptied": False,
        "message_id": 1542881883527057553, "paused": False, "cancelled": False,
        "last_real_phase": "wash", "energy_start": 11.6,
        "session_started_ts": STARTED, "offline_since": None,
        "last_eta_ts": None, "offline_unverified": False,
        "detector": {
            "phase": "active", "last_energy": 11.6,
            "last_rise_ts": 1787922106.291775, "idle_energy": 11.6,
        },
        "flap_times": list(FLAPS),
    }
    session.update(over)
    return session


def _codes(findings):
    return [f["code"] for f in findings]


def test_the_real_incident_is_diagnosed_without_a_human_doing_arithmetic():
    # The whole point. Working this out by hand took reading a storage file and
    # converting epoch timestamps; every conclusion reached that way is below.
    found = check(
        _incident(), STARTED + 7200,
        watched={"running": "off", "machine_state": "stop", "job_state": "none"},
    )
    codes = _codes(found)
    assert "machine_says_idle" in codes       # the washer contradicts the bot
    assert "meter_never_moved" in codes       # no energy was ever used
    assert "started_on_a_reconnect" in codes  # 34s after a drop
    assert "no_completion_estimate" in codes  # the washer never estimated one
    assert "connection_cadence" in codes      # drops on a timer, not at random
    # All warnings, none problems — by the module's own severity contract. A
    # phantom is "wrong now, and a safety net will eventually clear it" (the
    # flat-energy backstop ends it within the hour); PROBLEM is reserved for
    # states nothing will clear. The wording carries the action instead: the
    # meter finding names reset_session, and the machine finding asks for a
    # second run, which a wedge survives and an end-of-cycle window does not.
    assert all(f["severity"] == WARNING or f["code"] == "connection_cadence"
               for f in found)
    assert "warning" in summarise(found)
    assert "reset_session" in next(
        f for f in found if f["code"] == "meter_never_moved")["detail"]


def test_a_healthy_running_load_is_reported_healthy():
    # The failure mode that would make this useless: crying wolf during a
    # perfectly ordinary wash. The meter has moved, the estimate exists, and
    # the machine agrees it is running.
    session = _incident(
        energy_start=11.6, last_eta_ts=STARTED + 3600,
        detector={"phase": "active", "last_energy": 12.4,
                  "last_rise_ts": STARTED + 1800, "idle_energy": 11.6},
        flap_times=[],
    )
    found = check(
        session, STARTED + 3600,
        watched={"running": "on", "machine_state": "run", "job_state": "wash"},
    )
    assert _codes(found) == []
    assert summarise(found) == "healthy"


def test_a_wedge_is_called_a_problem_because_nothing_will_clear_it():
    # The two halves cannot disagree during a real load, so this is never
    # transient — and unlike the phantom, no safety net ends it.
    stale = _incident(detector={"phase": "idle", "last_energy": 11.6,
                                "last_rise_ts": None, "idle_energy": 11.6})
    found = check(stale, STARTED + 600, watched={})
    assert "wedged_stage_without_detector" in _codes(found)
    assert found[0]["severity"] == PROBLEM
    assert "reset_session" in found[0]["detail"]
    # ...and the mirror image, which blocks the *next* load rather than this one.
    other = _incident(stage="idle",
                      detector={"phase": "active", "last_energy": 11.6,
                                "last_rise_ts": STARTED, "idle_energy": 11.6})
    assert "wedged_detector_without_stage" in _codes(check(other, STARTED, watched={}))


def test_a_tracked_session_with_no_anchor_has_no_way_out():
    # The drying-resurrect signature: both time nets read session_started_ts,
    # so without it neither can ever fire.
    found = check(_incident(session_started_ts=None), STARTED + 60, watched={})
    assert "tracked_without_anchor" in _codes(found)
    assert any(f["severity"] == PROBLEM for f in found)


def test_the_meter_check_waits_long_enough_not_to_libel_a_slow_reporter():
    # This washer's meter is documented as lagging 15-45 minutes, so a check
    # that fired at 20 would accuse every real load.
    early = check(_incident(), STARTED + 30 * 60, watched={})
    assert "meter_never_moved" not in _codes(early)
    late = check(_incident(), STARTED + 80 * 60, watched={})
    assert "meter_never_moved" in _codes(late)
    # A meter that HAS moved is never accused, however long the load runs.
    moved = _incident(detector={"phase": "active", "last_energy": 13.0,
                                "last_rise_ts": STARTED + 60, "idle_energy": 11.6})
    assert "meter_never_moved" not in _codes(check(moved, STARTED + 600 * 60, watched={}))


def test_a_regular_cadence_is_distinguished_from_a_flaky_link():
    # The difference decides whether the fix is the network or the integration
    # doing the polling, and nothing else in the system reports it.
    count, median, regular = flap_cadence(FLAPS)
    assert count == 19
    assert 3080 <= median <= 3095
    assert regular is True
    # Jittered drops of a similar average are NOT called regular.
    import itertools
    jittered = list(itertools.accumulate([0, 400, 3000, 900, 5200, 1500, 4000]))
    assert flap_cadence(jittered)[2] is False
    assert flap_cadence([])[0] == 0 and flap_cadence([1.0])[2] is False


def test_a_claim_with_no_id_cannot_be_pinged():
    found = check(_incident(claimed_by="Alex", claimed_by_id=None), STARTED, watched={})
    assert "claim_without_id" in _codes(found)
    ok = _incident(claimed_by="Alex", claimed_by_id=42)
    assert "claim_without_id" not in _codes(check(ok, STARTED, watched={}))


def test_the_machine_contradiction_needs_the_machine_to_actually_say_so():
    # An unconfigured or missing entity must not be read as "idle" — it cannot
    # contradict anything, and treating silence as evidence would fire this on
    # every install that leaves machine_state unset.
    quiet = check(_incident(), STARTED + 60,
                  watched={"running": None, "machine_state": None})
    assert "machine_says_idle" not in _codes(quiet)
    # A pause is not a stop.
    paused = check(_incident(), STARTED + 60,
                   watched={"running": "off", "machine_state": "pause"})
    assert "machine_says_idle" not in _codes(paused)


def test_nothing_here_raises_on_junk():
    # It is run precisely when something is already wrong, so it must never be
    # the thing that fails.
    for bad in (None, {}, {"stage": None}, {"detector": "nonsense"},
                {"flap_times": "no"}, {"session_started_ts": "soon"},
                {"queue": "some"}, {"detector": {"phase": None}}):
        assert isinstance(check(bad, STARTED, watched=None), list)
    assert isinstance(check(_incident(), None, watched={}), list)
    assert isinstance(check(_incident(), "junk", watched="junk"), list)
    assert summarise(None) == "healthy" and summarise("x") == "healthy"


def test_the_impossible_pair_is_reported_as_proof_of_a_race():
    # No single-threaded path can produce "owned AND up for grabs", so seeing
    # it is evidence rather than a symptom: a tap landed inside a completion
    # that was holding the session lock across Discord round trips.
    racy = _incident(claimed_by="Alex", claimed_by_id=42, waiting=True)
    found = check(racy, STARTED, watched={})
    assert "claimed_and_waiting" in _codes(found)
    assert any(f["severity"] == PROBLEM for f in found)
    # Each half alone is perfectly ordinary and must not fire it.
    assert "claimed_and_waiting" not in _codes(
        check(_incident(claimed_by="Alex", claimed_by_id=42, waiting=False), STARTED, watched={})
    )
    assert "claimed_and_waiting" not in _codes(
        check(_incident(claimed_by="Unclaimed", claimed_by_id=None, waiting=True), STARTED, watched={})
    )


def test_an_outage_is_an_outage_not_a_phantom():
    # The worst false positive the first release had: a real load whose cloud
    # drops a few minutes in freezes the meter at its start value, and at 75
    # minutes the check called it "almost certainly a load that never existed"
    # and advised reset_session — killing a legitimate wash mid-outage. The
    # offline fact was sitting unread in the same dict the whole time.
    away = _incident(offline_since=STARTED + 180)
    found = check(
        away, STARTED + 7200,
        watched={"running": "unavailable", "machine_state": "unavailable"},
    )
    codes = _codes(found)
    assert "meter_never_moved" not in codes
    assert "no_completion_estimate" not in codes
    assert "started_on_a_reconnect" not in codes
    assert "washer_offline" in codes          # said out loud, not just skipped
    # Back online, the same state is accused again.
    assert "meter_never_moved" in _codes(check(_incident(), STARTED + 7200, watched={}))


def test_reconnect_proximity_corroborates_and_never_accuses_alone():
    # Drops arrive every ~51 min around the clock, so bare proximity would
    # flag ~8% of perfectly healthy loads. It may only ever second the silent
    # meter's accusation, so early in a load — meter legitimately unmoved
    # under its 15-45 min lag, but under the 75-min bar — it stays quiet.
    early = check(_incident(), STARTED + 40 * 60, watched={})
    assert "started_on_a_reconnect" not in _codes(early)
    late = check(_incident(), STARTED + 80 * 60, watched={})
    assert "started_on_a_reconnect" in _codes(late)
    # A moved meter clears both accusations at once.
    moved = _incident(detector={"phase": "active", "last_energy": 12.9,
                                "last_rise_ts": STARTED + 900, "idle_energy": 11.6})
    assert "started_on_a_reconnect" not in _codes(check(moved, STARTED + 80 * 60, watched={}))


def test_a_self_clean_is_never_accused_of_missing_an_estimate():
    # Drum cleans never publish a completion estimate, so its absence carries
    # no information. (The check's other suppressions still apply to it.)
    sc = _incident(stage="self_clean")
    assert "no_completion_estimate" not in _codes(check(sc, STARTED + 3600, watched={}))


def test_junk_numerics_are_refused_rather_than_carried():
    # A literal NaN cannot round-trip HA's store, but the STRING "nan" can —
    # json and float() both accept it — and one such value in flap_times
    # walked to int(median // 60) and raised, in the module whose contract is
    # that it never raises. Infinity is the same trap for the meter check.
    poisoned = _incident(flap_times=["nan", 1.0, 2.0])
    assert isinstance(check(poisoned, STARTED, watched={}), list)  # not raises
    rich = _incident(energy_start=float("inf"))
    assert "meter_never_moved" not in _codes(check(rich, STARTED + 7200, watched={}))
    count, median, regular = flap_cadence(["nan", "inf", 1.0, 2.0])
    assert count == 2 and regular is False


def test_regularity_is_a_fraction_not_a_unanimity_vote():
    # One drop the recorder missed merges two 3087s gaps into ~6174s. Under
    # the all-gaps rule that single outlier flipped the incident's own
    # metronome to "ordinary unreliable link" — pointing the owner at the
    # wifi while the evidence said timer.
    missing_one = FLAPS[:7] + FLAPS[8:]
    count, median, regular = flap_cadence(missing_one)
    assert regular is True, (count, median)
    # ...while two drops — one gap, conforming to itself — is not a cadence.
    assert flap_cadence(FLAPS[:2])[2] is False
    assert flap_cadence([0.0, 500.0, 1040.0])[2] is False  # nor two gaps


def test_the_multi_entry_summary_counts_entries_not_findings():
    # The first inline version rendered max-problems-within-one-entry as the
    # number of entries WITH problems — "2 entries, 4 with problems" from a
    # two-entry install. Units matter in the one line everybody reads first.
    two = [
        {"findings": [{"severity": "problem"}, {"severity": "problem"},
                      {"severity": "problem"}]},
        {"findings": [{"severity": "warning"}]},
    ]
    assert _d.summarise_entries(two) == "2 entries, 1 with problems"
    assert _d.summarise_entries([]) == "0 entries, 0 with problems"
    assert _d.summarise_entries("junk") == "0 entries, 0 with problems"


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
