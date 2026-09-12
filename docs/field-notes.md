# Field notes — what the real machine actually does

A permanent home for **observed behaviour of the live washer** and the **false
leads it generates**. Read this before "fixing" anything that looks broken from
a sensor reading alone.

The rule that earned this file: more than once a plausible-looking defect turned
out to be either designed behaviour that the house relies on, or a sensor
lying. Every entry below is backed by a dated observation, not by reasoning
about the code.

---

## 1. The machine

Samsung washer via SmartThings.

### 1.1 `running` and `machine_state` get stuck asserted after a cycle

Diagnostics run **2026-09-03 ~21:40 UTC**. The tracked load had completed at
**19:29 UTC** (bot stage → `Done — claimed`, claimant `JD8218`).

| entity | value | last changed |
|---|---|---|
| `binary_sensor.washer_running` | `on` | **7 h** |
| `sensor.…_machine_state` | `run` | **4 h** |
| `sensor.…_completion_time` | `2026-09-03T21:26:55Z` | 1 h *(already past)* |
| `sensor.…_job_state` | `none` | 1 h |
| `sensor.washer_energy` | `13.9` | 1 h |
| `sensor.washer_water_consumption` | `614.8` | 2 h |

Both `running` and `machine_state` stayed asserted for **hours** after the drum
stopped.

> **Consequence:** any exit path whose condition is "those two go quiet" has no
> exit on this machine. See §3.

### 1.2 The cloud runs ~70 minutes behind reality

At ~15:40 local SmartThings pushed three updates in one batch — `job_state →
none`, `energy 13.8 → 13.9`, and a fresh `completion_time` — for a cycle that
had ended at 14:29 local.

`job_state` moved into `none` from a **real phase**, not from `unavailable` (no
flap was recorded for it), so the cloud was still holding a live wash phase 70
minutes after the drum stopped. The README's "15–45 minute lag" is optimistic.

### 1.3 Energy resolution vs. load size

A full load on this machine is **0.7 kWh** (`energy_start: 13.1` →
`idle_energy: 13.8`). The meter reports at **0.1 kWh** resolution.

`energy_load_jump` (`DEFAULT_ENERGY_LOAD_JUMP`) is **0.3** — i.e. the
offline/batch start route needs a single sample carrying 43% of an entire load.
Worth knowing before assuming that route is available as a fallback.

### 1.4 Water is the honest sensor

`sensor.washer_water_consumption` is cumulative and only moves when the drum
actually fills. On 2026-09-03 it was **the only sensor that correctly reported
"nothing is running"**, while `running`, `machine_state` and `completion_time`
all claimed otherwise.

Use it as the tiebreaker when the machine's own account of itself is
self-contradictory. Note it is currently read **only** for the energy/water
summary (`_water_start`) and takes no part in detection.

> **Honest value, untrustworthy clock.** Water says *whether* a load ran. It
> does not say *when*, and the difference is the whole reason it is still not
> wired into detection. Water arrives through the same SmartThings cloud as
> everything else, and §1.2 has that cloud pushing `job_state`, `energy` and
> `completion_time` in a **single batch 70 minutes after** the cycle they
> describe. There is no reason water is exempt. A batched water delta landing
> after a load ended is indistinguishable, at the moment it lands, from a drum
> filling right now — so "water moved, therefore something is running" is the
> *same* inference that the replayed-phase phantom is built on, with a
> different sensor in it.
>
> This has now been proposed twice (2026-09-04, 2026-09-12) and backed out
> twice. It is not a bad idea; it is an unmeasured one. The measurement that
> would settle it is in §3, and the "The meters, close up" card on the
> dashboard exists to capture it.

### 1.5 Connection drops ran on a 51-minute timer, then stopped

17 drops spaced **3087 s ± 0.6 s**, from 2026-09-02 20:00 UTC to 2026-09-03
13:52 UTC, then nothing for eight hours.

Spacing that regular is a **timer** — a token refresh or poll cycle in whichever
integration supplies these sensors — not a flaky network. It is chased there,
not in this integration. Recorded here because it is the engine behind every
reconnect-shaped fault.

---

## 2. False leads — do **not** "fix" these

### 2.1 The self-clean card has no claim button and no ping

**Designed.** README, *Self-clean cycles*. A drum self-clean runs with
`job_state` stuck at `none` while `machine_state` is `run`;
`_looks_like_selfclean()` uses exactly that to label the cycle.

