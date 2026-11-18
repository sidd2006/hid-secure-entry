"""Pass 2: random-action challenge check (CONTRACT v2 challenge_check).

challenge_check(frames, ts, prompt_ts, actions)
  -> {status: PASS|FAIL|UNCERTAIN, reason, per_action, passive, tracking, ms}

frames    JPEG bytes (or BGR arrays), covering ~baseline_ms before the prompt
          and up to timeout_ms after it
ts        capture timestamps (ms) per frame, same clock as prompt_ts
prompt_ts when the instruction was shown
actions   ordered list, e.g. ["SMILE", "TURN_LEFT"]; from SMILE, OPEN_MOUTH,
          TURN_LEFT, TURN_RIGHT

Metrics per frame, from MediaPipe Tasks FaceLandmarker (478 landmarks + blendshapes):
  SMILE      = mean(mouthSmileLeft, mouthSmileRight)
  OPEN_MOUTH = jawOpen
  TURN_*     = yaw = (nose_tip_x - eye_midpoint_x) / inter-ocular distance
               (landmarks 1, 33, 263). TURN_LEFT is the VISITOR's left: +yaw on raw
               (unmirrored) frames; set CH_MIRRORED=1 if frames arrive mirrored.
               A flat print turned on its axis keeps this ratio roughly constant.

Rules:
  - baseline = median metric over frames with ts < prompt_ts (needs CH_MIN_BASELINE_FRAMES)
  - an action is DONE at the first frame after prompt_ts where the metric exceeds
    baseline + delta (sign-aware for turns) for CH_MIN_HOLD_FRAMES consecutive frames,
    within CH_TIMEOUT_MS of the prompt
  - its onset must be >= prompt_ts + CH_MIN_REACTION_MS (a change that is already under
    way when the prompt appears is not a response to it)
  - actions must happen in the given order: action k is only searched for from the
    onset of action k-1, and its rise must start then or later (a pose already held
    from earlier doesn't count), so doing them out of order is not seen
  - the same single face must be tracked through every frame
  - passive anti-spoof still runs on quality-selected frames. It never causes FAIL
    (no DENY from passive: genuine faces fell below threshold 4/10 times), but a
    passive SPOOF turns a completed challenge into UNCERTAIN (reception), so
    passive evidence can only block ALLOW

Status:
  FAIL      an action that "started" before the prompt could have been seen
            (pre-recorded response)
  PASS      every action done in order, tracking intact, passive not SPOOF
  UNCERTAIN anything else (no baseline, tracking lost, action not seen in order and
            in time, landmarker unavailable). Not doing the action is treated as
            unresolved, not as a spoof. Callers route this to reception, never ALLOW.

allow_eligible = (status == "PASS"). Gate decision 2026-11-14 (passive-only NO-GO):
a challenge PASS is required before any ALLOW.
"""
from __future__ import annotations

import hashlib
import statistics
import threading
import time
from collections import OrderedDict

import cv2
import numpy as np

from . import config as C
from .frames import decode

ACTIONS = ("SMILE", "OPEN_MOUTH", "TURN_LEFT", "TURN_RIGHT")
NOSE, EYE_R_OUTER, EYE_L_OUTER = 1, 33, 263  # MediaPipe face mesh indices

_lm = None
_lm_err: Exception | None = None
_lm_lock = threading.Lock()      # guards creation
_detect_lock = threading.Lock()  # guards detect(): the task graph is not re-entrant


def _landmarker():
    """IMAGE-mode FaceLandmarker with blendshapes, created once."""
    global _lm, _lm_err
    if _lm is None and _lm_err is None:
        with _lm_lock:
            if _lm is None and _lm_err is None:
                try:
                    from mediapipe.tasks import python as mpt
                    from mediapipe.tasks.python import vision
                    opts = vision.FaceLandmarkerOptions(
                        base_options=mpt.BaseOptions(model_asset_path=str(C.FACE_LANDMARKER_TASK)),
                        output_face_blendshapes=True,
                        num_faces=2,  # so a second face is noticed, not ignored
                        running_mode=vision.RunningMode.IMAGE,
                    )
                    _lm = vision.FaceLandmarker.create_from_options(opts)
                except Exception as e:
                    _lm_err = e
    if _lm is None:
        raise RuntimeError(f"FaceLandmarker unavailable: {_lm_err}")
    return _lm


