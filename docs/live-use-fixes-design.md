# Live-use fixes and the reservation redesign

Design for the changes that came out of running v0.21.0 against the real washer
for the first time. Written before implementation, like
[`rsvp-planner-design.md`](rsvp-planner-design.md), so the tradeoffs are argued
once rather than rediscovered in a bug report.

**Context:** shared house, 6–7 people, one washer/dryer combo. The RSVP planner
(phases 1–5) shipped and installed; the defects below appeared within the first
few loads, and are the kind that only surface when real people start tapping
things.

---

## 1. What live use found

| # | Symptom | Verdict |
|---|---|---|
| 1 | Cancel a wash on the machine and the card hangs | **Bug** — up to 12 hours |
| 2 | Recurring reservations can't be made | **Gap** — the write path doesn't exist |
| 3 | Tapping 🔜 does nothing visible | **Bug** — three separate causes |
| 4 | The grid can't be read at a glance | **Design flaw** — wrong visual encoding |

Plus a design question raised directly: *should "I'm next" and an upcoming
scheduled wash behave differently?* They should, and §4 answers it.

---

## 2. The cancel bug

`detect.py` has exactly three paths to `EV_FINISHED`:

```python
job_is_finish                                   # job_state == 'finish'
has_eta and eta_passed and flat_for >= grace    # the ETA gate
elif not has_eta and flat_for >= idle_timeout   # offline backstop
```

Cancel on the machine and:

- `job_state` goes to `none`, **not** `finish` → path 1 dead.
- `completion_time` still holds the *planned* finish, still in the future, so
  `eta_passed` is False → path 2 blocked.
- Because `has_eta` is still True, that `elif` means path 3 is **unreachable**.

Only `MAX_SESSION_MINUTES = 720` ends it. `machine_state == stop` is currently
passed to the detector solely as `machine_idle`, which vetoes a *start* — it can
never finish a load.

**There is no "the human cancelled" path at all.** That is a design gap rather
than a tuning problem, and it is the highest-priority item: it affects the
detection core that everything else rests on, and it is the one thing here that
was already working before the planner existed.

### The fix

A confirmed `machine_state → stop` (or `running → off`) during a tracked load
completes it, debounced through the existing `_schedule_job_confirm` /
`_async_job_confirmed` pattern in `coordinator.py` so a connection flap on a
washer that drops hourly cannot kill a live wash.

Constraints:

- **The ETA gate is not weakened.** It is what stops a frozen meter firing a
  false "done"; the new path is additive and keyed off a different signal.
- Worded *"looks like this load was stopped"*, not "done" — the bot should not
  claim a cycle completed when it knows it didn't.
- **A cancelled load must not feed the habit model.** A cancel is not a wash,
  and logging it would skew the predicted times that drive every nudge.
- A `laundry_discord.reset_session` service as a manual escape hatch, for when
  detection wedges some other way. Waiting 12 hours is not a recovery plan.

---

## 3. The nudge philosophy

The bot must never tell somebody to do their laundry. It should speak only when
**its private information is the point** — a fact you cannot see from your
bedroom.

| The bot knows | Worth saying? |
|---|---|
| the machine is free right now | ✅ |
| nobody has booked your usual slot | ✅ |
| your booked slot is about to pass unused | ✅ |
| you have dirty clothes / a free evening | ❌ never assume |

So: **one message at most, choosing the single most useful thing to say** —
never four independent triggers racing each other, each spending its own budget.

### Suppression — silence is the default

Dropped, never queued, if any hold:

- you washed within **your own** typical gap (learned from history, not a fixed
  number of days)
- the machine is busy
- you already heard from the bot today
- you said "not this week" for that slot
- the model has no confident opinion

### The message set

**Retire** the current in-slot day-of nudge. It fires *after* your slot has
already started, which is too late to act on, and it is fully superseded.

