"""HTTP API: v2 attempt flow + v1 /verify, all decided by api.decide.decide().

Run from the repo root:  uvicorn api.main:app --port 8000

Nothing in the decision path calls pipeline.liveness.fuse() or motion.py.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, wait
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, identity, mode, outbox, schedule, signals, sync_worker
from .attempt_log import late_result, write_decision
from .decide import decide, decide_staff, message_for
from .store import STORE, Attempt, identity_snapshot, now_ms

log = logging.getLogger("api")
_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="api")
CHALLENGE_UPLOAD_GRACE_MS = 2000
CHALLENGE_IDENTITY_TIMEOUT_S = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    identity.load_enrolled()   # reference bytes, once
    identity.init_client()     # shared boto3 client, once (keep-alive pool)
    identity.warm_up()         # throwaway CompareFaces: pay TLS setup now
    await asyncio.to_thread(signals.warm_up)  # load passive + landmarker models now
    await asyncio.to_thread(_warm_face_path)   # blendshapes + MiniFASNet on a real face
    stubs = signals.stubs_in_use()
    if stubs:
        log.warning("STUB pipeline functions in use (never ALLOW): %s", stubs)
    outbox.get()                               # open/create logs/outbox.db
    await asyncio.to_thread(mode.refresh)      # first health check
    stop = asyncio.Event()
    task = asyncio.create_task(sync_worker.run_forever(stop))
    warm = asyncio.create_task(_keep_identity_warm(stop))
    yield
    stop.set()
    await task
    warm.cancel()


async def _keep_identity_warm(stop: asyncio.Event) -> None:
    """Keep the Rekognition HTTPS connection open between attempts (ONLINE only)."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass
        if mode.current() == "ONLINE":
            await asyncio.to_thread(identity.keep_warm_once)


def _warm_face_path() -> None:
    """The gray-image warm-ups never find a face, so the blendshape graph and the
    MiniFASNet crop path stayed cold until the first real attempt. Run the first-pass
    pipeline once on an enrolled reference (in memory; nothing logged or kept)."""
    ids = identity.enrolled_ids()
    ref = identity.reference_for(ids[0]) if ids else None
    if not ref:
        return
    try:
        t = time.perf_counter()
        _first_pass_pipeline([ref, ref, ref], [0, 100, 200], 150, "SMILE")
        logging.getLogger("uvicorn.error").info(
            "face-path warm-up in %d ms", int((time.perf_counter() - t) * 1000))
    except Exception:
        log.exception("face-path warm-up failed")


app = FastAPI(title="HID liveness", lifespan=lifespan)
from .enrollment import router as enrollment_router
app.include_router(enrollment_router)

# Local kiosk front-ends (Shesh's /web dev server, any port) may call the API.
# Only http://localhost:* and http://127.0.0.1:* -- no other origins.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --- helpers ------------------------------------------------------------------

def _read_frames(files: list[UploadFile], max_frames: int | None = None) -> list[bytes]:
    max_frames = max_frames or config.MAX_FRAMES
    if not 1 <= len(files) <= max_frames:
        raise HTTPException(400, f"send 1-{max_frames} frames")
    data = [f.file.read() for f in files]
    if any(not b or len(b) > config.MAX_FRAME_BYTES for b in data):
        raise HTTPException(400, "each frame must be a non-empty image under 5 MB")
    return data


def _parse_ts(values: list[str] | None, n: int) -> list[int]:
    """Accept repeated fields or one comma-separated field. Pads with 0s."""
    out: list[int] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                try:
                    out.append(int(float(part)))
                except ValueError:
                    raise HTTPException(400, "capture_ts_ms must be integers")
    return (out + [0] * n)[:n]


OFFLINE_IDENTITY = {"identity": "UNKNOWN", "similarity": None, "ms": 0, "note": "offline"}


