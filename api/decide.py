"""Decision engine: a pure function from signals to (decision, reason, next_action).

The only path to ALLOW is an explicit whitelist at the end of each phase.
Anything missing, UNKNOWN, UNAVAILABLE, late or unrecognised falls through
to REQUEST_VERIFICATION. "Nothing bad found" is never a reason to ALLOW.
Pixel-difference motion and spoof labels are not inputs here at all.

signals = {
  "quality":   {"ok": bool|None, "fail_reason": str|None, "continuity_ok": bool|None} | None,
  "passive":   {"status": "LIVE"|"SPOOF"|"UNCERTAIN"|"UNAVAILABLE", "score": float|None} | None,
  "identity":  {"status": "MATCH"|"MISMATCH"|"UNKNOWN", "similarity": float|None} | None,
               # None = no result arrived before the deadline
  "challenge": None (first pass) |
               {"status": "PASS"|"FAIL"|"UNCERTAIN"|"UNAVAILABLE",
                "passive": {"status", "score"} | None, "timed_out": bool,
                "identity": {"status", "similarity"} | None},
               # challenge.identity = CompareFaces re-run on a good challenge
               # frame. In the challenge phase it REPLACES top-level identity.
  "quick_challenge": {"status": "PASS"|"FAIL"|"UNCERTAIN"|..., "action": str} | None,
               # 2 s redesign: the ONE action shown at the tap, checked in the first pass
  "passive_deny_enabled": bool,   # False (default): passive SPOOF is logged only
  "schedule":  {"status": "PERMITTED"|"PROHIBITED"|"APPROVAL_REQUIRED"|"UNKNOWN",
                "policy_version": str|None} | None,   # evaluated at decision time
  "mode":      "ONLINE"|"OFFLINE",                     # anything else = OFFLINE
}

GATE (14 Nov, NO-GO on passive): genuine faces scored below the passive
threshold 4/10 times, so passive can neither approve nor deny by default.

Order: ID_MISMATCH -> DENY (and passive SPOOF -> DENY only if
passive_deny_enabled); schedule PROHIBITED -> DENY; OFFLINE -> STAFF_VERIFY.
2 s redesign: first-pass ALLOW = identity MATCH (on time) + quick 1-action
challenge PASS + good, continuous quality + schedule PERMITTED + ONLINE.
Anything else in the first pass -> REQUEST_VERIFICATION + CHALLENGE (the
2-action step-up; may exceed 2 s, logged as total_ms). Step-up ALLOW =
challenge PASS + challenge-frame MATCH + good quality + PERMITTED; step-up
FAIL, UNCERTAIN or anything else -> RECEPTION. Passive is never required.

Passive DOWNGRADE (Anil's live data: passive flagged 5/6 attacks SPOOF, 5/5
genuine LIVE; a replayed smile passed the 1-action check twice): passive SPOOF
blocks an ALLOW but never causes DENY (unless PASSIVE_DENY_ENABLED):
  quick path + passive SPOOF            -> REQUEST_VERIFICATION + CHALLENGE
  quick path + passive not LIVE (UNCERTAIN/UNAVAILABLE/missing), while
    quick_requires_passive_live (default True, QUICK_REQUIRES_PASSIVE_LIVE=1)
                                        -> REQUEST_VERIFICATION + CHALLENGE
  step-up PASS + SPOOF on challenge frames -> RECEPTION
APPROVAL_REQUIRED/UNKNOWN -> STAFF_VERIFY.
"""
from typing import Any

ALLOW, REQUEST, DENY = "ALLOW", "REQUEST_VERIFICATION", "DENY"

# quality.fail_reason (from pipeline.quality) -> (reason_code, next_action)
_QUALITY_FAIL = {
    "TOO_SMALL": ("QUALITY_TOO_SMALL", "MOVE_CLOSER"),
    "QUALITY_TOO_SMALL": ("QUALITY_TOO_SMALL", "MOVE_CLOSER"),
    "BLUR": ("QUALITY_BLUR", "HOLD_STILL"),
    "QUALITY_BLUR": ("QUALITY_BLUR", "HOLD_STILL"),
    "DARK": ("QUALITY_DARK", "IMPROVE_LIGHT"),
    "QUALITY_DARK": ("QUALITY_DARK", "IMPROVE_LIGHT"),
    "MULTI_FACE": ("MULTI_FACE", "RETRY"),
    "NO_FACE": ("NO_FACE", "MOVE_CLOSER"),
    "TRACKING_LOST": ("TRACKING_LOST", "HOLD_STILL"),
}

Decision = tuple[str, str, str]


def _status(sig: Any) -> str | None:
    return sig.get("status") if isinstance(sig, dict) else None


