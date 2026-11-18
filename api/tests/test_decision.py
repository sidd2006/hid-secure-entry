"""Safety tests: (a)(b)(c) v2 decision path, (d) schedule, (e) offline/staff, (f) sync."""
import hashlib
import json
import itertools

import pytest
from fastapi.testclient import TestClient

import pipeline
from api import config, identity, mode, outbox, schedule, signals, sync_worker
from api.decide import decide, decide_staff
from api.main import app
from api.store import STORE

GOOD_Q = {"ok": True, "fail_reason": None, "continuity_ok": True}
LIVE = {"status": "LIVE", "score": 0.97}
MATCH = {"status": "MATCH", "similarity": 98.0}
PERMITTED = {"status": "PERMITTED", "policy_version": "t1"}
OK = {"schedule": PERMITTED, "mode": "ONLINE"}  # schedule + mode that allow
# Fictional test policy: u1 may enter Mon-Fri 09:00-18:00 IST, but a deny
# window covers 12:00-13:00. 2026-11-18 is a Wednesday.
POLICY = {"policy_version": "t1", "expires_utc": "2099-01-01T00:00:00Z",
          "users": {"u1": {"zone": "LAB-1", "windows": [
              {"days": ["MON", "TUE", "WED", "THU", "FRI"], "start": "09:00", "end": "18:00",
               "effect": "allow"},
              {"days": ["WED"], "start": "12:00", "end": "13:00", "effect": "deny"}]}}}
WED_10 = "2026-11-18T10:00:00"
WED_1230 = "2026-11-18T12:30:00"
FRAME = b"\xff\xd8not-really-a-jpeg"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEMO_CLOCK_ENABLED", True)
    monkeypatch.setattr(config, "ATTEMPT_LOG_PATH", str(tmp_path / "attempts.jsonl"))
    monkeypatch.setattr(config, "ATTEMPT_DEADLINE_MS", 150)
    monkeypatch.setattr(config, "PASSIVE_ALLOW_ENABLED", True)
    # Pipeline reports a perfect, live face: so the ONLY thing that can
    # stop an ALLOW below is the identity result and its timing.
    monkeypatch.setattr(pipeline, "quality", lambda f: dict(GOOD_Q, per_frame=[]), raising=False)
    monkeypatch.setattr(pipeline, "select_frames", lambda f, ts: list(range(len(f))), raising=False)
    monkeypatch.setattr(pipeline, "passive_liveness", lambda f: dict(LIVE, per_frame=[]), raising=False)
    monkeypatch.setattr(identity, "compare", lambda uid, fr: {
        "identity": "MATCH", "similarity": 98.0, "ms": 5, "note": "fake"})
    sched = tmp_path / "schedule.json"
    sched.write_text(json.dumps(POLICY), encoding="utf-8")
    monkeypatch.setattr(config, "SCHEDULE_PATH", str(sched))
    monkeypatch.setattr(config, "KIOSK_ZONE", "LAB-1")
    monkeypatch.setattr(mode, "current", lambda: "ONLINE")
    monkeypatch.setattr(config, "OUTBOX_PATH", str(tmp_path / "outbox.db"))
    # Quick 1-action (and step-up) challenge: the person does the action.
    monkeypatch.setattr(pipeline, "challenge_check", lambda f, t, pt, a: {
        "status": "PASS", "reason": "CHALLENGE_PASSED", "per_action": [],
        "passive": dict(LIVE)}, raising=False)
    schedule.set_override(WED_10)  # inside the allow window
    STORE.clear()
    yield TestClient(app)
    STORE.clear()
    schedule.set_override(None)


def _start(c, user="u1"):
    r = c.post("/attempt/start", json={"user_id": user, "consent": True})
    assert r.status_code == 200
    return r.json()["attempt_id"]


def _liveness(c, aid, quick=False):
    """quick=True: 2 s flow, 14 frames at 10 fps with the action prompt at 300 ms."""
    if not quick:
        return c.post(f"/attempt/{aid}/liveness",
                      files=[("frames", ("f.jpg", FRAME, "image/jpeg"))] * 3)
    return c.post(f"/attempt/{aid}/liveness",
                  data={"prompt_ts_ms": "300",
                        "capture_ts_ms": [str(i * 100) for i in range(14)]},
                  files=[("frames", ("f.jpg", FRAME, "image/jpeg"))] * 14)