**This is in active use in the house.** A proposal to "fix the self-clean
mislabel" was raised on 2026-09-03 and withdrawn — it would have removed a
working feature.

### 2.2 `stage: done_waiting` persisting for hours

There is no `done_waiting → idle` transition except `reset_session` or a new
load superseding it. Designed: the card stays up until something replaces it.

It blocks nothing — `_async_start_session` refuses only `washing`/`drying`, and
`_on_detector_started` returns early only on `washing`/`drying`/`self_clean`.

### 2.3 `session_started_ts: null` while `stage` is `done_waiting`

Normal. `_async_handle_finished` clears it on completion, and `diagnose.check`
only flags a missing anchor for a *tracked* stage.

### 2.4 `machine_state: run` or a future `completion_time` as proof a load is running

Neither is a start signal, deliberately:

* `machine_state` is only ever a **veto** (`machine_idle`, blocking a reconnect's
  meter catch-up from minting a phantom) plus the self-clean label.
* the ETA only gates **completion**, never a start.

Both were asserting `run` during the phantom-load incident while nothing was
washing. Treating either as evidence of a live load is how phantoms get minted.

---

## 3. Confirmed real: a self-clean overruns by ~1 hour

**Reported 2026-09-03:** a self-clean ran, and the bot's card "kept going for
like an hour after and finally stopped."

Both fast exits depend on the two sensors §1.1 proves get stuck:

| path | trigger | file |
|---|---|---|
| running sensor | `running → off` | `coordinator.py` `_on_running` |
| machine state | `machine_state → stop` | `coordinator.py` `_on_machine_state` |

…and `_async_selfclean_end_confirm` then requires **both** to agree:
`not running_on and self._machine_state() != MACHINE_RUN`.

With `running` stuck `on` and `machine_state` stuck `run`, neither trigger fires,
and the confirm would refuse even if one did. The only remaining exit is the
detector's flat-energy backstop — `idle_timeout` = `energy_idle` =
**60 minutes** (`DEFAULT_ENERGY_IDLE`), evaluated on the 5-minute health tick.

**60 minutes is exactly the reported overrun.** The exit logic is not buggy: it
is the backstop doing its job because every faster path was disabled by stuck
sensors.

### Why the obvious fixes are not obviously right

* **Shorten `energy_idle`** — it is the same backstop that protects a real load
  from a false completion when the meter is merely lagging (§1.2 shows the lag
  can exceed an hour). Shortening it trades a stuck self-clean card for early
  "done" messages on real loads.
* **Drop the `and` in the confirm** — accepting one sensor instead of both makes
  the confirm fire on a flap, which is what the `and` was added to stop.
* **Use water (§1.4) as a settlement signal** — plausible and currently unused
  by detection, but it needs its own observation of what water does during and
  after a self-clean before anything is built on it.

Nothing here is safe to change without first watching a self-clean end with
water, energy and both stuck sensors recorded side by side.

---

## 4. Confirmed real: a dead meter read as a finished load

**Observed 2026-09-04.** The bot announced a load done while the washer was
still drying. Diagnostics and the entity timeline at the moment it was wrong:

| fact | value |
|---|---|
| `stage` | `done_waiting`, claimed | 
| `watched.job_state` | **`drying`** (changed 2 h ago) |
| `sensor.washer_energy` | `13.9`, **last changed 6 h ago** |
| `detector` | `last_energy 13.9`, `idle_energy 13.9`, `last_rise_ts null` |
| `energy_start` | `13.9` |
| `sensor.washer_water_consumption` | 614.8 → **730.2 L** (+115 L) |

Water proves a real load ran (§1.4 again). The energy meter had not moved since
*before* that load started.

Every other completion route is excluded by the same snapshot: `job_state` never
reached `finish`; `machine_state` never left `run`, and a stop would have been
worded "stopped early"; `offline_since` and `last_eta_ts` are both null, so the
offline path could not fire; and the ETA (`2026-09-03T21:26:55Z`) predates the
session, so the staleness guard in `_eta_status` correctly rejected it.

That leaves the flat-energy backstop — and the stale-ETA guard working is
precisely *why* it fired. With the ETA rejected, the 60-minute flat-meter net
was the only rule left.

**The defect.** `detect.observe` guarded the backstop with `energy is not None`
under a comment reading "the meter IS reporting". Those are not the same claim:
`not None` says only that the entity *has* a value. Worse, `last_rise_ts` is
seeded when the load starts rather than on an observed rise, so for a meter that
never reports, "flat for `idle_timeout`" becomes true on a schedule regardless of
what the drum is doing.

