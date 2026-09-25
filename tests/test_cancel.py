"""Tests for the pure "stopped on the machine" rules.

Runnable with plain ``python3 tests/test_cancel.py``. ``cancel.py`` and
``const.py`` are loaded by file path so neither imports Home Assistant.
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _load(name: str, filename: str):
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "custom_components",
        "laundry_discord",
        filename,
    )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_const = _load("ld_const", "const.py")
# cancel.py does `import const` (no package to be relative to when loaded by
# path); register it under that bare name first.
sys.modules["const"] = _const
_cancel = _load("ld_cancel", "cancel.py")

VERDICT_IGNORE = _cancel.VERDICT_IGNORE
VERDICT_STOPPED = _cancel.VERDICT_STOPPED
VERDICT_FINISHED = _cancel.VERDICT_FINISHED
is_flap = _cancel.is_flap
machine_stop_signalled = _cancel.machine_stop_signalled
running_off_signalled = _cancel.running_off_signalled
stop_verdict = _cancel.stop_verdict
retracts_history = _cancel.retracts_history
HISTORY_RETRACT_MARGIN_S = _cancel.HISTORY_RETRACT_MARGIN_S

RUN = _const.MACHINE_RUN
PAUSE = _const.MACHINE_PAUSE
STOP = _const.MACHINE_STOP


def _verdict(**kwargs) -> str:
    """Defaults: a stopped washer mid-load with completion_time still ahead.
    Individual tests override one field at a time.
    """
    base = {
        "tracked": True,
        "machine_state": STOP,
        "running_on": False,
        "paused": False,
        "has_eta": True,
        "eta_passed": False,
        "job_finished": False,
    }
    base.update(kwargs)
    return stop_verdict(**base)


# --- flap immunity -----------------------------------------------------------


def test_a_value_arriving_from_unavailable_is_never_an_event() -> None:
    assert is_flap("unavailable") is True
    assert is_flap("unknown") is True
    assert is_flap(None) is True
    assert is_flap(RUN) is False
    assert is_flap("off") is False


def test_machine_stop_is_signalled_only_from_a_real_value() -> None:
    assert machine_stop_signalled(RUN, STOP) is True
    assert machine_stop_signalled(PAUSE, STOP) is True
    # A reconnect that lands on stop is the connection, not the machine.
    assert machine_stop_signalled("unavailable", STOP) is False
    assert machine_stop_signalled("unknown", STOP) is False
    assert machine_stop_signalled(None, STOP) is False


def test_pause_and_run_are_not_stop_signals() -> None:
    assert machine_stop_signalled(RUN, PAUSE) is False
    assert machine_stop_signalled(PAUSE, RUN) is False
    assert machine_stop_signalled(RUN, "unavailable") is False
    assert machine_stop_signalled(STOP, RUN) is False


def test_running_off_is_signalled_only_from_a_real_value() -> None:
    assert running_off_signalled("on", "off") is True
    assert running_off_signalled("unavailable", "off") is False
    assert running_off_signalled("unknown", "off") is False
    assert running_off_signalled(None, "off") is False
    assert running_off_signalled("off", "on") is False


def test_the_running_sensor_never_arms_a_stop_with_no_machine_state() -> None:
    """Without machine_state configured, nothing can tell a pause from a stop,
    so this signal must not arm on its own.
    """
    assert running_off_signalled("on", "off", machine_state_configured=False) is False
    assert running_off_signalled("on", "off", machine_state_configured=True) is True


# --- the verdict: when NOT to act -------------------------------------------


def test_no_tracked_load_means_nothing_to_end() -> None:
    assert _verdict(tracked=False) == VERDICT_IGNORE


def test_a_machine_that_came_back_to_run_is_ignored() -> None:
    assert _verdict(machine_state=RUN, running_on=True) == VERDICT_IGNORE
    assert _verdict(machine_state=RUN, running_on=False) == VERDICT_IGNORE


def test_a_paused_load_is_on_hold_not_over() -> None:
    assert _verdict(machine_state=PAUSE, running_on=False) == VERDICT_IGNORE
    # Pause with machine_state gone unreadable (e.g. "add a sock") must not end
    # the load either.
    assert _verdict(machine_state=None, running_on=False, paused=True) == VERDICT_IGNORE


def test_a_live_stop_outranks_a_stale_paused_flag() -> None:
    """`paused` is a stale cached flag; live `machine_state` must win, or
    run -> pause -> stop would never end the load.
    """
    assert _verdict(machine_state=STOP, running_on=False, paused=True) == (
        VERDICT_STOPPED
    )
    assert _verdict(machine_state=STOP, running_on=None, paused=True) == (
        VERDICT_STOPPED
    )
    assert _verdict(machine_state=STOP, paused=True, eta_passed=True) == (
        VERDICT_FINISHED
    )
    # ...but the guard still holds wherever nothing positively reads stopped.
    assert _verdict(machine_state=None, running_on=False, paused=True) == VERDICT_IGNORE
    assert (
        _verdict(machine_state="something_new", running_on=False, paused=True)
        == VERDICT_IGNORE
    )


def test_the_sensors_disagreeing_never_ends_a_load() -> None:
    assert _verdict(machine_state=STOP, running_on=True) == VERDICT_IGNORE


def test_an_offline_washer_is_not_a_stopped_washer() -> None:
    # An absence of "run" (e.g. a cloud drop) must never read as a stop.
    assert _verdict(machine_state=None, running_on=None) == VERDICT_IGNORE
    assert _verdict(machine_state=None, running_on=None, has_eta=False) == VERDICT_IGNORE
    assert (
        _verdict(machine_state=None, running_on=None, eta_passed=True)
        == VERDICT_IGNORE
    )


def test_an_unrecognised_machine_state_alone_is_not_a_stop() -> None:
    assert _verdict(machine_state="something_new", running_on=None) == VERDICT_IGNORE


# --- the verdict: stopped early ---------------------------------------------


def test_the_bug_stop_on_the_machine_with_the_eta_still_ahead() -> None:
    # All three routes to a "finished" verdict are shut here; it must say
    # stopped, not done.
    assert _verdict() == VERDICT_STOPPED


def test_the_running_sensor_alone_can_report_the_stop() -> None:
    # No machine_state entity configured: running going off is the same fact.
    assert _verdict(machine_state=None, running_on=False) == VERDICT_STOPPED


# --- the verdict: it simply finished ----------------------------------------


def test_a_stop_after_the_estimate_passed_is_a_normal_completion() -> None:
    assert _verdict(eta_passed=True) == VERDICT_FINISHED


def test_the_washer_saying_finish_outranks_a_future_estimate() -> None:
    assert _verdict(job_finished=True) == VERDICT_FINISHED


def test_with_no_estimate_a_stopped_machine_is_taken_as_finished() -> None:
    # No estimate means no evidence it was cut short, so it reads as finished,
    # not stopped.
    assert _verdict(has_eta=False) == VERDICT_FINISHED
    assert _verdict(has_eta=False, running_on=False, machine_state=None) == (
        VERDICT_FINISHED
    )


# --- retracting history is a higher bar than the wording ---------------------


def _retract(**kwargs) -> bool:
    base = {
        "verdict": VERDICT_STOPPED,
        "machine_state": STOP,
        "eta_remaining_s": HISTORY_RETRACT_MARGIN_S + 60,
    }
    base.update(kwargs)
    return retracts_history(**base)


def test_a_clear_cancel_still_un_logs_the_load() -> None:
    assert _retract() is True
    assert _retract(eta_remaining_s=3600) is True


def test_a_completion_that_merely_beat_its_estimate_keeps_its_history() -> None:
    """A real completion just under its estimate still reads as
    VERDICT_STOPPED; that must not be enough to delete history.
    """
    assert _retract(eta_remaining_s=60) is False
    assert _retract(eta_remaining_s=HISTORY_RETRACT_MARGIN_S) is False
    assert _retract(eta_remaining_s=0) is False
    assert _retract(eta_remaining_s=None) is False


def test_a_stop_inferred_without_a_live_reading_never_deletes_a_row() -> None:
    # Enough to hedge the card; not enough to destroy data.
    assert _retract(machine_state=None) is False
    assert _retract(machine_state="something_new") is False


def test_only_a_stopped_verdict_retracts_anything() -> None:
    assert _retract(verdict=VERDICT_FINISHED) is False
    assert _retract(verdict=VERDICT_IGNORE) is False


def test_every_verdict_is_one_of_the_three() -> None:
    seen = set()
    for machine_state in (RUN, PAUSE, STOP, None, "weird"):
        for running_on in (True, False, None):
            for paused in (True, False):
                for has_eta in (True, False):
                    for eta_passed in (True, False):
                        for job_finished in (True, False):
                            for tracked in (True, False):
                                seen.add(
                                    stop_verdict(
                                        tracked=tracked,
                                        machine_state=machine_state,
                                        running_on=running_on,
                                        paused=paused,
                                        has_eta=has_eta,
                                        eta_passed=eta_passed,
                                        job_finished=job_finished,
                                    )
                                )
    assert seen == {VERDICT_IGNORE, VERDICT_STOPPED, VERDICT_FINISHED}


def test_nothing_ends_a_load_while_the_machine_still_says_run() -> None:
    for running_on in (True, False, None):
        for paused in (True, False):
            for has_eta in (True, False):
                for eta_passed in (True, False):
                    assert (
                        stop_verdict(
                            tracked=True,
                            machine_state=RUN,
                            running_on=running_on,
                            paused=paused,
                            has_eta=has_eta,
                            eta_passed=eta_passed,
                            job_finished=False,
                        )
                        == VERDICT_IGNORE
                    )


def test_only_positive_evidence_ends_a_load() -> None:
    for has_eta in (True, False):
        for eta_passed in (True, False):
            verdict = stop_verdict(
                tracked=True,
                machine_state=None,
                running_on=None,
                paused=False,
                has_eta=has_eta,
                eta_passed=eta_passed,
                job_finished=True,
            )
            assert verdict == VERDICT_IGNORE


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
