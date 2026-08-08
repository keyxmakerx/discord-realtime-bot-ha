"""Constants for the Laundry Discord Bot integration."""

from __future__ import annotations

DOMAIN = "laundry_discord"

# --- Config / option keys ---
CONF_BOT_TOKEN = "bot_token"
CONF_CHANNEL_ID = "channel_id"
CONF_RUNNING_ENTITY = "running_entity"
CONF_JOB_STATE_ENTITY = "job_state_entity"
CONF_ETA_ENTITY = "eta_entity"
CONF_MACHINE_STATE_ENTITY = "machine_state_entity"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_WATER_ENTITY = "water_entity"
CONF_WRINKLE_ENTITY = "wrinkle_entity"
CONF_ETA_INTERVAL = "eta_interval"
CONF_CONFIRM_DELAY = "confirm_delay"
CONF_ENERGY_IDLE = "energy_idle"
CONF_PING_CLAIMANT_ON_COMPLETE = "ping_claimant_on_complete"
CONF_AVAILABILITY_GRACE = "availability_grace"
CONF_ENERGY_LOAD_JUMP = "energy_load_jump"
CONF_HANDOFF_FALLBACK = "handoff_fallback"
CONF_QUEUE_EXPIRY = "queue_expiry"
CONF_SHOW_ASSISTANT = "show_assistant"
CONF_LEARN_HABITS = "learn_habits"
CONF_REMIND_DMS = "remind_dms"
CONF_PLAN_DM_WEEKDAY = "plan_dm_weekday"
CONF_PLAN_DM_TIME = "plan_dm_time"
CONF_NUDGE_LEAD = "nudge_lead"
CONF_TRADES = "trades"

# --- Defaults ---
DEFAULT_RUNNING_ENTITY = "binary_sensor.washer_running"
DEFAULT_JOB_STATE_ENTITY = "sensor.washer_washer_job_state"
DEFAULT_ETA_ENTITY = "sensor.washer_washer_completion_time"
DEFAULT_MACHINE_STATE_ENTITY = "sensor.washer_washer_machine_state"
DEFAULT_ENERGY_ENTITY = "sensor.washer_energy"
DEFAULT_WATER_ENTITY = "sensor.washer_water_consumption"
DEFAULT_WRINKLE_ENTITY = "binary_sensor.washer_wrinkle_prevent_active"

# machine_state vocabulary (run / pause / stop / unavailable).
MACHINE_RUN = "run"
MACHINE_PAUSE = "pause"
MACHINE_STOP = "stop"

