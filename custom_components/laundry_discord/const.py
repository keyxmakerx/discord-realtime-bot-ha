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
CONF_ETA_INTERVAL = "eta_interval"
CONF_PING_CLAIMANT_ON_COMPLETE = "ping_claimant_on_complete"
CONF_AVAILABILITY_GRACE = "availability_grace"

# --- Defaults ---
DEFAULT_RUNNING_ENTITY = "binary_sensor.washer_running"
DEFAULT_JOB_STATE_ENTITY = "sensor.washer_washer_job_state"
DEFAULT_ETA_ENTITY = "sensor.washer_washer_completion_time"
DEFAULT_MACHINE_STATE_ENTITY = "sensor.washer_washer_machine_state"
DEFAULT_ENERGY_ENTITY = "sensor.washer_energy"
DEFAULT_WATER_ENTITY = "sensor.washer_water_consumption"

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
# How long (minutes) to keep showing the last-known ETA when the completion
# sensor goes unavailable, so a connection flap never flickers the embed.
DEFAULT_AVAILABILITY_GRACE = 5
MIN_AVAILABILITY_GRACE = 1
MAX_AVAILABILITY_GRACE = 120

# States that mean "I don't know" rather than a real value.
UNAVAILABLE_STATES = {"unavailable", "unknown"}

# --- Platforms ---
PLATFORMS = ["sensor", "binary_sensor"]

# --- Storage ---
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session"

# --- Dispatcher signal ---
SIGNAL_UPDATE = f"{DOMAIN}_update"

# --- Discord ---
CLAIM_CUSTOM_ID = "laundry_discord_claim"
UNCLAIM_CUSTOM_ID = "laundry_discord_unclaim"

# --- Services ---
SERVICE_TEST_POST = "test_post"

# --- Session stages ---
STAGE_IDLE = "idle"
STAGE_WASHING = "washing"
STAGE_DRYING = "drying"
STAGE_DONE_WAITING = "done_waiting"

STAGE_LABELS = {
    STAGE_IDLE: "Idle",
    STAGE_WASHING: "Washing",
    STAGE_DRYING: "Drying",
    STAGE_DONE_WAITING: "Done — waiting",
}

# --- Washer job_state vocabulary (observed on this machine) ---
JOB_STATE_NONE = "none"
JOB_STATE_DRYING = "drying"
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