def _identity(c, aid):
    return c.post(f"/attempt/{aid}/identity", files={"frame": ("f.jpg", FRAME, "image/jpeg")})


def _status_of(sig):
    return sig.get("status") if isinstance(sig, dict) else None


def test_a_missing_unknown_or_unavailable_signal_never_allows():
    qualities = [GOOD_Q, None, {"ok": None, "continuity_ok": None},
                 {"ok": False, "fail_reason": "BLUR", "continuity_ok": True},
                 {"ok": True, "continuity_ok": False}, {"ok": True, "continuity_ok": None}]
    passives = [LIVE, None, {"status": "UNAVAILABLE", "score": None},
                {"status": "UNCERTAIN", "score": 0.5}, {"status": "GARBAGE"},
                {"status": "SPOOF", "score": 0.99}]  # SPOOF is log-only (deny flag off)
    identities = [MATCH, None, {"status": "UNKNOWN", "similarity": None},
                  {"status": "UNKNOWN", "similarity": 70.0}]
    flags = [True, False, None]
    schedules = [PERMITTED, None, {"status": "UNKNOWN"}, {"status": "APPROVAL_REQUIRED"}]
    modes = ["ONLINE", None, "OFFLINE"]
    quicks = [None, {"status": "PASS"}, {"status": "FAIL"}, {"status": "UNCERTAIN"},
              {"status": "UNAVAILABLE"}]
    combos = 0
    for q, p, i, flag, sc, md, qc in itertools.product(
            qualities, passives, identities, flags, schedules, modes, quicks):
        d = decide({"quality": q, "passive": p, "identity": i, "challenge": None,
                    "quick_challenge": qc, "passive_allow_enabled": flag,
                    "schedule": sc, "mode": md})
        # 2 s first pass: ALLOW only with on-time MATCH + quick 1-action PASS +
        # good continuous quality + PERMITTED + ONLINE + passive LIVE (flag
        # QUICK_REQUIRES_PASSIVE_LIVE, default on). PASSIVE_ALLOW_ENABLED: no effect.
        all_good = (q is GOOD_Q and i is MATCH and qc is not None and qc["status"] == "PASS"
                    and sc is PERMITTED and md == "ONLINE"
                    and _status_of(p) == "LIVE")  # QUICK_REQUIRES_PASSIVE_LIVE (default on)
        assert (d[0] == "ALLOW") == all_good, (q, p, i, flag, sc, md, qc, d)
        assert d[0] != "DENY", d  # nothing in these lists is a hard negative
        if md == "ONLINE" and not (qc and qc["status"] == "PASS" and _status_of(p) == "LIVE"):
            assert d[2] == "CHALLENGE", d  # quick not PASS, or passive not LIVE -> step-up
        combos += 1
    assert combos == 6 * 6 * 4 * 3 * 4 * 3 * 5

    # QUICK_REQUIRES_PASSIVE_LIVE: passive UNCERTAIN blocks the quick ALLOW when on
    # (default / flag missing) and is allowed through only when flipped off.
    quick_unc = {"quality": GOOD_Q, "passive": {"status": "UNCERTAIN", "score": 0.55},
                 "identity": MATCH, "challenge": None, "quick_challenge": {"status": "PASS"},
                 "passive_allow_enabled": False, **OK}
    assert [decide(quick_unc)[2], decide({**quick_unc, "quick_requires_passive_live": True})[2],
            decide({**quick_unc, "quick_requires_passive_live": False})[0]] == \
        ["CHALLENGE", "CHALLENGE", "ALLOW"]

    # Passive downgrade (1): perfect quick path + passive SPOOF -> step-up, never DENY.
    assert decide({"quality": GOOD_Q, "passive": {"status": "SPOOF", "score": 0.05},
                   "identity": MATCH, "challenge": None, "quick_challenge": {"status": "PASS"},
                   "quick_requires_passive_live": False,  # SPOOF blocks even with flag off
                   "passive_allow_enabled": False, **OK}) == \
        ("REQUEST_VERIFICATION", "LIVENESS_UNCERTAIN", "CHALLENGE")

    # Challenge phase: ALLOW only with challenge PASS (+ MATCH, PERMITTED, ONLINE)
    # and no passive SPOOF on the challenge frames (passive LIVE is not needed).
    for ch_status, ch_passive, sc, md in itertools.product(
            ["PASS", "FAIL", "UNCERTAIN", "UNAVAILABLE", None],
            [LIVE, None, {"status": "UNAVAILABLE"}, {"status": "SPOOF"}],
            [PERMITTED, None, {"status": "UNKNOWN"}, {"status": "APPROVAL_REQUIRED"}],
            ["ONLINE", "OFFLINE"]):
        d = decide({"quality": GOOD_Q, "passive": {"status": "UNCERTAIN"}, "identity": MATCH,
                    "challenge": {"status": ch_status, "passive": ch_passive, "timed_out": False,
                                  "identity": MATCH},
                    "passive_allow_enabled": True, "schedule": sc, "mode": md})
        assert (d[0] == "ALLOW") == (ch_status == "PASS" and sc is PERMITTED and md == "ONLINE"
                                      and _status_of(ch_passive) != "SPOOF"), d
        assert d[0] != "DENY", d  # passive never DENYs (flag off)
        if md == "ONLINE" and ch_status in ("FAIL", "UNCERTAIN"):
            assert d[2] == "RECEPTION", d

    # Passive downgrade (2): step-up PASS + SPOOF on challenge frames -> RECEPTION.
    assert decide({"quality": GOOD_Q, "passive": LIVE, "identity": MATCH, **OK,
                   "passive_allow_enabled": False,
                   "challenge": {"status": "PASS", "passive": {"status": "SPOOF"},
                                 "timed_out": False, "identity": MATCH}}) == \
        ("REQUEST_VERIFICATION", "MANUAL_REVIEW", "RECEPTION")

    # Challenge phase uses the challenge-frame identity, not the first-pass one
    # (which may have been late/missing): MATCH -> ALLOW, UNKNOWN/missing ->
    # RECEPTION, MISMATCH on good quality -> DENY.
    def _after(ch_ident):
        return decide({"quality": GOOD_Q, "passive": {"status": "UNCERTAIN"}, "identity": None,
                       "passive_allow_enabled": False, **OK,
                       "challenge": {"status": "PASS", "passive": LIVE, "timed_out": False,
                                     "identity": ch_ident}})
    assert [_after(i) for i in (MATCH, {"status": "UNKNOWN"}, None,
                                {"status": "MISMATCH", "similarity": 10.0})] == [
        ("ALLOW", "OK_LIVE_MATCH", "NONE"),
        ("REQUEST_VERIFICATION", "ID_UNAVAILABLE", "RECEPTION"),
        ("REQUEST_VERIFICATION", "ID_UNAVAILABLE", "RECEPTION"),
        ("DENY", "ID_MISMATCH", "NONE")]

    # The real STUBs (pipeline has no v2 functions yet) can never produce ALLOW.
    d = decide({"quality": signals._stub_quality([]), "passive": signals._stub_passive([]),
                "identity": MATCH, "challenge": None, "passive_allow_enabled": True, **OK})
    assert d[0] != "ALLOW"
    ch = signals._stub_challenge([], [], 0, [])
    d = decide({"quality": GOOD_Q, "passive": LIVE, "identity": MATCH,
                "challenge": {"status": ch["status"], "passive": ch["passive"], "timed_out": False,
                              "identity": MATCH},
                "passive_allow_enabled": True, **OK})
    assert d == ("REQUEST_VERIFICATION", "MANUAL_REVIEW", "RECEPTION")


