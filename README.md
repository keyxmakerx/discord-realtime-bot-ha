# Laundry Discord Bot

A Home Assistant custom integration that runs a Discord bot for a shared washer.
It posts **one message per load** and edits it in place with a live ETA and
progress bar, lets people claim the load, and sends **one ping when it's done** —
to whoever claimed it. Everything else updates silently.

- **Domain:** `laundry_discord`
- **Install:** [HACS](https://hacs.xyz/) custom repository
- **Requires:** Home Assistant 2025.3 or newer
- Built and tuned against a Samsung washer/dryer on SmartThings; the default
  entity ids match that integration.

The bot runs inside Home Assistant and connects out to Discord's gateway, so it
needs no open ports. Updating it requires a Home Assistant restart.

## Features

**The card.** When a load starts, the bot posts a card with the estimated finish
and a `🟩 Wash → 🟦 Rinse → ⬜ Spin → ⬜ Dry` bar, then edits it as the cycle
moves (paused, drying, offline, done). Edits never notify anyone.

| Button | What it does |
|---|---|
| 🧺 **Claim** / **Unclaim** | Call dibs from the moment the wash starts. The claimant gets the one "done" ping. Unclaimed loads finish with no ping. |
| 🌙 **Quiet** | The claimant is named at completion but not pinged (for when they're asleep). |
| 🔜 **I'm next** | Join or leave the line. The next person is pinged when the washer is actually *free*, not merely finished. |
| ✅ **Emptied it** | The claimant says the drum is clear, which hands the washer to the next person. If nobody taps it, the next person gets a hedged ping after the handoff backstop (25 min by default). |
| 🤖 | Opens a private panel (only you can see it): a first-time explainer, and your own settings. |

**The 🤖 panel** lets each person choose where pings about *them* go (an @mention
in the channel by default, a DM, or no ping), and opens:

- **📅 My week** — a private week grid with four slots a day. You see your own
  bookings; other people's show as taken, never by name. You can book a slot for
  this week or every week.
- **🔔 What I send you** — switch off individual kinds of unprompted message
  and set quiet hours.
- **🔮 Fix a guess** — correct or stop the day guesses (when day learning is on).

**Optional features (all off by default):**

| Option | What it adds |
|---|---|
| Learn the days each person washes | Logs each Claim tap and marks your usual slots with `?` on *your* grid only. No stats are ever shown to the house. Each person can opt out (👁 Monitoring) or stop the guesses (🔮). |
| Send reminder DMs | A weekly check-in and a heads-up before a slot you booked, only to people who chose **📬 DM me**. At most 1 DM per person per day and 2 per week; anything over is dropped. Never posted in the channel. |
| Swap requests | Ask whoever holds a slot, anonymously, whether they'd trade. Names are revealed only if they accept. Guardrails limit asking (one ask per slot per week, refusals stick, a permanent 🚫, asks expire after 48 h). |

Quiet hours and the 🔔 switches only ever remove messages. Replies to something
you did (your load is done, the washer is yours) are never held back.

Other behaviour: self-clean cycles get their own card with no claim button or
ping; a load stopped on the machine is reported as **🛑 Stopped early**, not
"done"; and if the washer drops offline mid-load the card says so.

The rules behind all of this are in [`docs/design.md`](docs/design.md).

## How detection works

Cloud-connected washers report late, freeze, and replay old values when they
reconnect, so the bot combines signals by how far each can be trusted:

- **Start:** the washer's job state entering an early phase (`weight_sensing`,
  `wash`) and holding for the confirm delay. A phase that arrives straight after
  a reconnect only counts once the energy meter agrees.
- **Finish:** job state `finish`, or the washer's own completion estimate having
  passed with the energy meter flat for a short settle. Only an estimate
  published during the current cycle counts.
- **Fallbacks:** a single energy jump with no phase reported is a load that ran
  while the washer was offline; a flat meter ends a load only when there is no
  estimate; a 12-hour cap closes anything left.
- **Stops:** a confirmed `stop` (or the running sensor turning off) mid-load ends
  it as stopped early.

Observed quirks of the real machine, and things that look like bugs but aren't,
are recorded in [`docs/field-notes.md`](docs/field-notes.md).

## Setup

### 1. Create the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, open **Bot**, and copy the token. No privileged
   intents are needed; leave **Message Content** off.
2. Under **OAuth2 → URL Generator**, pick the `bot` scope with **View Channels,
   Send Messages, Embed Links, Read Message History**, open the URL and invite
   the bot to your server. Mentions and DMs need no extra permission.
3. Turn on Developer Mode (User Settings → Advanced), right-click the channel and
   **Copy ID**.

### 2. Install

1. HACS → ⋮ → **Custom repositories** → add this repository as an
   **Integration**.
2. Download **Laundry Discord Bot** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Laundry Discord Bot**.

If you already have washer automations that post to Discord, disable them — this
integration handles the whole lifecycle and they would double-post.

### 3. Configure

Setup asks for the token, the channel ID and the washer entities:

| Field | Default |
|---|---|
| Washer running sensor | `binary_sensor.washer_running` |
| Job state sensor | `sensor.washer_washer_job_state` |
| Completion time sensor | `sensor.washer_washer_completion_time` |
| Machine state sensor | `sensor.washer_washer_machine_state` |
| Energy sensor | `sensor.washer_energy` if left empty |
| Water sensor | `sensor.washer_water_consumption` if left empty |
| Wrinkle-prevent sensor | `binary_sensor.washer_wrinkle_prevent_active` if left empty |

Everything else is under **Configure** and can be changed at any time (saving
reloads the integration and briefly reconnects the bot):

| Option | Default | Range |
|---|---|---|
| ETA update interval | 90 s | 30–3600 |
| Confirm delay | 30 s | 0–300 |
| Flat-meter timeout (offline backstop) | 60 min | 10–240 |
| Offline load threshold | 0.3 kWh | 0.1–5 |
| Ping the claimant when done | on | |
| ETA hold time while the sensor is unavailable | 5 min | 1–120 |
| Handoff backstop (0 disables) | 25 min | 0–240 |
| Queue expiry | 12 h | 1–72 |
| Show the 🤖 assistant button | on | |
| Learn the days each person washes | off | |
| Send reminder DMs | off | |
| Weekly check-in day and time | Sunday 18:00 | |
| Slot heads-up lead time | 60 min | 5–180 |
| Swap requests | off | |

## Entities

| Entity | Description |
|---|---|
| `sensor.laundry_stage` | `Idle`, `Washing`, `Drying`, `Done — waiting`, `Done — claimed` or `Self-clean`. Attributes `queue_count`, `queue`, `next_up` (the names are kept out of recorder history). |
| `sensor.laundry_claimed_by` | The claimant, or `Unclaimed`. |
| `binary_sensor.laundry_waiting` | On while a finished load is unclaimed. |
| `sensor.laundry_health` | Worst diagnostics finding: `ok`, `note`, `warning` or `problem`, with the summary and findings as attributes. Refreshed every 5 minutes. |
| `sensor.laundry_connection_health` | Washer cloud drops in the last 24 h. |
| `button.laundry_test_post`, `button.laundry_reset_session`, `button.laundry_track_load`, `button.laundry_run_diagnostics` | The actions below, one tap each. |
| `number.laundry_*`, `switch.laundry_*` | The timing options and house-wide features. Changing one reloads the integration. |

Entity ids depend on Home Assistant's entity ID format: with the default format
and the device in an area, ids get the area as a prefix (for example
`sensor.laundry_room_laundry_stage`). Installs created before 0.34.0 keep the ids
they already have. **Settings → Devices & services → Laundry Discord Bot** lists
yours.

Per-person settings are deliberately not entities: they belong to each person
and are changed in Discord.

## Actions

| Action | What it does |
|---|---|
| `laundry_discord.test_post` | Posts a sample card with working buttons, to test the Discord side without a wash. |
| `laundry_discord.reset_session` | The bot thinks a load is running and it isn't: closes the card and returns to idle. Announces nothing, pings nobody. |
| `laundry_discord.track_load` | A load is running and the bot missed it: starts tracking it and posts a card. Does nothing if a load is already tracked. |
| `laundry_discord.diagnostics` | Returns the health findings as response data. Changes nothing; safe mid-wash. |

## Dashboard

[`dashboards/laundry.yaml`](dashboards/laundry.yaml) is a ready-made dashboard:
health, the bot's view, what the washer reports, the actions, the options and
history. Create a new dashboard, open **⋮ → Edit → ⋮ → Raw configuration
editor**, paste the file, and adjust the entity ids if yours have a prefix.

## Troubleshooting

- **Something looks wrong:** run **Diagnostics** (the button or the action). It
  checks for a wedged session, a "load" the energy meter says never happened,
  the washer contradicting the bot, a missed load, and how the cloud connection
  is dropping. If it says to re-run in a few minutes, do — the end of every load
  briefly looks inconsistent.
- **Stuck card:** `reset_session`. **Missed load:** `track_load`.
- **A DM never arrives:** the person has DMs from server members turned off. The
  bot falls back to a channel mention for pings and explains the fix the next
  time they open 🤖.
- **The ETA is off:** it's the washer's own estimate, shown as approximate.

Before changing detection, read [`docs/field-notes.md`](docs/field-notes.md).

## Development

- Tests are standalone scripts: `python3 tests/test_<name>.py`. Most need nothing
  installed; the coordinator, panel and reminder-loop tests need
  `homeassistant` and `discord.py==2.7.1`. CI runs all of them, plus hassfest
  and the HACS check.
- To release, bump `version` in `manifest.json` and publish a GitHub release
  tagged to match (for example `v0.34.0`).
- Known issues, ideas and open questions are tracked in
  [GitHub issues](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues).

This integration uses `discord.py` and coexists with Home Assistant's built-in
Discord notify integration.

## License

[MIT](LICENSE)