# Phases that indicate a load is already well underway (=> "in progress" wording
# / treated as a mid-cycle catch-up rather than a fresh start).
MIDCYCLE_PHASES = {"rinse", "spin", "drying", "finish"}
DEFAULT_ETA_INTERVAL = 90
MIN_ETA_INTERVAL = 30
MAX_ETA_INTERVAL = 3600
DEFAULT_PING_CLAIMANT_ON_COMPLETE = True
# Seconds a job_state transition must persist before we act on it (mirrors the
# `for: 30s` in the proven HA automations — confirms it isn't a transient).
DEFAULT_CONFIRM_DELAY = 30
MIN_CONFIRM_DELAY = 0
MAX_CONFIRM_DELAY = 300
# Minutes the energy meter must be FLAT (no kWh increase) before a tracked cycle
# is considered done — the reliable completion signal when this washer's
# job_state/machine_state freeze. Must exceed the active low-power update gap.
# Minutes the energy meter must be flat to finish a load — used ONLY as the
# offline backstop, when the washer's own completion estimate is unavailable. A
# normal (online) load completes on that estimate + a short settle, or on
# job_state='finish', not on this.
DEFAULT_ENERGY_IDLE = 60
MIN_ENERGY_IDLE = 10
MAX_ENERGY_IDLE = 240
# Absolute safety net (minutes): force-finish a tracked load that has somehow
# neither hit 'finish' nor had its completion estimate pass, so a stuck session
# (e.g. an estimate frozen in the future) can't live forever.
MAX_SESSION_MINUTES = 720
# Minutes the washer must be continuously unavailable (cloud/device offline)
# during a tracked load before we (a) flag the card as offline/unverifiable and
# (b) allow an offline completion.
OFFLINE_NOTICE_MINUTES = 60
# Extra minutes past the last-known ETA before completing a load while offline —
# a cushion in case the dry ran a bit long. The completion is marked unverified.
OFFLINE_COMPLETE_GRACE_MINUTES = 30
# How long (minutes) to keep showing the last-known ETA when the completion
# sensor goes unavailable, so a connection flap never flickers the embed.
DEFAULT_AVAILABILITY_GRACE = 5
MIN_AVAILABILITY_GRACE = 1
MAX_AVAILABILITY_GRACE = 120
# kWh the energy meter must rise in a SINGLE sample (while job_state is dark) to
# count as a load that ran entirely while the washer's cloud was offline — its
# telemetry arrives as one batch step when the cloud reconnects. Set above any
# standby / wrinkle-prevent creep step and below a real load's total, so neither
# slow creep nor a meter reset can false-trigger it.
DEFAULT_ENERGY_LOAD_JUMP = 0.3
MIN_ENERGY_LOAD_JUMP = 0.1
MAX_ENERGY_LOAD_JUMP = 5.0
# Minutes after a load finishes before whoever is next in line gets pinged
# anyway, when the claimant never taps "Emptied it". A finished washer is not
# an *empty* washer — the claimant's clothes are still in it — so the handoff
# ping normally waits for that tap. People forget, hence the backstop; its
# wording hedges because at that point we genuinely don't know. 0 disables it.
DEFAULT_HANDOFF_FALLBACK = 25
MIN_HANDOFF_FALLBACK = 0
MAX_HANDOFF_FALLBACK = 240
# Hours a "I'm next" entry stays in the line. Someone who tapped last night and
# went to bed shouldn't be pinged at 6am, and a line that never empties would
# strand every future handoff.
DEFAULT_QUEUE_EXPIRY = 12
MIN_QUEUE_EXPIRY = 1
MAX_QUEUE_EXPIRY = 72
# Whether the 🤖 button rides along on the card. On by default: the panel is
# inert until somebody taps it (no pings, no channel lines, no stored record)
# and it is the only place a newcomer or a guest can find out what the buttons
# do. Turning it off hides the button entirely — existing prefs are kept.
DEFAULT_SHOW_ASSISTANT = True
# Whether the habit model runs at all: logging a Claim tap to history, and
# drawing ? on somebody's own week where it thinks they usually wash.
#
# **Default off**, per design doc §14 rule 7 — every new behaviour is behind an
# option and starts off, so anything misbehaving is one toggle away from gone.
# The 🤖 panel is the documented exception ("inert when unused"); this is not
# inert, because it writes to a store on every claim.
#
# Off means off at both ends: no history rows are written and no guess is
# rendered, so a house that never turns it on never accumulates a byte of it.
# Per-person consent is a *second*, independent gate on top of this one (the 👁
# Monitoring toggle, §11) — the house switch can never override somebody's no.
DEFAULT_LEARN_HABITS = False
# Whether the bot may start a conversation. This is the ONLY option in the
# integration that makes it contact somebody who didn't tap anything first —
# everything else here answers a button or edits a card — so it is off by
# default (design doc §14 rule 7 / P7) and off means *inert*: with it off no
# time trigger is registered, no dispatcher listener is connected, nothing is
# evaluated and nothing is written.
#
# It is also not sufficient on its own. A reminder needs, on top of this: the
# house's day-learning option on, and the person to have chosen 📬 DM in the
# 🤖 panel, with 🔮 guessing and 👁 monitoring left on, and not be paused. The
# default for somebody who never opened the panel is the channel, so switching
# this on for a house that has never used the panel sends exactly nothing.
DEFAULT_REMIND_DMS = False
# When the Sunday plan DM goes out (design doc §10.2) — weekday 0=Monday, and a
# local wall-clock time. Sunday evening by default: late enough that the week is
# actually over, early enough to still be a reasonable hour to get a phone
# buzz. One trigger, once a week, per person.
DEFAULT_PLAN_DM_WEEKDAY = 6
DEFAULT_PLAN_DM_TIME = "18:00:00"
# How long before the END of somebody's slot the day-of backstop fires (§10.4
# trigger b), in minutes. Not a fixed clock time: a single 18:00 reminder cannot
# be "near the end of that slot" for four different slots, and it is useless to
# the person who washes on Saturday mornings. One time per slot is derived from
# this — with the default that is 11:00, 15:00, 19:00 and 23:00 — and each one
# lands inside the window it belongs to, which is what lets the backstop and the
# washer-freed event be the same decision asked twice.
DEFAULT_NUDGE_LEAD = 60
MIN_NUDGE_LEAD = 5
MAX_NUDGE_LEAD = 180
# Whether somebody can ask for a slot another person has (design doc §9). The
# second option in this integration that lets the bot contact somebody who
# didn't tap anything first — and the only one where the *cause* is another
# housemate rather than the bot's own arithmetic — so it is off by default
# (§14 rule 7 / P7), and off means inert: no 🔁 button is rendered, no request
# is ever written and no DM can be produced.
#
# It is not sufficient on its own either. A trade DM needs, on top of this: the
# recipient to have chosen 📬 DM in the 🤖 panel and not be paused, the asker to
# hold a slot of their own to offer, and every §9 guardrail to be clear — one
# ask per slot per person per week, no re-asking a slot somebody was refused
# that week, no pair that has used 🚫, one ask in flight per person either way,
# and the recipient's 1-DM-a-day budget. Turning it on for a house where nobody
# has opened the panel sends exactly nothing.
DEFAULT_TRADES = False