def test_b_late_result_is_ignored_and_attempts_are_isolated(client):
    a, b = _start(client), _start(client)
    assert STORE.get(a).first_action in ("SMILE", "OPEN_MOUTH")

    # A: identity on time + quick action PASS -> first-pass ALLOW.
    # B gets nothing from A's result.
    assert _identity(client, a).json()["identity"] == "MATCH"
    assert STORE.get(a).identity is not None
    assert STORE.get(b).identity is None

    ra = _liveness(client, a, quick=True).json()
    assert (ra["decision"], ra["reason_code"]) == ("ALLOW", "OK_LIVE_MATCH")
    assert ra["signals"]["challenge"]["status"] == "PASS"
    assert ra["signals"]["identity"]["status"] == "MATCH" and ra["attempt_id"] == a

    # B: same perfect quick action, but no identity before the deadline ->
    # TIMEOUT + step-up. (If a late identity counted, B would be ALLOW.)
    rb = _liveness(client, b, quick=True).json()
    assert (rb["decision"], rb["reason_code"]) == ("REQUEST_VERIFICATION", "TIMEOUT")
    assert rb["signals"]["identity"]["status"] == "UNKNOWN" and rb["next_action"] == "CHALLENGE"
    assert rb["attempt_id"] == b

    # A MATCH for B that arrives late is logged, stored aside, never used.
    assert _identity(client, b).status_code == 200
    assert STORE.get(b).identity is None and len(STORE.get(b).identity_late) == 1
    again = _liveness(client, b, quick=True).json()
    assert again == rb and again["decision"] != "ALLOW"  # first-pass decision is FINAL

    # A's decision is untouched by anything that happened to B.
    assert STORE.get(a).decision == ra

    # Unknown attempt ids are rejected outright.
    assert _identity(client, "does-not-exist").status_code == 404
    assert _liveness(client, "does-not-exist").status_code == 404

    # Exactly one log line per decision (A, B), none for the replay/late result.
    lines = open(config.ATTEMPT_LOG_PATH, encoding="utf-8").read().splitlines()
    assert len(lines) == 2


