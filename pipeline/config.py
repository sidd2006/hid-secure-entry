"""Settings and tunable thresholds for the liveness pipeline.

Credentials come from the environment / repo-root .env only (CONTRACT.md).
Nothing here is a secret.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, never overrides real env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


_load_dotenv(REPO_ROOT / ".env")

# Region comes from the environment / .env (sandbox is us-east-1).
# ap-south-1 is only a last-resort fallback when nothing is set.
AWS_REGION = (os.environ.get("AWS_DEFAULT_REGION")
              or os.environ.get("AWS_REGION")
              or "ap-south-1")

# Enrolled reference photos: <ENROLL_DIR>/<user_id>.jpg|.jpeg|.png
ENROLL_DIR = Path(os.environ.get("ENROLL_DIR", REPO_ROOT / "data" / "enrolled"))

# --- Timing -----------------------------------------------------------------
BUDGET_MS = 2000
# How long we wait for the parallel Rekognition calls before giving up.
REKOGNITION_DEADLINE_S = 1.7
CONNECT_TIMEOUT_S = 2.0  # integrator fix 14 Nov: 1.6 s read cut off slow-network calls
READ_TIMEOUT_S = 3.0     # (identity is still bounded by the attempt deadline / step-up)

# --- Contract-fixed ---------------------------------------------------------
SIMILARITY_THRESHOLD = 99  # CONTRACT.md hard rule

# --- Spoof labels (detect_labels on the full frame) -------------------------
SCREEN_LABELS = {"Mobile Phone", "Screen", "Electronics"}
PRINT_LABELS = {"Paper", "Picture", "Poster"}
LABEL_MIN_CONFIDENCE = 70.0     # at/above: weak spoof signal -> step-up
LABEL_STRONG_CONFIDENCE = 90.0  # at/above: strong spoof signal -> DENY

# --- Face quality (detect_faces, Attributes=ALL) ----------------------------
MIN_SHARPNESS = 20.0
MIN_BRIGHTNESS = 30.0
MAX_BRIGHTNESS = 95.0
MAX_POSE_DEG = 30.0  # |yaw| and |pitch|

# --- Motion (mean abs pixel diff across the burst, 0..1) --------------------
MOTION_SIZE = (64, 64)
MIN_MOTION = 0.004  # ~1 grey level; calibrate on real kiosk captures


# --- Local quality gate (pipeline/quality.py) --------------------------------
# All env-overridable so they can be tuned without code changes.
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


Q_MIN_FACE_PX = _f("Q_MIN_FACE_PX", 90)        # shorter side of the face box, frame pixels
# Q_MIN_BLUR: Laplacian variance of the face crop resized to 160x160. Default from the live
# kiosk webcam (14 Nov, demo_page genuine attempts): lowest genuine frame 9.8-11.1, typical
# 15-30 -> ~half the lowest genuine value. 30 rejected real people (QUALITY_BLUR).
Q_MIN_BLUR = _f("Q_MIN_BLUR", 5)
Q_MIN_BRIGHTNESS = _f("Q_MIN_BRIGHTNESS", 60)  # mean grey (0-255) of the face crop (whole frame if no face)
Q_MIN_IOU = _f("Q_MIN_IOU", 0.3)               # bbox IoU between consecutive frames
Q_MIN_USABLE = int(_f("Q_MIN_USABLE", 2))      # usable frames needed for the burst to be ok
SELECT_MAX = int(_f("SELECT_MAX", 4))          # select_frames picks at most this many

# --- Passive liveness (pipeline/passive.py) ----------------------------------
# Silent-Face live_score thresholds. Model scores, NOT probabilities.
# LIVE only if min score >= HIGH; SPOOF if median <= LOW; else UNCERTAIN.
# Placeholders until set from the Step 1 gate run (python -m pipeline.spoof_gate).
PASSIVE_HIGH = _f("PASSIVE_HIGH", 0.80)
PASSIVE_LOW = _f("PASSIVE_LOW", 0.30)

# Gate decision 2026-11-14: NO-GO for passive-only. Passive LIVE is never enough for ALLOW;
# a challenge_check PASS is required first. Keep at 1; 0 exists only for experiments.
REQUIRE_CHALLENGE = os.environ.get("REQUIRE_CHALLENGE", "1") != "0"

# --- Pass 2 challenge (pipeline/challenge.py) --------------------------------
# Metrics: SMILE = mean(mouthSmileLeft, mouthSmileRight); OPEN_MOUTH = jawOpen
# (MediaPipe blendshapes, 0..1); TURN_* = yaw = (nose_x - eye_mid_x) / inter-ocular.
CH_DELTA = {
    "SMILE": _f("CH_DELTA_SMILE", 0.35),
    "OPEN_MOUTH": _f("CH_DELTA_OPEN_MOUTH", 0.30),
    "TURN_LEFT": _f("CH_DELTA_TURN", 0.12),
    "TURN_RIGHT": _f("CH_DELTA_TURN", 0.12),
}
CH_MIN_REACTION_MS = _f("CH_MIN_REACTION_MS", 150)  # onset earlier than this after the prompt = not a reaction
CH_TIMEOUT_MS = _f("CH_TIMEOUT_MS", 5000)
CH_MIN_HOLD_FRAMES = int(_f("CH_MIN_HOLD_FRAMES", 2))  # consecutive frames above threshold
CH_MIN_BASELINE_FRAMES = int(_f("CH_MIN_BASELINE_FRAMES", 2))
# challenge_check thins its input to about this many frames per second before running the
# landmarker (30 fps input took ~3.7 s to check). 0 = use every frame.
CH_CHECK_FPS = _f("CH_CHECK_FPS", 10)
# Frames reaching the pipeline are NOT mirrored (raw camera). Visitor turning to THEIR left
# moves the nose to image right (+yaw). Set CH_MIRRORED=1 if the browser sends mirrored frames.
CH_MIRRORED = os.environ.get("CH_MIRRORED", "0") == "1"
FACE_LANDMARKER_TASK = Path(os.environ.get(
    "FACE_LANDMARKER_TASK", REPO_ROOT / "pipeline" / "third_party" / "face_landmarker.task"))

# --- Pass 1 flat-sheet motion (pipeline/flatsheet.py) ------------------------
FS_MIN_FACE_MOTION = _f("FS_MIN_FACE_MOTION", 0.04)  # face displacement / face width needed to judge
FS_RING_SCALE = _f("FS_RING_SCALE", 1.9)             # ring = face box scaled by this, minus the face
FS_ATTACH_TOL = _f("FS_ATTACH_TOL", 0.25)            # ring point "moves with face" if error < tol * face motion
FS_FLAT_RATIO = _f("FS_FLAT_RATIO", 0.6)             # >= this share of moving-with-face ring points -> FLAT
FS_NOT_FLAT_RATIO = _f("FS_NOT_FLAT_RATIO", 0.3)     # <= this -> NOT_FLAT
FS_MIN_POINTS = int(_f("FS_MIN_POINTS", 12))