# States that mean "I don't know" rather than a real value.
UNAVAILABLE_STATES = {"unavailable", "unknown"}

# --- Platforms ---
PLATFORMS = ["sensor", "binary_sensor"]

# --- Storage ---
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session"
# Per-person preferences live in their OWN store, deliberately not alongside the
# session (design doc §12): the session store is rewritten on every meter
# sample and holds the live load, so a bug in the planner half must not be able
# to corrupt it. Its own version too, so the two schemas can move separately.
PLANNER_STORAGE_VERSION = 1
PLANNER_STORAGE_KEY = f"{DOMAIN}.planner"

# --- Dispatcher signals ---
SIGNAL_UPDATE = f"{DOMAIN}_update"
# "The washer is now free" — emitted at the moments the coordinator ALREADY
# treats as the handoff: the ✅ Emptied tap, the handoff fallback timer, and an
# unclaimed completion (all three funnel through one method). Deliberately not a
# second notion of "free": if the reminder loop invented its own, the two would
# drift and the day-of nudge would start telling people the machine is empty
# when the queue was told nothing of the sort. The dependency runs one way —
# :mod:`coordinator` sends and never imports the listener (§14 rule 5).
#
# Carries one dict describing what actually happened, because "the washer came
# free" alone is not enough to say anything true about it:
#   ``handed_off``  — the head of the 🔜 line was just given this machine, so it
#                     is not free for anybody else.
#   ``hedged``      — the backstop fired and nobody confirmed anything; the
#                     machine is *probably* free, which is not a fact.
#   ``claimant_id`` — whose load this was, so a listener does not tell somebody
#                     to do the laundry they have just taken out.
SIGNAL_WASHER_FREE = f"{DOMAIN}_washer_free"

