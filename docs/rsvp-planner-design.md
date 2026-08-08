# RSVP, planning & reminders — design

Design for the queue / scheduling / reminder layer on top of the existing
Laundry Discord Bot. Written before implementation so the tradeoffs are argued
once, in one place, rather than rediscovered in a bug report.

**Context:** shared house, 6–7 people, one washer/dryer combo, one Discord
channel. The existing integration is reliable and heavily tuned (see
`custom_components/laundry_discord/detect.py` and the README's detection
section). **Nothing here is allowed to make that less reliable.**

---

## 1. Goals

1. **Hand the machine off cleanly.** When a load finishes, whoever is waiting
   should find out — without standing in the laundry room.
2. **Reduce collisions.** With 7 people the washer is a contended resource that
   clusters into evenings and weekends. Make that contention visible.
3. **Soft reminders on the days you usually wash** — a nudge, not an alarm.
4. **Zero added channel noise.** The channel already carries one card per load.
   Everything personal happens in DMs or ephemeral messages.

## 2. Non-goals

- **Not a booking system.** Nothing here can enforce a reservation, so nothing
  here pretends to. See §8.
- **Not an LLM.** This is a histogram and a scheduler. Natural-language RSVP
  ("I'll wash Thursday") would require Discord's **privileged Message Content
  intent**, which the integration deliberately does not use. Buttons and select
  menus need no intent at all.
- **Not a chore tracker.** No leaderboards, no "X hasn't washed in 3 weeks", no
  per-person stats surfaced to the household. See §11.

---

## 3. Principles

These are load-bearing. Violating one is how this feature becomes the thing
everyone mutes.

| # | Principle | Consequence |
|---|-----------|-------------|
| P1 | **Never ask randomly.** | The bot initiates only on a fixed schedule or in response to a real event. No spontaneous "hey, washing today?" |
| P2 | **Hard nudge budget, enforced in code.** | Max **1 DM per person per day**, **2 per week**. Over budget = dropped, not queued. A constant in `const.py`, not a good intention. |
| P3 | **Presence beats plans.** | Someone tapping a button right now outranks something written down on Sunday. Nobody ever waits on a no-show. |
| P4 | **Learning is visible and correctable.** | The model's guesses are shown and can be argued with. No silent model that pings your phone. |
| P5 | **Plans are anonymous; the live load is not.** | The forward-looking grid shows occupancy only. The live card keeps showing "Claimed by Alex" — you need to know who to hand the machine to. |
| P6 | **Confidence-gated silence.** | No prediction, no nudge. Thin data → the bot says nothing. |
| P7 | **Additive and reversible.** | Every new behaviour is behind an option. `detect.py` is never touched. |

---

## 4. Feature 1 — the queue ("I'm next")

The highest-value, lowest-risk piece. Ships first, standalone.

### 4.1 The subtlety

**Done ≠ free.** The washer finishing does not mean it is empty — the
claimant's clothes are still in it. Pinging the next person at completion sends
them to a full machine, and after two of those the ping stops being trusted.
The handoff therefore needs its **own trigger**, separate from completion.

### 4.2 Flow

Live card, while washing:

```
🫧 Laundry started
The washer is running. Tap Claim to call dibs.

Progress          🟦 Wash → ⬜ Rinse → ⬜ Spin → ⬜ Dry
Estimated finish  ~3:40 PM, about 1h12m left
Claimed by        🧺 Alex
Next up           🔜 Sam, then Ty

[ ↩️ Unclaim ] [ 🌙 Quiet ] [ 🔜 I'm next ] [ 🤖 ]
```

At completion — the queue head is **named, not pinged**, preserving the
existing "one push per load" rule:

```
✅ Laundry done!
This load used  ⚡ 0.92 kWh · 💧 34 L
Next up         🔜 Sam — you're up once Alex clears it

[ ✅ Emptied it ] [ 🔜 I'm next ] [ 🤖 ]
```

Message sequence:

1. `@Alex 🧺 Your laundry's done — don't forget the lint tray!`
   *(unchanged from today; still the only push at completion)*
