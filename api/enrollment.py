"""Operator-authorized enrollment. Reference photos are intentionally retained."""
import hmac
import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Header
from PIL import Image, UnidentifiedImageError

from . import config, identity

router = APIRouter()
_lock = threading.Lock()


def authorize(token):
    expected = os.environ.get("ENROLLMENT_TOKEN", "")
    if not expected or not hmac.compare_digest(token or "", expected):
        raise HTTPException(403, "Valid operator enrollment key required")


@router.get("/enrollment/users")
def users(x_enrollment_token: str | None = Header(None)):
    authorize(x_enrollment_token)
    return {"users": identity.enrolled_ids()}


@router.post("/enrollment/users")
def enroll(user_id: str = Form(...), photo: UploadFile = File(...),
           consent: bool = Form(...), id_checked: bool = Form(...),
           start: str = Form(...), end: str = Form(...), days: str = Form(...),
           zone: str = Form(...), effect: str = Form(...),
           x_enrollment_token: str | None = Header(None)):
    authorize(x_enrollment_token)
    if not identity.USER_ID_RE.fullmatch(user_id):
        raise HTTPException(400, "ID: 1-64 letters, digits, underscores or hyphens")
    if not consent or not id_checked:
        raise HTTPException(400, "Consent and an operator physical ID check are required")
    day_list = days.upper().split(",")
    if not day_list or any(d not in {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"} for d in day_list):
        raise HTTPException(400, "Select valid days")
    try:
        st, en = datetime.strptime(start, "%H:%M"), datetime.strptime(end, "%H:%M")
        if st >= en:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Use HH:MM access hours with end after start")
    if effect not in {"allow", "approval"} or not zone.strip() or len(zone) > 64:
        raise HTTPException(400, "Valid zone and access rule required")
    if config.KIOSK_ZONE and zone != config.KIOSK_ZONE:
        raise HTTPException(400, "Zone must match this kiosk: " + config.KIOSK_ZONE)
    raw = photo.file.read(config.MAX_FRAME_BYTES + 1)
    if not raw or len(raw) > config.MAX_FRAME_BYTES:
        raise HTTPException(400, "Photo must be under 5 MB")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 20_000_000:
                raise ValueError()
            im = im.convert("RGB")
            im.thumbnail((640, 640))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            image = buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, "Upload a valid photo under 20 megapixels")
    import cv2
    import numpy as np
    from pipeline.challenge import metrics_for
    m = metrics_for(image, cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR))
    if m.get("n_faces") != 1:
        raise HTTPException(400, "Exactly one clearly visible face is required")
    with _lock:
        directory = Path(config.ENROLL_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        if user_id in identity.enrolled_ids() or any((directory / (user_id + ext)).exists() for ext in (".jpg", ".jpeg", ".png")):
            raise HTTPException(409, "ID already enrolled; existing references cannot be overwritten here")
        policy_path = Path(config.SCHEDULE_PATH)
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {"users": {}}
            if not isinstance(policy, dict) or not isinstance(policy.get("users"), dict):
                raise ValueError()
        except (OSError, ValueError):
            raise HTTPException(409, "Existing schedule needs operator repair before enrollment")
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        version = "enrollment-" + datetime.now(timezone.utc).isoformat()
        policy["users"][user_id] = {"zone": zone, "expires_utc": expiry,
            "policy_version": version, "windows": [{"days": day_list,
            "start": start, "end": end, "effect": effect}]}
        target = directory / (user_id + ".jpg")
        tmp = policy_path.with_suffix(".json.tmp")
        try:
            with target.open("xb") as f:
                f.write(image)
            tmp.write_text(json.dumps(policy, indent=2), encoding="utf-8")
            tmp.replace(policy_path)
        except Exception:
            target.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            raise
        # Update only this reference; ongoing attempts retain their existing results.
        with identity._enrolled_lock:
            identity._ENROLLED[user_id] = image
    return {"user_id": user_id, "enrolled": True, "expires_utc": expiry,
            "message": "Reference photo retained server-side. Access policy expires in 7 days."}