def _emit(attempt_id: str, user_id: str, d, sig: dict, method: str,
          operator_id: str | None = None) -> None:
    """Queue one decision event for sync. Never breaks the decision path."""
    try:
        outbox.get().add(attempt_id=attempt_id, user_id=user_id, decision=d[0],
                         reason_code=d[1],
                         policy_version=(sig.get("schedule") or {}).get("policy_version"),
                         verification_method=method, operator_id=operator_id)
    except Exception:
        log.exception("outbox write failed for attempt %s", attempt_id)


def _get_attempt(attempt_id: str) -> Attempt:
    at = STORE.get(attempt_id)
    if at is None:
        raise HTTPException(404, "unknown attempt_id")
    return at


def _labels_future(frame: bytes):
    """Optional, flag-gated detect_labels. Logged only; never decides."""
    if not config.LABELS_SIGNAL_ENABLED:
        return None
    from pipeline.liveness import _call_labels, get_client
    return _EXECUTOR.submit(lambda: _call_labels(get_client(), frame))


def _labels_result(fut) -> list | None:
    if fut is None or not fut.done():
        return None
    try:
        return fut.result(timeout=0)
    except Exception:
        return None


def _quality_and_passive(frames: list[bytes], ts: list[int]) -> tuple[dict, dict, dict]:
    t = time.perf_counter()
    q = signals.quality(frames)
    t_q = time.perf_counter()
    idx = signals.select_frames(frames, ts)  # quality + spacing only
    p = signals.passive_liveness([frames[i] for i in idx])
    t_p = time.perf_counter()
    stage = {"quality": int((t_q - t) * 1000), "passive": int((t_p - t_q) * 1000)}
    return q, p, stage


def _first_pass_pipeline(frames: list[bytes], ts: list[int], prompt_ts: int,
                         action: str) -> tuple[dict, dict, dict]:
    """ONE MediaPipe pass per frame. quality() landmarks every frame once (memoised by
    bytes); challenge_check() then reuses those landmarks and its passive run reuses the
    per-frame quality. Sequential on purpose: run in parallel, both missed the cache and
    landmarked every frame twice behind the same detector lock (650-740 ms live)."""
    t = time.perf_counter()
    q = signals.quality(frames)
    t_q = time.perf_counter()
    ch = signals.challenge_check(frames, ts, prompt_ts, [action])
    t_c = time.perf_counter()
    p_ms = int(((ch.get("passive") or {}).get("ms")) or 0)
    ch_ms = int((t_c - t_q) * 1000)
    stage = {"quality": int((t_q - t) * 1000), "passive": p_ms,
             "challenge": max(ch_ms - p_ms, 0), "pipeline": int((t_c - t) * 1000)}
    return q, ch, stage


def _quality_view(q: dict | None) -> dict:
    q = q or {}
    return {"ok": q.get("ok"), "fail_reason": q.get("fail_reason"),
            "continuity_ok": q.get("continuity_ok"),
            "per_frame": q.get("per_frame", []), "stub": bool(q.get("stub"))}


def _identity_view(ident: dict | None) -> dict:
    if ident is None:
        return {"status": "UNKNOWN", "similarity": None, "note": "not_received_by_deadline"}
    return {"status": ident.get("identity", "UNKNOWN"),
            "similarity": ident.get("similarity"), "note": ident.get("note")}


def _decision_signals(q, p, ident, challenge=None, *, user_id: str, quick=None) -> dict:
    """Schedule and mode are evaluated NOW, at the moment of the decision."""
    return {
        "schedule": schedule.evaluate(user_id, config.KIOSK_ZONE),
        "mode": mode.current(),
        "quality": q if isinstance(q, dict) and isinstance(q.get("ok"), bool) else None,
        "passive": p,
        "identity": None if ident is None else
                    {"status": ident.get("identity"), "similarity": ident.get("similarity")},
        "challenge": challenge,
        "quick_challenge": quick,
        "quick_requires_passive_live": config.QUICK_REQUIRES_PASSIVE_LIVE,
        "passive_allow_enabled": config.PASSIVE_ALLOW_ENABLED,
        "passive_deny_enabled": config.PASSIVE_DENY_ENABLED,
    }


