"""API-side settings for the v2 attempt flow.

Importing pipeline.config runs its .env loader (repo-root .env, never
overrides real env vars), so AWS creds reach boto3 without python-dotenv.
Read these as `config.X` at call time so tests can monkeypatch them.
"""
import os

from pipeline import config as pipeline_config

REPO_ROOT = pipeline_config.REPO_ROOT
ENROLL_DIR = pipeline_config.ENROLL_DIR  # data/enrolled/<user_id>.jpg


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# --- Attempt lifecycle -------------------------------------------------------
ATTEMPT_DEADLINE_MS = int(_num("ATTEMPT_DEADLINE_MS", 1800))  # t0 + this (2 s redesign)
ATTEMPT_TTL_S = _num("ATTEMPT_TTL_S", 120)                     # evict after
CHALLENGE_TIMEOUT_MS = 5000
CHALLENGE_BASELINE_MS = 400
CHALLENGE_ACTIONS = ("SMILE", "OPEN_MOUTH", "TURN_LEFT", "TURN_RIGHT")
# 2 s redesign: one quick action shown at the tap, checked inside the first pass.
QUICK_ACTIONS = ("SMILE", "OPEN_MOUTH")
# Step-up: exactly ONE pose + ONE expression, random order.
POSE_ACTIONS = ("TURN_LEFT", "TURN_RIGHT")
EXPRESSION_ACTIONS = ("SMILE", "OPEN_MOUTH")

# --- Decision gates ------------------------------------------------------------
# Passive-only ALLOW is off unless explicitly enabled. Off -> a clean first
# pass becomes REQUEST_VERIFICATION + CHALLENGE instead of ALLOW.
PASSIVE_ALLOW_ENABLED = _flag("PASSIVE_ALLOW_ENABLED", False)  # gate: stays off; no effect
# Opt-in demo profile. The passive-only gate did not meet the calibration target.
FALLBACK_CHALLENGE_ONLY = _flag("FALLBACK_CHALLENGE_ONLY", False)
# GATE NO-GO: genuine faces scored below threshold 4/10 times. Off -> passive
# SPOOF never causes DENY; passive status/score are logged only.
PASSIVE_DENY_ENABLED = _flag("PASSIVE_DENY_ENABLED", False)
# Quick-path ALLOW needs passive == LIVE (on) or only passive != SPOOF (off).
# UNCERTAIN/SPOOF then go to the 2-action step-up. Never DENYs either way.
QUICK_REQUIRES_PASSIVE_LIVE = _flag("QUICK_REQUIRES_PASSIVE_LIVE", True)
# detect_labels is a logged signal only; it never changes a decision.
LABELS_SIGNAL_ENABLED = _flag("LABELS_SIGNAL_ENABLED", False)

# --- Identity (CompareFaces similarity, 0..100) ------------------------------
ID_MATCH = _num("ID_MATCH", 90)       # >= -> MATCH
ID_MISMATCH = _num("ID_MISMATCH", 50)  # < (face found, adequate quality) -> MISMATCH

# --- Request limits ------------------------------------------------------------
MAX_FRAMES = 16  # ~14 frames at ~10 fps over ~1.4 s
MAX_CHALLENGE_FRAMES = 50  # step-up: ~0.4 s baseline + 4 s at 10 fps
MAX_FRAME_BYTES = 5 * 1024 * 1024  # Rekognition's image-bytes limit

# --- Logging -------------------------------------------------------------------
ATTEMPT_LOG_PATH = os.environ.get(
    "ATTEMPT_LOG_PATH", str(REPO_ROOT / "logs" / "attempts.jsonl"))

# --- v3: schedule / kiosk ----------------------------------------------------------
SCHEDULE_PATH = os.environ.get("SCHEDULE_PATH", str(REPO_ROOT / "data" / "schedule.json"))
KIOSK_ZONE = os.environ.get("KIOSK_ZONE") or None   # None = don't check zone
KIOSK_ID = os.environ.get("KIOSK_ID", "kiosk-01")
DEMO_CLOCK_ENABLED = _flag("DEMO_CLOCK_ENABLED", True)

# --- v3: offline outbox + staff ----------------------------------------------------
OUTBOX_PATH = os.environ.get("OUTBOX_PATH", str(REPO_ROOT / "logs" / "outbox.db"))
MAX_PIN_FAILURES = 5  # per attempt, then 429
