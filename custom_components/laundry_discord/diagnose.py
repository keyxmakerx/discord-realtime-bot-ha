"""Pure health checks for the ``diagnostics`` action.

No Home Assistant / discord imports, so every check is unit-testable against
handmade state. The caller gathers the facts and the clock; this module only
judges them.

Severity is about action, not alarm: ``problem`` won't fix itself, ``warning``
will (a safety net clears it, eventually), ``note`` is context, not a fault.
"""

from __future__ import annotations

import math

PROBLEM = "problem"
WARNING = "warning"
NOTE = "note"

# Stages the integration considers the machine busy.
TRACKED_STAGES = ("washing", "drying", "self_clean")

# Past this with no movement, it's not a load. Comfortably past the washer's
# documented 15-45 min meter lag, so a slow reporter isn't accused.
METER_SILENT_MINUTES = 75

# Past this with no ETA, one isn't coming — the washer publishes its
# estimate early in a real cycle.
NO_ETA_MINUTES = 45

# A phantom session starts `confirm_delay` after a reconnect, so landing
# within this of a recorded drop is the signature, not coincidence.
# Generous against the default 30s debounce.
FLAP_PROXIMITY_SECONDS = 120

# Phases a fresh cycle begins at. This washer freezes on whatever phase it
# ended on, so a stale late phase while idle means nothing — only an early
# phase means somebody actually started a wash.
EARLY_PHASES = ("weight_sensing", "wash")


