# Per-person control over what the bot sends you

## The problem

The 🤖 panel asks one question — *"how should I reach you?"* — and takes it as the
answer to every question. 📬 **DM me** currently means all of:

| Message | Who starts it | Fires |
|---|---|---|
| Your load is done | you (you tapped 🧺 Claim) | when it finishes |
| The washer's free, you're next | you (you tapped 🔜) | at the handoff |
| Sunday check-in | **the bot** | weekly |
| Slot heads-up | **the bot** | an hour before a slot you booked |
| Opportunity nudge | **the bot** | when you're overdue and a slot is clear |
| Somebody wants your Thursday | **a housemate** | whenever they ask |

Those are six different relationships to your phone, and there is exactly one
switch. Somebody who wants to be told their own load is done, and *nothing else*,
has only two options today: take the lot, or take none of it and lose the one
message they actually wanted.

That is the wrong shape, and it gets worse the more the bot can say. v0.26.0
added two message kinds; a person who dislikes one of them currently has to turn
off the other as well.

## The principle this follows

Design doc P7 already says an opt-out must not be a one-way door. The same
reasoning applies one level down: **the unit somebody opts out of should be the
thing that annoyed them**, not the whole feature it arrived in. A single switch
turns "I don't want the 5am one" into "stop talking to me", and the bot then
loses the ability to tell them their load is done — which nobody asked for.

## Two axes, because the complaints are different

Everything people actually object to is one of two things: *this kind of message*
or *this time of day*. So there are two controls and no more.

### 1. Which kinds — four toggles

| Toggle | Governs | Default |
|---|---|---|
| 📅 **Weekly check-in** | the Sunday DM | on |
| ⏰ **Slot heads-up** | "you're down for tonight, still want it?" | on |
| 💡 **Spare-slot nudge** | "tonight's wide open and you're overdue" | on |
| 🔁 **Swap requests** | a housemate asking for your slot | on |

**All default on**, so an upgrade changes nobody's experience. The house-wide
options (`remind_dms`, `trades`) and the existing per-person consents (📬 DM,
👁 Monitoring, 🔮 guessing) all still gate everything above them — these are a
*narrowing*, never a widening. Nothing here can cause a message that wasn't
already going to be sent.

Note the split between the last two rows and the first two: 🔁 Swap requests are
a **housemate** talking, not the bot. That is why it is a separate toggle from
the others rather than folded into "stop guessing", and why turning off the habit
model must not silence it — somebody who doesn't want to be predicted at is still
a person who can be asked whether they'd swap. `trade.reachable` already argues
exactly this; the toggle sits beside it.

### 2. Quiet hours — one select

Overnight presets rather than a free-form time picker, because Discord has no
time input and a select is the only honest control available:

```
No quiet hours   ·   22:00–08:00   ·   23:00–09:00   ·   00:00–07:00   ·   21:00–09:00
```

Overnight-only is not a limitation, it's the actual complaint. The heads-up
triggers land at **05:00, 11:00, 15:00 and 19:00** — one per slot, an hour before
it opens. The 05:00 one is the only message in the system that can wake somebody
up, and a "quiet before 8am" preset is precisely aimed at it. A midday quiet
window would be a setting nobody would use, and every unused setting is one more
thing a newcomer has to read past.

Windows wrap midnight, so `22 → 8` is stored as-is and the containment test is
`start <= hour or hour < end` when `start > end`. This is the one bit of
arithmetic here worth a test of its own.

## What is deliberately *not* gated

**The done ping and the handoff ping.** Both are answers to something the person
did — they tapped 🧺 Claim, or they tapped 🔜 — and both are time-critical: a
handoff ping held until 08:00 tells somebody the washer was free eight hours ago,
which is worse than not sending it. They stay under `reminders` (📬 / 💬 / 🚫),
which is where "how do you want to be reached about your own laundry" already
lives.

This is the line: **quiet hours and kind toggles govern messages the bot or a
housemate starts. They never govern a reply to something you did.**

## Where it is enforced

One place per path, both of which already exist and already return a reason
string per gate:

- `nudge.eligible(people_map, user_id, moment, kind=None)` — gains the kind and
  the quiet-hours check, with two new reasons (`REASON_KIND_OFF`,
  `REASON_QUIET`) so "why did nothing arrive" stays answerable without adding a
  log line per evaluation.
- `trade.reachable(people_map, user_id, moment)` — gains the 🔁 toggle and the
  same quiet-hours check, via `nudge` so there is one definition of quiet, the
  way `is_paused` is already shared between them.

`kind=None` keeps the current meaning ("may this person be messaged at all"), so
every existing caller and test is unaffected until it opts in by passing a kind.

## Storage

Five new fields on the person record, normalised defensively like every other
field there (§12):

```python
"dm_checkin": True,
"dm_headsup": True,
"dm_opportunity": True,
"dm_trades": True,
"quiet_start": None,   # local hour, 0-23, inclusive
"quiet_end": None,     # local hour, 0-23, exclusive
```

Both quiet fields are None or both are set; a half-set pair reads as "no quiet
hours" rather than as a window with one end missing. `_flag` already handles the
booleans' hazard (a stored `"false"` is truthy, and reading it as True would flip
a consent nobody gave).

## The panel

Row 1 of the 🤖 panel has two free slots (👁 / 📅 / 🔮 of five), so 🔔 goes there
and opens a sub-panel of its own rather than crowding the main one:

```
🔔 What I send you

  [ 📅 Weekly check-in: on ]  [ ⏰ Slot heads-up: on ]
  [ 💡 Spare-slot nudge: on ]  [ 🔁 Swap requests: on ]
  [ Quiet hours: none ▾ ]
  [ ↩️ Back ]
```

Buttons labelled with their current state and toggled by tapping, matching 👁
Monitoring. Each needs its own `custom_id` registered in the `add_view` template
— a button whose id was never registered doesn't error, it silently stops
dispatching, which this integration has been bitten by twice.

## Verification

- `tests/test_people.py` — the five fields, their defaults, the half-set quiet
  pair, and a junk record.
- `tests/test_reminders.py` — each kind gated independently; quiet hours
  including the midnight wrap; `kind=None` unchanged.
- `tests/test_trade.py` — 🔁 off refuses, and refuses *identically* to every
  other holder-side reason (a requester must not learn from a refusal that a
  particular housemate switched swaps off — that is the §11 rule the double-blind
  broker is built on).
- The real-import gate — every new `custom_id` present in the registration
  template, no row over 5.

**The regression that matters:** with every new field left at its default, every
existing test must pass untouched. These settings can only ever *subtract* from
what was already going to be sent.