| Message | Fires | Buttons |
|---|---|---|
| **Slot heads-up** | ~1 h before *your reserved slot*, machine free | `On it` · `Free it up` · `Push to tomorrow` |
| **Opportunity** | past your own usual gap AND your usual slot is clear now | `On it` · `Not this week` |
| **Sunday check-in** *(exists)* | weekly, only with a confident prediction | `Yep` · `Change` · `Stop asking` |

> **`Free it up` is the most valuable button here**, and it serves the *house*
> rather than the person. A reservation about to lapse unused is exactly the
> capacity the grid exists to reclaim. It also means a nudge somebody ignores
> still does something useful — the slot frees itself rather than silently
> blocking a cell all evening.

Budget is unchanged: **1 per person per day, 2 per week**, already enforced in
`habit.claim_nudge_for`. What changes is that the *content* is chosen
dynamically instead of several triggers competing.

---

## 4. The four relationships to the machine

Conflating any two produces spam or silence.

| State | Means | When | Gets |
|---|---|---|---|
| **Claimed** | this running load is mine | now | the done ping *(works)* |
| **In line 🔜** | I'm waiting, right now | now | handoff ping *(works, invisible)* |
| **Reserved** | I said I'd wash then | future | **nothing — the gap** |
| **Expected `?`** | the model's guess | future | opportunity nudge only |

`nudge.current_cell` is explicitly *"right now"*; `in_slot_now` gates both
triggers; `CONF_NUDGE_LEAD` is a lead before the slot **ends**. So booking
Thursday Eve does nothing until you are already inside the window *and* the
washer happens to be free at that moment.

A reservation is a **stated fact** — it should be the strongest proactive signal
in the system and is currently the weakest.

### "I'm next" is invisible — three causes

1. **No personal feedback.** The success path of `_NextUpButton` only edits the
   shared card; joining and leaving look identical to the tapper. `queue.position`
   already exists and is unit-tested with **no production caller**.
2. **The handoff pops you off the line**, so the "Next up" field vanishes and it
   reads as though the tap never happened. The done card should name who was told.
3. **Nothing links a 📅 reservation to the live card**, so a booking is invisible
   in the channel where everyone actually looks.

---

## 5. The visual system