def test_c_passive_spoof_then_passed_challenge_is_deny(client, monkeypatch):
    # With PASSIVE_DENY_ENABLED on, a passive SPOOF then a passed challenge is DENY.
    spoof_then_pass = {"quality": GOOD_Q, "passive": {"status": "SPOOF", "score": 0.99},
                       "identity": MATCH, "passive_allow_enabled": True, **OK,
                       "challenge": {"status": "PASS", "passive": LIVE, "timed_out": False,
                                     "identity": MATCH}}
    assert decide({**spoof_then_pass, "passive_deny_enabled": True}) == \
        ("DENY", "SPOOF_PASSIVE", "NONE")
    # Default (flag off, gate NO-GO): SPOOF is log-only, the challenge decides.
    assert decide({**spoof_then_pass, "passive_deny_enabled": False})[0] == "ALLOW"

    # Through the API with the flag on: SPOOF first pass is final, no step-up.
    monkeypatch.setattr(config, "PASSIVE_DENY_ENABLED", True)
    monkeypatch.setattr(pipeline, "passive_liveness",
                        lambda f: {"status": "SPOOF", "score": 0.99, "per_frame": []}, raising=False)
    aid = _start(client)
    _identity(client, aid)
    assert _liveness(client, aid).json()["decision"] == "DENY"
    assert client.post(f"/attempt/{aid}/challenge/start").status_code == 409


