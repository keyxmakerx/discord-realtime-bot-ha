# Field notes — what the real washer does

Observed behaviour of the live machine (a Samsung washer/dryer on SmartThings)
and the false leads it produces. Read this before "fixing" anything from a
sensor reading alone: plausible fixes have been aimed at the wrong thing more
than once. Open problems are tracked in GitHub issues, linked below.

## 1. The machine

**`running` and `machine_state` stay asserted after a cycle.** On 2026-09-03 a
load completed at 19:29 UTC; two hours later `binary_sensor.washer_running` was
still `on` (unchanged for 7 h) and `machine_state` still `run` (4 h), while
`job_state` was `none` and the completion time had passed. Any exit path that
waits for these two to go quiet has no exit on this machine.

**The cloud runs about 70 minutes behind.** SmartThings pushed `job_state →
none`, an energy step and a fresh `completion_time` in one batch at ~15:40 local
for a cycle that ended at 14:29. `job_state` left a real phase, not
`unavailable`, so the cloud was still holding a live phase 70 minutes late.

**Energy resolution is coarse.** A full load is about 0.7 kWh and the meter
reports in 0.1 kWh steps, so the default offline-load threshold (0.3 kWh) needs
one sample carrying ~40% of a load.

**Water is the honest sensor, with an untrustworthy clock.** The cumulative
water meter only moves when the drum fills, and on 2026-09-03 it was the only
sensor correctly saying nothing was running. But it arrives through the same
batching cloud, so a late water delta looks like a drum filling now. It is shown
in diagnostics and the dashboard, not used by detection — see
[#40](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues/40).

**Connection drops ran on a timer.** 17 drops spaced 3087 s ± 0.6 s
(2026-09-02 20:00 to 09-03 13:52 UTC), then none for eight hours. Spacing that
regular is a token refresh or poll cycle in the washer's integration, not the
network. It is the engine behind every reconnect-shaped fault: on reconnect the
cloud replays the last phase it saw, which once minted a load that never ran.

## 2. False leads — designed behaviour

- **The self-clean card has no claim button and no ping.** A self-clean runs
  with `job_state` stuck at `none` while `machine_state` is `run`, and
  `_looks_like_selfclean()` labels it from exactly that. The house uses this.
- **`done_waiting` lasting for hours.** There is no `done_waiting → idle`
  transition except `reset_session` or the next load. It blocks nothing: new
  sessions are refused only while washing, drying or self-cleaning.
- **`session_started_ts: null` in `done_waiting`.** Normal: completion clears
  it, and diagnostics only flag a missing anchor for a tracked stage.
- **`machine_state: run` or a future `completion_time` as proof of a load.**
  Neither is a start signal. `machine_state` is only a veto (and the self-clean
  label); the ETA only gates completion. Both read `run`/future during the
  phantom-load incident while nothing was washing.

## 3. Incidents

**Self-clean card overruns by ~1 hour (2026-09-03).** Both fast exits wait on the
two stuck sensors from §1, so only the 60-minute flat-energy backstop ends it.
Open: [#37](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues/37).

**A dead meter read as a finished load (2026-09-04).** The bot announced "done"
mid-dry: the energy meter hadn't moved since before the load (water rose 115 L),
the stale ETA was correctly rejected, and the flat-meter backstop fired on a
schedule because its timer is seeded at session start. Fixed in v0.31.0: the
backstop needs the meter to have reported during the session
(`meter_reporting`). Remaining gap:
[#38](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues/38).

**Diagnostics were blind to a missed load (2026-09-12).** The bot sat at
"Done — claimed" while the washer ran a new wash, and the health check said all
was well because every check asked whether a *tracked* load was real. Fixed in
v0.33.0 with the `untracked_load_running` / `early_phase_while_idle` findings and
the `track_load` action. Detection still can't start a load with a stuck meter:
[#39](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues/39).