**Shape encodes KIND. Weight encodes WHOSE.** The current `░ ▓ █` set is three
steps of one shading ramp — a ramp encodes *magnitude*, but these are
*categories* (a guess, someone's booking, your booking). That mismatch is why
it can't be read, and it gets worse the moment a fifth state exists.

```
      Mo Tu We Th Fr Sa Su
AM     ·  ·  ▒  ·  ·  █  ·
Mid    ·  ·  *  ?  ·  ·  ·
PM     █  ·  ·  ▒  ·  ·  ║
Eve    ·  ▒  ·  █  ·  ▒  ·

█ yours   ▒ taken   ║ taken, every week
? a guess   * running now   · free
```

| Glyph | Code point | Meaning |
|---|---|---|
| `·` | U+00B7 | free |
| `?` | ASCII | the model's guess — yours only, never on a shared view |
| `▒` | U+2592 | someone else's, this week only |
| `║` | U+2551 | someone else's, **every week** |
| `█` | U+2588 | yours |
| `*` | ASCII | running right now |

**Width verified:** all six are single-width, and `▒`/`║` share the same East
Asian Width class (`A`) as `·`/`▓`/`█`, which already render correctly on the
household's clients — so they behave identically. `?` and `*` are narrow ASCII.
None collide with the day (`Mo Tu We Th Fr Sa Su`) or slot (`AM Mid PM Eve`)
labels.

ANSI colour code blocks were considered and rejected: clients that don't support
them render the raw escape sequences as visible garbage, and seven people means
mixed devices.

Cadence is shown for *other people's* slots because it changes whether asking
for a trade is worthwhile — a standing commitment is less likely to move. Your
own cadence goes in the "Yours this week" text rather than a seventh glyph.

### Migration hazards

- `░` is hard-coded in three user-visible strings outside `plan.py`
  (`assistant.py:1568`, `:2180`, `:2231`). Changing `CELL_EXPECTED` alone leaves
  them describing a glyph that no longer exists.
- `guessed` is detected by substring — `plan_mod.CELL_EXPECTED in grid`
  (`assistant.py:1549`). Should become an explicit flag.
- Grid slot buttons are binary (`success` = mine, `secondary` = everything
  else), so free / taken / expected share one appearance on the button row.
- `effective_week` flattens recurring + overrides into `{cell: [ids]}` with no
  provenance, so the two are byte-identical downstream. Its return shape has to
  carry the source for `║` to be possible at all.

### Colour collisions worth fixing while here

- Grey `0x95A5A6` means both **"claimed"** and **"idle"**.
- Blurple `0x5865F2` serves **all five** assistant embeds, so grid, ask-to-swap,
  welcome, settings and guess are colour-indistinguishable.

---

## 6. Phases

Ordering is a dependency chain, not a preference.

### Phase 1 — the cancel fix
`coordinator.py`, `detect.py`, `__init__.py`. Ships alone. §2 above.

### Phase 2 — queue visibility
`discord_bot.py`, `coordinator.py`, `sensor.py`. Ephemeral confirmation on tap
reusing `queue.position`; the done card names who was told; the line exposed as
an attribute on `sensor.laundry_stage` so it can reach a dashboard.

### Phase 3 — the glyph and colour redesign
`plan.py`, `assistant.py`. §5 above, including provenance in `effective_week`.
Must land before Phase 4 so the grid can express the new states.

### Phase 4 — reservations become real
`plan.py`, `assistant.py`, `nudge.py`, `reminders.py`. Pure-module-then-wire, as
with every previous phase.

- **Writable recurring slots.** `person["slots"]` is defaulted, normalised and
  read — only the writer is missing.
- **Live occupancy (`*`)**, derived from coordinator state on each render and
  **never stored**, so it vanishes when the load ends. Only the slots the ETA
  actually overlaps. Must not count as `is_taken_by_other` — you cannot trade a
  slot the machine is using. **This is why Phase 1 comes first:** a hung session
  would black out most of a day on everyone's grid.
- **A time axis** — a today marker in `render_grid`, and `describe_cells` sorted
  soonest-first. It currently sorts Monday-first with no past/future
  distinction, so on a Friday "Yours this week" leads with Monday's already-past
  slot presented identically to a live one.
- **The slot heads-up and `Free it up`**, replacing the in-slot nudge.
- **The opportunity nudge**, gated on the learned gap.

### Deferred deliberately

Design-doc §8 rules **R2/R3/R4** (slot holder jumps the line; holder told their
slot got used; three bumps suggests moving it) remain unimplemented. R2 couples
the queue to the plan, and it is worth watching the two behave separately for a
real week before wiring them together.

---

## 7. Verification

Pure logic first, as established — every new rule lands in a module with no HA
or discord imports and a plain-`python3` test file.

```
python3 tests/test_plan.py       # glyphs, provenance, time axis
python3 tests/test_queue.py      # position, handoff display
python3 tests/test_nudge.py      # suppression, message selection
python3 tests/test_habit.py tests/test_detect.py tests/test_energy_detector.py
```

**Regression gates:**

- `detect.py`'s 15 tests and `test_energy_detector.py`'s 16 pass unmodified.
- Log inventory stays at **0 info, 1 warning** — normal operation silent.
- No per-tick Store writes.
- The real-import check: install `discord.py` + `homeassistant`, import every
  module, build every view, assert all `custom_id`s registered and no action row
  exceeds five components.

**End-to-end, on the real machine:**

1. Start a load, **cancel it on the washer** — the card closes within ~1 minute,
   not 12 hours.
2. Tap 🔜 — an ephemeral "you're 2nd in line" appears.
3. Let a load finish and tap ✅ — the done card still names who was told.
4. Open 📅 mid-load — running slots show `*`; a recurring booking shows `║`
   where a one-off shows `▒`.
5. Book a slot an hour out — the heads-up arrives with `Free it up`.