# Which config entry owns the reminder loop. The planner Store key is global
# (one household, one set of people), so a second entry — the config flow keys
# on the *channel*, so two channels are two entries — would otherwise run a
# second copy of the loop against the same people and send every reminder DM
# twice, each entry claiming the budget against its own in-memory copy.
DATA_REMINDER_OWNER = f"{DOMAIN}_reminder_owner"

# --- Discord ---
CLAIM_CUSTOM_ID = "laundry_discord_claim"
UNCLAIM_CUSTOM_ID = "laundry_discord_unclaim"
# Toggle: when on, the claimant is named in plain text at completion instead of
# being @mentioned — a visible "done" with no push (for when they're asleep).
QUIET_CUSTOM_ID = "laundry_discord_quiet"
# Join/leave the "I'm next" line — get pinged when the washer is actually free.
NEXT_CUSTOM_ID = "laundry_discord_next"
# The claimant confirming they've pulled their load out, which is what releases
# the machine to whoever is next (completion alone doesn't — see the handoff
# fallback above).
EMPTIED_CUSTOM_ID = "laundry_discord_emptied"
# The 🤖 assistant: opens a private (ephemeral) panel — the first-time explainer
# for somebody who's never used the channel, the settings panel for everybody
# else. Added last on the card so it renders rightmost; Discord lays buttons out
# in add order and has no right-align.
ASSISTANT_CUSTOM_ID = "laundry_discord_assistant"
# The panel's own buttons. Every one of these must be handed to add_view or it
# silently stops dispatching after a restart — the panel is ephemeral, but its
# components dispatch through the same persistent-view registry.
PANEL_DM_CUSTOM_ID = "laundry_discord_panel_dm"
PANEL_CHANNEL_CUSTOM_ID = "laundry_discord_panel_channel"
PANEL_OFF_CUSTOM_ID = "laundry_discord_panel_off"
PANEL_MONITOR_CUSTOM_ID = "laundry_discord_panel_monitor"
# The 📅 week grid, opened from the panel. Same registry rule as above: the
# grid's message is ephemeral, but its components dispatch through the
# persistent-view registry, so every id here goes into a view handed to
# add_view. The four slot ids are a mapping keyed by plan.SLOTS — a key that
# didn't match would raise the moment the registration template is built, at
# startup, rather than dying quietly in somebody's panel a week later.
PANEL_WEEK_CUSTOM_ID = "laundry_discord_panel_week"
GRID_DAY_CUSTOM_ID = "laundry_discord_grid_day"
GRID_BACK_CUSTOM_ID = "laundry_discord_grid_back"
GRID_SLOT_CUSTOM_IDS = {
    "am": "laundry_discord_grid_am",
    "mid": "laundry_discord_grid_mid",
    "pm": "laundry_discord_grid_pm",
    "eve": "laundry_discord_grid_eve",
}
# ♻ Every week — promotes the cell you just tapped to a standing weekly slot,
# or demotes it back. Registered unconditionally like 🔁 next to it: it is only
# *shown* when there is a booking of yours to promote, and a button whose id was
# never handed to add_view does not error, it silently stops dispatching.
GRID_RECUR_CUSTOM_ID = "laundry_discord_grid_recur"
# 🔮 Fix a guess (design doc §7.3) — the panel that shows what the habit model
# thinks, and the three ways to answer it. Same registry rule again: all five
# ids go into views handed to add_view, including the 🔮 button itself, which is
# hidden while day-learning is off but must still dispatch the moment it is
# switched on rather than waiting for the next restart.
PANEL_GUESS_CUSTOM_ID = "laundry_discord_panel_guess"
GUESS_RIGHT_CUSTOM_ID = "laundry_discord_guess_right"
GUESS_WRONG_CUSTOM_ID = "laundry_discord_guess_wrong"
GUESS_OFF_CUSTOM_ID = "laundry_discord_guess_off"
GUESS_BACK_CUSTOM_ID = "laundry_discord_guess_back"
# The reminder DMs' own buttons (design doc §10.2 / §10.3). A DM is a normal
# message, so its components dispatch through the same persistent-view registry
# as everything else — and these are the ids most likely to be tapped hours
# after the message was sent, which is exactly when an unregistered one shows
# the user "interaction failed". They are registered unconditionally, even with
# the reminder option off: a DM already sitting in somebody's inbox must still
# be able to say "stop asking".
PLAN_YES_CUSTOM_ID = "laundry_discord_plan_yes"
PLAN_CHANGE_CUSTOM_ID = "laundry_discord_plan_change"
PLAN_STOP_CUSTOM_ID = "laundry_discord_plan_stop"
NUDGE_ON_IT_CUSTOM_ID = "laundry_discord_nudge_on_it"
NUDGE_PUSH_CUSTOM_ID = "laundry_discord_nudge_push"
NUDGE_SKIP_CUSTOM_ID = "laundry_discord_nudge_skip"
# The trade broker (design doc §9). Same registry rule as everything else: all
# seven ids go into views handed to add_view, registered unconditionally even
# with trades switched off — a request DM already sitting in somebody's inbox
# must still be answerable, and "🚫 Don't ask me again" is the last button in
# this integration that should ever say "interaction failed".
#
# Nothing per-request can go in a custom_id: a per-request id cannot be a
# persistent view, so the button would die at the next restart. Which request a
# tap answers is resolved from the recipient plus the DM's own timestamp
# (``trade.match_request``), which is why only one ask may be waiting on one
# person at a time.
TRADE_ASK_CUSTOM_ID = "laundry_discord_trade_ask"
TRADE_OFFER_CUSTOM_ID = "laundry_discord_trade_offer"
TRADE_SEND_CUSTOM_ID = "laundry_discord_trade_send"
TRADE_BACK_CUSTOM_ID = "laundry_discord_trade_back"
TRADE_ACCEPT_CUSTOM_ID = "laundry_discord_trade_accept"
TRADE_PASS_CUSTOM_ID = "laundry_discord_trade_pass"
TRADE_BLOCK_CUSTOM_ID = "laundry_discord_trade_block"

