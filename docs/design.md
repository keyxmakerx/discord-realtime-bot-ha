# Design notes

Maintainer reference: the rules the code enforces and why, one sentence of
rationale each. No history, no roadmap, no incident narratives — if a comment
would just repeat one of these, cut the comment and point at this file
instead. Roadmap items and known bugs live in
[GitHub issues](https://github.com/keyxmakerx/discord-realtime-bot-ha/issues).

## Principles

| # | Principle | Why |
|---|---|---|
| P1 | Never ask randomly | initiates only on a fixed schedule or a real event, never a spontaneous ping |
| P2 | Hard nudge budget, enforced in code | max 1 DM/person/day, 2/week; over budget is **dropped, not queued** |
| P3 | Presence beats plans | someone acting right now outranks something written down earlier |
| P4 | Learning is visible and correctable | guesses are shown with their arithmetic and can be argued with |
| P5 | Plans are anonymous; the live load is not | the grid shows occupancy only; the card names the claimant, because you need to know who to hand off to |
| P6 | Confidence-gated silence | thin data means the bot says nothing — never a hedge |
| P7 | Additive, reversible, subtractive-only | every behaviour sits behind an option; a new switch can silence a message already being sent, never cause a new one |
| P8 | Speak only when private information is the point | the bot never assumes you have dirty clothes or a free evening |
| P9 | Compare people internally; never surface the findings | the bot may combine everyone's data to decide what to say, but no stats, names or rankings reach the household — a board that can identify or rank people becomes a scoreboard nobody uses |

## Detection

`job_state` is the spine: it reliably reports wash phases, `drying`, and
`finish`. The energy meter is a *fallback* for what `job_state` can't see and
is **never** allowed to time completion while `job_state` is live, because on
this washer it can freeze, reset, or read flat for a whole load.

| Path | Rule | Why |
|---|---|---|
| Fast start | A confirmed early phase (`weight_sensing`/`wash`) starts the load before the meter moves, gated on `confirm_delay` (30s default, 0–300) settled **and** not arriving from `unavailable` | the meter lags 15–45 min; a reconnect replay is settled but not new |
| Fast finish | A confirmed `job_state = finish` completes immediately | on this washer `finish` only ever appears at the true end |
| Completion (normal) | The washer's own `completion_time` has **passed** *and* the meter has been flat for `eta_grace` (fixed 20 min, not configurable) | robust to a meter frozen all load (waits for the estimate) and to `job_state` stuck mid-cycle (estimate drives it, not the phase) |
| — freshness | Only an estimate published *this cycle* is trusted | a value frozen from the previous load must not end the next one early |
| Offline backstop | No `completion_time` → flat energy for `energy_idle` (default 60 min, 10–240) ends the load | the only signal left for a genuinely offline load |
| Absolute cap | `MAX_SESSION_MINUTES` = 720 (12h) force-finishes any tracked load | the last-resort net when every other signal is stuck |
| Mid-cycle catch-up | A phase in `MIDCYCLE_PHASES` only starts a load if energy has risen since the idle baseline | a phase frozen at the last completion reading is stale, not a catch-up |
| Stop on the machine | A confirmed `machine_state → stop`, debounced by `confirm_delay`, ends a tracked load worded "🛑 Stopped early" and retracts it from habit history | otherwise there is no cancel path at all (only the 12h cap), and logging a cancel would skew every nudge |
| Offline load (whole cycle) | A single-sample energy jump ≥ `energy_load_jump` (0.3 kWh default, 0.1–5.0) with no phase reported = a load ran offline, **unless** the machine reports idle/stopped at that moment | the meter batches on reconnect; without the veto, catch-up reads as a phantom load |
| Offline mid-load | After 60 min unavailable the card says "can't verify"; if the last-known ETA already passed, complete at ETA+30min flagged unverified; if it went offline *before* the ETA, only the 12h cap ends it | honest about what the bot can no longer see, without guessing a finish it never saw evidence for |
| Self-clean | Meter starts with no wash phase while the machine reports running → its own message, no claim button, no ping; ends when `running` turns off or `machine_state` stops (confirmed after `confirm_delay`), else via the flat-energy backstop, the offline completion or the 12h cap | it has no claimant to hand off to, so it has its own finish path |
| Flap immunity | A change arriving from `unavailable`/`unknown` (or with no previous state) is a reconnect, not an event (`cancel.is_flap`): it can't arm a stop, and a phase arriving that way can't fast-start a load | this washer's cloud drops roughly hourly and replays the last phase it saw |
| Wrinkle-prevent | An optional sensor marks post-cycle tumbling so it can't re-arm the flat-energy timer | otherwise a finished load looks "alive" for hours |

Card edits (ETA/progress, every `eta_interval`, default 90s) never push a
notification — only a new message does, which is why completion sends one
short `@mention` beside the edited embed instead.

## The card and the 🔜 handoff

- **Done ≠ free.** Completion pings the claimant, never the queue head — a ping
  to a machine still full of laundry stops being trusted after it happens twice.
- **Handoff trigger.** Tapping **✅ Emptied it** pings the queue head. The
  button appears on a claimed load once it finishes and goes away once tapped;
  completion alone never hands the washer over.
- **Backstop.** `handoff_fallback` (default 25 min, 0–240; 0 disables) pings the
  head anyway, hedged ("probably free, nobody confirmed") — the bot genuinely
  doesn't know at that point.
- **Unclaimed completion.** With no claimant to empty it, the queue head is
  pinged immediately instead.
- **Toggle + FIFO.** Tapping 🔜 again while queued leaves the line.
- **Cap: 5** (`QUEUE_CAP`) — past it the button says the line is full rather
  than silently dropping the tap.
- **Expiry.** `queue_expiry` (default 12h, 1–72) drops stale entries *before* a
  handoff is chosen, so the line never strands on someone long gone to bed.
- **Claimant exclusion + pop-on-handoff.** `select_handoff` removes the
  claimant first, then pops whoever it picks — you can't be handed the machine
  you're running, and the backstop can't double-ping the same person.
- **Carry-forward.** The line rolls into the next session minus whoever claimed
  it, so a 3-deep queue (A finishes, B takes it, C is still next) works.
- **The tap answers privately.** A card edit spends the interaction's one
  response, so join/leave confirmation is a followup on the same token — join
  and leave otherwise produce an identical edit and the button reads as broken.
- **Handed-over field.** The done card records who was told and whether it was
  confirmed or hedged — popping them off the queue the instant they're told
  would otherwise make it look like they were never waiting.
- **Not a new stage.** "Emptied" is a boolean on `STAGE_DONE_WAITING`; the stage
  machine itself is never extended.

## Discord mechanics

- **Every `custom_id` is registered in `on_ready`, shown or not** (every view
  built in its argument-free template form) — an id never handed to `add_view`
  doesn't error, it silently stops dispatching, which reads exactly like a
  dead button.
- **One response per interaction.** A card edit spends it; a private reply to
  the same tap is a **followup** on the same token, never a second response.
- **Ephemeral messages exist only in response to an interaction** — never
  unprompted, which is why reminders are DMs. Not durable; do support
  attachments.
- **Interaction tokens expire after 15 minutes.** A tap on a stale one gets a
  brand-new ephemeral instead of a failed edit.
- **Component limits: 5 buttons/row × 5 rows = 25 max; a select holds 25
  options.** The week grid is 7×4=28 cells, so it can't be a clickable table —
  a day dropdown plus 4 slot buttons stands in.
- **No right-alignment** — buttons lay out left-to-right in add order, so
  "rightmost" only ever means "added last".
- **No privileged intent.** DMs to a known id, buttons and selects need none;
  `Message Content` is never requested.

## The 🤖 panel and per-person settings

```
🤖 Your laundry assistant

  Pings             💬 In the channel, with an @mention
  Monitoring        👁 on — when you tap Claim I note the day and time
  What I send you   🔔 all four on · no quiet hours
  Guessing          🔮 on — I'll mark your usual days with ? on your week

[ 📬 DM me ] [ 💬 In the channel ] [ 🚫 No pings ]
[ 👁 Monitoring: on ] [ 📅 My week ] [ 🔔 What I send you ] [ 🔮 Fix a guess ]
        Only you can see this
```

- **Delivery is `dm` / `channel` / `off`.** Default is **`channel`**
  (`people.DEFAULT_REMINDERS`), the same as never opening the panel, so a DM is
  always an opt-in.
- **`dm_ok` is tri-state**: `None` untested, `True` delivered, `False` refused
  (`discord.Forbidden`, 50007) — collapsing "unknown" into a bool would
  misroute the untested case.
- **DM failure self-heals.** A refusal sets `dm_ok=False`; `people.delivery()`
  reroutes that person to the channel automatically, and their next panel
  shows a one-time notice. Choosing DM again resets `dm_ok` to `None` — the
  only signal the bot ever gets that it may work now.
- **Monitoring gates the write, not the read.** `habit.record_load` requires
  `monitor is True` exactly — off means the tap is never logged at all.
- **Four notification kinds** (`people.KINDS`), all default on: check-in,
  heads-up, opportunity, trades. An unrecognised kind reads as **True**
  (`wants_kind`) — a typo can only fail open, never silently mute someone.
- **Quiet hours are both-or-neither** (a half-set pair is *no window*) and
  wrap midnight; the panel offers presets only, because Discord has no time
  input.
- **Kinds and quiet hours only ever subtract** — they gate
  `nudge.eligible(kind=...)` and `trade.reachable()` on top of the existing
  delivery choice, never in place of it; a caller passing no `kind` is
  unaffected.
- **Replies are never gated by kind or quiet hours.** The done ping and the
  handoff ping answer something the person just did and are time-critical;
  they're gated only by delivery mode itself.

## Week grid

- **Four slots a day**: AM 06–12, Mid 12–16, PM 16–20, Eve 20–24 (half-open).
  00:00–06:00 is in no slot.
- **One precedence rule** (`plan.cell_state`), highest wins: yours `█` →
  someone else every week `║` → someone else this week `▒` → running now `*`
  → your own guess `?` → free `·`. A booking always beats a guess; running
  sits below every booking because a booking is the thing you can still act on.
- **Anonymity is structural.** `expected_cells` returns `[]` whenever
  `viewer_id` is `None` — the anonymous board has no code path to a guess, a
  name, or a count.
- **Ephemeral because rendering is per-viewer** — your cells must differ from
  everyone else's, and one shared message can only have one rendering. A
  pinned board reuses the identical renderer with no viewer id.
- **A tap books this week only** (`toggle_booking`, a per-ISO-week override) —
  "Thursday evening" is the only thing anyone can know on a Tuesday. The
  override snapshots the cell's whole holder list, which lets an empty one
  mean "nobody this week" and pins the cell against later changes to anyone's
  standing slots.
- **`♻️` never touches this week's override** — promoting/demoting a standing
  slot is a separate write, so "every week" can't silently mean "except a week
  someone edited".
- **`*` running is derived, never stored**, capped at 4 cells
  (`MAX_RUNNING_CELLS`) so a wedged session can't paint days of everyone's
  grid, and never counts as taken — you can't trade a slot the machine is using.
- **26-char width cap**, ASCII/box-drawing only inside the fence (emoji breaks
  monospace alignment), no ANSI colour (unsupported clients render raw escapes).

## Habit model

- **The only signal is a Claim tap** (`habit.record_load`); nothing derived
  from a prediction can reach history, which is what makes "never learns from
  its own guesses" structural.
- **Confidence gate, all three or nothing**: ≥3 observations in the bucket,
  ≥30% share of retained loads, ≥4 weeks of history (`MIN_OBSERVATIONS` /
  `MIN_SHARE_PERCENT` / `MIN_WEEKS`) — each fails for a different reason
  (barely seen you / wash whenever / haven't watched long enough).
- **Retention: 90 days**, pruned on every write, plus a 1000-row absolute
  ceiling so a future-dated row (an unsynced clock) can't defeat ageing.
- **Dedup: 1 hour** (`LOAD_DEDUPE_SECONDS`) — unclaim/reclaim is a real button
  pair, and two genuine loads an hour apart can't happen on a 4–5h cycle.
- **Corrections are the other training signal.** "Wrong" retires a bucket
  until the person washes there again; "Push to tomorrow" is explicitly *not*
  wrongness — the day was right, they're just busy tonight.
- **Cancelled loads are retracted**, not just unlogged (`habit.forget_load`
  deletes that person's rows inside the cancelled session's own window) — a
  cancel would otherwise skew the predictions driving every nudge.
- **Typical gap is a median** (≥2 gaps), not a mean — one holiday must not
  drag a weekly washer's cadence past ten days.
- **Budget: 1/day, 2/week**, tracked with a day/week key beside each counter
  so a rollover is a string comparison. An unreadable or backwards clock
  **denies**, never grants.
- **Two spend paths.** `claim_nudge` charges both caps (the bot's own
  initiative); `claim_daily_nudge` charges the day cap only (the trade
  broker — see the weekly-budget exception under Swap requests).
- **Consent gates the write, not the arithmetic** — `predict` decides whether
  to *act* on a guess, but `monitor` must gate the write itself, since by
  prediction time the data would already exist.

## Reminder DMs

- **Gate order** (`nudge.eligible`, first failure wins): unreadable moment →
  not opted in → reminders off → not-DM (channel is default, so never-opened
  means nothing) → DMs closed → predict off → monitor off → paused →
  *(once a kind is known)* that kind's switch off → quiet hours.
- **At most one message, chosen** (`nudge.select`). A slot heads-up (a booking
  opening within `HEADS_UP_LEAD_MINUTES`, default 60, 5–180) beats an
  opportunity nudge (overdue by median gap, slot free, opening within the same
  lead) — booking beats guess, as on the grid.
- **Suppressed before the budget is touched**: washer busy, already washed
  *today* (the whole day, not just the slot), already nudged about this exact
  slot (`already_nudged_in_slot`, windowed from the lead, not the slot start).
- **Budget: 1/day, 2/week, dropped not queued.** A bounced DM still spends
  it — refunding it would retry a closed-DM person at every trigger, forever.
- **Never posted in the channel.** Closed DMs mean the reminder is dropped,
  not redirected — a per-person fact broadcast to six others is exactly the
  leak this avoids. (The handoff ping does fall back to the channel — that
  one's about the machine, not a person's habits.)
- **Quiet hours drop, they don't delay** — a heads-up delivered at 8am is
  about a slot that's gone.
- **Whichever trigger fires first wins** — the washer actually coming free, or
  a fixed lead-before-start tick — and the other is dropped, so nobody hears
  about one evening twice.

## Swap requests

**"Someone" is the only word used until an accept**, including in a
refusal — every holder-side reason renders as one identical sentence, because
a refusal that read differently would be a free, repeatable oracle about a
person the requester can't even name.

| Guardrail | Enforced by | Why |
|---|---|---|
| One ask per slot, per requester, per week | `asked_this_week` | re-asking someone who already ignored you is the exact thing this stops |
| A refused slot (declined *or* blocked) is shut to **everyone** that week | `slot_refused` | "no" to one person must not become an invitation to the other five |
| 🚫 Don't ask again — permanent, per requester-pair | `is_blocked` | never revealed to the asker, so a block reads identically to a pass |
| ≤2 asks outstanding per requester | `MAX_OPEN_PER_REQUESTER` | bounds one person's asks, not the house's replies to them |
| ≤1 ask waiting per holder | `MAX_OPEN_PER_HOLDER` | nobody should open Discord to a queue of people wanting their Thursday |
| Must hold the slot you're offering | `REASON_NOT_YOURS` | a swap with nothing on the other side is just a request to give something up |
| The requester must be reachable too | `_delivery_gate` on the requester | the answer only ever arrives as a DM, later |
| Expires 48h unanswered (`REQUEST_TTL_HOURS`) | `is_expired` | a week's plan is worthless a week later |

### Who can be asked

A holder cannot be asked at all if they never opened the 🤖 panel, have
reminders 🚫 off, are on the channel default, have DMs closed, are paused, have
🔁 Swaps switched off, or are inside their quiet hours — and the asker is told
the same flat refusal whatever the reason. (`trade.reachable` /
`_delivery_gate` — verified against the code: these are the only reasons
either function returns, besides an unreadable clock.) Blocked, already
fielding an ask, and over their own daily budget are separate, holder-specific
gates (`trade.check_holder`) layered on top, not part of general reachability.

**Weekly-budget exception.** A swap ask spends only the recipient's **daily**
DM cap (`claim_daily_nudge_for`), never the weekly one. The weekly cap bounds
how often *the bot's own arithmetic* starts a conversation; a housemate's ask
is not the bot's initiative, and charging it to the week would let one swap
silence reminders the person actually opted into.

**Storage nuance.** A request stores two ids, two cells, and two timestamps —
`ts` decides whether it's still *live* (ages past the TTL on a refusal or an
undelivered DM, so a stale ask can't keep blocking the holder), and `made` is
when the ask happened and is what the asker's own 2-outstanding cap counts
against. Neither a refusal nor a bounced DM may keep haunting the holder, but
both must still cost the asker one of their own two.

## Storage

- **Two `Store` keys, one direction**: `laundry_discord.session` (coordinator:
  stage, claimant, queue, emptied flag) and `laundry_discord.planner`
  (assistant: people, overrides, history, corrections, budgets, trades). Kept
  apart so a planner bug can never corrupt a live load; the planner reads
  coordinator state, never the reverse.
- **Normalised on every load.** Records are rebuilt field-by-field from
  defaults, not merged over them, so an old, half-written, or corrupt record
  comes back usable instead of raising inside a button callback.
- **JSON keys are always strings; `interaction.user.id` is an int.** Every
  lookup normalises to `str(id)` first (`person_key`, `same_user`) —
  otherwise a second, empty record appears for someone who already has one.
- **Retention runs on the write/load path, never a sweep job**: history 90
  days + 1000 rows absolute; corrections the same window; trade requests
  pruned to the current ISO week + 250 rows absolute; plan overrides pruned to
  the current week on. Each cap bounds storage by the act of using it.
