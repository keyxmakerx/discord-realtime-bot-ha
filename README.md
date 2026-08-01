# Laundry Discord Bot — Home Assistant custom integration

A self-hosted Discord bot that runs **inside** Home Assistant. It watches your
washer and posts **one** rich Discord message per load, with a **live-updating
ETA** and a **wash → dry progress bar** that edit the same message in place, plus
a **claim / unclaim button** you can tap from the moment the wash starts. The
only push notification is a single **@mention to whoever claimed the load, sent
when it's done** — everything else updates silently, and if nobody claimed it the
"done" message is posted with no ping at all. Anyone waiting on the machine can
tap **🔜 I'm next** and gets their own ping when it's **actually free**, which is
not the same moment the cycle ends. A **🤖** button opens a private panel where a
newcomer can find out what any of that means and anyone can choose how they'd
rather be pinged — in the channel (the default), in a DM, or not at all.

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
   the one **@mention** to that person ("your laundry's done") — unless they've
   set **🌙 Quiet** (see below), in which case they're **named in plain text with
   no ping**. If **nobody claimed** it, the done message is posted with **no
   ping**, and the Claim button stays.
5. **Claim / Unclaim / Quiet** — tapping **Claim** records the claimant in HA and
   swaps in an **Unclaim** button (to undo an accidental claim) plus a **🌙 Quiet**
   toggle. Quiet means completion *names* the claimant but does **not** @mention
   them — a visible "done" with no push, for when they're asleep; tapping it again
   (**🔔 Unmute**) restores the ping. The load stops being claimable only when the
   **next load starts**.
6. **The line** — anyone can tap **🔜 I'm next** at any point during a load to
   get in the queue, and tap it again to get out. The card shows who's waiting
   ("Next up 🔜 Sam, then Ty"), and when the machine is actually free the person
   at the head gets **their own ping**. See below for what "actually free" means.
7. **The assistant** — the **🤖** button opens a panel **only you can see**: the
   first-time explainer if you've never used the channel, otherwise your own
   settings for how you'd rather be pinged. It posts nothing and changes nothing
   until you tap something in it.

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
> posted — **unless** the washer is reporting **stopped/idle** at that moment, in
> which case the jump is taken as harmless meter catch-up on reconnect, not a
> load (this prevents a false "running" right after the device comes back online).
> Slow standby/wrinkle-prevent creep (small per-sample steps) and meter
> resets (a decrease) never trip it. Telemetry for a fully offline load only
> reaches HA on reconnect, so that message is necessarily *after the fact*, and a
> load smaller than the threshold with no phases can't be caught — a cloud limit.