# --- Services ---
SERVICE_TEST_POST = "test_post"
# The manual escape hatch: force-close a session the bot has got stuck on and
# return it to idle. Detection ends a load on its own in every case it knows
# about, and the absolute net is MAX_SESSION_MINUTES above — 12 hours, which is
# not a recovery plan for the failure nobody predicted.
SERVICE_RESET_SESSION = "reset_session"

# --- Session stages ---
STAGE_IDLE = "idle"
STAGE_WASHING = "washing"
STAGE_DRYING = "drying"
STAGE_DONE_WAITING = "done_waiting"
STAGE_SELF_CLEAN = "self_clean"

STAGE_LABELS = {
    STAGE_IDLE: "Idle",
    STAGE_WASHING: "Washing",
    STAGE_DRYING: "Drying",
    STAGE_DONE_WAITING: "Done — waiting",
    STAGE_SELF_CLEAN: "Self-clean",
}

# --- Washer job_state vocabulary (observed on this machine) ---
JOB_STATE_NONE = "none"
JOB_STATE_DRYING = "drying"
JOB_STATE_FINISH = "finish"
JOB_STATE_WEIGHT_SENSING = "weight_sensing"
# Real wash phases. A transition INTO "none" from one of these means "finished".
REAL_PHASES = {"weight_sensing", "wash", "rinse", "spin", "drying", "finish"}
# old_state values that indicate a flap / startup, never a real phase transition.
INVALID_OLD_STATES = {"unavailable", "unknown", "none"}

# Progress bar: ordered (label, {job_state values that map to this phase}).
PROGRESS_PHASES = [
    ("Wash", {"weight_sensing", "wash"}),
    ("Rinse", {"rinse"}),
    ("Spin", {"spin"}),
    ("Dry", {"drying"}),
]

UNCLAIMED = "Unclaimed"