def _quality_ok(q: Any) -> bool:
    return isinstance(q, dict) and q.get("ok") is True


def _continuity_ok(q: Any) -> bool:
    return isinstance(q, dict) and q.get("continuity_ok") is True


def _quality_problem(q: Any) -> Decision | None:
    """A specific, user-fixable quality failure, or None if not one."""
    if not isinstance(q, dict):
        return None
    if q.get("ok") is False:
        reason, action = _QUALITY_FAIL.get(
            str(q.get("fail_reason") or "").upper(), ("QUALITY_BLUR", "RETRY"))
        return REQUEST, reason, action
    if q.get("ok") is True and q.get("continuity_ok") is False:
        return REQUEST, "TRACKING_LOST", "HOLD_STILL"
    return None


def _passive_spoof(signals: dict) -> bool:
    """Passive SPOOF anywhere -- counts only when PASSIVE_DENY_ENABLED is on."""
    if signals.get("passive_deny_enabled") is not True:
        return False
    ch = signals.get("challenge")
    return "SPOOF" in (_status(signals.get("passive")),
                       _status(ch.get("passive")) if isinstance(ch, dict) else None,
                       _status(signals.get("challenge_passive")))


def _deny(signals: dict) -> Decision | None:
    """Hard negatives. Checked first in every phase; nothing overrides them."""
    if _passive_spoof(signals):
        return DENY, "SPOOF_PASSIVE", "NONE"
    ch = signals.get("challenge")
    # Identity in force: the challenge-frame result once a challenge ran.
    ident = ch.get("identity") if isinstance(ch, dict) else signals.get("identity")
    if _status(ident) == "MISMATCH" and _quality_ok(signals.get("quality")):
        return DENY, "ID_MISMATCH", "NONE"
    return None


def _first_pass(signals: dict) -> Decision:
    """ALLOW only on the quick-challenge whitelist; else REQUEST_VERIFICATION + CHALLENGE."""
    q = signals.get("quality")
    ident = signals.get("identity")
    passive = _status(signals.get("passive"))
    quick = _status(signals.get("quick_challenge"))

    if (signals.get("fallback_challenge_only") is True and passive == "LIVE"
            and _status(ident) == "MATCH" and _quality_ok(q) and _continuity_ok(q)):
        return ALLOW, "OK_LIVE_MATCH", "NONE"

    if (quick == "PASS" and _status(ident) == "MATCH"
            and _quality_ok(q) and _continuity_ok(q)):
        if passive == "SPOOF":  # downgrade: step-up, never DENY
            return REQUEST, "LIVENESS_UNCERTAIN", "CHALLENGE"
        # Missing flag = True (safe default): quick ALLOW needs passive LIVE.
        if signals.get("quick_requires_passive_live") is not False and passive != "LIVE":
            return REQUEST, "LIVENESS_UNCERTAIN", "CHALLENGE"
        return ALLOW, "OK_LIVE_MATCH", "NONE"  # schedule/mode still checked by decide()

    problem = _quality_problem(q)
    if problem:
        return REQUEST, problem[1], "CHALLENGE"
    if ident is None:
        return REQUEST, "TIMEOUT", "CHALLENGE"
    if _status(ident) != "MATCH":
        return REQUEST, "ID_UNAVAILABLE", "CHALLENGE"
    if quick == "FAIL":
        return REQUEST, "CHALLENGE_FAILED", "CHALLENGE"
    if quick is None and passive not in ("LIVE", "UNCERTAIN", "SPOOF"):
        return REQUEST, "LIVENESS_UNAVAILABLE", "CHALLENGE"
    return REQUEST, "LIVENESS_UNCERTAIN", "CHALLENGE"  # quick action not seen / not run


def _after_challenge(signals: dict) -> Decision:
    """One challenge per attempt: anything short of a clean pass -> RECEPTION."""
    ch = signals["challenge"]
    q = signals.get("quality")
    if ch.get("timed_out") is True:
        return REQUEST, "TIMEOUT", "RECEPTION"
    if _status(ch) == "FAIL":
        return REQUEST, "CHALLENGE_FAILED", "RECEPTION"
    if _status(ch) != "PASS":  # UNCERTAIN / UNAVAILABLE / missing
        return REQUEST, "MANUAL_REVIEW", "RECEPTION"
    if _status(ch.get("identity")) != "MATCH":  # UNKNOWN / missing
        return REQUEST, "ID_UNAVAILABLE", "RECEPTION"
    if not (_quality_ok(q) and _continuity_ok(q)):
        return REQUEST, "MANUAL_REVIEW", "RECEPTION"
    if _status(ch.get("passive")) == "SPOOF":  # downgrade: reception, never DENY
        return REQUEST, "MANUAL_REVIEW", "RECEPTION"
    # Passive LIVE is NOT required (it can miss genuine faces); only SPOOF blocks.
    return ALLOW, "OK_LIVE_MATCH", "NONE"


