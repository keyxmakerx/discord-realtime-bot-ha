# Laundry Discord Bot — Home Assistant custom integration

A self-hosted Discord bot that runs **inside** Home Assistant. It watches your
washer and posts **one** rich Discord message per load, with a **live-updating
ETA** and a **wash → dry progress bar** that edit the same message in place, plus
a **claim / unclaim button** you can tap from the moment the wash starts. The
only push notification is a single **@mention to whoever claimed the load, sent
when it's done** — everything else updates silently, and if nobody claimed it the
"done" message is posted with no ping at all.

- **Domain:** `laundry_discord`
- **Install:** via [HACS](https://hacs.xyz/) as a custom repository
- **Target:** Home Assistant OS / Core **2026.5+**, Python 3.13

## What it does

For a single load (a "session"):

1. **Start** — when the watched *running* sensor goes `off → on`, it posts a new
   Discord embed **with a Claim button**, so anyone can call dibs early. This is
   a normal, visible message but **never @mentions anyone**.
2. **ETA + progress** — every N seconds it **edits the same message** with the
   current estimated finish and a `🟩 Wash → 🟦 Rinse → ⬜ Spin → ⬜ Dry` stage
   bar (and "Claimed by …" once someone grabs it). Edits never push, so this is
   silent by design.
3. **Drying** — when the job-state sensor enters `drying`, it silently edits the
   embed to "drying starting — pull out anything you don't want dried."
4. **Finished** — when the job-state sensor reaches **`finish`** (the real
   completion), it edits the embed to "Laundry done — don't forget the lint
   tray." It does **not** wait for `none`, which on this washer can lag by hours
   while the drum sits idle after the cycle is actually done (`none` straight
   from a phase is kept as a backup). If someone **claimed** it, the bot sends
   the one **@mention** to that person ("your laundry's done"). If **nobody
   claimed** it, the done message is posted with **no ping**, and the Claim
   button stays.
5. **Claim / Unclaim** — tapping **Claim** records the claimant in HA and swaps in
   an **Unclaim** button so an accidental claim can be undone. The load stops
   being claimable only when the **next load starts**.

There is only ever **one active embed per load**. Duplicate "start" transitions
are ignored while a wash is already running.

> **Detection mixes signals by reliability.** `job_state` is the spine: it
> reliably reports the wash phases, the `drying` start, and `finish`. The
> **energy meter** is a *fallback* for loads that ran while the cloud was dark
> (`job_state` never reported a phase) — it's monotonic so it never lies that a
> cycle happened, but on some washers it freezes/resets/reads flat mid-load, so
> it is **never** allowed to time completion while `job_state` is live.
> - **Fast start (online):** a confirmed early phase (`weight_sensing`/`wash`)
>   starts the load immediately, before the meter (which lags 15–45 min) has even
>   moved. A change must persist `confirm_delay` (default **30s**) first, so a
>   transient flap can't start anything.
> - **Fast finish:** a confirmed `job_state = finish` completes immediately — on
>   this washer it only ever appears at the true end, never at the wash→dry
>   handoff, so it's safe.
> - **Completion (the normal path):** the washer publishes its **own estimated
>   finish** (`completion_time`), which lines up well with the real end. A load
>   completes once that estimate has **passed** *and* the energy meter has been
>   **flat for a short settle** (~20 min). This is robust to this washer's two
>   quirks: the energy meter can freeze/read flat for a whole load — but it can't
>   fire early because it must wait for the estimate to pass; and `job_state` can
>   freeze mid-cycle (e.g. stuck on `wash`) — but that can't block completion
>   because the estimate, not the phase, drives it. Only a *fresh* estimate
>   (published during the current cycle) is trusted, so a value frozen from the
>   previous load can't end the next one early. If `completion_time` is
>   unavailable (a genuinely offline load) it falls back to the flat-energy
>   `energy_idle` backstop. An absolute max-session cap is the final safety net.
> - **Mid-cycle catch-up:** if the bot joins a load already in progress, a real
>   phase + a meter that has moved since idle starts it (a phase *frozen* at the
>   last completion reading is ignored as a stale leftover).
>
> Because completion is timed off the reliable `drying`/`finish` transitions (not
> the flaky meter), the failure modes that plagued earlier versions are gone:
> back-to-back loads each get their own card, and a frozen or dead energy meter
> can no longer fire a false "done" mid-cycle. The `running`/`machine_state`
> sensors are used only for the **"⏸ Paused"** display and prompt self-clean end.
> A new cycle supersedes a finished-but-unclaimed message.

> **Offline loads:** if the washer's cloud is offline for a whole cycle,
> `job_state` never reports a phase — but the meter jumps in one batch when the
> cloud reconnects. A single energy jump of `energy_load_jump` kWh (default
> **0.3**) is treated as a load that ran while offline, and a catch-up message is
> posted. Slow standby/wrinkle-prevent creep (small per-sample steps) and meter
> resets (a decrease) never trip it. Telemetry for a fully offline load only
> reaches HA on reconnect, so that message is necessarily *after the fact*, and a
> load smaller than the threshold with no phases can't be caught — a cloud limit.

> **Wrinkle-prevent:** after a cycle the drum tumbles for hours, nudging the
> meter. Point the optional **wrinkle-prevent sensor** at the bot and those
> nudges won't keep a finished load "alive" or spawn a phantom one.

> **Self-clean cycles:** a drum self-clean runs *without* reporting any
> `job_state` phase (it stays `none` while `machine_state` is `run`). When the
> meter starts a cycle with no wash phase but the washer reports running, it's
> posted as a separate **"🧼 Self-clean running / finished"** message with the ETA
> and energy/water used — **no claim button and no ping**.

> **Mid-cycle startup:** if the washer is **already running** when the bot
> connects (e.g. you installed the integration, or restarted HA, during a load),
> it picks the load up right away and posts a "Laundry in progress" message,
> rather than waiting for the next cycle.

> **Why the ping is a separate little message:** editing an embed never makes a
> phone buzz (that's what keeps the ETA/progress updates silent). So to actually
> notify at completion, the bot posts one short "@you — laundry's done" line next
> to the main embed. That single line is the only push per load, and it's a
> *user* mention — which needs no special bot permission and never pings a whole
> role or @everyone.

### Entities it creates

| Entity | Meaning |
|--------|---------|
| `sensor.laundry_claimed_by` | Current claimant's display name, or `Unclaimed`. |
| `sensor.laundry_stage` | `Idle` / `Washing` / `Drying` / `Done — waiting` / `Done — claimed`. |
| `binary_sensor.laundry_waiting` | `on` when a finished load is unclaimed. |
| `sensor.laundry_connection_health` | Diagnostic: number of cloud-connection drops in the last 24h (with `last_drop` + `minutes_since_last_drop`). Great for a dashboard chip and for judging a wifi/AP change. |

## Why a custom integration (not an add-on)

- Installs through **HACS as a custom repository** (a simple git → HACS flow).
- Runs inside HA's process, so it has **direct access to HA state and the
  service registry** — the "who claimed it" write-back and the entity watching
  need zero glue (no MQTT, no REST).
- The Discord gateway connection is **outbound only**, so buttons and edits work
  with **no open ports** and nothing exposed through a reverse proxy.

**Tradeoffs (by design):**
- Updating the integration requires an **HA restart**.
- A bug in the bot runs in HA's event loop. The bot's tasks are written
  defensively (every Discord call is wrapped, the gateway task swallows errors)
  specifically so a bot failure logs rather than taking HA down.

## ⚠️ Retire the old laundry automations

This integration owns the **entire** laundry-notification lifecycle. If you
already have washer automations that post via `notify.hanotf` (start / drying /
finished), **disable or delete them** once this is live — the integration
watches the washer entities directly and would otherwise **double-post**.

## ⚠️ ETA accuracy caveat

The ETA shown in the embed is only as accurate as the **washer's own cloud
estimate**, which can drift. It is presented as approximate (`~3:40 PM, about
1h12m left`) and the embed footer says so. Don't treat it as exact.

---

## 1. Create the Discord bot (one-time)

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**.
2. **Bot** tab → add a bot → **Reset/Copy Token**. This is your `bot_token`.
   - No privileged intents are needed for buttons. Leave **Message Content** OFF.
3. **OAuth2 → URL Generator** → scope **`bot`**; bot permissions:
   **View Channels, Send Messages, Embed Links, Read Message History**. That's
   all — pinging the claimant is a *user* mention, which needs no special
   permission. Open the generated URL and invite the bot to your server.
4. Enable **Developer Mode** (User Settings → Advanced), then right-click your
   target channel → **Copy ID** → that's your `channel_id`.

## 2. Install via HACS

1. HACS → ⋮ → **Custom repositories** → paste this repo's URL, category
   **Integration** → **Add**.
2. Download **Laundry Discord Bot**, then **restart Home Assistant**.
3. Settings → Devices & Services → **Add Integration** → "Laundry Discord Bot"
   → complete the config flow.

## 3. Configuration

Collected in the UI config flow (options can be changed later without re-adding):

| Key | Default | Notes |
|-----|---------|-------|
| Bot token | — | Stored in the config entry; never logged. |
| Channel ID | — | Numeric Discord channel ID. Validated as digits. |
| Running sensor | `binary_sensor.washer_running` | Debounced on/off; drives **start**. |
| Job-state sensor | `sensor.washer_washer_job_state` | Drives drying/finished. |
| Completion-time sensor | `sensor.washer_washer_completion_time` | ISO timestamp for the ETA. |
| Machine-state sensor | `sensor.washer_washer_machine_state` | `run`/`pause`/`stop` — adds a **"⏸ Paused"** display and a `stop` veto to the start consensus. |
| Energy sensor *(optional)* | `sensor.washer_energy` | Shows **kWh used** on the done message. |
| Water-usage sensor *(optional)* | `sensor.washer_water_consumption` | Shows **water used** on the done message. |
| ETA interval | `90` s | How often to edit the ETA/progress (min 30). |
| Ping claimant on complete | `true` | @mention whoever claimed the load when it's done. Turn off for zero pings. |

> **Ping note:** the only push per load is a *user* mention of the claimant, sent
> as a small separate message when the load finishes. If nobody claimed it, no
> ping is sent. There are no role/@everyone pings, so the bot needs no special
> mention permission.

## 4. Test without doing laundry

1. Developer Tools → **Actions** → call `laundry_discord.test_post`.
   - Confirm the embed posts, **Claim** updates it to "Claimed by *you*" (and
     `sensor.laundry_claimed_by` updates in HA), and **Unclaim** reverts it.
2. To exercise the real path, Developer Tools → **States**: set
   `sensor.washer_washer_job_state` to `drying`, then to `none`, to trigger the
   drying and finished edits.
   - *(Manually set states are temporary and get overwritten by the real device
     on its next update.)*

## 5. Releasing for HACS

Tag a GitHub release whose tag matches the `version` in
`custom_components/laundry_discord/manifest.json` (e.g. `v0.1.0`). HACS enforces
this match.

## How it stays reliable

- **Flap immunity.** This washer's cloud connection drops to `unavailable` on a
  ~51-minute timer and recovers. Start keys off the **debounced** running sensor;
  drying/finished transitions **ignore any `old_state` of
  `unavailable`/`unknown`/`none`/`None`**, so flaps never produce phantom events.
- **Persistent Claim button.** The button uses a persistent view
  (`timeout=None` + fixed `custom_id`) re-registered on every startup, so it
  keeps working after an HA/bot restart.
- **Restart-safe sessions.** The active message ID, stage, waiting flag, and
  claimant are persisted with HA's `Store`. On startup an in-progress session is
  restored — ETA edits resume and the button still works.
- **Outbound-only & defensive.** No inbound ports; the bot runs as a background
  task tied to the config entry and is closed cleanly on unload. The bot token
  is never written to logs.

## A note on `discord.py` and the official Discord integration

This integration requires `discord.py` (imported as `discord`). It coexists with
Home Assistant's built-in (send-only) Discord notify integration, which uses a
different library. If HA ever reports a dependency clash, adjust the pin in
`manifest.json` and note it.

## Roadmap

- **Prep-dry support** — handle the washer's "stop after wash, set up drying"
  feature with its own "wash done, dry prep pending" alert + ping. Pending the
  exact `job_state` (or `machine_state`) value that mode reports.
- Per-person claim buttons instead of "whoever taps".
- A second **Folded ✓** button to close the loop after the claimant finishes.
- A nag edit if `binary_sensor.laundry_waiting` stays `on` for more than X hours.

## License

[MIT](LICENSE)
