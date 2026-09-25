"""Constants for the Laundry Discord Bot integration."""

from __future__ import annotations

DOMAIN = "laundry_discord"
DEVICE_NAME = "Laundry"

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

# --- Default washer entities ---
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

# Phases that mean a load is already well underway (a mid-cycle catch-up).
MIDCYCLE_PHASES = {"rinse", "spin", "drying", "finish"}

# --- Tunables: default / min / max ---
# Seconds between ETA/progress edits of the live card.
DEFAULT_ETA_INTERVAL = 90
MIN_ETA_INTERVAL = 30
MAX_ETA_INTERVAL = 3600
DEFAULT_PING_CLAIMANT_ON_COMPLETE = True
# Seconds a job_state or stop signal must persist before it is acted on.
DEFAULT_CONFIRM_DELAY = 30
MIN_CONFIRM_DELAY = 0
MAX_CONFIRM_DELAY = 300
# Minutes of flat energy that end a load. Only the offline backstop: an online
# load completes on job_state 'finish' or on the washer's own estimate.
DEFAULT_ENERGY_IDLE = 60
MIN_ENERGY_IDLE = 10
MAX_ENERGY_IDLE = 240
# Minutes before a tracked load that never ended is force-finished.
MAX_SESSION_MINUTES = 720
# Minutes the washer must be offline mid-load before the card says so and an
# offline completion is allowed.
OFFLINE_NOTICE_MINUTES = 60
# Minutes past the last-known ETA before an offline load is completed
# (flagged unverified).
OFFLINE_COMPLETE_GRACE_MINUTES = 30
# Minutes to keep showing the last ETA while the completion sensor is unavailable.
DEFAULT_AVAILABILITY_GRACE = 5
MIN_AVAILABILITY_GRACE = 1
MAX_AVAILABILITY_GRACE = 120
# kWh rise in a single sample (with job_state dark) that counts as a load run
# while the washer's cloud was offline. Above standby creep, below a real load.
DEFAULT_ENERGY_LOAD_JUMP = 0.3
MIN_ENERGY_LOAD_JUMP = 0.1
MAX_ENERGY_LOAD_JUMP = 5.0
# Minutes after a claimed load finishes before the next person in line is
# pinged anyway (hedged) if the claimant never taps "Emptied it". 0 disables.
DEFAULT_HANDOFF_FALLBACK = 25
MIN_HANDOFF_FALLBACK = 0
MAX_HANDOFF_FALLBACK = 240
# Hours an "I'm next" entry stays in the line.
DEFAULT_QUEUE_EXPIRY = 12
MIN_QUEUE_EXPIRY = 1
MAX_QUEUE_EXPIRY = 72
# Show the 🤖 button on the card. Inert until tapped.
DEFAULT_SHOW_ASSISTANT = True
# Log Claim taps and guess each person's usual slots. Off by default; each
# person can still opt out with 👁 Monitoring.
DEFAULT_LEARN_HABITS = False
# Let the bot DM people unprompted (weekly check-in, slot heads-up). Off by
# default; also needs learn_habits and the person choosing 📬 DM me.
DEFAULT_REMIND_DMS = False
# Weekly check-in: weekday (0 = Monday) and local time.
DEFAULT_PLAN_DM_WEEKDAY = 6
DEFAULT_PLAN_DM_TIME = "18:00:00"
# Minutes before the START of a booked slot that its heads-up is sent.
DEFAULT_NUDGE_LEAD = 60
MIN_NUDGE_LEAD = 5
MAX_NUDGE_LEAD = 180
# Let housemates ask each other, anonymously, to swap slots. Off by default.
DEFAULT_TRADES = False

# States that mean "I don't know" rather than a real value.
UNAVAILABLE_STATES = {"unavailable", "unknown"}

# --- Platforms ---
PLATFORMS = ["sensor", "binary_sensor", "button", "number", "switch"]

# --- Storage ---
# The session store is rewritten on every meter sample; per-person planner data
# lives in its own store so a planner bug can't corrupt a live load.
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session"
PLANNER_STORAGE_VERSION = 1
PLANNER_STORAGE_KEY = f"{DOMAIN}.planner"

# --- Dispatcher signals ---
# Per entry: f"{SIGNAL_UPDATE}_{entry_id}".
SIGNAL_UPDATE = f"{DOMAIN}_update"
# Sent at the handoff moments only (✅ Emptied it, the handoff backstop, an
# unclaimed completion). Carries {"handed_off", "hedged", "claimant_id"}.
SIGNAL_WASHER_FREE = f"{DOMAIN}_washer_free"