def decide(signals: dict) -> Decision:
    """Pure: no I/O, no clock, no config reads. Same input -> same output."""
    if not isinstance(signals, dict):
        return REQUEST, "MANUAL_REVIEW", "RETRY"
    denied = _deny(signals)
    if denied:
        return denied
    sched = _status(signals.get("schedule"))
    if sched == "PROHIBITED":
        return DENY, "SCHEDULE_DENIED", "NONE"
    if signals.get("mode") != "ONLINE":
        return REQUEST, "OFFLINE_STAFF_REQUIRED", "STAFF_VERIFY"
    if signals.get("challenge") is not None:
        if not isinstance(signals["challenge"], dict):
            return REQUEST, "MANUAL_REVIEW", "RECEPTION"
        bio = _after_challenge(signals)
    else:
        bio = _first_pass(signals)
    if bio[0] != ALLOW:
        return bio
    if sched == "PERMITTED":
        return bio
    if sched == "APPROVAL_REQUIRED":
        return REQUEST, "SCHEDULE_APPROVAL_REQUIRED", "STAFF_VERIFY"
    return REQUEST, "SCHEDULE_UNKNOWN", "STAFF_VERIFY"


def decide_staff(signals: dict) -> Decision:
    """Staff decision after a physical ID check. Pure.

    signals = {"passive": first-pass passive, "challenge_passive": ... | None,
               "passive_deny_enabled": bool,
               "schedule": evaluated NOW, "staff": {"approve", "id_checked", "reason"}}
    Staff can never override a PROHIBITED schedule, nor a passive SPOOF while
    PASSIVE_DENY_ENABLED is on (when off, SPOOF is log-only, like everywhere else).
    """
    if not isinstance(signals, dict):
        return REQUEST, "MANUAL_REVIEW", "STAFF_VERIFY"
    if _passive_spoof(signals):
        return DENY, "SPOOF_PASSIVE", "NONE"
    if _status(signals.get("schedule")) == "PROHIBITED":
        return DENY, "SCHEDULE_DENIED", "NONE"
    staff = signals.get("staff") if isinstance(signals.get("staff"), dict) else {}
    if staff.get("id_checked") is not True or not str(staff.get("reason") or "").strip():
        return REQUEST, "MANUAL_REVIEW", "STAFF_VERIFY"
    if staff.get("approve") is True:
        return ALLOW, "STAFF_VERIFIED", "NONE"
    return DENY, "STAFF_REJECTED", "NONE"


MESSAGES = {
    "OK_LIVE_MATCH": "Welcome.",
    "SPOOF_PASSIVE": "Entry denied.",
    "ID_MISMATCH": "Entry denied.",
    "ID_UNAVAILABLE": "We couldn't confirm who you are. Please try again.",
    "LIVENESS_UNCERTAIN": "One more quick check, please.",
    "LIVENESS_UNAVAILABLE": "Liveness check unavailable. Please try again.",
    "QUALITY_TOO_SMALL": "Please move closer.",
    "QUALITY_BLUR": "Please hold still.",
    "QUALITY_DARK": "Please find better light.",
    "MULTI_FACE": "Only one person in frame, please.",
    "NO_FACE": "We can't see your face. Please look at the camera.",
    "TRACKING_LOST": "Please stay in frame and hold still.",
    "TIMEOUT": "That took too long. Please try again.",
    "CHALLENGE_FAILED": "Please see reception.",
    "MANUAL_REVIEW": "Please see reception.",
    "SCHEDULE_DENIED": "Entry not permitted at this time.",
    "SCHEDULE_APPROVAL_REQUIRED": "Staff approval needed for this time.",
    "SCHEDULE_UNKNOWN": "Staff will verify your access.",
    "OFFLINE_STAFF_REQUIRED": "Kiosk offline. Staff will verify your ID.",
    "STAFF_VERIFIED": "Welcome. Verified by staff.",
    "STAFF_REJECTED": "Entry denied by staff.",
}


def message_for(reason: str, next_action: str) -> str:
    if next_action == "RECEPTION":
        return "Please see reception."
    if next_action == "CHALLENGE":
        return "One more quick check, please."
    if next_action == "STAFF_VERIFY" and reason not in MESSAGES:
        return "Please wait for staff to verify your ID."
    return MESSAGES.get(reason, "Please try again.")