def _schedule_view(sig: dict) -> dict:
    sc = sig.get("schedule") or {}
    return {"status": sc.get("status", "UNKNOWN"), "policy_version": sc.get("policy_version")}


def _log(at_id, user_id, scenario, phase, d, q, p, ident, stage, first_ms, total_ms,
         challenge=None, labels=None, sig=None):
    sig = sig or {}
    write_decision({
        "attempt_id": at_id, "user_id": user_id, "scenario": scenario, "phase": phase,
        "decision": d[0], "reason": d[1], "next_action": d[2],
        "schedule": {**_schedule_view(sig), "note": (sig.get("schedule") or {}).get("note")},
        "mode": sig.get("mode"), "demo_clock": schedule.override_active(),
        "scores": {
            "quality_ok": (q or {}).get("ok"),
            "continuity_ok": (q or {}).get("continuity_ok"),
            "blur_per_frame": [f.get("blur") for f in (q or {}).get("per_frame", [])
                               if isinstance(f, dict)],
            "passive_status": (p or {}).get("status"),
            "passive_score": (p or {}).get("score"),
            "identity_status": _identity_view(ident)["status"],
            "identity_similarity": _identity_view(ident)["similarity"],
            "identity_note": _identity_view(ident)["note"],
            "challenge_status": (challenge or {}).get("status"),
            "quick_challenge": sig.get("quick_challenge"),
            "labels": labels,
        },
        "stubs": signals.stubs_in_use(),
        "stage_ms": stage, "first_pass_ms": first_ms, "total_ms": total_ms,
    })


# --- v2: attempt flow -------------------------------------------------------------

class StartBody(BaseModel):
    user_id: str
    consent: bool


@app.post("/attempt/start")
def attempt_start(body: StartBody) -> dict:
    if body.consent is not True:
        raise HTTPException(400, "consent required")
    if not identity.USER_ID_RE.match(body.user_id or ""):
        raise HTTPException(400, "invalid user_id")
    at = STORE.create(body.user_id)
    at.first_action = random.choice(config.QUICK_ACTIONS)
    return {"attempt_id": at.attempt_id, "server_t0_ms": at.server_t0_ms,
            "deadline_ms": at.deadline_ms, "action": at.first_action}


@app.post("/attempt/{attempt_id}/identity")
def attempt_identity(attempt_id: str, frame: UploadFile = File(...),
                     capture_ts_ms: str | None = Form(None)) -> dict:
    at = _get_attempt(attempt_id)
    id_recv = now_ms() - at.server_t0_ms  # t0 -> identity frame received
    data = _read_frames([frame])
    try:
        res = (dict(OFFLINE_IDENTITY) if mode.current() == "OFFLINE"
               else identity.compare(at.user_id, data[0]))
    finally:
        data.clear()
    id_done = now_ms() - at.server_t0_ms
    try:
        cap = int(float(capture_ts_ms)) - at.server_t0_ms if capture_ts_ms else None
    except ValueError:
        cap = None
    at.identity_timing = {"id_capture": cap, "id_recv": id_recv, "id_compare": res.get("ms"),
                          "id_done": id_done}
    if res.get("identity") == "UNKNOWN":
        log.warning("identity UNKNOWN for %s: %s (%s ms)", attempt_id, res.get("note"),
                    res.get("ms"))
    outcome = STORE.record_identity(attempt_id, res)
    if outcome == "unknown_attempt":
        raise HTTPException(404, "unknown attempt_id")
    if outcome == "late":
        late_result(attempt_id, "identity", res)
    return {"identity": res["identity"], "similarity": res["similarity"], "ms": res["ms"]}