# hass.data key naming the one config entry that runs the reminder loop. The
# planner store is shared, so a second entry must not send every DM twice.
DATA_REMINDER_OWNER = f"{DOMAIN}_reminder_owner"

# --- Discord custom_ids ---
# Every id below must be registered through a persistent view in on_ready,
# whether or not its button is currently shown: an unregistered custom_id
# doesn't error, it silently stops dispatching after a restart.
CLAIM_CUSTOM_ID = "laundry_discord_claim"
UNCLAIM_CUSTOM_ID = "laundry_discord_unclaim"
QUIET_CUSTOM_ID = "laundry_discord_quiet"
NEXT_CUSTOM_ID = "laundry_discord_next"
EMPTIED_CUSTOM_ID = "laundry_discord_emptied"
ASSISTANT_CUSTOM_ID = "laundry_discord_assistant"
# 🤖 panel
PANEL_DM_CUSTOM_ID = "laundry_discord_panel_dm"
PANEL_CHANNEL_CUSTOM_ID = "laundry_discord_panel_channel"
PANEL_OFF_CUSTOM_ID = "laundry_discord_panel_off"
PANEL_MONITOR_CUSTOM_ID = "laundry_discord_panel_monitor"
# 📅 week grid. Keyed by plan.SLOTS; a mismatch raises at startup.
PANEL_WEEK_CUSTOM_ID = "laundry_discord_panel_week"
GRID_DAY_CUSTOM_ID = "laundry_discord_grid_day"
GRID_BACK_CUSTOM_ID = "laundry_discord_grid_back"
GRID_SLOT_CUSTOM_IDS = {
    "am": "laundry_discord_grid_am",
    "mid": "laundry_discord_grid_mid",
    "pm": "laundry_discord_grid_pm",
    "eve": "laundry_discord_grid_eve",
}
GRID_RECUR_CUSTOM_ID = "laundry_discord_grid_recur"
# 🔮 Fix a guess
PANEL_GUESS_CUSTOM_ID = "laundry_discord_panel_guess"
GUESS_RIGHT_CUSTOM_ID = "laundry_discord_guess_right"
GUESS_WRONG_CUSTOM_ID = "laundry_discord_guess_wrong"
GUESS_OFF_CUSTOM_ID = "laundry_discord_guess_off"
GUESS_BACK_CUSTOM_ID = "laundry_discord_guess_back"
# 🔔 What I send you. Keyed by people.KINDS; a mismatch raises at startup.
PANEL_NOTIFY_CUSTOM_ID = "laundry_discord_panel_notify"
NOTIFY_KIND_CUSTOM_IDS = {
    "checkin": "laundry_discord_notify_checkin",
    "slot": "laundry_discord_notify_slot",
    "opportunity": "laundry_discord_notify_opportunity",
    "trades": "laundry_discord_notify_trades",
}
NOTIFY_QUIET_CUSTOM_ID = "laundry_discord_notify_quiet"
NOTIFY_BACK_CUSTOM_ID = "laundry_discord_notify_back"
# Reminder DM replies
PLAN_YES_CUSTOM_ID = "laundry_discord_plan_yes"
PLAN_CHANGE_CUSTOM_ID = "laundry_discord_plan_change"
PLAN_STOP_CUSTOM_ID = "laundry_discord_plan_stop"
NUDGE_ON_IT_CUSTOM_ID = "laundry_discord_nudge_on_it"
NUDGE_PUSH_CUSTOM_ID = "laundry_discord_nudge_push"
NUDGE_SKIP_CUSTOM_ID = "laundry_discord_nudge_skip"
NUDGE_FREE_CUSTOM_ID = "laundry_discord_nudge_free"
# 🔁 Swap requests. Nothing per-request fits in a persistent custom_id, so a
# tap is matched to its request by recipient and DM timestamp
# (trade.match_request).
TRADE_ASK_CUSTOM_ID = "laundry_discord_trade_ask"
TRADE_OFFER_CUSTOM_ID = "laundry_discord_trade_offer"
TRADE_SEND_CUSTOM_ID = "laundry_discord_trade_send"
TRADE_BACK_CUSTOM_ID = "laundry_discord_trade_back"
TRADE_ACCEPT_CUSTOM_ID = "laundry_discord_trade_accept"
TRADE_PASS_CUSTOM_ID = "laundry_discord_trade_pass"
TRADE_BLOCK_CUSTOM_ID = "laundry_discord_trade_block"

# --- Services ---
SERVICE_TEST_POST = "test_post"
SERVICE_RESET_SESSION = "reset_session"
SERVICE_DIAGNOSTICS = "diagnostics"
SERVICE_TRACK_LOAD = "track_load"

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
