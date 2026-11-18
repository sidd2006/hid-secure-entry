"""Live webcam test of challenge_check, one attempt per run, results appended to a CSV.

    python -m pipeline.live_challenge --label genuine --who sonu [--cam 0] [--actions SMILE,TURN_LEFT]
    python -m pipeline.live_challenge --tally

Per attempt: BASELINE_MS of frames with no instruction, then the actions are shown on
screen (prompt_ts = when they first appear), frames keep coming until CH_TIMEOUT_MS
after the prompt, then challenge_check runs. Frames are raw (unmirrored) like the kiosk
sends; only the preview is mirrored. No images are written; the CSV holds numbers only.

For spoof labels a PASS means the attack got through; FAIL or UNCERTAIN means it was
stopped (UNCERTAIN routes to reception, never ALLOW). Passive is logged, not decisive.
Fully local: no network calls.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

LABELS = ("genuine", "print", "phone", "replay", "mask", "mask_cutout")
BASELINE_MS = 800
CSV_PATH = Path("logs/live_challenge.csv")  # logs/ is gitignored
FIELDS = ["time", "label", "who", "actions", "status", "reason", "allow_eligible",
          "onsets_ms", "peaks", "tracking_ok", "min_iou", "passive_status", "passive_score",
          "passive_median", "n_frames", "fps", "check_ms", "note"]
TEXT = {"SMILE": "SMILE", "OPEN_MOUTH": "OPEN YOUR MOUTH",
        "TURN_LEFT": "TURN HEAD TO YOUR LEFT", "TURN_RIGHT": "TURN HEAD TO YOUR RIGHT"}


def capture(cam: int, actions: list[str], timeout_ms: int):
    import cv2
    cap = cv2.VideoCapture(cam)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    for _ in range(10):  # let exposure settle
        cap.read()
    # get ready screen
    t_ready = time.perf_counter()
    while (time.perf_counter() - t_ready) < 1.5:
        ok, fr = cap.read()
        if not ok:
            raise SystemExit("camera read failed")
        v = cv2.flip(fr, 1)
        cv2.putText(v, "Look at the camera...", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        cv2.imshow("live_challenge", v)
        cv2.waitKey(1)
    frames, ts = [], []
    t0 = time.perf_counter()
    prompt_ts = None
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        now = (time.perf_counter() - t0) * 1000
        ok2, jpg = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        frames.append(jpg.tobytes())
        ts.append(now)
        v = cv2.flip(fr, 1)
        if now >= BASELINE_MS:
            if prompt_ts is None:
                prompt_ts = now
            for i, a in enumerate(actions):
                cv2.putText(v, f"{i + 1}. {TEXT[a]}", (10, 40 + 35 * i),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
            if now - prompt_ts > timeout_ms:
                break
        else:
            cv2.putText(v, "Hold still", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        cv2.imshow("live_challenge", v)
        cv2.waitKey(1)
    cap.release()
    return frames, ts, prompt_ts


def run(a) -> None:
    from . import config as C
    from .challenge import challenge_check, warmup
    from .passive import warmup as pw
    pw()
    warmup()
    actions = [x.strip().upper() for x in a.actions.split(",")] if a.actions else \
        random.sample(["SMILE", "OPEN_MOUTH", random.choice(["TURN_LEFT", "TURN_RIGHT"])], 2)
    frames, ts, prompt_ts = capture(a.cam, actions, C.CH_TIMEOUT_MS)
    if prompt_ts is None:
        raise SystemExit("no frames after baseline")
    r = challenge_check(frames, ts, prompt_ts, actions)
    import cv2
    v = None
    msg = f"{r['status']}  {r['reason']}"
    print(msg)
    try:
        import numpy as np
        v = np.zeros((120, 640, 3), np.uint8)
        cv2.putText(v, msg, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 255, 0) if r["status"] == "PASS" else (0, 0, 255), 2)
        cv2.imshow("live_challenge", v)
        cv2.waitKey(1500)
        cv2.destroyAllWindows()
    except Exception:
        pass
    p = r.get("passive") or {}
    pa = r.get("per_action") or []
    tr = r.get("tracking") or {}
    dur = (ts[-1] - ts[0]) / 1000 if len(ts) > 1 else 0
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "label": a.label, "who": a.who,
        "actions": "+".join(actions), "status": r["status"], "reason": r["reason"],
        "allow_eligible": r.get("allow_eligible"),
        "onsets_ms": json.dumps([x.get("onset_ms") for x in pa]),
        "peaks": json.dumps([x.get("peak") for x in pa]),
        "tracking_ok": tr.get("ok"), "min_iou": tr.get("min_iou"),
        "passive_status": p.get("status"), "passive_score": p.get("score"),
        "passive_median": p.get("median"), "n_frames": len(frames),
        "fps": round(len(frames) / dur, 1) if dur else None, "check_ms": r.get("ms"),
        "note": a.note or "",
    }
    if tr.get("frames"):
        # how well the face was found; a low share means the action check was never really tested
        diag = f"faces={tr.get('face_frames')}/{tr['frames']} face_px={tr.get('face_px')}"
        print("face found in", diag.replace("faces=", "").replace(" face_px=", " checked frames, median face height px "))
        if tr.get("face_frames", 0) < 0.9 * tr["frames"]:
            print("** FACE NOT TRACKED in most frames: move the phone closer / cut glare and rerun **")
        row["note"] = (row["note"] + " " + diag).strip()
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.is_file()
    with CSV_PATH.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(json.dumps(row, indent=1))
    print(f"appended to {CSV_PATH}")


def tally() -> None:
    if not CSV_PATH.is_file():
        raise SystemExit(f"no results yet at {CSV_PATH}")
    rows = list(csv.DictReader(CSV_PATH.open()))
    by = defaultdict(Counter)
    ms = defaultdict(list)
    for r in rows:
        by[r["label"]][r["status"]] += 1
        if r["check_ms"]:
            ms[r["label"]].append(int(r["check_ms"]))
    print(f"{'label':12} {'n':>3} {'PASS':>5} {'FAIL':>5} {'UNCERT':>6}  result")
    for lab in sorted(by, key=lambda x: LABELS.index(x) if x in LABELS else 99):
        c = by[lab]
        n = sum(c.values())
        if lab == "genuine":
            res = f"{c['PASS']}/{n} genuine passed"
        else:
            res = f"{n - c['PASS']}/{n} attacks stopped"
        print(f"{lab:12} {n:>3} {c['PASS']:>5} {c['FAIL']:>5} {c['UNCERTAIN']:>6}  {res}")
    allms = sorted(x for v in ms.values() for x in v)
    if allms:
        med = allms[len(allms) // 2]
        print(f"challenge_check ms: median {med}, min {allms[0]}, max {allms[-1]} (n={len(allms)})")
    ps = Counter((r["label"], r["passive_status"]) for r in rows)
    print("passive (logged only):", dict(sorted(ps.items())))

    # by condition: label x note x actions asked. "act_seen" = every asked action was seen
    # after the minimum reaction time (the action check alone, before tracking/passive).
    from . import config as C
    grp = defaultdict(list)
    for r in rows:
        n_act = len(r["actions"].split("+"))
        note = (r.get("note") or "").split(" faces=")[0].split("faces=")[0].strip()
        if not note and r["who"].startswith("steady_"):
            note = "steady"
        grp[(r["label"], note or "-", f"{n_act}:{r['actions']}" if n_act == 1 else str(n_act))].append(r)
    print()
    print(f"{'label':12} {'note':10} {'actions':8} {'n':>3} {'act_seen':>8} {'PASS':>5} {'UNCERT':>6} {'FAIL':>5} "
          f"{'pasv_SPOOF':>10} {'trk_lost':>8} {'no_base':>7} {'med_ms':>6}")
    for (lab, note, act), rs in sorted(grp.items()):
        seen = 0
        for r in rs:
            try:
                on = json.loads(r["onsets_ms"])
            except Exception:
                on = [None]
            seen += bool(on) and all(o is not None and o >= C.CH_MIN_REACTION_MS for o in on)
        st = Counter(r["status"] for r in rs)
        m = sorted(int(r["check_ms"]) for r in rs if r["check_ms"])
        print(f"{lab:12} {note:10} {act:8} {len(rs):>3} {seen:>8} {st['PASS']:>5} {st['UNCERTAIN']:>6} {st['FAIL']:>5} "
              f"{sum(r['passive_status'] == 'SPOOF' for r in rs):>10} "
              f"{sum(r['reason'] == 'TRACKING_LOST' for r in rs):>8} "
              f"{sum(r['reason'] == 'NO_BASELINE' for r in rs):>7} {m[len(m) // 2] if m else '':>6}")
    print("UNCERTAIN = held for reception (not allowed in), not 'detected as a spoof'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", choices=LABELS)
    ap.add_argument("--who", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--cam", default=0, type=int)
    ap.add_argument("--actions", default="", help="comma list; default 2 random")
    ap.add_argument("--tally", action="store_true")
    a = ap.parse_args()
    if a.tally:
        tally()
    elif a.label:
        run(a)
    else:
        ap.error("--label or --tally")


if __name__ == "__main__":
    main()