def _first_pass_response(at: Attempt, d, q, p, ident, stage, first_ms, sig) -> dict:
    quick = sig.get("quick_challenge")
    resp = {
        "decision": d[0], "reason_code": d[1], "message": message_for(d[1], d[2]),
        "next_action": d[2], "attempt_id": at.attempt_id,
        "schedule": _schedule_view(sig), "mode": sig["mode"],
        "signals": {"quality": _quality_view(q),
                    "passive": {"status": (p or {}).get("status", "UNAVAILABLE"),
                                "score": (p or {}).get("score")},
                    "identity": _identity_view(ident)},
        "stage_ms": stage, "first_pass_server_ms": first_ms,
    }
    if quick is not None:
        resp["signals"]["challenge"] = quick
    return resp


@app.post("/attempt/{attempt_id}/liveness")
def attempt_liveness(attempt_id: str, frames: list[UploadFile] = File(...),
                     capture_ts_ms: list[str] | None = Form(None),
                     prompt_ts_ms: str | None = Form(None),
                     scenario: str | None = Form(None)) -> dict:
    at = _get_attempt(attempt_id)
    recv_ms = now_ms() - at.server_t0_ms  # t0 -> liveness upload fully received
    if at.decision is not None:  # first pass is FINAL
        return at.decision
    data = _read_frames(frames)
    timed_out = False
    try:
        ts = _parse_ts(capture_ts_ms, len(data))
        timing = {"liveness_recv": recv_ms,
                  "last_capture": (max(ts) - at.server_t0_ms) if any(ts) else None}
        labels_fut = _labels_future(data[len(data) // 2])
        quick = None
        if prompt_ts_ms and at.first_action and mode.current() == "ONLINE":
            # 2 s redesign: quality + the ONE quick action in parallel. The
            # challenge's own passive run is the (log-only) passive signal.
            prompt_ts = _parse_ts([prompt_ts_ms], 1)[0]
            future = _EXECUTOR.submit(_first_pass_pipeline, list(data), ts, prompt_ts, at.first_action)
            try:
                q, ch, stage = future.result(timeout=max(0, at.deadline_mono-time.monotonic()))
            except FutureTimeout:
                timed_out = True
                future.cancel()
                q, ch, stage = {}, {"status": "UNAVAILABLE", "reason": "TIMEOUT"}, {}
            p = ch.get("passive") or {"status": "UNAVAILABLE", "score": None}
            quick = {"action": at.first_action, "status": ch.get("status"),
                     "reason": ch.get("reason")}
        else:  # no prompt sent, or offline (offline: staff verify, no challenge)
            future = _EXECUTOR.submit(_quality_and_passive, list(data), ts)
            try:
                q, p, stage = future.result(timeout=max(0, at.deadline_mono-time.monotonic()))
            except FutureTimeout:
                timed_out = True
                future.cancel()
                q, p, stage = {}, {"status": "UNAVAILABLE", "score": None}, {}
    finally:
        data.clear()  # kiosk keeps nothing

    t_wait = time.perf_counter()
    if mode.current() == "ONLINE":  # offline: no identity is coming
        at.identity_event.wait(timeout=max(at.deadline_mono - time.monotonic(), 0.0))
    stage["identity_wait"] = int((time.perf_counter() - t_wait) * 1000)
    stage.update(timing)

    with at.lock:
        if at.decision is not None:  # a concurrent request decided first
            return at.decision
        t_dec = time.perf_counter()
        ident = identity_snapshot(at)  # on-time result only
        if at.identity_timing:
            stage.update(at.identity_timing)
        sig = _decision_signals(q, p, ident, user_id=at.user_id, quick=quick)
        d = decide(sig)
        if (timed_out or at.past_deadline()) and d[0] != 'DENY' and d[2] != 'STAFF_VERIFY':
            d = ("REQUEST_VERIFICATION", "TIMEOUT", "CHALLENGE")
        stage["decide"] = int((time.perf_counter() - t_dec) * 1000)
        first_ms = now_ms() - at.server_t0_ms
        at.scenario, at.passive, at.quality = scenario, p, _quality_view(q)
        at.continuity_ok = (q or {}).get("continuity_ok")
        at.stage_ms = dict(stage)
        mine = _first_pass_response(at, d, q, p, ident, stage, first_ms, sig)
        resp = at.set_decision(mine)
    if resp is mine:  # only the request that actually decided logs it
        _log(at.attempt_id, at.user_id, scenario, "first_pass", d, q, p, ident, stage,
             first_ms, first_ms, labels=_labels_result(labels_fut), sig=sig)
        _emit(at.attempt_id, at.user_id, d, sig, "face")
    return resp


def _stepup_actions() -> list[str]:
    """Exactly one pose + one expression, in random order (never two of a kind)."""
    acts = [random.choice(config.POSE_ACTIONS), random.choice(config.EXPRESSION_ACTIONS)]
    random.shuffle(acts)
    return acts


@app.post("/attempt/{attempt_id}/challenge/start")
def challenge_start(attempt_id: str) -> dict:
    at = _get_attempt(attempt_id)
    with at.lock:
        if at.decision is None or at.decision["decision"] != "REQUEST_VERIFICATION":
            raise HTTPException(409, "challenge only after a REQUEST_VERIFICATION first pass")
        if at.challenge is not None:
            raise HTTPException(409, "one challenge per attempt")
        at.challenge = {"challenge_id": str(uuid.uuid4()),
                        "actions": _stepup_actions(),
                        "started_mono": time.monotonic()}
        ch = at.challenge
    return {"challenge_id": ch["challenge_id"], "actions": ch["actions"],
            "baseline_ms": config.CHALLENGE_BASELINE_MS,
            "timeout_ms": config.CHALLENGE_TIMEOUT_MS}


def _good_tracked_frame(q: dict | None, n: int) -> int | None:
    """Index of the best good frame, only if quality vouches it's one tracked face."""
    if not (isinstance(q, dict) and q.get("ok") is True and q.get("continuity_ok") is True):
        return None
    per = q.get("per_frame") or []
    good = [(f.get("face_px") or 0, i) for i, f in enumerate(per[:n])
            if isinstance(f, dict) and f.get("ok") is True and f.get("face_count") in (None, 1)]
    return max(good)[1] if good else None


def _challenge_identity(fut) -> dict:
    if fut is None:
        return {"identity": "UNKNOWN", "similarity": None, "ms": 0,
                "note": "no_good_tracked_frame"}
    try:
        return fut.result(timeout=CHALLENGE_IDENTITY_TIMEOUT_S)
    except Exception as e:  # timeout or failure -> never a pass
        return {"identity": "UNKNOWN", "similarity": None, "ms": None,
                "note": f"error:{type(e).__name__}"}


@app.post("/attempt/{attempt_id}/challenge")
def challenge_submit(attempt_id: str, frames: list[UploadFile] = File(...),
                     capture_ts_ms: list[str] | None = Form(None),
                     prompt_ts_ms: str | None = Form(None),
                     challenge_id: str = Form(...)) -> dict:
    at = _get_attempt(attempt_id)
    if at.challenge is None or at.challenge["challenge_id"] != challenge_id:
        raise HTTPException(409, "no such challenge for this attempt")
    if at.challenge_decision is not None:
        return at.challenge_decision
    elapsed_ms = (time.monotonic() - at.challenge["started_mono"]) * 1000
    timed_out = elapsed_ms > config.CHALLENGE_TIMEOUT_MS + CHALLENGE_UPLOAD_GRACE_MS
    data = _read_frames(frames, config.MAX_CHALLENGE_FRAMES)
    try:
        ts = _parse_ts(capture_ts_ms, len(data))
        prompt_ts = _parse_ts([prompt_ts_ms] if prompt_ts_ms else [], 1)[0]
        t = time.perf_counter()
        q = signals.quality(data)
        t_q = time.perf_counter()
        # Re-run identity on one good frame of the same tracked face, in
        # parallel with the challenge check. No such frame -> no call -> UNKNOWN.
        good = _good_tracked_frame(q, len(data))
        offline = mode.current() == "OFFLINE"
        id_fut = (_EXECUTOR.submit(identity.compare, at.user_id, data[good])
                  if good is not None and not offline else None)
        ch = signals.challenge_check(data, ts, prompt_ts, at.challenge["actions"])
        t_c = time.perf_counter()
    finally:
        data.clear()
    ch_ident = dict(OFFLINE_IDENTITY) if offline else _challenge_identity(id_fut)
    t_id = time.perf_counter()
    challenge = {"status": ch.get("status"), "passive": ch.get("passive"),
                 "timed_out": timed_out,
                 "identity": {"status": ch_ident["identity"],
                              "similarity": ch_ident["similarity"]}}
    with at.lock:
        if at.challenge_decision is not None:
            return at.challenge_decision
        ident = ch_ident  # challenge-frame identity replaces the first-pass one
        t_dec = time.perf_counter()
        sig = _decision_signals(q, at.passive, None, challenge, user_id=at.user_id)
        d = decide(sig)
        stage = {"quality": int((t_q - t) * 1000), "passive": int((t_c - t_q) * 1000),
                 "identity_wait": int((t_id - t_c) * 1000),
                 "decide": int((time.perf_counter() - t_dec) * 1000)}
        first_ms = at.decision["first_pass_server_ms"]
        total_ms = now_ms() - at.server_t0_ms
        resp = _first_pass_response(at, d, q, (ch.get("passive") or {}), ident, stage,
                                    first_ms, sig)
        resp["total_ms"] = total_ms
        mine = resp
        resp = at.set_challenge_decision(mine)
        if resp is mine:
            at.challenge_passive = ch.get("passive")
    if resp is mine:
        _log(at.attempt_id, at.user_id, at.scenario, "challenge", d, q, ch.get("passive"),
             ident, stage, first_ms, total_ms, challenge=challenge, sig=sig)
        _emit(at.attempt_id, at.user_id, d, sig, "face+challenge")
    return resp


# --- v3: staff verification (physical ID), works online and offline ---------------------

class StaffBody(BaseModel):
    operator_pin: str
    id_checked: bool
    reason: str
    approve: bool


def _operator_id(pin_hash: str) -> str:
    return os.environ.get("OPERATOR_ID") or f"op-{pin_hash[:8]}"


@app.post("/attempt/{attempt_id}/staff_verify")
def staff_verify(attempt_id: str, body: StaffBody) -> dict:
    at = _get_attempt(attempt_id)
    expected = (os.environ.get("OPERATOR_PIN_HASH") or "").strip().lower()
    if not expected:
        raise HTTPException(503, "staff verification not configured (OPERATOR_PIN_HASH)")
    with at.lock:
        if at.pin_failures >= config.MAX_PIN_FAILURES:
            raise HTTPException(429, "too many wrong PINs for this attempt")
        given = hashlib.sha256(body.operator_pin.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(given, expected):
            at.pin_failures += 1
            raise HTTPException(403, "wrong operator PIN")
    if body.id_checked is not True:
        raise HTTPException(400, "physical ID must be checked (id_checked=true)")
    reason = (body.reason or "").strip()
    if not reason or len(reason) > 200:
        raise HTTPException(400, "reason required (1-200 chars)")

    with at.lock:
        if at.staff_decision is not None:
            return at.staff_decision
        latest = at.challenge_decision or at.decision
        if latest is None:
            raise HTTPException(409, "no decision yet for this attempt")
        if latest["decision"] != "REQUEST_VERIFICATION":
            # ALLOW needs nothing; DENY (spoof, mismatch, schedule) is final.
            raise HTTPException(409, "blocked: attempt is %s (%s); staff cannot override"
                                % (latest["decision"], latest["reason_code"]))
        sig = {"passive": at.passive, "challenge_passive": at.challenge_passive,
               "passive_deny_enabled": config.PASSIVE_DENY_ENABLED,
               "schedule": schedule.evaluate(at.user_id, config.KIOSK_ZONE),
               "mode": mode.current(),
               "staff": {"approve": body.approve, "id_checked": body.id_checked,
                         "reason": reason}}
        d = decide_staff(sig)
        op = _operator_id(expected)
        total_ms = now_ms() - at.server_t0_ms
        mine = {
            "decision": d[0], "reason_code": d[1], "message": message_for(d[1], d[2]),
            "next_action": d[2], "attempt_id": at.attempt_id,
            "schedule": _schedule_view(sig), "mode": sig["mode"],
            "staff": {"operator_id": op, "method": "physical_id", "reason": reason},
            "first_pass_server_ms": (at.decision or {}).get("first_pass_server_ms"),
            "total_ms": total_ms,
        }
        at.staff_decision = mine
    write_decision({"attempt_id": at.attempt_id, "user_id": at.user_id,
                    "scenario": at.scenario, "phase": "staff", "decision": d[0],
                    "reason": d[1], "next_action": d[2],
                    "schedule": {**_schedule_view(sig), "note": sig["schedule"].get("note")},
                    "mode": sig["mode"], "demo_clock": schedule.override_active(),
                    "staff": mine["staff"], "first_pass_ms": mine["first_pass_server_ms"],
                    "total_ms": total_ms})
    _emit(at.attempt_id, at.user_id, d, sig, "physical_id", operator_id=op)
    return mine


PASSIVE_GATE_NOTE = "DISABLED: calibration gate failed (6/10 genuine)"


@app.get("/status")
def status() -> dict:
    pending, synced = outbox.get().counts()
    m = mode.current()
    pol = schedule.policy_state()
    return {"mode": m, "pending_sync": pending, "synced": synced,
            "policy_version": pol["version"],
            "kiosk_time": schedule.now().isoformat(timespec="seconds"),
            "demo_clock": schedule.override_active(),
            "passive_allow": "ENABLED" if config.PASSIVE_ALLOW_ENABLED else PASSIVE_GATE_NOTE,
            "challenge": "REQUIRED",
            "identity_service": ("available" if m == "ONLINE" and identity.service_ok()
                                 else "unavailable"),
            "policy": pol,
            "audit_queue": {"pending": pending, "synced": synced}}


# --- fallback demo page (integrator override; Shesh's kiosk replaces it) ---------------

_STATIC = Path(__file__).resolve().parent / "static"


@app.get("/demo", include_in_schema=False)
def demo_page():
    return FileResponse(_STATIC / "demo.html", media_type="text/html")


@app.get("/demo/users", include_in_schema=False)
def demo_users() -> dict:
    """Enrolled user ids for the demo name buttons (ids only, never photos)."""
    return {"users": identity.enrolled_ids()}


# --- v3: operator demo clock ---------------------------------------------------------

class ClockBody(BaseModel):
    iso_time: str | None = None


@app.post("/operator/clock")
def operator_clock(body: ClockBody) -> dict:
    """Demo-only clock override (Asia/Kolkata if no offset). null = real time."""
    if not config.DEMO_CLOCK_ENABLED:
        raise HTTPException(403, "demo clock disabled")
    try:
        schedule.set_override(body.iso_time)
    except ValueError:
        raise HTTPException(400, "iso_time must be ISO 8601, e.g. 2026-11-14T10:30:00")
    log.warning("DEMO CLOCK %s", "set to " + body.iso_time if body.iso_time else "cleared")
    return {"kiosk_time": schedule.now().isoformat(timespec="seconds"),
            "demo_clock": schedule.override_active()}


# --- v1: /verify (shape unchanged, routed through decide()) ----------------------

_V1_REASON = {
    "OK_LIVE_MATCH": "LIVE_MATCH",
    "SPOOF_PASSIVE": "SCREEN_DETECTED",
    "ID_MISMATCH": "NO_MATCH",
    "ID_UNAVAILABLE": "NO_MATCH",
    "TIMEOUT": "NO_MATCH",
    "LIVENESS_UNCERTAIN": "NO_MOTION",
    "LIVENESS_UNAVAILABLE": "NO_MOTION",
}


def _v1_confidence(d, p, sim) -> float:
    if d[0] == "ALLOW":
        c = min(sim / 100 if sim is not None else 0.0, (p or {}).get("score") or 0.0)
    elif d[1] == "SPOOF_PASSIVE":
        c = (p or {}).get("score") or 0.5
    elif d[1] == "ID_MISMATCH":
        c = 1 - (sim or 0.0) / 100
    else:
        c = 0.5
    return round(min(max(float(c), 0.0), 1.0), 4)


@app.post("/verify")
def verify(frames: list[UploadFile] = File(...), user_id: str = Form(...)) -> dict:
    # Sync endpoint: FastAPI runs it in a worker thread, so the blocking
    # Rekognition calls don't stall the event loop.
    t0 = time.perf_counter()
    data = _read_frames(frames)
    attempt_id = str(uuid.uuid4())
    try:
        mid = data[len(data) // 2]
        id_fut = (None if mode.current() == "OFFLINE"
                  else _EXECUTOR.submit(identity.compare, user_id, mid))
        labels_fut = _labels_future(mid)
        q, p, stage = _quality_and_passive(data, [0] * len(data))
        t_wait = time.perf_counter()
        remaining = config.ATTEMPT_DEADLINE_MS / 1000 - (t_wait - t0)
        if id_fut is None:
            ident = dict(OFFLINE_IDENTITY)
        else:
            wait([id_fut], timeout=max(remaining, 0.0))
            ident = id_fut.result(timeout=0) if id_fut.done() else None  # late -> missing
        stage["identity_wait"] = int((time.perf_counter() - t_wait) * 1000)
    finally:
        data.clear()  # kiosk keeps nothing
    t_dec = time.perf_counter()
    sig = _decision_signals(q, p, ident, user_id=user_id)
    d = decide(sig)
    stage["decide"] = int((time.perf_counter() - t_dec) * 1000)
    decision_ms = int((time.perf_counter() - t0) * 1000)
    sim = (ident or {}).get("similarity")
    q_ok = [f for f in (q or {}).get("per_frame", []) if isinstance(f, dict)]
    labels = _labels_result(labels_fut)
    _log(attempt_id, user_id, "verify_v1", "first_pass", d, q, p, ident, stage,
         decision_ms, decision_ms, labels=labels, sig=sig)
    _emit(attempt_id, user_id, d, sig, "face")
    return {
        "decision": d[0],
        "confidence": _v1_confidence(d, p, sim),
        "decision_ms": decision_ms,
        "reason_code": _V1_REASON.get(d[1], "LOW_QUALITY"),
        "signals": {
            "labels": [l["name"] for l in labels or []],
            "motion": 0.0,
            "quality": round(sum(1 for f in q_ok if f.get("ok")) / len(q_ok), 4) if q_ok else 0.0,
            "similarity": round(float(sim or 0.0), 2),
        },
        # v3 additive fields (v1 keys above unchanged)
        "schedule": _schedule_view(sig), "mode": sig["mode"], "reason_detail": d[1],
    }

# Mount last so API routes take precedence; only public web assets are exposed.
app.mount('/', StaticFiles(directory=str(config.REPO_ROOT / 'web'), html=True), name='kiosk')