def test_d_perfect_biometrics_but_schedule_prohibited_is_deny(client, monkeypatch):
    perfect = {"quality": GOOD_Q, "passive": LIVE, "identity": MATCH,
               "passive_allow_enabled": True, "mode": "ONLINE",
               "challenge": {"status": "PASS", "passive": LIVE, "timed_out": False,
                             "identity": MATCH}}
    assert decide({**perfect, "schedule": PERMITTED})[0] == "ALLOW"  # control
    assert decide({**perfect, "schedule": {"status": "PROHIBITED", "policy_version": "t1"}}) \
        == ("DENY", "SCHEDULE_DENIED", "NONE")

    # Through the API with the real schedule file + demo clock.
    assert client.post("/operator/clock", json={"iso_time": WED_1230}).json()["demo_clock"]
    aid = _start(client)
    _identity(client, aid)
    r = _liveness(client, aid).json()
    assert (r["decision"], r["reason_code"]) == ("DENY", "SCHEDULE_DENIED")
    assert r["schedule"] == {"status": "PROHIBITED", "policy_version": "t1"}
    assert r["signals"]["identity"]["status"] == "MATCH" and r["mode"] == "ONLINE"

    # Re-evaluated at the final decision: a challenge started at 10:00 (allowed)
    # but finished at 12:30 (deny window) is DENY even though the challenge passed.
    monkeypatch.setattr(pipeline, "passive_liveness",
                        lambda f: {"status": "UNCERTAIN", "score": 0.5, "per_frame": []},
                        raising=False)
    monkeypatch.setattr(pipeline, "quality", lambda f: dict(
        GOOD_Q, per_frame=[{"ok": True, "face_count": 1, "face_px": 200}] * len(f)), raising=False)
    monkeypatch.setattr(pipeline, "challenge_check", lambda f, t, pt, a: {
        "status": "PASS", "per_action": {}, "passive": LIVE}, raising=False)
    client.post("/operator/clock", json={"iso_time": WED_10})
    aid = _start(client)
    _identity(client, aid)
    assert _liveness(client, aid).json()["next_action"] == "CHALLENGE"
    ch = client.post(f"/attempt/{aid}/challenge/start").json()
    # Step-up = exactly one pose + one expression, random order.
    assert sorted(a in ("TURN_LEFT", "TURN_RIGHT") for a in ch["actions"]) == [False, True]
    assert sorted(a in ("SMILE", "OPEN_MOUTH") for a in ch["actions"]) == [False, True]
    client.post("/operator/clock", json={"iso_time": WED_1230})
    r = client.post(f"/attempt/{aid}/challenge", data={"challenge_id": ch["challenge_id"]},
                    files=[("frames", ("f.jpg", FRAME, "image/jpeg"))] * 3).json()
    assert (r["decision"], r["reason_code"]) == ("DENY", "SCHEDULE_DENIED")


PIN = "4321"


def _staff(c, aid, approve=True, pin=PIN, id_checked=True, reason="checked campus ID card"):
    return c.post(f"/attempt/{aid}/staff_verify", json={
        "operator_pin": pin, "id_checked": id_checked, "reason": reason, "approve": approve})