def _num(value):
    """A finite float, or None.

    Rejects NaN-as-string and infinity too, not just unparseable values:
    this module must never raise, since it runs precisely when something's
    already wrong.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _minutes_since(ts, now):
    """Whole minutes between two timestamps, or None if either is unreadable."""
    a, b = _num(ts), _num(now)
    if a is None or b is None:
        return None
    return (b - a) / 60.0


def _finding(severity, code, headline, detail, evidence=None):
    return {
        "severity": severity,
        "code": code,
        "headline": headline,
        "detail": detail,
        "evidence": evidence or {},
    }


def flap_cadence(flap_times):
    """`(count, median_gap_seconds, regular)` for the recorded drops.

    `regular` distinguishes a flaky network (random intervals) from a timer
    — a token refresh or polling cycle — repeating at the same interval.
    Decides whether the fix is the wifi or the integration doing the polling.
    """
    stamps = sorted(t for t in (_num(x) for x in (flap_times or ())) if t is not None)
    if len(stamps) < 2:
        return (len(stamps), None, False)
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    # 80% of gaps within 10% of median, with >=3 gaps required: a missed
    # sample would otherwise merge two gaps into one that still "conforms",
    # and two gaps alone can look regular by coincidence.
    conforming = sum(1 for g in gaps if abs(g - median) <= median * 0.10)
    regular = (
        median > 0 and len(gaps) >= 3 and conforming >= math.ceil(len(gaps) * 0.8)
    )
    return (len(stamps), median, regular)


def check(session, now, *, watched=None, max_session_minutes=720):
    """Every verdict, worst first. `session` is the stored session dict.

    `watched` is the washer's own entity state, as `{"running",
    "machine_state", "energy", "job_state"}` (or None if unconfigured) — the
    one external source the checks can contradict the bot with.
    """
    data = session if isinstance(session, dict) else {}
    watched = watched if isinstance(watched, dict) else {}
    found = []

    stage = data.get("stage")
    tracked = stage in TRACKED_STAGES
    # Offline changes what a frozen meter means: no readings arrive, so it's
    # an outage, not a phantom load.
    offline_since = _num(data.get("offline_since"))
    offline = offline_since is not None
    detector = data.get("detector") if isinstance(data.get("detector"), dict) else {}
    started = _num(data.get("session_started_ts"))
    age = _minutes_since(started, now) if started is not None else None

    # --- the wedge: the two halves of the state machine disagree -------------
    # Neither half can end a load the other isn't in, so a mismatch isn't
    # transient — only reset_session gets out of it.
    phase = detector.get("phase")
    if tracked and phase == "idle":
        found.append(_finding(
            PROBLEM, "wedged_stage_without_detector",
            f"The bot says {stage} but its detector is idle.",
            "These cannot disagree during a real load. The session can no "
            "longer complete on its own and is swallowing every new load — no "
            "card, no claim button, no completion ping. Run "
            "laundry_discord.reset_session.",
            {"stage": stage, "detector_phase": phase},
        ))
    if not tracked and phase == "active":
        found.append(_finding(
            PROBLEM, "wedged_detector_without_stage",
            f"The detector is active but the bot says {stage}.",
            "The detector will refuse to start the next load, because it "
            "believes one is already running. Run "
            "laundry_discord.reset_session.",
            {"stage": stage, "detector_phase": phase},
        ))

    # --- a tracked session with no anchor: both safety nets are dead ---------
    if tracked and started is None:
        found.append(_finding(
            PROBLEM, "tracked_without_anchor",
            f"The bot says {stage} with no session start recorded.",
            "The 12-hour max-session net and the offline completion both read "
            "that timestamp, so with it missing neither can ever fire. Nothing "
            "will end this session. Run laundry_discord.reset_session.",
            {"stage": stage},
        ))

    # --- the phantom: a load during which the meter provably never moved -----
    energy_start = _num(data.get("energy_start"))
    last_energy = _num(detector.get("last_energy"))
    idle_energy = _num(detector.get("idle_energy"))
    meter_silent = (
        tracked
        and age is not None
        and age >= METER_SILENT_MINUTES
        and energy_start is not None
        and last_energy is not None
        and last_energy <= energy_start
    )
    if meter_silent and not offline:
        found.append(_finding(
            WARNING, "meter_never_moved",
            f"A {stage} load has run {int(age)} min and the meter has not moved.",
            "A real cycle consumes energy. This is almost certainly a load "
            "that never existed — most often minted by a cloud reconnect "
            "replaying a stale phase. It will close itself via the "
            "flat-energy backstop and announce a completion for a wash that "
            "never happened; reset_session closes it without the false "
            "announcement.",
            {
                "minutes": round(age, 1),
                "energy_start": energy_start,
                "energy_now": last_energy,
                "idle_energy": idle_energy,
            },
        ))

    # --- started right after a drop: the reconnect signature -----------------
    count, median, regular = flap_cadence(data.get("flap_times"))
    stamps = sorted(t for t in (_num(x) for x in (data.get("flap_times") or ())) if t is not None)
    # Corroboration only, gated on meter_silent: bare proximity alone would
    # flag many healthy loads, since this washer's cloud reconnects on a
    # regular cadence.
    if meter_silent and not offline and started is not None and stamps:
        gap = min(abs(started - t) for t in stamps)
        if gap <= FLAP_PROXIMITY_SECONDS:
            found.append(_finding(
                WARNING, "started_on_a_reconnect",
                f"This session began {int(gap)}s after a connection drop.",
                "That is the reconnect signature: the cloud comes back, "
                "republishes the phase it last saw, and the debounce settles "
                "on it. Combined with a meter that has not moved, treat the "
                "load as phantom.",
                {"seconds_after_drop": round(gap, 1)},
            ))

    # --- no estimate: the ETA gate can never fire ---------------------------
    # Excludes self-clean (never publishes an estimate) and offline (can't
    # publish anything; reported separately below).
    if (
        tracked
        and stage != "self_clean"
        and not offline
        and _num(data.get("last_eta_ts")) is None
        and (age or 0) >= NO_ETA_MINUTES
    ):
        found.append(_finding(
            WARNING, "no_completion_estimate",
            f"A {stage} load has run {int(age or 0)} min with no estimate.",
            "The washer publishes its own finish estimate early in a real "
            "cycle, and the main completion route needs it. Without one only "
            "the flat-energy backstop and the 12-hour net can end this.",
            {"minutes": round(age or 0, 1)},
        ))

    # --- the machine's own account contradicts the bot -----------------------
    running, machine = watched.get("running"), watched.get("machine_state")
    # WARNING not PROBLEM: every load ends through a brief window where the
    # machine already looks stopped while the bot's debounce is still
    # settling. One snapshot can't tell that apart from a real mismatch.
    if tracked and running in ("off", False) and machine not in ("run", "pause"):
        found.append(_finding(
            WARNING, "machine_says_idle",
            f"The bot says {stage}; the washer says it is not running.",
            "The machine's own sensors are the external truth here — but "
            "every load ends through a short window that looks exactly like "
            "this while the stop-debounce settles. Run diagnostics again in "
            "two or three minutes: if this is still here, the session is "
            "wrong rather than lagging.",
            {"stage": stage, "running": running, "machine_state": machine},
        ))

    # --- ...and the mirror: the washer runs and the bot hasn't noticed -------
    # The opposite of every check above: is an *untracked* load real. Gated
    # on an early phase, not `running`/`machine_state` — those stay asserted
    # for hours after the drum stops, so either alone would cry wolf daily.
    job = watched.get("job_state")
    if not tracked and job in EARLY_PHASES:
        # `idle_energy` is the meter reading when the detector went idle, so
        # this is the one question that tells a real wash from a stale phase
        # replay.
        meter_moved = (
            last_energy is not None
            and idle_energy is not None
            and last_energy > idle_energy
        )
        if meter_moved:
            found.append(_finding(
                PROBLEM, "untracked_load_running",
                f"The washer is at {job} and the bot is not tracking a load.",
                "The meter has also moved since the last load ended, so this "
                "is a real wash that the bot missed -- there is no card, no "
                "claim button, and nobody will be pinged when it finishes. "
                "Press 'Track the load running now' (or run "
                "laundry_discord.track_load) to pick it up mid-cycle.",
                {
                    "stage": stage,
                    "job_state": job,
                    "running": watched.get("running"),
                    "machine_state": watched.get("machine_state"),
                    "energy_now": last_energy,
                    "idle_energy": idle_energy,
                },
            ))
        else:
            found.append(_finding(
                WARNING, "early_phase_while_idle",
                f"The washer is at {job} and the bot is not tracking a load.",
                "The meter has not moved since the last load ended, which "
                "leaves two readings and they need a human to tell apart. "
                "Either a wash genuinely just started and this meter is "
                "inside its documented 15-45 minute lag -- in which case the "
                "bot will pick it up on its own -- or a cloud reconnect "
                "replayed a stale phase and there is nothing in the drum. "
                "Look at the machine. If it is running, press 'Track the "
                "load running now'.",
                {
                    "stage": stage,
                    "job_state": job,
                    "running": watched.get("running"),
                    "machine_state": watched.get("machine_state"),
                    "energy_now": last_energy,
                    "idle_energy": idle_energy,
                },
            ))

    # --- the outage itself, since it suppressed the checks above -------------
    if tracked and offline:
        minutes_off = _minutes_since(offline_since, now)
        found.append(_finding(
            NOTE, "washer_offline",
            "The washer has been unreachable"
            + (f" for {int(minutes_off)} min" if minutes_off is not None else "")
            + ".",
            "While it is offline no meter readings or estimates arrive, so "
            "the phantom-load checks are suspended — a frozen meter during an "
            "outage is the outage, not a fake load. If this persists, the "
            "offline completion ends the session after the last known "
            "estimate passes.",
            {"offline_minutes": None if minutes_off is None else round(minutes_off, 1)},
        ))

    # --- overdue against the absolute net ------------------------------------
    if tracked and age is not None and age >= max_session_minutes:
        found.append(_finding(
            PROBLEM, "past_max_session",
            f"This session is {int(age)} min old, past the {max_session_minutes} min cap.",
            "The safety net should already have closed it. That it has not "
            "means the periodic tick is not running or not reaching the check.",
            {"minutes": round(age, 1)},
        ))

    # --- internally inconsistent claim state ---------------------------------
    claimed_by, claimed_id = data.get("claimed_by"), data.get("claimed_by_id")
    if claimed_by not in (None, "", "Unclaimed") and claimed_id is None:
        found.append(_finding(
            WARNING, "claim_without_id",
            f"Claimed by {claimed_by}, but no user id is stored.",
            "The completion ping needs the id, so this load would be "
            "announced without reaching its claimant. It also makes the "
            "dashboard and the card disagree, since they test this "
            "differently.",
            {"claimed_by": claimed_by},
        ))

    # --- the impossible pair -------------------------------------------------
    # No single-threaded path produces both an owner and "up for grabs" at
    # once. Seeing it means a button tap landed inside a completion, which
    # holds the session lock across Discord round trips while button
    # handlers take none.
    if (
        claimed_by not in (None, "", "Unclaimed")
        and claimed_id is not None
        and data.get("waiting") is True
    ):
        found.append(_finding(
            PROBLEM, "claimed_and_waiting",
            f"{claimed_by} owns this load and it is also up for grabs.",
            "These two cannot both be true. A tap landed during a completion "
            "and the two disagreed about who owns the load, so it was "
            "probably announced as free and handed to the queue as well. "
            "Expect somebody to have been told the washer is theirs when it "
            "is not.",
            {"claimed_by": claimed_by, "waiting": True},
        ))

    # --- a queue with nothing to wait for ------------------------------------
    queue = data.get("queue")
    if isinstance(queue, list) and queue and stage == "idle":
        found.append(_finding(
            NOTE, "queue_while_idle",
            f"{len(queue)} person(s) waiting with no load running.",
            "Ordinary right after a handoff, and it ages out on its own. Only "
            "worth acting on if it persists across several loads.",
            {"queue_count": len(queue)},
        ))

    # --- the connection itself ----------------------------------------------
    if count >= 2 and median:
        found.append(_finding(
            NOTE if not regular else WARNING,
            "connection_cadence",
            f"{count} connection drops recorded, about every {int(median // 60)} min.",
            (
                "The spacing is near-identical every time, which is not what a "
                "flaky network looks like — that pattern points at a timer: a "
                "token refresh or a polling cycle in whichever integration "
                "supplies these sensors. Worth chasing separately, because it "
                "is what makes every reconnect-related fault recur."
                if regular else
                "Irregular spacing, which is consistent with an ordinary "
                "unreliable link rather than something on a timer."
            ),
            {"drops": count, "median_seconds": round(median, 1), "regular": regular},
        ))

    order = {PROBLEM: 0, WARNING: 1, NOTE: 2}
    found.sort(key=lambda f: order.get(f["severity"], 9))
    return found


def summarise_entries(entries):
    """One-line header summarizing multiple entries."""
    rows = entries if isinstance(entries, list) else []
    troubled = sum(
        1 for e in rows
        if isinstance(e, dict)
        and any(
            isinstance(f, dict) and f.get("severity") == PROBLEM
            for f in (e.get("findings") or [])
        )
    )
    return f"{len(rows)} entries, {troubled} with problems"


def worst_severity(findings):
    """The most serious severity present, or "ok" if none.

    Short and low-cardinality on purpose: this becomes an entity's state,
    and the recorder writes a new row on every change. The full text
    belongs in an attribute instead.
    """
    rows = findings if isinstance(findings, list) else []
    for severity in (PROBLEM, WARNING, NOTE):
        if any(f.get("severity") == severity for f in rows):
            return severity
    return "ok"


def summarise(findings):
    """One line for the log and the response header."""
    rows = findings if isinstance(findings, list) else []
    problems = sum(1 for f in rows if f.get("severity") == PROBLEM)
    warnings = sum(1 for f in rows if f.get("severity") == WARNING)
    if problems:
        return f"{problems} problem(s) and {warnings} warning(s) — action needed"
    if warnings:
        return f"{warnings} warning(s) — nothing is stuck, but something is off"
    return "healthy"
