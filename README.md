# Laundry Discord Bot — Home Assistant custom integration

A self-hosted Discord bot that runs **inside** Home Assistant. It watches your
washer and posts **one** rich Discord message per load, with a **tap-to-claim
button** and a **live-updating ETA** that edits the same message in place — so
there are no repeat pings.

- **Domain:** `laundry_discord`
- **Install:** via [HACS](https://hacs.xyz/) as a custom repository
- **Target:** Home Assistant OS / Core **2026.5+**, Python 3.13

## What it does

For a single load (a "session"):

1. **Start** — when the watched *running* sensor goes `off → on`, it posts a new
   Discord embed. This is the only message that may ping (optional role mention).
2. **ETA updates** — every N seconds it **edits the same message** with the
   current estimated finish. Edits never push, so this is silent by design.
3. **Drying alert** — when the job-state sensor enters `drying`, it edits the
   embed to "wash done, drying starting — pull out anything you don't want
   dried." Optionally it can send **one** ping here (configurable).
4. **Finished** — when the job-state sensor returns to `none` from a real wash
   phase, it edits the embed to "Laundry done — don't forget the lint tray" and
   shows a **Claim** button.
5. **Claim** — tapping the button edits the embed to "🧺 Claimed by *name*",
   records the claimant in HA, and ends the session.

There is only ever **one active embed per load**. Duplicate "start" transitions
are ignored while a session is active.

### Entities it creates

| Entity | Meaning |
|--------|---------|
| `sensor.laundry_claimed_by` | Current claimant's display name, or `Unclaimed`. |
| `sensor.laundry_stage` | `Idle` / `Washing` / `Drying` / `Done — waiting`. |
| `binary_sensor.laundry_waiting` | `on` when a finished load is unclaimed. |

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
   **Send Messages, Embed Links, Read Message History**
   (add **Mention @everyone/Roles** only if you'll use a role mention). Open the
   generated URL and invite the bot to your server.
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
| ETA interval | `90` s | How often to edit the ETA (min 30). |
| Ping role ID | *(none)* | Role to mention on the **start** message only. |
| Ping on drying | `false` | Send one ping when drying starts. |

> **Drying ping note:** because editing an embed never triggers a push, the
> optional drying ping is sent as a small **separate** message that mentions the
> role. It's off by default.

## 4. Test without doing laundry

1. Developer Tools → **Actions** → call `laundry_discord.test_post`.
   - Confirm the embed posts, the **Claim** button updates it to "Claimed by
     *you*", and `sensor.laundry_claimed_by` updates in HA.
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

## Roadmap (not in v0.1)

- Per-person claim buttons instead of "whoever taps".
- A second **Folded ✓** button to close the loop after the claimant finishes.
- A nag edit if `binary_sensor.laundry_waiting` stays `on` for more than X hours.

## License

[MIT](LICENSE)
