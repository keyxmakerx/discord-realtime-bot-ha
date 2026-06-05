# Laundry Discord Bot — Home Assistant custom integration

A self-hosted Discord bot that runs **inside** Home Assistant. It watches your
washer and posts **one** rich Discord message per load, with a **live-updating
ETA** and a **wash → dry progress bar** that edit the same message in place, plus
a **claim / unclaim button**. The only push notification is a single **@mention
when the load is done** — everything else updates silently.

- **Domain:** `laundry_discord`
- **Install:** via [HACS](https://hacs.xyz/) as a custom repository
- **Target:** Home Assistant OS / Core **2026.5+**, Python 3.13

## What it does

For a single load (a "session"):

1. **Start** — when the watched *running* sensor goes `off → on`, it posts a new
   Discord embed. This is a normal, visible message but **never @mentions
   anyone** — pings are reserved for completion.
2. **ETA + progress** — every N seconds it **edits the same message** with the
   current estimated finish and a `🟩 Wash → 🟦 Rinse → ⬜ Spin → ⬜ Dry` stage
   bar. Edits never push, so this is silent by design.
3. **Drying alert** — when the job-state sensor enters `drying`, it edits the
   embed to "drying starting — pull out anything you don't want dried." It can
   optionally send one @mention here (off by default).
4. **Finished** — when the job-state sensor returns to `none` from a real wash
   phase, it edits the embed to "Laundry done — don't forget the lint tray",
   shows a **Claim** button, and (if enabled) sends the one **@mention ping** of
   the load: "come grab it."
5. **Claim / Unclaim** — tapping **Claim** edits the embed to "🧺 Claimed by
   *name*" and records the claimant in HA. The message stays live with an
   **Unclaim** button so an accidental claim can be undone. The load stops being
   claimable only when the **next load starts**.

There is only ever **one active embed per load**. Duplicate "start" transitions
are ignored while a wash is already running.

> **Why the ping is a separate little message:** editing an embed never makes a
> phone buzz (that's what keeps the ETA/progress updates silent). So to actually
> notify at completion, the bot posts one short "@role — laundry's done" line
> next to the main embed. That single line is the only push per load.

### Entities it creates

| Entity | Meaning |
|--------|---------|
| `sensor.laundry_claimed_by` | Current claimant's display name, or `Unclaimed`. |
| `sensor.laundry_stage` | `Idle` / `Washing` / `Drying` / `Done — waiting` / `Done — claimed`. |
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
| ETA interval | `90` s | How often to edit the ETA/progress (min 30). |
| Ping role ID | *(none)* | Role to @mention when the load is **done**. Leave blank for no pings at all. |
| Ping on complete | `true` | Send the one @mention when the load finishes. |
| Ping on drying | `false` | Also @mention when drying starts. |

> **Ping note:** because editing an embed never triggers a push, any @mention is
> sent as a small **separate** message next to the main embed. With no role ID
> set, the bot never pings — the message just updates silently.

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
