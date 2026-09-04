"""Pure, dependency-free health checks for the ``diagnostics`` action.

Kept free of Home Assistant / discord imports so every verdict below is unit
testable against handmade state, the same discipline that made :mod:`detect`
and :mod:`nudge` reliable. The caller gathers the facts and owns the clock;
this module only judges them.

**Why this exists.** The integration logs everything at ``debug`` so a quiet
household produces no log traffic. The cost of that showed up the first time
something actually went wrong: the bot misbehaved six times in one morning and
left nothing at the default log level, and the only way to find out what had
happened was to read a storage file by hand and do arithmetic on epoch
timestamps. The facts were all there — they simply were not reachable from
inside Home Assistant.

So the same arithmetic lives here instead, and an action returns it. Every
check is written to answer one question a person actually asks about this bot:
*is it stuck, is it lying, and is it about to say something wrong?*

**Severity is about action, not alarm.** ``problem`` means something is wrong
now and will not fix itself. ``warning`` means something is wrong now and a
safety net will eventually clear it — worth knowing, because "eventually" is up
to twelve hours. ``note`` is context that is not a fault but changes how the
rest reads.
"""

from __future__ import annotations

import math

PROBLEM = "problem"
WARNING = "warning"
NOTE = "note"

# Stages in which the integration believes the machine is busy. A check that
# says "the bot thinks a load is running" means one of these.
TRACKED_STAGES = ("washing", "drying", "self_clean")

# A load whose meter has not moved at all by this point is not a load. Well
# past the 15-45 minute lag this washer's meter is documented to have, so a
# genuinely slow reporter is not accused.
METER_SILENT_MINUTES = 75

# Past this, a session with no completion estimate has almost certainly not got
# one coming: the washer publishes its estimate early in a real cycle.
NO_ETA_MINUTES = 45

# A phantom session is minted `confirm_delay` after a reconnect, so a start
# landing within this of a recorded drop is the signature rather than a
# coincidence. Generous against the default 30s debounce.
FLAP_PROXIMITY_SECONDS = 120


def _num(value):
    """A finite float, or None — every field here comes off disk or an entity.

    Finite, not merely parseable: HA's store loader refuses a bare NaN token,
    but the *string* "nan" sails through json and float() alike, and one such
    value in flap_times walked far enough to raise at int(median // 60) —
    in the module whose whole contract is that it never raises, because it
    runs precisely when something is already wrong. Infinity is refused for
    the same reason: energy_start=inf would fire the phantom check on
    arithmetic nobody performed.
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
    """``(count, median_gap_seconds, regular)`` for the recorded drops.

    ``regular`` is the interesting one. An unreliable network drops at random
    intervals; a token refresh or a polling cycle drops at *the same* interval
    every time, and this washer's did — nineteen drops spaced within a few
    seconds of 3087. Telling those apart matters because it decides whether the
    fix is "improve the wifi" or "look at the integration doing the polling",
    and nothing else in the system reports it.
    """
    stamps = sorted(t for t in (_num(x) for x in (flap_times or ())) if t is not None)
    if len(stamps) < 2:
        return (len(stamps), None, False)
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    # Regular = at least 80% of gaps within 10% of the median, and at least
    # three gaps to say so. The first version demanded ALL gaps conform, which
    # failed in both directions at once: one drop the recorder missed merged
    # two 3087s gaps into ~6174s and flipped the incident's own metronome to
    # "ordinary unreliable link" — active misdirection, pointing the owner at
    # the wifi when the evidence said timer — while any two drops produced a
    # single gap that conformed to itself, so a coincidence was declared a
    # cadence. A real timer survives a missed sample; one gap is not a rhythm.
    conforming = sum(1 for g in gaps if abs(g - median) <= median * 0.10)
    regular = (
        median > 0 and len(gaps) >= 3 and conforming >= math.ceil(len(gaps) * 0.8)
    )
    return (len(stamps), median, regular)


def check(session, now, *, watched=None, max_session_minutes=720):
    """Every verdict, worst first. ``session`` is the stored session dict.

    ``watched`` is what the washer's own entities currently say, as
    ``{"running": ..., "machine_state": ..., "energy": ..., "job_state": ...}``
    — strings straight from the state machine, or None when not configured. It
    is what lets the checks contradict the bot with the machine's own account
    of itself, which is the only external truth available.
    """
    data = session if isinstance(session, dict) else {}
    watched = watched if isinstance(watched, dict) else {}
    found = []

    stage = data.get("stage")
    tracked = stage in TRACKED_STAGES
    # The washer being offline changes what every meter-shaped fact means:
    # no readings arrive, so a frozen meter is an outage, not a phantom. The
    # first release never read this field — it sat unread in the very dict the
    # checks were handed — and the phantom check duly accused a real load whose
    # cloud dropped early, with advice to reset it. The opposite of help.
    offline_since = _num(data.get("offline_since"))
    offline = offline_since is not None
    detector = data.get("detector") if isinstance(data.get("detector"), dict) else {}
    started = _num(data.get("session_started_ts"))
    age = _minutes_since(started, now) if started is not None else None

    # --- the wedge: the two halves of the state machine disagree -------------
    # Neither half can end a load the other is not in: the detector only emits
    # a finish while ACTIVE, and the session only completes from a tracked
    # stage. So a mismatch is not a transient, it is a machine that has stopped
    # being able to move, and only reset_session gets out of it.
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
    # Only ever offered as corroboration on a load the meter already suspects.
    # On this washer the cloud drops every ~51 minutes around the clock, so
    # bare proximity would flag ~8% of perfectly healthy loads (240s window /
    # 3087s cadence) — one real wash in thirteen accused by coincidence. Tied
    # to the silent meter, the pair of findings reads as one diagnosis.
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
    # Not for a self-clean (drum cleans never publish an estimate, so its
    # absence says nothing) and not while offline (an unreachable washer
    # cannot publish anything, and the outage is already reported below).
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
    # WARNING rather than PROBLEM, deliberately: every load ends through a
    # short window where the machine already says stopped while the bot is
    # still settling its stop-debounce, and a duration-less PROBLEM would cry
    # "action needed" at the tail of every healthy cycle. The honest gate is
    # persistence, which one snapshot cannot measure — so the wording asks for
    # the one thing that does: a second run.
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
    # A load cannot both have an owner and be up for grabs. No single-threaded
    # path produces it, which is what makes it worth checking: seeing it is
    # proof that a button tap landed *inside* a completion, which holds the
    # session lock across Discord round trips while the button handlers take
    # no lock at all. The completion read "unclaimed" before that window
    # opened, announced the load as up for grabs and handed it to the queue —
    # for a load that had acquired an owner in the meantime.
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
    """The one-line header for a multi-entry response.

    A pure function because its first inline version shipped a units error:
    it computed the worst PROBLEM count *within* one entry and rendered it as
    the number of entries *with* problems — "2 entries, 4 with problems" from
    a two-entry install. Counting the right thing is one line; being testable
    is why it lives here.
    """
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
    """The most serious severity present, or ``"ok"`` when nothing was found.

    A short, low-cardinality value, because this is what an entity's *state*
    becomes: the recorder writes a row on every change, and a state that is a
    whole sentence changes whenever the wording does. The sentence lives in an
    attribute, where it belongs.
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