def test_e_offline_never_auto_allows_and_staff_cannot_override_spoof_or_schedule(
        client, monkeypatch):
    perfect = {"quality": GOOD_Q, "passive": LIVE, "identity": MATCH, "challenge": None,
               "passive_allow_enabled": True, "schedule": PERMITTED}
    assert decide({**perfect, "mode": "OFFLINE"}) == \
        ("REQUEST_VERIFICATION", "OFFLINE_STAFF_REQUIRED", "STAFF_VERIFY")
    staff_ok = {"approve": True, "id_checked": True, "reason": "ID checked"}
    on = {"passive_deny_enabled": True}  # staff can't override SPOOF while the flag is on
    assert decide_staff({"passive": {"status": "SPOOF"}, "schedule": PERMITTED,
                         "staff": staff_ok, **on})[0] == "DENY"
    assert decide_staff({"passive": LIVE, "challenge_passive": {"status": "SPOOF"},
                         "schedule": PERMITTED, "staff": staff_ok, **on})[0] == "DENY"
    assert decide_staff({"passive": LIVE, "schedule": {"status": "PROHIBITED"},
                         "staff": staff_ok}) == ("DENY", "SCHEDULE_DENIED", "NONE")

    monkeypatch.setenv("OPERATOR_PIN_HASH", hashlib.sha256(PIN.encode()).hexdigest())
    monkeypatch.setattr(mode, "current", lambda: "OFFLINE")

    # Offline + perfect signals -> STEP-UP, never ALLOW, until staff verifies.
    aid = _start(client)
    _identity(client, aid)
    r = _liveness(client, aid).json()
    assert (r["decision"], r["reason_code"], r["next_action"], r["mode"]) ==         ("REQUEST_VERIFICATION", "OFFLINE_STAFF_REQUIRED", "STAFF_VERIFY", "OFFLINE")
    assert _staff(client, aid, pin="0000").status_code == 403
    assert _staff(client, aid, id_checked=False).status_code == 400
    assert _staff(client, aid, reason="  ").status_code == 400
    r = _staff(client, aid).json()
    assert (r["decision"], r["reason_code"]) == ("ALLOW", "STAFF_VERIFIED")
    assert r["staff"]["method"] == "physical_id" and r["staff"]["operator_id"]

    # Offline SPOOF (deny flag on) -> DENY, and staff cannot override it.
    monkeypatch.setattr(config, "PASSIVE_DENY_ENABLED", True)
    monkeypatch.setattr(pipeline, "passive_liveness",
                        lambda f: {"status": "SPOOF", "score": 0.99, "per_frame": []}, raising=False)
    aid = _start(client)
    assert _liveness(client, aid).json()["decision"] == "DENY"
    assert _staff(client, aid).status_code == 409
    assert STORE.get(aid).staff_decision is None

    # PROHIBITED by the time staff decides -> DENY even with approve=true.
    monkeypatch.setattr(config, "PASSIVE_DENY_ENABLED", False)
    monkeypatch.setattr(pipeline, "passive_liveness", lambda f: dict(LIVE, per_frame=[]),
                        raising=False)
    aid = _start(client)
    assert _liveness(client, aid).json()["next_action"] == "STAFF_VERIFY"
    client.post("/operator/clock", json={"iso_time": WED_1230})
    r = _staff(client, aid).json()
    assert (r["decision"], r["reason_code"]) == ("DENY", "SCHEDULE_DENIED")

    # Every decision became its own outbox event; staff events carry operator_id.
    rows = outbox.get().unsynced()
    assert len(rows) == 5 and sum(r["verification_method"] == "physical_id" for r in rows) == 2


class _FakeS3:
    """Stands in for the central store: objects keyed by S3 key."""
    def __init__(self):
        self.objects, self.puts, self.fail_next = {}, 0, False

    def head_bucket(self, Bucket):
        return {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts += 1
        if self.fail_next:
            self.fail_next = False
            return {"ResponseMetadata": {"HTTPStatusCode": 500}}
        self.objects[Key] = json.loads(Body)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def test_f_sync_is_idempotent_and_outbox_survives_reopen(tmp_path):
    db = tmp_path / "outbox.db"
    box = outbox.Outbox(db)
    eid = box.add(attempt_id="a1", user_id="u1", decision="ALLOW", reason_code="OK_LIVE_MATCH",
                  policy_version="t1", verification_method="face")
    fake = _FakeS3()
    up = sync_worker.S3Uploader(client=fake, bucket="test-bucket")

    fake.fail_next = True  # non-200 -> stays pending
    assert sync_worker.sync_once(box, up) == {"sent": 0, "failed": 1}
    assert box.counts() == (1, 0)
    assert sync_worker.sync_once(box, up) == {"sent": 1, "failed": 0}

    # Lost ack: the same event is sent again -> still ONE central record.
    box._db.execute("UPDATE events SET synced=0 WHERE event_id=?", (eid,))
    assert sync_worker.sync_once(box, up)["sent"] == 1
    assert fake.puts == 3 and list(fake.objects) == [f"events/{eid}.json"]
    record = fake.objects[f"events/{eid}.json"]
    assert set(record) == set(outbox.COLUMNS) - {"synced"}
    assert "u1" not in json.dumps(record) and record["decision"] == "ALLOW"

    # Survives restart: reopen the file, same rows, flags and pseudonym salt.
    pseudonym = box.pseudonym("u1")
    box.add(attempt_id="a2", user_id="u1", decision="DENY", reason_code="SPOOF_PASSIVE",
            policy_version="t1", verification_method="face")
    box.close()
    box2 = outbox.Outbox(db)
    assert box2.counts() == (1, 1)
    assert box2.get(eid)["synced"] == 1
    assert box2.pseudonym("u1") == pseudonym
    box2.close()