**The fix (v0.31.0).** A new `meter_reporting` argument, computed by the
coordinator as "the energy entity's `last_changed` is at or after
`_session_started_ts`" — has this meter produced a reading *for this load*.
False vetoes the backstop. `last_changed` and not `last_updated` deliberately:
the question is whether the reading moved, and a republish carrying the same
number is not a meter doing anything.

**Why not just change `energy_idle`.** It cannot fix both directions. §3 has the
same 60-minute timer running too *slow* to end a self-clean; this has it running
too *fast* against a stalled meter. One number cannot be both, which is why the
veto is a separate condition rather than a tuning change.

**What is still not fixed.** With the meter dead, the ETA stale and `job_state`
never reaching `finish`, the only remaining net is the 12-hour cap. A load in
that state now stays open far too long instead of closing far too early. That is
the better failure of the two — a stale card beats a false ping — but it is not
a good one, and it is the same gap §3 describes from the other side.

---

## 5. Confirmed real: the health check could not see the actual complaint

**Observed 2026-09-12.** The bot sat at `Done — claimed` while the washer ran.
The dashboard's Health card said **"✅ healthy — Nothing to report"** at the same
moment, and that is the part worth recording: every check in `diagnose.py` asked
whether a *tracked* load was real, and not one asked the opposite question. The
one failure the household actually feels — the bot missing a load — was the one
thing the diagnostic was structurally blind to.

The state at the time:

| fact | value |
|---|---|
| bot `stage` | `Done — claimed` (completed 16:38, claimant Ginko) |
| handoff backstop | fired 17:03, nudged Yuki (25 min, as configured) |
| `sensor.…_job_state` | **`Wash`** |
| `sensor.…_machine_state` | `Running` |
| `binary_sensor.washer_running` | `On` |
| `sensor.…_completion_time` | "in 3 hours" |
| `sensor.washer_energy` | `22.10 kWh` |
| `sensor.washer_water_consumption` | `1011.2 L` |
| `sensor.…_power` | `0.00 W` |
| `sensor.…_energy_meter`, `…_power_meter` | **`Unavailable`** |
| `sensor.laundry_health` | `ok` |

**Two readings, and the snapshot does not separate them.** Either the 16:38
completion was false and the same load was still running, or it was correct and
a *new* load started afterwards (Yuki was nudged at 17:03 that the machine was
free) which the bot never picked up. A three-hour estimate and a `Wash` phase
both fit a freshly started cycle better than the tail of an old one, but that is
inference, not evidence, and no stored fact settles it.

What *is* settled: `job_state` read `Wash`, and §1.1 establishes that this washer
freezes on the **mid/late** phase it ended on. An early phase is not a shape it
gets stuck in. So whichever reading is right, the bot should have been tracking
something and was not.

**The fix (v0.33.0).** Two things, and deliberately neither of them a change to
detection:

* `diagnose.check` gained the mirror of `machine_says_idle`. Not tracking +
  `job_state` in an **early** phase is now a finding — `untracked_load_running`
  (PROBLEM) when the meter has also moved since the last completion, i.e. a real
  wash is confirmed; `early_phase_while_idle` (WARNING) when it has not, which is
  the genuinely ambiguous case: a fresh load inside the meter's 15–45 minute lag,
  or a reconnect replaying a stale phase. Gated on the early phase and not on
  `running`/`machine_state`, which §1.1 disqualifies — either of those alone
  would fire daily.
* A `track_load` action and a **Track the load running now** button. The mirror
  of `reset_session`: that one retracts a load the bot invented, this one picks
  up a load the bot missed. It reads no sensor. A person standing in front of a
  running washer is better evidence than anything this machine publishes about
  itself, and every automatic route has to weigh sensors that freeze, lag an
  hour, or get replayed wholesale. Tracked as a catch-up, so the card reads "in
  progress" and claims no usage baseline it cannot know.

**Note what was *not* done.** The obvious fix — re-arm the completion timer
whenever water rises, so a dead energy meter can no longer time a load out — was
written and reverted. See the box in §1.4.

**What is still not fixed.** Detection still cannot start a load whose energy
meter is stuck, and the ambiguity above still needs a human to resolve. The
button is an escape hatch, not a repair: it converts a silent failure into a
visible one with a way out, which is worth shipping on its own, but the next
real fix is still waiting on the §3 measurement.
