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