2. Alex taps **✅ Emptied it** → `@Sam 🔜 Washer's free — you're up.`
3. If nobody taps within `handoff_fallback` (default **25 min**) →
   `@Sam 🔜 Washer's been done a while and nobody's checked in — probably free,
   worth a look.` (hedged, because we genuinely don't know)

If the load was **unclaimed**, there is nobody to do the emptying, so the queue
head is pinged directly at completion alongside the existing up-for-grabs nudge.

### 4.3 Rules

- **Toggle.** Tapping 🔜 when already queued removes you.
- **FIFO**, with one exception (§8, rule 2).
- **Carry-forward.** When a new load starts, the queue carries over minus
  anyone who claims the new load — that's the 3-deep line working correctly.
- **Expiry.** A queue entry older than `queue_expiry` (default **12 h**) is
  dropped, so a stale line never strands a handoff.
- **Cap.** Max 5 in the queue; beyond that the button reports the line is full.
- **Reset.** Cleared on unload/reset like any other session state.

### 4.4 "Emptied it"

This is the roadmap's **Folded ✓** button with a reason to exist: people tap it
because they're handing off, not because a bot asked them to do a chore.

Modelled as a **boolean flag on `STAGE_DONE_WAITING`**, *not* a new stage — the
stage machine is not extended.

---

## 5. Feature 2 — the 🤖 assistant panel

One button, rightmost on every card, opening an **ephemeral** message.

> **Discord fact:** buttons lay out left-to-right in the order added. There is
> **no right-alignment** in the API. "Rightmost in the row" is achievable by
> adding it last; true right-edge alignment is not.

### 5.1 First-time / guest

```
👋 First time?
This channel tracks the washer. Here's what you can do:

🧺 Claim    — call dibs on a running load, get pinged when it's done
🔜 I'm next — get pinged when the washer frees up
🌙 Quiet    — claim without the ping (for sleeping)

Want reminders on the days you usually wash?
[ ✅ Yes, DM me ]  [ 📅 Set my days ]  [ 🚫 No thanks ]
        Only you can see this
```

This is the entire onboarding story, private, with zero channel noise. For a
house with turnover and guests this is the single most useful surface.

### 5.2 Returning

```
🤖 Your laundry assistant

  Reminders     DM me, on the day
  Your slots    Thu Eve · Sun AM
  Predictions   on — I'll guess your days
  Monitoring    on — I'll log your loads

[ 📅 My week ] [ 🔮 Fix a guess ] [ ⏸ Pause ] [ 🚫 Off ]
        Only you can see this
```

- **Pause** — no nudges for N days, then resumes.
- **Off** — permanent opt-out. Never nagged again.
- **Monitoring off** — stop logging this person's loads to history at all.
- **Fix a guess** — see §7.3.

### 5.3 Ephemeral mechanics (constraints that shape everything)

- Ephemeral messages **can only be sent in response to an interaction**. The bot
  cannot send one unprompted. This is why reminders must be DMs.
- The interaction token expires after **15 minutes**. Each button tap creates a
  *new* interaction with a fresh token, so a panel stays editable as long as the
  user keeps interacting. Walking away and returning >15 min later must be
  handled by responding with a **fresh** ephemeral rather than editing.
- They are **not durable** — they vanish on client restart.
- They **do** support file attachments (relevant to §6.4).
- Components on them dispatch via the persistent-view registry, so they survive
  a bot restart provided `custom_id`s stay registered.

---

## 6. Feature 3 — the week grid

### 6.1 Slots

At 4–5 hours a cycle, **one slot ≈ one load.** A cell is not a time range, it's
a load. Four slots cover a day:

| Slot | Window |
|------|--------|
| AM | 06:00 – 12:00 |
| Mid | 12:00 – 16:00 |
| PM | 16:00 – 20:00 |
| Eve | 20:00 – 24:00 |

### 6.2 Anonymity → the grid is per-person → the grid is ephemeral

The grid shows **no names**. But you must still see *your own* slots to correct
them, so the grid renders **differently per viewer**. A single shared channel
message cannot do that — one message, one rendering.

Therefore the **interactive grid is ephemeral**, opened from 🤖. A **pinned,
fully anonymous occupancy board** may also live in the channel (same renderer,
personalisation off) because passive visibility is what actually prevents
collisions — you see it without deciding to look.

### 6.3 Rendering

> **The glyphs below are superseded.** The density ramp shipped, met a real
> household, and could not be read: it encodes *magnitude* for what are actually
> *categories*. It was replaced in v0.24.0 — see
> [live-use-fixes-design.md §5](live-use-fixes-design.md) for the replacement and
> the argument. Everything else in this section (the layout, the width cap, the
> emoji rule) still holds; only the alphabet changed.

```
      Mo Tu We Th Fr Sa Su
AM     ·  ·  ▓  ·  ·  ▓  ·
Mid    ·  ·  ·  ▓  ·  ·  ·
PM     ▓  ·  ·  ▓  ·  ·  ▓
Eve    ·  ▓  ·  █  ·  ▓  ·

█ yours  ▓ taken  ░ expected  · free

[ Day: Thursday ▾ ]
[ AM ] [ Mid ] [ PM ] [ Eve ]
        Only you can see this
```

Density ramp: `·` free → `░` expected (model guess) → `▓` planned (someone said
so) → `█` yours.

Formatting rules (both matter, both learned the hard way elsewhere):

- **Emoji break monospace alignment inside code blocks.** The grid uses ASCII /
  block characters only. Emoji are fine in the legend *outside* the block.
- **Keep the block ≤ ~30 characters wide** so it doesn't wrap on phones. The
  grid above is 26.

### 6.4 You cannot click the cells

**Discord message content is not interactive.** Only buttons and select menus
are, and the ceilings are hard:

- **5 buttons per action row, 5 rows per message → 25 components max.**
- A select menu holds **25 options** max.

7 days × 4 slots = **28 cells**, so a button-per-cell grid is impossible even
before considering readability. The grid is a **display**; selection happens
underneath it: a day dropdown (7 options) plus 4 slot toggle buttons,
re-rendering the ephemeral in place. Two taps to book.

> Note: this removes the main advantage a text matrix was assumed to have over a
> rendered image — an image is equally unclickable. The interaction model is
> identical either way.

### 6.5 Text now, PNG later

Text grid first:

- It **edits in place**, so the pinned board updates silently all week (the same
  trick the load card uses). Swapping an image attachment on an existing message
  is far clunkier and realistically means re-posting — the exact channel spam
  we're avoiding.
- No new bot permission. **A PNG requires the `Attach Files` permission**, which
  the documented invite (View Channels, Send Messages, Embed Links, Read Message
  History) does **not** include — it would need a re-invite.
- No bundled font. **Pillow ships with Home Assistant core**, so drawing is
  cheap, but there is no guaranteed usable TTF on the HA base image, so a PNG
  renderer means vendoring an OFL-licensed font (~200 KB).
- Readable on mobile, in dark mode, and by a screen reader.

The grid data is **one function's return value**, so `render_png(grid)` is a
drop-in second renderer later. This defers the image; it does not preclude it.

Anything heavier — matplotlib, SVG rasterisers, headless browsers — is out of
the question inside HA's process.

---

## 7. Feature 4 — the habit model

### 7.1 The data already exists

Every **Claim** tap is a labelled data point: *user X ran a load at time T*.
`handle_claim` has been capturing `interaction.user.id` since v0.x. Six weeks of
that is a per-person (weekday × slot) histogram, for free.

**This is why the bot never asks randomly.** It doesn't need to.

Two levels of signal:

- **House level** — every load start, no identity. Always available.
- **Person level** — claims only. The useful one for reminders.

If people claim unreliably the model stays thin, stays silent (P6), and the
explicit grid covers the gap. Self-healing.

### 7.2 Confidence gate

A prediction for (person, weekday, slot) requires **all** of:

- ≥ `MIN_OBSERVATIONS` (**3**) loads in that bucket, **and**
- that bucket is ≥ **30 %** of that person's total loads, **and**
- ≥ **4 weeks** of history for that person.

Otherwise: no prediction, no nudge, nothing rendered. Silence is the default.

### 7.3 Corrections are the real training signal

```
🔮 I think you wash Thursday evenings
   (5 of your last 8 loads)

[ ✅ That's right ] [ 📅 Wrong — pick ] [ 🚫 Stop guessing ]
```

The model learns from **explicit corrections and actual claims only** — never
from its own guesses. That's what stops it drifting into confident nonsense.

"Push to tomorrow" on a nudge is a correction too, and does **not** count as the
model being wrong.

---

## 8. Conflict rules — queue vs. reservation

The sharpest question in the design. Bob taps "I'm next"; the load finishes at
19:45, inside Carol's planned Thu Eve slot. **Bob gets it.**

| # | Rule |
|---|------|
| **R1** | **The live queue always beats an unclaimed plan.** Bob tapped a button in real time and is standing there with a basket; Carol wrote something down on Sunday. Nobody waits on a no-show — that would wreck the queue, which is used ten times a week. |
| **R2** | **Inside the queue, the slot holder jumps the line — once.** If Carol *also* queued, she goes ahead of Bob for her own slot. This is the only place the schedule has teeth, and it's safe: it reorders people who are both present and never blocks anyone. |
| **R3** | **The holder is always told, never blocked.** Carol gets: *"Your Thu Eve slot got used — someone was waiting. Next free looks like Fri Mid."* Information, not a veto. |
| **R4** | **Being bumped is training data.** Three bumps on the same slot and the assistant suggests moving it. The plan bends toward reality instead of fighting it. |

The trade broker (§9) is the pressure valve: a conflict resolves into an
anonymous ask rather than a passive-aggressive kitchen note.

### Why not a real booking system

Nothing can enforce a reservation. If Alex books Thu Eve and Kim throws a load
in at 18:45, the bot cannot stop her and must not try. Build a lock that can't be
enforced and the first time it's ignored, trust in the whole board dies.

Both tiers are therefore **information, not permission**: *planned* (someone said
so) vs *expected* (the model guessed) — `▒`/`║` vs `?` since v0.24.0, `▓`/`░`
when this was written. No implied authority. The value
is "huh, three people are aiming for Thursday night" — enough to make one of
them move on their own, which is the mechanism that actually works in a shared
house.

---

## 9. Feature 5 — the trade broker

Double-blind, like a matching system.

1. You tap a taken slot → *"That one's spoken for. Want me to ask?"*
2. Bot DMs the holder, **no name attached**: *"Someone's asking about **Thursday
   Eve**. They'd offer you **Wednesday Eve** in return."*
   `[ ✅ Trade ] [ ❌ Pass ] [ 🚫 Don't ask me again ]`
3. **Accept → both names revealed.** At that point you have to coordinate, and
   you live together.
4. **Pass → requester hears "they passed."** No name, no reason. Nothing to be
   awkward about at breakfast.

Guardrails, so this can never become harassment:

- One request per slot per person per **week**.
- A declined slot cannot be re-asked that week.
- **"Don't ask me again"** is permanent, per requester-pair.
- Trade requests do **not** consume the nudge budget for the *recipient*, but
  the recipient's own per-day DM cap still applies.

---

## 10. Feature 6 — the DM loop

### 10.1 Why DM

Ephemeral can't be sent unprompted (§5.3); a channel ping for a personal
reminder is noise for six other people. So reminders are DMs.

**Enrollment is free** — the bot knows your ID the moment you tap anything.
No roster, no YAML, no config.

### 10.2 Sunday

```
🗓️ Next week's laundry
Based on the last 6 weeks I've got you down for Thursday evening.
Look right?

[ ✅ Yep ] [ 📅 Change ] [ 🔕 Stop asking ]
```

### 10.3 On the day

```
🧺 Laundry day
You're down for tonight, and the washer's free right now.

[ 👍 On it ] [ ⏭ Push to tomorrow ] [ 🚫 Skip this week ]
```

- **On it** → the anonymous board marks the slot taken, so the house knows
  without anyone announcing it.
- **Push to tomorrow** → moves the nudge; not counted as a wrong prediction.
- **Skip** → silent for the week.

### 10.4 Event-driven, not clock-driven

A fixed 18:00 reminder is a guess. The washer **going idle** is a fact. So the
day-of nudge fires on whichever comes first:

- the washer transitions to idle/free during your slot, **or**
- a fallback time near the end of your slot, if it was never busy.

"It's your slot and the machine is free *right now*" is strictly more useful
than "it's 6pm", and the coordinator already knows the washer state.

### 10.5 DM failure — the one real failure mode

If a user has DMs from server members disabled, the send raises
`discord.Forbidden` (**error 50007**) and **they never find out**. So:

1. **Record `dm_ok = False`** for that user.
2. **Fall back to an in-channel mention** so the reminder is not silently lost.
3. **The next time they tap any button**, respond with an ephemeral:

```
⚠️ I couldn't DM you
Two settings control this:
 • Right-click the server icon → Privacy Settings → allow DMs from members
 • User Settings → Privacy → allow direct messages from server members
Until then I'll ping you in the channel instead.
        Only you can see this
```

This reaches the one person who can fix it, costs the channel nothing, and
self-heals the moment they do. **Not** an owner DM — the owner can't fix someone
else's privacy settings, so that's pure noise to them.

*Optional:* a weekly owner digest, sent **only when the set changes** — "2
people have DMs closed: Kim, Ty" — never the same list twice in a row.

---

## 11. Privacy & social guardrails

Seven people sharing a channel. The failure mode is a passive-aggressive
scoreboard.

- History stays local (HA `Store`), never leaves the instance.
- **No stats are ever surfaced to the household.** No streaks, no counts, no
  "who does the most laundry."
- The forward grid is anonymous (P5).
- 🚫 Off is permanent and never nagged.
- Monitoring can be disabled per person — the bot stops logging them entirely.

---

## 12. Data model

Separate `Store` key (`laundry_discord.planner`), independent of the session
store, so a planner bug can never corrupt session state.

```jsonc
{
  "people": {
    "<discord_user_id>": {
      "name": "Alex",              // last seen display name
      "dm_ok": true,               // null = untested, false = Forbidden
      "reminders": "dm",           // dm | channel | off
      "predict": true,
      "monitor": true,
      "onboarded": true,
      "slots": [[3, "eve"], [6, "am"]],   // recurring: [weekday 0-6, slot]
      "paused_until": null,
      "no_trade_from": [],
      "last_nudge_ts": 1754000000,
      "nudges_this_week": 1
    }
  },
  "history": [ {"ts": 1754000000, "user_id": "123"} ],   // capped at 90 days
  "overrides": { "2026-W32": { "3-eve": "123" } },        // one-off, per ISO week
  "trades": [ {"id": "...", "from": "123", "to": "456",
               "want": "3-eve", "offer": "2-eve",
               "ts": 1754000000, "made": 1754000000, "state": "open"} ],
  "nudges": { "123": {"cell": "3-eve", "message": "1401..."} }
}
```

Two fields there earn a note.

`trades[].made` is when the ask was **made**; `ts` is what decides whether it is
still *live*. They are equal on a fresh row and diverge on the two paths that
age a row past its TTL on purpose — a holder-side refusal, which is born lapsed,
and a withdrawal, when the DM could not be delivered. Liveness has to move,
because neither of those may hold the holder's slot or inbox. The **asker's**
own cap must not, or it reports back how many of their asks were really
delivered, which is the holder-side fact the flat refusal exists to hide.

`nudges` is one row per person: which cell that person's last reminder DM was
about, and **which message it was**. Both halves are load-bearing. The heads-up
is sent before its slot opens, so the message's own timestamp can no longer
identify it (19:00 for a 20:00 booking reads as PM), and a note that named only
the person answered for whichever DM was tapped — so an unanswered heads-up from
yesterday acted on today's cell. Persisted rather than held in memory, because a
tap has to keep meaning what the DM said across a restart.

Queue state is **session** state, not planner state, and lives in the existing
session store alongside `claimed_by`:

```jsonc
{ "queue": [ {"id": 123, "name": "Sam", "ts": 1754000000} ],
  "emptied": false }
```

---

## 13. Discord constraints reference

Collected because several of them shaped decisions above.

| Constraint | Value |
|---|---|
| Buttons per action row | 5 |
| Action rows per message | 5 (**25 components max**) |
| Select menu options | 25 |
| Button alignment | left-to-right, in add order; **no right-align** |
| Ephemeral | interaction responses only; not durable; supports attachments |
| Interaction token lifetime | **15 minutes** |
| Emoji in code blocks | **breaks monospace alignment** |
| DM to a known user ID | no privileged intent needed; fails `Forbidden` (50007) if DMs closed |
| Buttons / selects | no privileged intent needed |
| Message Content | **privileged** — avoided entirely |
| Images | require **Attach Files** permission (not in the current invite) |
| Slash commands | require `applications.commands` OAuth scope + a `CommandTree` (the bot is a bare `discord.Client`) — a one-time re-invite |
| Dynamic timestamps | `<t:UNIX:R>` renders live, per-viewer timezone, **no bot edits** |

### Free win, unrelated to this feature

The card's **Estimated finish** could use `<t:UNIX:R>`, which updates in each
client between the 90-second edits and is correct for anyone in another
timezone. Worth doing regardless.

---

## 14. Isolation plan — how this doesn't break what works

The existing detection core is heavily tuned and has been regressed before.
Rules:

1. **`detect.py` is not touched.** Zero edits. All 15 `test_energy_detector.py`
   cases keep passing unmodified.
2. **The stage machine is not extended.** No new `STAGE_*`. "Emptied" is a
   boolean on `DONE_WAITING`.
3. **Same integration** — same `laundry_discord` domain, same config entry, same
   bot connection, one HACS install, one restart. Not a second bot.
4. **Pure logic in its own file**, HA-free and importable by path, exactly like
   `detect.py`, runnable with plain `python3 tests/...`. This is the specific
   discipline that made detection stop breaking; habit maths and queue ordering
   have the same "easy to get subtly wrong, miserable to debug live" quality.
5. **One-way dependency.** The planner reads coordinator state and subscribes to
   a dispatcher signal. `coordinator.py` never imports the planner.
6. **Separate storage keys.** A planner bug cannot corrupt session state.
7. **Feature-flagged, default off** (except the queue, which is default on and
   inert when unused). Anything misbehaves → flip it off in options.

### Known small change to existing code

`DiscordBot._message` is a **single-message cache**, and `async_post`
overwrites it. A second long-lived message (a pinned board) makes it refetch on
every alternation. It becomes a small `dict[int, discord.Message]` with a bound
on size. Needed before any second message is introduced, not after.

---

## 15. Phasing

| Phase | Contents | Touches `coordinator.py`? |
|---|---|---|
| **1 — Queue** | 🔜 I'm next, Next up field, ✅ Emptied it, handoff ping + timed fallback, carry-forward, expiry, pure `queue.py` + tests | **Yes** — the one intrusive phase, kept small and shipped alone |
| **2 — Assistant** | 🤖 button, ephemeral onboarding + settings panel, per-person prefs, DM plumbing incl. the `Forbidden` self-heal | Barely — one button |
| **3 — Grid & plan** | 4-slot week grid, anonymous rendering, day/slot pickers, pinned occupancy board, overrides | No |
| **4 — Model & DMs** | History logging, confidence-gated prediction, Sunday + day-of DMs, event-driven nudge, corrections | No |
| **5 — Trades** | Double-blind broker + guardrails | No |
| **6 — Optional** | PNG renderer, HA `calendar.laundry` mirror, `/laundry` slash command | No |

Phase 1 ships alone and gets proven across several real loads before Phase 2,
because it is the only phase that modifies the working session machine.

---

## 16. Testing

Mirrors the existing convention — pure modules, no pytest, no HA harness,
runnable directly:

```
python3 tests/test_queue.py
python3 tests/test_plan.py
```

Phase 1 cases: toggle add/remove, FIFO order, dedupe, expiry, cap, carry-forward
minus claimant, empty-queue completion, unclaimed-load completion, fallback
timing, R2 slot-holder reordering.

Two of those do not land in Phase 1, deliberately:

- **R2 slot-holder reordering** needs the week grid to have a slot holder at
  all, so it moves to Phase 3 with the rest of the plan layer.
- **Fallback *timing*** is `async_call_later`, i.e. the HA timer rather than
  queue logic, so it stays out of the pure suite. What was pure in it has been
  extracted: `queue.select_handoff` makes the *selection* (expiry, claimant
  exclusion, pop) testable, and both the ✅ and fallback paths go through it.

---

## 17. Open questions

1. **Pinned occupancy board — keep or drop?** *Deferred at Phase 3, not
   dropped.* The renderer already supports it: `render_grid(occupancy)` with no
   `viewer_id` produces the fully anonymous board, which is the only difference
   between the two. What's missing is the plumbing — a persisted message id, a
   refresh on every booking change, and an answer to when a second permanent
   message earns its place in a channel whose whole premise is "one card per
   load". Worth revisiting once the house has used the ephemeral grid for a
   week and knows whether it actually wants passive visibility. Nothing was
   shipped half-built: there is no board option in the config flow.
2. **Slot boundaries** — the §6.1 windows are a first guess; may want to be
   configurable once real usage lands.
3. **HA `calendar.laundry` mirror** — cheap (one service call) and puts the plan
   on the household dashboard. Phase 6 unless it turns out to be the thing
   people actually look at.
4. **Should the queue itself be anonymous?** Currently no (P5). Revisit if the
   live card's naming feels inconsistent with the anonymous grid.