> **Washer goes offline mid-load:** if the washer drops off the cloud (its
> entities go `unavailable`) for ~1h during a tracked load, the card shows a
> **"⚠️ Washer offline — can't verify"** notice instead of pretending to track.
> If by then its **last-known ETA has already passed**, the bot still posts the
> completion (at ETA + ~30 min, a cushion for a long dry) but **flags it as
> unverified** ("the washer went offline, couldn't confirm — worth a peek"). It
> will *not* guess a finish for a load that went offline *before* its ETA passed
> (it can't know), leaving the absolute max-session cap as the last resort.

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

### The "I'm next" line (🔜) and the handoff

In a shared house the washer is a contended resource, and the thing that
actually wastes people's evenings isn't *not knowing the load is done* — it's
walking down to a machine that's still full. So the queue is deliberately not
wired to completion.

> **Done ≠ free.** The washer finishing does not mean it's empty. The claimant's
> clothes are still in the drum, and they may be at work, asleep, or three
> episodes deep. Pinging the next person the moment the cycle ends sends them to
> a full machine, and after that happens twice **the ping stops being trusted** —
> at which point the whole feature is worse than nothing. The handoff therefore
> gets its **own trigger**, separate from completion.

The sequence, for a **claimed** load:

1. The load finishes → the existing one-per-load ping goes to the claimant
   ("your laundry's done — don't forget the lint tray"). Whoever's next is
   **named on the card, not pinged**: *"Next up 🔜 Sam — you're up once Alex
   clears it."*
2. The claimant taps **✅ Emptied it** → Sam gets *"🔜 Washer's free — you're
   up."* That tap is the handoff, and it only exists on a claimed, finished load
   that hasn't been cleared yet — nobody else can confirm it, and once it's been
   tapped there's nothing left to confirm.
3. Nobody taps it within the **handoff backstop** (default **25 min**, 0 to
   disable) → Sam is pinged anyway, but **hedged**: *"the washer's been done a
   while and nobody's checked in — probably free, worth a look."* The wording is
   honest about the fact that at that point we genuinely don't know. This is the
   backstop, not the mechanism; people forget.

If the load was **unclaimed** there is nobody to do the emptying, so the head of
the line is pinged **immediately** at completion, alongside (not instead of) the
existing up-for-grabs nudge.

Rules that stop the line becoming its own problem:

- **It's a toggle.** Tapping 🔜 while already in the line takes you out of it.
- **FIFO**, and **capped at 5** — past that the button says the line is full
  rather than silently swallowing the tap.
- **Pinged means popped.** Whoever gets the handoff comes out of the line, so the
  backstop timer can't ping the same person twice.
- **It carries forward.** When the next load starts the line rolls over, minus
  whoever claimed that load — they're running a wash, not waiting for one. A
  three-deep queue is a real thing: A finishes, B takes the machine, C is still
  next.
- **Entries expire** after `queue_expiry` (default **12 h**). Somebody who tapped
  last night and went to bed shouldn't be pinged at 6am, and a line that never
  empties would strand every future handoff.
- **The line is never @mentioned from the card.** It's rendered as plain names;
  the only push anyone in it gets is their own handoff message.

The queue is **session state** — it lives in the same store as the claimant and
resets with the session — and it is **inert when unused**: no taps, no line, no
field on the card, no extra messages.

### The 🤖 assistant panel

Rightmost on every card is a single **🤖** button. It opens a message that
**only you can see** — Discord labels it in those words and posts nothing to the
channel — which is what lets onboarding and personal settings exist here at all.
The channel carries one card per load and nothing else; anything that's about
*one person* happens in private or in a DM.

> **Why rightmost, and why one button.** Discord lays buttons out left-to-right
> in the order they were added and has **no right-alignment**, so "rightmost" is
> achieved by adding it last — which is also where the least important control
> belongs. It never competes with the buttons that actually do laundry.

The button works on **any** card, including one from last week with no live load
behind it. That's deliberate: someone scrolling back through the channel is
exactly the person who has never used it before.

**First time?** Anyone who hasn't answered the panel gets the explainer rather
than the settings — the whole onboarding story, in private, with zero channel
noise:

```
👋 First time?
This channel watches the washer and posts one message per load…

🧺 Claim      — call dibs, and I'll tell you when it's done
🔜 I'm next   — get told when the washer is actually free
🌙 Quiet      — claim without the ping, for when you're asleep
✅ Emptied it — you've cleared the drum; whoever's waiting is told

How should I reach you when something's actually for you?
[ 📬 Yes, DM me ] [ 💬 In the channel ] [ 🚫 No thanks ]
        Only you can see this
```

**Returning:**

```
🤖 Your laundry assistant

  Pings        💬 In the channel, with an @mention
  Monitoring   👁 on — when you tap Claim I note the day and time
  Guessing     🔮 on — I'll mark your usual days as ░ on your week

[ 📬 DM me ] [ 💬 In the channel ] [ 🚫 No pings ]
[ 👁 Monitoring: on ] [ 📅 My week ] [ 🔮 Fix a guess ]
        Only you can see this
```

Everything on it is something that's actually true right now — a settings screen
that lists a preference nothing reads stops being believed. So the **Guessing**
line only appears when there's guessing to have an opinion about, and 🔮 is
absent entirely while day-learning is off for the channel. Slot trades are a
later phase of the [planner design](docs/rsvp-planner-design.md), and a button
that does nothing yet is worse than no button:

- **Pings** — where the messages that are **about you** go: your load finishing,
  and the "washer's free" handoff after you tapped 🔜. Nothing else moves.
  - **💬 In the channel** — an @mention, exactly as before. **This is the
    default**, so anyone who never opens the panel keeps getting precisely what
    they got in v0.16 and earlier. Opting in is a deliberate act.
  - **📬 DM me** — the same message, sent to you directly, nothing in the
    channel. If the DM fails it falls back to the channel mention, because a
    handoff nobody hears is worse than a line in the channel.
  - **🚫 No pings** — the message is still posted and you're still *named*, but
    push-silently and with the mention suppressed. It removes the buzz, not the
    information — the same trade 🌙 Quiet already makes on the card.
- **Monitoring** — per-person consent to logging your loads, which is what lets
  the bot work out the days you usually wash (**The 🔮 day guesses**, below).
  Off means your Claim taps are **never written down at all** — not written and
  then filtered out. While the channel has day-learning switched off, nothing is
  logged for anyone and this is simply your answer for if it's switched on.
  **No stats about anyone are ever shown to the household** — no streaks, no
  counts, no "who does the most laundry."
- **📅 My week** and **🔮 Fix a guess** open the two displays below: the week
  grid, and what the bot thinks your usual days are.

#### When Discord won't let the bot DM you

If you have **DMs from server members** turned off, a DM from the bot fails with
`Forbidden` (error 50007) — and Discord tells the *sender*, never you. So a DM
that bounces is handled in three steps: the failure is remembered (the bot stops
trying and uses the channel), the message goes out as a normal channel @mention
so it isn't lost, and **the next time you open 🤖** the panel leads with the fix:

```
⚠️ I couldn't DM you
Two settings control this:
 • Right-click the server icon → Privacy Settings → allow DMs from members
 • User Settings → Privacy → allow direct messages from server members
Until then I'll ping you in the channel instead.
```

That notice shows **once** per failure — it's information, not nagging. Tapping
**📬 DM me** again re-arms DM delivery, which is how this self-heals: it reaches
the only person who can fix it, costs the channel nothing, and the moment they
fix it the next DM goes through. It is deliberately **not** an alert to the HA
owner, who can't change somebody else's privacy settings.

> DMing a known user ID needs **no privileged intent and no extra bot
> permission** — the same as the @mention. **Message Content** stays off.

The panel's preferences live in their **own** `Store` key
(`laundry_discord.planner`), separate from the session store that holds the live
load, the claimant and the 🔜 line. That separation is on purpose: the session
store is rewritten on every meter sample, and a bug on the preferences side must
not be able to corrupt a load in progress. They survive restarts, unloads and
resets, and the whole button can be hidden from the card with the **Show the 🤖
assistant button** option if it's not wanted.

### The 📅 week grid

Behind **📅 My week** on the assistant panel is the whole week at a glance:

```
      Mo Tu We Th Fr Sa Su
AM     ·  ·  ▓  ·  ·  ▓  ·
Mid    ·  ·  ·  ▓  ·  ·  ·
PM     ▓  ·  ·  ▓  ·  ·  ▓
Eve    ·  ▓  ·  █  ·  ▓  ·

█ yours  ▓ taken  · free
```

**Four slots a day, because at 4–5 hours a cycle one slot is about one load.**
A cell isn't a time range you're renting, it's roughly "a wash" — AM 06:00–12:00,
Mid 12:00–16:00, PM 16:00–20:00, Eve 20:00–00:00.

> **Nobody's name is ever on it.** A cell is *free*, *taken* or *yours* — never
> "taken by Alex", never a count that would let you work it out. In a house this
> size a plan board that names people turns into a scoreboard, and the moment it
> does, people stop putting anything on it. What you actually need to know to
> avoid a collision is that Thursday evening is spoken for, not who by.
>
> That anonymity is exactly why the grid is **ephemeral** and not a pinned
> channel message: your own cells have to render differently from everyone
> else's (`█` vs `▓`), and one shared message can only have one rendering. So
> each person gets their own private view — "only you can see this" — and the
> channel stays at one card per load.

**It's information, not permission.** Booking a slot says *I'm planning to wash
then*. It doesn't reserve the machine, nothing stops anyone else using it, and
two people can hold the same cell — the grid's job is to make that visible so
one of them moves, not to arbitrate. Nothing here can enforce a booking, so
nothing here pretends to.

**A tap always means "this week".** Bookings are stored as per-week overrides
rather than as a standing habit, because "Thursday evening" is the only thing
you can actually know on a Tuesday. Touching a cell also pins it for that week,
so a later change to anyone's usual days can't reach back into a week somebody
has already edited by hand.

You pick a day from the dropdown and tap one of the four slot buttons; the grid
redraws in place. It's a **display**, not a clickable table — Discord caps a
message at 25 components and 7 days × 4 slots is 28, so a button per cell isn't
possible even before it'd be unreadable on a phone. The block is kept to 26
characters wide for the same reason, and everything decorative lives outside the
code fence, since an emoji inside one breaks the column alignment the whole grid
depends on.

### The 🔮 day guesses (`░`)

Turn on **Learn the days each person washes** in the options and the bot starts
noticing something it has always had and never used: **every 🧺 Claim tap is a
labelled data point** — this person ran a load, at this time, on this day. Six
weeks of that is a per-person histogram of when you actually do laundry, for
free, with nothing to fill in and nothing to ask.

A fourth character then appears on **your own** grid:

```
      Mo Tu We Th Fr Sa Su
AM     ·  ·  ▓  ·  ·  ▓  ·
Mid    ·  ·  ·  ▓  ·  ·  ·
PM     ▓  ·  ·  ▓  ·  ·  ▓
Eve    ·  ▓  ·  ░  ·  ▓  ·

█ yours  ▓ taken  ░ expected  · free
```

**`░` means "I think you usually wash then."** It is a guess from your own past
loads — not a booking, not something anybody asked for, and not something you
have to act on. It appears only where a slot is otherwise **free**: a real
booking, yours or anyone else's, always wins, so a guess can never sit on top of
`▓` or `█` and hide it. The grid's job is to show you what the house has
actually planned; the bot's opinion is strictly the layer underneath.

> **Your guesses are yours alone.** Nobody else ever sees them — not on their
> grid, not on the pinned board (which has no viewer and therefore no `░` at
> all), not in the channel, not in a DM to anyone. This is a stricter rule than
> the one covering bookings, and deliberately so: a booking is something you
> chose to put on a shared board, while a guess is the bot telling other people
> what it reckons your week looks like. In a house of seven that's how a plan
> board turns into a scoreboard.

**Silence is the default, for about a month.** A slot is only ever guessed at
when *all three* of these hold: at least **3 loads** in that same slot, that slot
is at least **30%** of your loads, and there are at least **4 weeks** of history
for you. Miss one and you get nothing — not a hedged "maybe Thursdays?", which is
the sort of thing you stop believing after reading it once. For the first few
weeks after switching this on, **no `░` anywhere is the correct behaviour.**

#### Arguing with it

**🔮 Fix a guess** on the assistant panel shows the guess and the arithmetic
behind it:

```
🔮 What I think
I think you wash Thursday evenings — 5 of your last 8 loads.

[ ✅ That's right ] [ ❌ Wrong ] [ 🚫 Stop guessing ] [ ↩️ Back ]
```

- **✅ That's right** — acknowledged, and nothing is stored. The loads behind the
  guess are already counted; a "confirm" row would just be the guess feeding
  itself evidence with your tap laundering it.
- **❌ Wrong** — the guess for that slot is retired. It isn't blacklisted
  forever: the only thing that brings it back is you actually washing then
  again. Being told "no" and then arguing from the same loads would be the model
  learning from itself with extra steps.
- **🚫 Stop guessing** — no more `░` for you, ever, and no guessing done on your
  behalf. Tapping it again (**🔮 Start guessing**) puts it back; the loads
  already noted are still there.

And if there's no guess yet, it says so plainly, with your own numbers and the
bar it hasn't cleared — never an invented one.

#### Turning it off

Three independent switches, any of which stops it:

| Switch | Where | Effect |
|--------|-------|--------|
| **Learn the days each person washes** | integration options | The house-wide master. **Off by default.** Nothing is logged for anybody and no `░` is drawn. |
| **👁 Monitoring** | 🤖 panel, per person | Your loads are never written to history at all — not written and then filtered, simply not written. Also stops any guessing about you, so switching it off makes the existing history stop being read too (it isn't deleted; a toggle you flipped to see what it does shouldn't destroy three months of data). |
| **🔮 Stop guessing** | 🔮 panel, per person | Keeps logging, stops guessing. For somebody who's happy to feed a future reminder but doesn't want the grid marked up. |

History lives in the same planner `Store` as the panel's preferences, **never
leaves your Home Assistant instance**, and is **capped at 90 days** — the cap is
applied every time a row is written, so it's bounded by the act of using it
rather than by a cleanup job somebody has to remember. A row holds a timestamp,
a user id and a slot; there is nowhere in it to put a name.

> **Nothing in this version DMs you.** Predictions exist, are visible on your own
> grid and can be argued with — and that's the whole of it. The reminders that
> use them (the Sunday check-in and the day-of nudge, with a hard budget of 1 DM
> a day and 2 a week) are the next phase, deliberately after this one, so the
> guesses are correctable *before* anything reaches a phone.

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
   permission, and neither the 🤖 panel (an interaction response) nor a DM to a
   known user ID needs one either. Open the generated URL and invite the bot to
   your server.
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
| Handoff backstop *(options)* | `25` min | Ping whoever's next this long after a load finishes if the claimant never tapped **✅ Emptied it**. `0` disables the backstop — the tap still works. |
| "I'm next" expiry *(options)* | `12` h | How long a 🔜 tap stays in the line before it ages out. |
| Show the 🤖 assistant button *(options)* | `true` | Puts the 🤖 button on the card. It's inert until tapped — no pings, no channel lines, nothing stored — and it's the only place a newcomer finds out what the other buttons do. Turning it off hides the button; anything already chosen in the panel keeps working. |

> **Ping note:** the only push per load is a *user* mention of the claimant, sent
> as a small separate message when the load finishes. If nobody claimed it, no
> ping is sent. There are no role/@everyone pings, so the bot needs no special
> mention permission.

## 4. Test without doing laundry

1. Developer Tools → **Actions** → call `laundry_discord.test_post`.
   - Confirm the embed posts, **Claim** updates it to "Claimed by *you*" (and
     `sensor.laundry_claimed_by` updates in HA), and **Unclaim** reverts it.
   - The test post also carries **🤖**, so you can open the panel and (by
     choosing **📬 DM me** and finishing a load) check the DM path without
     waiting for a real wash.
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
- **Persistent buttons.** Every button uses a persistent view (`timeout=None` +
  fixed `custom_id`) and the views re-registered on startup deliberately contain
  **every** `custom_id` — claim, unclaim, quiet, next, emptied, 🤖 and the
  panel's own four — not just the ones the current card happens to show. An
  unregistered `custom_id` doesn't error, it silently stops dispatching, which
  looks exactly like a dead button. The 🤖 button is registered even when the
  option hides it, so switching the option on doesn't leave a dead button behind
  until the next restart.
- **Restart-safe sessions.** The active message ID, stage, waiting flag,
  claimant, the 🔜 line and the "emptied" flag are persisted with HA's `Store`.
  On startup an in-progress session is restored — ETA edits resume and the
  buttons still work. A store written by an older version simply comes back
  without a line.
- **Two stores, one direction.** Per-person preferences live under their own
  `Store` key, never alongside the session, so a fault on that side can't corrupt
  a live load. Records are normalised on load — missing, half-written and
  outright corrupt fields all come back as sane defaults instead of raising
  inside a button callback, and a mapping keyed by user ID is re-keyed
  explicitly, because JSON object keys are always strings and
  `interaction.user.id` is an int.
- **Ephemeral panels expire, gracefully.** A Discord interaction token dies after
  **15 minutes**. Every tap carries a fresh one, so a panel someone is actively
  using keeps editing in place — but a tap on a panel opened an hour ago can't
  edit that old message, and Discord rejects it. That's expected, not an error,
  so it answers with a brand new private panel instead of leaving the user
  looking at "interaction failed".
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
- ~~A second **Folded ✓** button to close the loop after the claimant finishes.~~
  **Shipped** as **✅ Emptied it** — with a reason to exist: people tap it because
  they're handing the machine over, not because a bot asked them to do a chore.
- A nag edit if `binary_sensor.laundry_waiting` stays `on` for more than X hours.
- ~~A 🤖 assistant panel with per-person reminder settings.~~ **Shipped** as
  Phase 2 — the private panel above, including the DM plumbing and the
  "I couldn't DM you" self-heal.
- ~~An anonymous week grid.~~ **Shipped** as Phase 3 — 📅 above.
- A **pinned anonymous occupancy board** in the channel. Deliberately deferred
  rather than dropped: the renderer already produces it (the board is just the
  grid with no viewer), but it needs a persisted message and a refresh on every
  change, and a second permanent message has to earn its place in a channel
  whose whole premise is one card per load. Worth revisiting once the house has
  lived with the ephemeral grid for a week.
- ~~A model of the days each person usually washes.~~ **Shipped** as the first
  half of Phase 4 — 🔮 above: history from Claim taps, confidence-gated guesses
  drawn as `░` on your own grid, and the corrections that train it.
- The rest of the planner (see [`docs/rsvp-planner-design.md`](docs/rsvp-planner-design.md)):
  the DM reminders those guesses are *for* — a Sunday check-in and an
  event-driven nudge on the day, under a hard budget of 1 DM a day and 2 a week
  (the second half of Phase 4) — and a double-blind slot trade broker (Phase 5).
  The 🔜 queue was Phase 1 and shipped alone, because it's the only phase that
  touches the session machine.

## License

[MIT](LICENSE)