def face_metrics(img_bgr: np.ndarray) -> dict:
    """-> {n_faces, smile, jaw, yaw, bbox} for the first face (metrics None if no face)."""
    import mediapipe as mp
    rgb = np.ascontiguousarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    lm = _landmarker()
    with _detect_lock:
        r = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    n = len(r.face_landmarks)
    out = {"n_faces": n, "smile": None, "jaw": None, "yaw": None, "bbox": None}
    if n == 0:
        return out
    lms = r.face_landmarks[0]
    h, w = img_bgr.shape[:2]
    xs = [p.x * w for p in lms]
    ys = [p.y * h for p in lms]
    out["bbox"] = [int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))]
    bs = {c.category_name: c.score for c in (r.face_blendshapes[0] if r.face_blendshapes else [])}
    if bs:
        out["smile"] = (bs.get("mouthSmileLeft", 0.0) + bs.get("mouthSmileRight", 0.0)) / 2
        out["jaw"] = bs.get("jawOpen", 0.0)
    eye_mid = (xs[EYE_R_OUTER] + xs[EYE_L_OUTER]) / 2
    iod = float(np.hypot(xs[EYE_L_OUTER] - xs[EYE_R_OUTER], ys[EYE_L_OUTER] - ys[EYE_R_OUTER]))
    if iod > 1:
        out["yaw"] = (xs[NOSE] - eye_mid) / iod
    return out


_mcache: "OrderedDict[str, dict]" = OrderedDict()
_mcache_lock = threading.Lock()
_MCACHE_MAX = 64


def metrics_for(frame, img_bgr: np.ndarray) -> dict:
    """face_metrics(img), memoised by frame bytes so quality() and challenge_check()
    (which run on the same burst, often in parallel) don't landmark a frame twice."""
    key = hashlib.sha1(frame).hexdigest() if isinstance(frame, (bytes, bytearray)) else None
    if key is not None:
        with _mcache_lock:
            if key in _mcache:
                _mcache.move_to_end(key)
                return _mcache[key]
    m = face_metrics(img_bgr)
    if key is not None:
        with _mcache_lock:
            _mcache[key] = m
            while len(_mcache) > _MCACHE_MAX:
                _mcache.popitem(last=False)
    return m


def cached_metrics(frame) -> dict | None:
    """Landmarks already computed for these frame bytes (by quality()), else None."""
    if not isinstance(frame, (bytes, bytearray)):
        return None
    key = hashlib.sha1(frame).hexdigest()
    with _mcache_lock:
        return _mcache.get(key)


def landmarker_ok() -> bool:
    try:
        _landmarker()
        return True
    except Exception:
        return False


def _metric(m: dict, action: str) -> float | None:
    if action == "SMILE":
        return m["smile"]
    if action == "OPEN_MOUTH":
        return m["jaw"]
    if m["yaw"] is None:
        return None
    yaw = -m["yaw"] if C.CH_MIRRORED else m["yaw"]
    return yaw if action == "TURN_LEFT" else -yaw  # "rises" in the asked direction


def _iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _passive_on(frames, ts):
    """Passive anti-spoof on quality-selected frames (selection never uses liveness)."""
    try:
        from .passive import passive_liveness
        from .quality import select_frames
        idx = select_frames(frames, ts)
        if not idx:
            return {"status": "UNCERTAIN", "score": None, "reason": "NO_USABLE_FRAMES"}
        p = passive_liveness([frames[i] for i in idx])
        p["frames_used"] = idx
        return p
    except Exception as e:
        return {"status": "UNAVAILABLE", "score": None, "reason": str(e)[:200]}


def challenge_check(frames: list, ts: list[float], prompt_ts: float, actions: list[str]) -> dict:
    t0 = time.perf_counter()

    def done(status, reason, per_action=None, passive=None, tracking=None):
        return {"status": status, "reason": reason, "allow_eligible": status == "PASS",
                "per_action": per_action or [],
                "passive": passive, "tracking": tracking,
                "ms": int((time.perf_counter() - t0) * 1000)}

    actions = [a.upper() for a in (actions or [])]
    if not actions or any(a not in ACTIONS for a in actions):
        return done("UNCERTAIN", f"BAD_ACTIONS {actions}")
    if not frames or len(ts) != len(frames):
        return done("UNCERTAIN", "NO_FRAMES_OR_TS_MISMATCH")

    order = sorted(range(len(frames)), key=lambda i: ts[i])
    frames = [frames[i] for i in order]
    ts = [float(ts[i]) for i in order]

    # thin to ~CH_CHECK_FPS by capture time only (never by content) to stay inside the budget
    n_in = len(frames)
    if C.CH_CHECK_FPS > 0:
        gap = 0.85 * 1000.0 / C.CH_CHECK_FPS  # 0.85: tolerate timestamp jitter
        keep, last = [], None
        for i, t in enumerate(ts):
            if last is None or t - last >= gap:
                keep.append(i)
                last = t
        frames = [frames[i] for i in keep]
        ts = [ts[i] for i in keep]

    # passive runs in its own thread while the landmarker works (both release the GIL);
    # same inputs and same result as running it afterwards, just inside the time budget
    _pas: dict = {}
    _pas_thread = threading.Thread(target=lambda: _pas.update(p=_passive_on(frames, ts)), daemon=True)
    _pas_thread.start()

    try:
        mets = []
        for f in frames:
            m = cached_metrics(f)  # quality() ran first: no second decode or landmark pass
            if m is not None:
                mets.append(m)
                continue
            img = decode(f)
            mets.append(metrics_for(f, img) if img is not None else
                        {"n_faces": 0, "smile": None, "jaw": None, "yaw": None, "bbox": None})
    except Exception as e:
        return done("UNCERTAIN", f"LANDMARKER_UNAVAILABLE: {e}"[:200])

    # same single face throughout
    ious = [_iou(a["bbox"], b["bbox"]) for a, b in zip(mets, mets[1:]) if a["bbox"] and b["bbox"]]
    tracking = {
        "frames": len(mets),
        "frames_in": n_in,
        "face_frames": sum(1 for m in mets if m["n_faces"] >= 1),  # diagnostics only
        "face_px": int(statistics.median([m["bbox"][3] for m in mets if m["bbox"]]))
        if any(m["bbox"] for m in mets) else None,
        "single_face_all": all(m["n_faces"] == 1 for m in mets),
        "min_iou": round(min(ious), 3) if ious else None,
    }
    tracking["ok"] = tracking["single_face_all"] and all(i >= C.Q_MIN_IOU for i in ious)

    _pas_thread.join()
    passive = _pas.get("p") or {"status": "UNAVAILABLE", "score": None, "reason": "PASSIVE_THREAD_FAILED"}

    per_action = []
    search_from = prompt_ts  # each action is searched for after the previous one started
    for a in actions:
        vals = [_metric(m, a) for m in mets]
        base_vals = [v for v, t in zip(vals, ts) if t < prompt_ts and v is not None]
        rec = {"action": a, "baseline": None, "threshold": None, "peak": None,
               "onset_ms": None, "done": False, "pre_prompt": False}
        if len(base_vals) >= C.CH_MIN_BASELINE_FRAMES:
            base = statistics.median(base_vals)
            thr = base + C.CH_DELTA[a]
            rec["baseline"], rec["threshold"] = round(base, 3), round(thr, 3)
            post = [(v, t) for v, t in zip(vals, ts)
                    if t >= prompt_ts and t - prompt_ts <= C.CH_TIMEOUT_MS]
            rec["peak"] = round(max((v for v, _ in post if v is not None), default=float("nan")), 3) \
                if post else None
            # first run of >= CH_MIN_HOLD_FRAMES frames above threshold whose rising edge is at
            # or after the previous action's onset (a run already under way doesn't count)
            run = 0
            for k, (v, t) in enumerate(post):
                run = run + 1 if (v is not None and v > thr) else 0
                start_t = post[k - run + 1][1] if run else None
                if run >= C.CH_MIN_HOLD_FRAMES and start_t >= search_from:
                    rec["onset_ms"] = round(start_t - prompt_ts)
                    rec["pre_prompt"] = rec["onset_ms"] < C.CH_MIN_REACTION_MS
                    rec["done"] = not rec["pre_prompt"]
                    search_from = start_t
                    break
        per_action.append(rec)

    # --- verdict (the challenge decides; passive SPOOF can only hold back a PASS) ---
    if any(r["pre_prompt"] for r in per_action):
        return done("FAIL", "ACTION_BEFORE_PROMPT", per_action, passive, tracking)
    if any(r["baseline"] is None for r in per_action):
        return done("UNCERTAIN", "NO_BASELINE", per_action, passive, tracking)
    if not tracking["ok"]:
        return done("UNCERTAIN", "TRACKING_LOST", per_action, passive, tracking)
    if all(r["done"] for r in per_action):
        if passive.get("status") == "SPOOF":  # never DENY on passive, but never ALLOW over it
            return done("UNCERTAIN", "PASSIVE_SPOOF_REVIEW", per_action, passive, tracking)
        return done("PASS", "CHALLENGE_PASSED", per_action, passive, tracking)
    return done("UNCERTAIN", "ACTION_NOT_SEEN", per_action, passive, tracking)


def warmup() -> bool:
    try:
        face_metrics(np.full((480, 640, 3), 128, np.uint8))
        return True
    except Exception:
        return False
