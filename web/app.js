/* Secure Entry kiosk — v2 attempt flow (CONTRACT.md "v2 attempt API", api/main.py on akash/api-v2).
 * Plain JS, no build step, no dependencies.
 *
 *   Verify ─► POST /attempt/start ┐
 *          └► first frame ────────┴► POST /attempt/{id}/identity   (fired, never awaited)
 *                                 └► 6 frames @170ms ─► POST /attempt/{id}/liveness ─► first decision (≤ 2.00 s)
 *   Pending + CHALLENGE ─► "Continue verification" ─► /challenge/start ─► prompts @5fps ─► /challenge ─► final
 *
 * Safety: the screen shows "Access Approved" only when the server says ALLOW *and* its signals say
 * passive=LIVE and identity=MATCH. Anything else (missing, late, error, timeout) is never approval.
 */

// ============================================================
// CONFIG
// ============================================================
const params = new URLSearchParams(location.search);

const CONFIG = {
  // The one flag. true = local mock (clearly labelled MOCK). false = real backend.
  // URL override: ?mock=0 / ?mock=1
  USE_MOCK: params.get("mock") === "1",

  // "" = same origin (use web/serve.py to proxy the API). URL override: ?api=http://host:8000
  API_BASE: params.get("api") || "",

  // Face-only identification (1:N): no ID field. The v4 backend still REQUIRES user_id, so this is
  // off by default (1:1 mode with the ID field). See web/IDENTIFY_API.md. URL override: ?identify=1
  IDENTIFY_MODE: params.has("identify") ? params.get("identify") !== "0" : false,

  // Optional suggestions for the ID field in 1:1 mode (ids only, no names shown). Empty = free text.
  ENROLLED_IDS: [],
  USER_ID_RE: /^[A-Za-z0-9_-]{1,64}$/, // same rule as api/identity.py

  FRAME_MAX_WIDTH: 640,
  JPEG_QUALITY: 0.85,
  // CONTRACT v4 first pass: recording starts at the tap (before the prompt), 14 frames at 10 fps,
  // the quick action from /attempt/start is shown once >= BASELINE_FRAMES are captured.
  LIVENESS_FRAMES: 14,         // includes the first (identity) frame; server max 16
  LIVENESS_FPS: 10,
  BASELINE_FRAMES: 3,          // frames captured before the prompt appears (server needs >= 2)
  FIRST_PASS_DEADLINE_MS: Number(params.get("deadline")) || 2000, // HID requirement; from the first captured frame
  CHALLENGE_FPS: 5,
  CHALLENGE_BASELINE_MS: 400,
  CHALLENGE_ACTION_MS: 1500,

  RESULT_RESET_MS: 5000,       // back to home after a final result
  ACTION_RESET_MS: 12000,      // longer when a Continue / Try again button is offered
  REQUEST_TIMEOUT_MS: 8000,
  STATUS_POLL_MS: 3000,        // operator status table refresh while the panel is open
};

// ============================================================
// API ADAPTER — matches api/main.py on akash/api-v2.
//   POST /attempt/start                json {user_id, consent:true}
//   POST /attempt/{id}/identity        multipart: frame, capture_ts_ms
//   POST /attempt/{id}/liveness        multipart: frames (repeated), capture_ts_ms (repeated), scenario
//   POST /attempt/{id}/challenge/start
//   POST /attempt/{id}/challenge       multipart: frames, capture_ts_ms, prompt_ts_ms (one int), challenge_id
// `scenario` is added to every multipart request and to the start body; endpoints that don't read it ignore it.
// Timestamps are integer epoch ms (performance.timeOrigin + performance.now(): monotonic within the page).
// ============================================================
const enc = encodeURIComponent;

const realApi = {
  // Identify mode omits user_id entirely; 1:1 mode sends the typed ID.
  startAttempt: ({ user_id }) =>
    postJson("/attempt/start", { ...(user_id ? { user_id } : {}), consent: true, scenario: state.scenario }),

  identity: ({ attempt_id, frame }) =>
    postForm(`/attempt/${enc(attempt_id)}/identity`, [
      ["frame", frame.blob, "frame_0.jpg"],
      ["capture_ts_ms", frame.ts],
    ]),

  liveness: ({ attempt_id, frames, prompt_ts }) =>
    postForm(`/attempt/${enc(attempt_id)}/liveness`, [
      ...frames.map((f, i) => ["frames", f.blob, `frame_${i}.jpg`]),
      ...frames.map((f) => ["capture_ts_ms", f.ts]),
      ["prompt_ts_ms", prompt_ts],
    ]),

  challengeStart: ({ attempt_id }) => postForm(`/attempt/${enc(attempt_id)}/challenge/start`, []),

  status: () => request("/status", { method: "GET" }),

  challenge: ({ attempt_id, challenge_id, frames, prompt_ts, baseline_ts }) =>
    postForm(`/attempt/${enc(attempt_id)}/challenge`, [
      ...frames.map((f, i) => ["frames", f.blob, `c_${i}.jpg`]),
      ...frames.map((f) => ["capture_ts_ms", f.ts]),
      // Server reads one integer (the first action's prompt time); extra values are comma-separated and ignored.
      ["prompt_ts_ms", prompt_ts.join(",")],
      ["baseline_start_ts_ms", baseline_ts],
      ["challenge_id", challenge_id],
    ]),
};

function postForm(path, entries) {
  const fd = new FormData();
  for (const [k, v, name] of entries) {
    if (v === undefined || v === null) continue;
    if (v instanceof Blob) fd.append(k, v, name); else fd.append(k, String(v));
  }
  fd.append("scenario", state.scenario);
  return request(path, { method: "POST", body: fd });
}

function postJson(path, body) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function request(path, init) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), CONFIG.REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(CONFIG.API_BASE + path, { ...init, signal: ctrl.signal });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch {}
      const err = new Error(`${path} returned ${res.status}${detail ? `: ${detail}` : ""}`);
      err.status = res.status;
      throw err;
    }
    const text = await res.text();
    return text ? JSON.parse(text) : {};
  } finally {
    clearTimeout(timer);
  }
}

const nowEpochMs = () => Math.round(performance.timeOrigin + performance.now());

// ============================================================
// MOCK — clearly labelled. Default outcome is REQUEST_VERIFICATION; ALLOW/DENY only when the
// operator picks them to preview the result screens. Never used when USE_MOCK is false.
// ============================================================
const MOCK_MATCH_ID = "mock-user"; // what the mock "recognises" in identify mode
const MOCK_CYCLE = ["CHALLENGE", "MOVE_CLOSER", "HOLD_STILL", "IMPROVE_LIGHT", "RETRY", "RECEPTION", "NONE"];
let mockCycleIdx = 0;
const MOCK_REASON = {
  CHALLENGE: "LIVENESS_UNCERTAIN", MOVE_CLOSER: "QUALITY_TOO_SMALL", HOLD_STILL: "QUALITY_BLUR",
  IMPROVE_LIGHT: "QUALITY_DARK", RETRY: "TRACKING_LOST", RECEPTION: "MANUAL_REVIEW", NONE: "LIVENESS_UNAVAILABLE",
};

const mockApi = {
  async startAttempt(f) {
    log("mock start", { ...f, scenario: state.scenario });
    await sleep(rand(40, 90));
    const t0 = Date.now();
    return { attempt_id: "mock-" + uuid(), server_t0_ms: t0, deadline_ms: t0 + 1800,
             action: Math.random() < 0.5 ? "SMILE" : "OPEN_MOUTH" };
  },
  async identity(f) {
    log("mock identity", { attempt_id: f.attempt_id, capture_ts_ms: f.frame.ts, bytes: f.frame.blob.size });
    const ms = Math.round(rand(250, 450));
    await sleep(ms);
    const o = els.mockOutcome.value;
    if (o === "ALLOW") return { identity: "MATCH", user_id: MOCK_MATCH_ID, similarity: 99.2, ms };
    if (o === "DENY_MISMATCH") return { identity: "MISMATCH", user_id: null, similarity: 31.5, ms };
    return { identity: "UNKNOWN", user_id: null, similarity: null, ms };
  },
  async liveness(f) {
    log("mock liveness", { attempt_id: f.attempt_id, frames: f.frames.length, capture_ts_ms: f.frames.map((x) => x.ts), prompt_ts_ms: f.prompt_ts });
    await sleep(rand(200, 400));
    const o = els.mockOutcome.value;
    const quick = (status) => ({ action: "SMILE", status, reason: "MOCK" });
    const withQuick = (d, status) => { d.signals.challenge = quick(status); return d; };
    if (els.mockMode.value === "OFFLINE") return mockDecision(f.attempt_id, "REQUEST_VERIFICATION", "OFFLINE_STAFF_REQUIRED", "STAFF_VERIFY", { status: "UNCERTAIN", score: 0.55 }, { status: "UNKNOWN", similarity: null });
    if (els.mockCampus.value === "PROHIBITED") return mockDecision(f.attempt_id, "DENY", "SCHEDULE_DENIED", "NONE", { status: "UNCERTAIN", score: 0.6 }, { status: "MATCH", similarity: 99.2 });
    // v4: first-pass ALLOW = identity MATCH + quick action PASS + campus PERMITTED + ONLINE.
    if (o === "ALLOW" && els.mockCampus.value === "PERMITTED") return withQuick(mockDecision(f.attempt_id, "ALLOW", "OK_LIVE_MATCH", "NONE", { status: "UNCERTAIN", score: 0.62 }, { status: "MATCH", similarity: 99.2, user_id: MOCK_MATCH_ID }), "PASS");
    if (o === "ALLOW") return withQuick(mockDecision(f.attempt_id, "REQUEST_VERIFICATION", els.mockCampus.value === "APPROVAL_REQUIRED" ? "SCHEDULE_APPROVAL_REQUIRED" : "SCHEDULE_UNKNOWN", "STAFF_VERIFY", { status: "UNCERTAIN", score: 0.62 }, { status: "MATCH", similarity: 99.2 }), "PASS");
    if (o === "STEPUP_ALLOW") return withQuick(mockDecision(f.attempt_id, "REQUEST_VERIFICATION", "CHALLENGE_FAILED", "CHALLENGE", { status: "UNCERTAIN", score: 0.6 }, { status: "MATCH", similarity: 99.2 }), "FAIL");
    if (o === "DENY_SPOOF") return mockDecision(f.attempt_id, "DENY", "SPOOF_PASSIVE", "NONE", { status: "SPOOF", score: 0.12 }, { status: "UNKNOWN", similarity: null });
    if (o === "DENY_MISMATCH") return mockDecision(f.attempt_id, "DENY", "ID_MISMATCH", "NONE", { status: "LIVE", score: 0.9 }, { status: "MISMATCH", similarity: 31.5 });
    let next = els.mockNext.value;
    if (next === "auto") { next = MOCK_CYCLE[mockCycleIdx % MOCK_CYCLE.length]; mockCycleIdx++; }
    return mockDecision(f.attempt_id, "REQUEST_VERIFICATION", MOCK_REASON[next], next, { status: "UNCERTAIN", score: 0.55 }, { status: "UNKNOWN", similarity: null });
  },
  async challengeStart(f) {
    log("mock challenge/start", f);
    await sleep(rand(60, 120));
    // v4 step-up: one pose + one expression, random order
    const actions = shuffle([shuffle(["TURN_LEFT", "TURN_RIGHT"])[0], shuffle(["SMILE", "OPEN_MOUTH"])[0]]);
    return { challenge_id: "mockc-" + uuid(), actions, baseline_ms: 400, timeout_ms: 5000 };
  },
  async challenge(f) {
    log("mock challenge", { attempt_id: f.attempt_id, frames: f.frames.length, prompt_ts_ms: f.prompt_ts });
    await sleep(rand(300, 600));
    const id = { status: "MATCH", similarity: 99.1, user_id: MOCK_MATCH_ID };
    let d;
    if (els.mockOutcome.value !== "STEPUP_ALLOW") d = mockDecision(f.attempt_id, "REQUEST_VERIFICATION", "CHALLENGE_FAILED", "RECEPTION", { status: "UNCERTAIN", score: 0.5 }, { status: "UNKNOWN", similarity: null });
    else if (els.mockCampus.value === "PERMITTED") d = mockDecision(f.attempt_id, "ALLOW", "OK_LIVE_MATCH", "NONE", { status: "UNCERTAIN", score: 0.64 }, id);
    else d = mockDecision(f.attempt_id, "REQUEST_VERIFICATION", els.mockCampus.value === "APPROVAL_REQUIRED" ? "SCHEDULE_APPROVAL_REQUIRED" : "SCHEDULE_UNKNOWN", "STAFF_VERIFY", { status: "UNCERTAIN", score: 0.64 }, id);
    return { ...d, total_ms: 0 };
  },
  async status() {
    await sleep(rand(20, 60));
    const off = els.mockMode.value === "OFFLINE", pending = off ? 3 : 0;
    return { mode: els.mockMode.value, pending_sync: pending, synced: 12,
             policy_version: "MOCK-2026-11-14.1", kiosk_time: new Date().toISOString().slice(0, 19), demo_clock: false,
             passive_allow: "DISABLED: calibration gate failed (6/10 genuine)", challenge: "REQUIRED",
             identity_service: off ? "unavailable" : "available",
             policy: { status: "current", version: "MOCK-2026-11-14.1", expires_utc: "2026-12-31T00:00:00Z" },
             audit_queue: { pending, synced: 12 } };
  },
};

function mockDecision(attempt_id, decision, reason_code, next_action, passive, identity) {
  return {
    decision, reason_code, message: "MOCK", next_action, attempt_id,
    signals: { quality: { ok: true, continuity_ok: true }, passive, identity },
    schedule: { status: els.mockCampus.value, policy_version: "MOCK-2026-11-14.1" },
    mode: els.mockMode.value,
    stage_ms: { quality: 40, passive: 180, identity_wait: 120, decide: 1 },
    first_pass_server_ms: Math.round(rand(500, 900)),
  };
}

const api = CONFIG.USE_MOCK ? mockApi : realApi;

// ============================================================
// Copy + icons
// ============================================================
const REJECT_TEXT = {
  SPOOF_PASSIVE: "Spoof detected. A photo, screen or replay was presented instead of a live person.",
  ID_MISMATCH: "Identity mismatch. The face does not match the enrolled photo.",
  SCHEDULE_DENIED: "Campus access is not permitted at this time.",
  STAFF_REJECTED: "Entry denied by staff.",
};
const PENDING_TEXT = "We couldn't confidently complete verification.";

const REASON_DETAIL = {
  ID_UNAVAILABLE: "Identity could not be confirmed.",
  LIVENESS_UNCERTAIN: "Live-person check was not conclusive.",
  LIVENESS_UNAVAILABLE: "Live-person check is unavailable.",
  QUALITY_TOO_SMALL: "Face too far from the camera.",
  QUALITY_BLUR: "Image blurred by movement.",
  QUALITY_DARK: "Image too dark.",
  MULTI_FACE: "More than one face in view.",
  NO_FACE: "No face found in view.",
  TRACKING_LOST: "Face left the frame during the check.",
  TIMEOUT: "The check did not finish in time.",
  CHALLENGE_FAILED: "On-screen prompts were not completed.",
  MANUAL_REVIEW: "This entry needs a manual check.",
  SCHEDULE_APPROVAL_REQUIRED: "Staff approval is needed for entry at this time.",
  SCHEDULE_UNKNOWN: "Your campus access could not be confirmed.",
  OFFLINE_STAFF_REQUIRED: "The kiosk is offline. Staff will check your ID.",
};

const ICON = {
  check: '<circle cx="12" cy="12" r="10"/><path d="M7.5 12.5l3 3 6-6.5"/>',
  cross: '<circle cx="12" cy="12" r="10"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7"/>',
  shield: '<path d="M12 2.5l8 3v6c0 4.8-3.4 8.8-8 10-4.6-1.2-8-5.2-8-10v-6l8-3z"/><path d="M12 8v4.5M12 15.8h.01"/>',
  left: '<path d="M15 5l-7 7 7 7M8 12h13"/>',
  right: '<path d="M9 5l7 7-7 7M16 12H3"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  smile: '<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2.5 4 2.5 4-2.5 4-2.5M9 9h.01M15 9h.01"/>',
  mouth: '<circle cx="12" cy="12" r="10"/><ellipse cx="12" cy="15.5" rx="2.6" ry="3"/><path d="M9 9h.01M15 9h.01"/>',
  closer: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/><circle cx="12" cy="12" r="3"/>',
  still: '<path d="M7 11V6a2 2 0 014 0v5M11 10V4a2 2 0 014 0v7M15 9a2 2 0 014 0v5a7 7 0 01-7 7h-1a7 7 0 01-6-3.5L3 14a2 2 0 013.4-2L7 13"/>',
  light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  retry: '<path d="M3 12a9 9 0 0115.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 01-15.5 6.2L3 16M3 21v-5h5"/>',
  desk: '<circle cx="12" cy="7" r="3.5"/><path d="M5 21v-2a5 5 0 015-5h4a5 5 0 015 5v2M2 21h20"/>',
};
const svg = (name, width = 1.8) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round">${ICON[name] || ICON.eye}</svg>`;

const ACTION_UI = {
  BASELINE: { word: "Look at the camera", icon: "eye" },
  TURN_LEFT: { word: "Turn left", icon: "left" },
  TURN_RIGHT: { word: "Turn right", icon: "right" },
  SMILE: { word: "Smile", icon: "smile" },
  OPEN_MOUTH: { word: "Open your mouth", icon: "mouth" },
};

// mode: "continue" = Continue verification button, "retry" = Try again button, null = no extra button
const NEXT_UI = {
  MOVE_CLOSER: { icon: "closer", text: "Move closer", sub: "Fill more of the camera view with your face.", mode: "retry" },
  HOLD_STILL: { icon: "still", text: "Hold still", sub: "Keep your head steady while we check.", mode: "retry" },
  IMPROVE_LIGHT: { icon: "light", text: "Find better light", sub: "Face the light and avoid bright light behind you.", mode: "retry" },
  RETRY: { icon: "retry", text: "Please try again", sub: "Look straight at the camera.", mode: "retry" },
  CHALLENGE: { icon: "shield", text: "One more quick check", sub: "Follow two short prompts on screen.", mode: "continue" },
  RECEPTION: { icon: "desk", text: "Please see reception for manual review", sub: "A member of staff will help you.", mode: null },
  STAFF_VERIFY: { icon: "desk", text: "Please wait for staff", sub: "A member of staff will check your physical ID.", mode: null },
};

// Evidence list: fixed wording; a check mark appears only for a positive value the server actually returned.
const OFFLINE_NOTE = "Central identity service: unavailable \u00b7 Automatic entry: blocked \u00b7 Decision record: queued for sync";
const MARK = { ok: "\u2713", bad: "\u2717", na: "\u2013" };

// ============================================================
// DOM + state
// ============================================================
const $ = (id) => document.getElementById(id);
const els = {
  video: $("video"), canvas: $("canvas"),
  camStatus: $("camStatus"), camMessage: $("camMessage"), clock: $("clock"), mockBadge: $("mockBadge"),
  idForm: $("idForm"), userId: $("userId"), idHelp: $("idHelp"), enrolledList: $("enrolledList"), verifyBtn: $("verifyBtn"),
  hint: $("hint"), progress: $("progress"), progressLabel: $("progressLabel"), stopwatch: $("stopwatch"),
  prompt: $("prompt"), promptIcon: $("promptIcon"), promptWord: $("promptWord"),
  resultCard: $("resultCard"), resultUser: $("resultUser"), resultIcon: $("resultIcon"),
  resultHeading: $("resultHeading"), resultText: $("resultText"),
  mLiveness: $("mLiveness"), mSimilarity: $("mSimilarity"), mTime: $("mTime"), mTimeLabel: $("mTimeLabel"),
  timingDetail: $("timingDetail"), evidence: $("evidence"), offlineNote: $("offlineNote"),
  nextAction: $("nextAction"), nextIcon: $("nextIcon"), nextText: $("nextText"), nextSub: $("nextSub"),
  continueBtn: $("continueBtn"), retryBtn: $("retryBtn"), homeBtn: $("homeBtn"),
  resultMock: $("resultMock"), resetHint: $("resetHint"), attemptInfo: $("attemptInfo"),
  operator: $("operator"), scenario: $("scenario"), mockRows: $("mockRows"),
  mockOutcome: $("mockOutcome"), mockNext: $("mockNext"), mockCampus: $("mockCampus"), mockMode: $("mockMode"),
  opStatus: $("opStatus"), opStatusNote: $("opStatusNote"),
  opBackend: $("opBackend"), opEncoder: $("opEncoder"), opAttempt: $("opAttempt"), opTimings: $("opTimings"),
};

const state = {
  scenario: "genuine",
  cameraReady: false,
  flow: 0,                  // bumps on every attempt / reset; stale async work checks it and bails
  usedAttemptIds: new Set(),
  userId: null,
  attemptId: null,
  t0: 0,                    // performance.now() when the first frame was captured (monotonic)
  firstPassS: null,
  identityResult: null,     // {identity, similarity, ms, rtt_ms} once /identity answers
  challengeAvailable: false,
  stopwatchRaf: 0,
  resetTimer: 0,
  resetTick: 0,
  lastCapture: null,
  statusTimer: 0,
  statusSeq: 0,
};

class Cancelled extends Error {}
const guard = (flow) => { if (flow !== state.flow) throw new Cancelled(); };

// ============================================================
// Camera
// ============================================================
async function startCamera() {
  if (!navigator.mediaDevices?.getUserMedia) return cameraFailed("Camera access needs https or localhost.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
      audio: false,
    });
    els.video.srcObject = stream;
    els.video.addEventListener("loadedmetadata", fitCameraToVideo);
    els.video.addEventListener("resize", fitCameraToVideo);
    await els.video.play().catch(() => {});
    fitCameraToVideo();
    state.cameraReady = true;
    setCamStatus("ok", "Camera connected");
    els.camMessage.hidden = true;
    stream.getVideoTracks()[0]?.addEventListener("ended", () => cameraFailed("The camera was disconnected."));
  } catch (err) {
    cameraFailed(err?.name === "NotAllowedError" ? "Camera permission was denied." : "No camera was found.");
  }
  updateVerifyEnabled();
}

/** Make the preview frame the camera's real shape (e.g. 4:3), so there are no black bars and no cropping. */
function fitCameraToVideo() {
  const w = els.video.videoWidth, h = els.video.videoHeight;
  if (w && h) document.documentElement.style.setProperty("--cam-ar", String(w / h));
}

function cameraFailed(detail) {
  state.cameraReady = false;
  setCamStatus("err", "Camera unavailable");
  els.camMessage.textContent = `Camera unavailable\n${detail}${CONFIG.USE_MOCK ? "\n\nMock mode: verification runs with blank frames." : ""}`;
  els.camMessage.hidden = false;
  updateVerifyEnabled();
}

function setCamStatus(kind, text) {
  els.camStatus.className = `status ${kind}`;
  els.camStatus.querySelector(".status-text").textContent = text;
}

function updateVerifyEnabled() {
  els.verifyBtn.disabled = !state.cameraReady && !CONFIG.USE_MOCK;
}

// ============================================================
// Frame capture — JPEG encoding runs in a Web Worker (OffscreenCanvas) when supported,
// so the rendering thread only takes the snapshot.
// ============================================================
const encoder = (() => {
  const supported = typeof Worker !== "undefined" && typeof OffscreenCanvas !== "undefined"
    && typeof createImageBitmap === "function" && location.protocol !== "file:";
  let worker = null, seq = 0;
  const pending = new Map();
  if (supported) {
    try {
      worker = new Worker("frame-worker.js");
      worker.onmessage = (e) => {
        const p = pending.get(e.data.id);
        if (!p) return;
        pending.delete(e.data.id);
        e.data.error ? p.reject(new Error(e.data.error)) : p.resolve(e.data.blob);
      };
      worker.onerror = () => { log("frame worker failed; using main-thread encoder"); worker = null; };
    } catch { worker = null; }
  }
  return {
    get name() { return worker ? "Web Worker (OffscreenCanvas)" : "main thread (canvas.toBlob)"; },
    async encode(source) {
      if (worker) {
        const bitmap = await createImageBitmap(source);
        const id = ++seq;
        return new Promise((resolve, reject) => {
          pending.set(id, { resolve, reject });
          worker.postMessage({ id, bitmap, maxWidth: CONFIG.FRAME_MAX_WIDTH, quality: CONFIG.JPEG_QUALITY }, [bitmap]);
        });
      }
      return encodeOnMainThread(source);
    },
  };
})();

function encodeOnMainThread(source) {
  const srcW = source.videoWidth || source.width || 640, srcH = source.videoHeight || source.height || 360;
  const w = Math.min(CONFIG.FRAME_MAX_WIDTH, srcW), h = Math.round((srcH / srcW) * w);
  const c = els.canvas;
  c.width = w; c.height = h;
  const ctx = c.getContext("2d");
  if (source instanceof HTMLVideoElement && !state.cameraReady) { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h); }
  else ctx.drawImage(source, 0, 0, w, h);
  return new Promise((resolve) => c.toBlob(resolve, "image/jpeg", CONFIG.JPEG_QUALITY));
}

/**
 * One frame: { blob, ts (epoch ms at capture), perf (performance.now() at capture) }.
 * MIRRORING RULE: only the preview is mirrored (CSS scaleX(-1) on <video>). Frames sent to the backend
 * are UNMIRRORED: never flip the canvas or the bitmap here or in frame-worker.js.
 */
async function grabFrame() {
  const perf = performance.now();
  const ts = nowEpochMs();
  const blob = state.cameraReady ? await encoder.encode(els.video) : await encodeOnMainThread(els.video);
  return { blob, ts, perf };
}

// ============================================================
// First pass
// ============================================================
function readUserId() {
  const id = els.userId.value.trim();
  if (!id) return { error: "Enter your ID to start." };
  if (!CONFIG.USER_ID_RE.test(id)) return { error: "IDs use letters, numbers, - and _ only." };
  return { id };
}

async function beginAttempt(userId) {
  if (!state.cameraReady && !CONFIG.USE_MOCK) return;
  clearReset();
  const flow = ++state.flow;
  state.userId = userId;
  state.attemptId = null;
  state.firstPassS = null;
  state.identityResult = null;
  state.challengeAvailable = false;
  els.idHelp.textContent = "";

  setScreen("processing");
  setProgress("Starting");
  els.stopwatch.textContent = "0.00";
  els.prompt.hidden = true;
  els.userId.disabled = true;
  els.verifyBtn.disabled = true;

  let deadlineTimer;
  try {
    // v4: recording starts at the tap, BEFORE the prompt, at 10 fps. /attempt/start runs concurrently
    // and returns the quick action; it is shown once >= BASELINE_FRAMES frames exist (baseline).
    // Frames are encoded off-thread and collected as promises so encoding never slows the 10 fps clock.
    const startP = api.startAttempt({ user_id: userId });
    const interval = 1000 / CONFIG.LIVENESS_FPS;
    const firstP = grabFrame();
    const framePs = [firstP];
    const tap = performance.now();
    const recorder = (async () => {
      for (let i = 1; i < CONFIG.LIVENESS_FRAMES; i++) {
        await sleepUntil(tap + i * interval);
        if (flow !== state.flow) return;
        framePs.push(grabFrame());
      }
    })();

    const first = await firstP;
    guard(flow);
    state.t0 = first.perf;          // timing starts at the first captured frame
    startStopwatch();
    deadlineTimer = setTimeout(() => {
      if (flow !== state.flow) return;
      const timeoutFlow = ++state.flow;
      renderResult(timeoutDecision(state.attemptId), timeoutFlow, "first").catch(() => {});
    }, Math.max(0, state.t0 + CONFIG.FIRST_PASS_DEADLINE_MS - performance.now()));
    setProgress("Verifying");

    const start = await startP;
    guard(flow);
    const attemptId = start?.attempt_id;
    if (!attemptId) throw new Error("No attempt_id from /attempt/start");
    if (state.usedAttemptIds.has(attemptId)) throw new Error(`attempt_id reused: ${attemptId}`);
    state.usedAttemptIds.add(attemptId);
    state.attemptId = attemptId;
    showAttempt(attemptId);

    // Identity (Rekognition): fire and forget; result kept only for display.
    const idSent = performance.now();
    api.identity({ attempt_id: attemptId, frame: first })
      .then((r) => { if (flow === state.flow) state.identityResult = { ...r, rtt_ms: Math.round(performance.now() - idSent) }; })
      .catch((e) => log("identity request failed", e));

    // Quick action prompt, after the baseline frames (capture_ts of >= 2 frames < prompt_ts).
    let prompt_ts = null;
    const action = start?.action;
    if (action) {
      await sleepUntil(tap + (CONFIG.BASELINE_FRAMES - 1) * interval + 20);
      guard(flow);
      const ui = ACTION_UI[action] || { word: humanize(action), icon: "eye" };
      els.promptIcon.innerHTML = svg(ui.icon, 2);
      els.promptWord.textContent = ui.word;
      els.prompt.hidden = false;
      prompt_ts = nowEpochMs();
    }

    await recorder;
    guard(flow);
    let frames = await Promise.all(framePs);
    guard(flow);
    els.prompt.hidden = true;
    state.lastCapture = { frames: frames.length, before_prompt: prompt_ts == null ? null : frames.filter((f) => f.ts < prompt_ts).length,
                          span_ms: frames[frames.length - 1].ts - frames[0].ts, action: action || null };
    log("first-pass capture", state.lastCapture);

    // Race the server against the 2.00 s deadline (from the first frame).
    const livenessP = api.liveness({ attempt_id: attemptId, frames, prompt_ts });
    frames = null;
    const deadline = sleepUntil(state.t0 + CONFIG.FIRST_PASS_DEADLINE_MS).then(() => DEADLINE);
    const winner = await Promise.race([livenessP, deadline]);
    clearTimeout(deadlineTimer);
    guard(flow);

    if (winner === DEADLINE) {
      livenessP.then((d) => log("late liveness result ignored", d?.decision, d?.reason_code)).catch(() => {});
      await renderResult(timeoutDecision(attemptId), flow, "first");
    } else {
      await renderResult(winner, flow, "first");
    }
  } catch (err) {
    if (err instanceof Cancelled) return;
    log("attempt failed", err);
    if (flow === state.flow) await renderFailure(flow, err);
  } finally {
    clearTimeout(deadlineTimer);
  }
}

const DEADLINE = Symbol("deadline");

function timeoutDecision(attempt_id) {
  return {
    decision: "REQUEST_VERIFICATION", reason_code: "TIMEOUT", next_action: "RETRY", attempt_id,
    signals: null, client_timeout: true,
  };
}

// ============================================================
// Additional verification (challenge) — separate phase, started by the user
// ============================================================
async function runChallenge() {
  if (!state.attemptId || !state.challengeAvailable) return;
  clearReset();
  const flow = ++state.flow;
  state.challengeAvailable = false;
  setScreen("challenge");
  setProgress("Additional verification");
  startStopwatch();   // keeps counting from the original first frame: shows true total time

  try {
    const cs = await api.challengeStart({ attempt_id: state.attemptId });
    guard(flow);
    const actions = Array.isArray(cs?.actions) ? cs.actions : [];
    const baselineMs = Number(cs?.baseline_ms) || CONFIG.CHALLENGE_BASELINE_MS;
    const budget = Number(cs?.timeout_ms) ? (cs.timeout_ms - baselineMs - 600) / Math.max(1, actions.length) : Infinity;
    const actionMs = Math.max(800, Math.min(CONFIG.CHALLENGE_ACTION_MS, budget));

    const frames = [];
    const prompt_ts = [];
    let baseline_ts = null;
    let recording = true;
    const interval = 1000 / CONFIG.CHALLENGE_FPS;
    const recorder = (async () => {
      let nextAt = performance.now();
      while (recording && flow === state.flow) {
        frames.push(await grabFrame());
        nextAt += interval;
        await sleepUntil(nextAt);
      }
    })();

    const showPrompt = (action) => {
      const ts = nowEpochMs();
      if (action === "BASELINE") baseline_ts = ts; else prompt_ts.push(ts);
      const ui = ACTION_UI[action] || { word: humanize(action), icon: "eye" };
      els.promptIcon.innerHTML = svg(ui.icon, 2);
      els.promptWord.textContent = ui.word;
      els.prompt.hidden = false;
    };

    try {
      showPrompt("BASELINE");
      await sleep(baselineMs);
      for (const a of actions) {
        guard(flow);
        showPrompt(a);
        await sleep(actionMs);
      }
    } finally {
      recording = false;
      await recorder;
    }
    guard(flow);

    els.prompt.hidden = true;
    setProgress("Verifying");
    const d = await api.challenge({ attempt_id: state.attemptId, challenge_id: cs?.challenge_id, frames, prompt_ts, baseline_ts });
    frames.length = 0;
    guard(flow);
    await renderResult(d, flow, "final");
  } catch (err) {
    if (err instanceof Cancelled) return;
    log("challenge failed", err);
    if (flow === state.flow) await renderFailure(flow, err);
  }
}

// ============================================================
// Result rendering
// ============================================================
/**
 * APPROVED only when the server says ALLOW and backs it up: OK_LIVE_MATCH (only reachable after a
 * passed challenge) with its own identity MATCH and campus PERMITTED (must be returned), or a staff ID check.
 * Passive LIVE is not required: the server's calibration gate failed, so passive never approves.
 */
function classify(d) {
  const s = d?.signals || {};
  const match = s.identity?.status === "MATCH";
  const campusOk = d?.schedule?.status === "PERMITTED";
  if (d?.decision === "ALLOW") {
    // First pass (v4) also returns signals.challenge; when present it must be PASS.
    const quickOk = !s.challenge || s.challenge.status === "PASS";
    if (d.reason_code === "OK_LIVE_MATCH" && match && campusOk && quickOk && d.mode !== "OFFLINE") return "approved";
    if (d.reason_code === "STAFF_VERIFIED") return "approved";
    log("ALLOW without the backing signals \u2014 shown as pending", d);
    return "pending";
  }
  if (d?.decision === "DENY") return "rejected";
  return "pending";
}

/**
 * phase: "first" (first pass) | "final" (after the challenge).
 * Time is measured from the first captured frame to the frame in which the result is drawn
 * (DOM updated, then read inside the next requestAnimationFrame).
 */
async function renderResult(d, flow, phase) {
  if (d?.next_action === "NONE") d.next_action = null;
  if (d?.attempt_id && d.attempt_id !== state.attemptId) throw new Error(`Response for another attempt: ${d.attempt_id}`);
  log(`decision (${phase})`, d);

  const kind = classify(d);
  stopStopwatch();
  els.userId.disabled = false;
  updateVerifyEnabled();

  els.resultCard.className = `result ${kind}`;
  els.resultUser.textContent = resultUserLine(d, kind);
  if (kind === "approved") {
    els.resultIcon.innerHTML = svg("check", 1.6);
    els.resultHeading.textContent = "Access Approved";
    els.resultText.textContent = "Live person detected. Identity verified.";
  } else if (kind === "rejected") {
    els.resultIcon.innerHTML = svg("cross", 1.6);
    els.resultHeading.textContent = "Access Rejected";
    els.resultText.textContent = REJECT_TEXT[d.reason_code] || "Verification failed.";
  } else {
    els.resultIcon.innerHTML = svg("shield", 1.6);
    els.resultHeading.textContent = "Additional Verification Required";
    const detail = REASON_DETAIL[d?.reason_code];
    els.resultText.textContent = detail ? `${PENDING_TEXT} ${detail}` : PENDING_TEXT;
  }

  renderEvidence(evidenceFor(d, phase, kind), d?.mode === "OFFLINE");

  const s = d?.signals || {};
  els.mLiveness.textContent = pct(s.passive?.score, 1);
  els.mSimilarity.textContent = pct(s.identity?.similarity, 100);

  const next = kind === "pending" ? d?.next_action : null;
  const ui = NEXT_UI[next];
  const canChallenge = ui?.mode === "continue" && phase === "first" && !d.client_timeout;
  state.challengeAvailable = canChallenge;
  renderNext(ui);
  els.continueBtn.hidden = !canChallenge;
  els.retryBtn.hidden = ui?.mode !== "retry";
  els.resultMock.hidden = !CONFIG.USE_MOCK;
  els.resetHint.textContent = "";
  els.timingDetail.textContent = "";
  els.mTime.textContent = "…";   // never show a previous attempt's time; filled in on the paint frame
  setScreen("result");

  await nextFrame();
  guard(flow);
  const elapsed = (performance.now() - state.t0) / 1000;
  if (phase === "first") {
    state.firstPassS = elapsed;
    els.mTimeLabel.textContent = "Decision time";
    els.mTime.textContent = `${elapsed.toFixed(2)} s`;
  } else {
    els.mTimeLabel.textContent = "Total time";
    els.mTime.textContent = `${elapsed.toFixed(2)} s`;
  }
  els.timingDetail.textContent = timingDetail(d, phase, elapsed);
  els.opTimings.textContent = els.timingDetail.textContent || "—";

  scheduleReset(els.continueBtn.hidden && els.retryBtn.hidden ? CONFIG.RESULT_RESET_MS : CONFIG.ACTION_RESET_MS);
}

/**
 * 1:1 mode: the ID the person typed. Identify mode: the matched ID, shown ONLY on approval —
 * a rejected or pending face never reveals who it most resembled.
 */
function resultUserLine(d, kind) {
  if (!CONFIG.IDENTIFY_MODE) return state.userId ? `ID ${state.userId}` : "";
  if (kind !== "approved") return "";
  const matched = d?.signals?.identity?.user_id || d?.user_id || state.identityResult?.user_id;
  return matched ? `ID ${matched}` : "";
}

function timingDetail(d, phase, elapsed) {
  const parts = [];
  if (phase === "final" && state.firstPassS != null) parts.push(`first pass ${state.firstPassS.toFixed(2)} s`);
  if (d?.client_timeout) parts.push(`no server decision by ${(CONFIG.FIRST_PASS_DEADLINE_MS / 1000).toFixed(2)} s`);
  const st = d?.stage_ms;
  if (st) {
    if (st.quality != null) parts.push(`quality ${st.quality} ms`);
    if (st.passive != null) parts.push(`liveness ${st.passive} ms`);
    if (st.identity_wait != null) parts.push(`identity wait ${st.identity_wait} ms`);
  }
  const idr = state.identityResult;
  if (phase === "first" && idr?.ms != null) parts.push(`Rekognition ${idr.ms} ms`);
  const cap = state.lastCapture;
  if (phase === "first" && cap) parts.push(`${cap.frames} frames / ${cap.span_ms} ms${cap.before_prompt != null ? `, ${cap.before_prompt} before prompt` : ", no prompt"}`);
  if (st?.challenge != null) parts.push(`action check ${st.challenge} ms`);
  if (d?.first_pass_server_ms != null && phase === "first") parts.push(`server ${d.first_pass_server_ms} ms`);
  return parts.join(" · ");
}

/** [label, tone, value, policy] rows, from the server's response only. */
function evidenceFor(d, phase, kind) {
  const rc = d?.reason_code;
  const id = d?.signals?.identity?.status;
  const identity = id === "MATCH" ? ["ok", "confirmed"]
    : (id === "MISMATCH" || rc === "ID_MISMATCH") ? ["bad", "not confirmed"]
    : ["na", "unavailable"];

  // The server returns no challenge status, only reason codes. Completed: OK_LIVE_MATCH, or the
  // schedule codes it returns after a challenge only once biometrics passed (api/decide.py).
  // Failed: CHALLENGE_FAILED / a challenge-phase TIMEOUT.
  const passedThenSchedule = phase === "final" && (rc === "SCHEDULE_APPROVAL_REQUIRED" || rc === "SCHEDULE_UNKNOWN");
  // v4 first pass returns signals.challenge {action, status}: that is used directly.
  const quick = phase === "first" ? d?.signals?.challenge?.status : undefined;
  const challenge = quick === "PASS" ? ["ok", "completed"]
    : quick === "FAIL" ? ["bad", "failed"]
    : quick ? ["na", "required"]
    : ((d?.decision === "ALLOW" && rc === "OK_LIVE_MATCH" && phase === "final") || passedThenSchedule) ? ["ok", "completed"]
    : (phase === "final" && (rc === "CHALLENGE_FAILED" || rc === "TIMEOUT")) ? ["bad", "failed"]
    : ["na", "required"];

  const sc = d?.schedule?.status;
  const campus = sc === "PERMITTED" ? ["ok", "permitted"]
    : sc === "PROHIBITED" ? ["bad", "not permitted now"]
    : ["na", "staff approval required"];   // APPROVAL_REQUIRED, UNKNOWN or not returned

  const entry = kind === "approved" ? ["ok", "ALLOWED"] : ["bad", "BLOCKED"];
  return [["Identity", ...identity], ["Interactive challenge", ...challenge],
          ["Campus access", ...campus, d?.schedule?.policy_version], ["Entry", ...entry]];
}

function renderEvidence(rows, offline) {
  els.evidence.replaceChildren(...rows.map(([label, tone, value, policy]) => {
    const li = document.createElement("li");
    li.className = `ev ev-${tone}`;
    const mark = document.createElement("span"); mark.className = "ev-mark"; mark.textContent = MARK[tone];
    const l = document.createElement("span"); l.className = "ev-label"; l.textContent = label;
    const v = document.createElement("span"); v.className = "ev-value"; v.textContent = value;
    if (policy) { const n = document.createElement("span"); n.className = "ev-note"; n.textContent = `policy ${policy}`; v.append(" ", n); }
    li.append(mark, l, v);
    return li;
  }));
  els.offlineNote.hidden = !offline;
  els.offlineNote.textContent = OFFLINE_NOTE;
}

function renderNext(ui) {
  els.nextAction.hidden = !ui;
  if (!ui) return;
  els.nextIcon.innerHTML = svg(ui.icon);
  els.nextText.textContent = ui.text;
  els.nextSub.textContent = ui.sub;
}

async function renderFailure(flow, err) {
  stopStopwatch();
  els.userId.disabled = false;
  updateVerifyEnabled();
  state.challengeAvailable = false;
  els.resultCard.className = "result pending";
  els.resultUser.textContent = CONFIG.IDENTIFY_MODE ? "" : (state.userId ? `ID ${state.userId}` : "");
  els.resultIcon.innerHTML = svg("shield", 1.6);
  els.resultHeading.textContent = "Additional Verification Required";
  const rejectedInput = err?.status === 400 || err?.status === 422;
  els.resultText.textContent = rejectedInput
    ? (CONFIG.IDENTIFY_MODE
        ? `${PENDING_TEXT} The server does not support face-only identification yet.`
        : `${PENDING_TEXT} The ID was not accepted.`)
    : `${PENDING_TEXT} The verification service could not be reached.`;
  renderEvidence(evidenceFor(null, "first", "pending"), false);
  els.mLiveness.textContent = "—";
  els.mSimilarity.textContent = "—";
  els.mTimeLabel.textContent = "Decision time";
  els.mTime.textContent = "—";
  els.timingDetail.textContent = String(err?.message || "");
  renderNext(NEXT_UI.RECEPTION);
  els.continueBtn.hidden = true;
  els.retryBtn.hidden = false;
  els.resultMock.hidden = !CONFIG.USE_MOCK;
  setScreen("result");
  scheduleReset(CONFIG.ACTION_RESET_MS);
}

// ============================================================
// Screens, reset, stopwatch
// ============================================================
function setScreen(name) { document.body.dataset.screen = name; }

function setProgress(label) {
  els.progressLabel.textContent = label;
  els.progress.hidden = false;
}

function showAttempt(id) {
  els.attemptInfo.textContent = `attempt ${id}`;
  els.opAttempt.textContent = id;
}

function scheduleReset(ms) {
  clearReset();
  const until = performance.now() + ms;
  const tick = () => {
    const left = Math.ceil((until - performance.now()) / 1000);
    els.resetHint.textContent = left > 0 ? `Returning to the main screen in ${left} s` : "";
  };
  tick();
  state.resetTick = setInterval(tick, 250);
  state.resetTimer = setTimeout(goHome, ms);
}

function clearReset() {
  clearTimeout(state.resetTimer);
  clearInterval(state.resetTick);
  els.resetHint.textContent = "";
}

function goHome() {
  clearReset();
  state.flow++;               // cancel anything still in flight
  state.userId = null;
  state.attemptId = null;     // never reused: every Verify calls /attempt/start
  state.challengeAvailable = false;
  stopStopwatch();
  els.progress.hidden = true;
  els.prompt.hidden = true;
  els.userId.value = "";      // don't leave the previous person's ID on screen
  els.userId.disabled = false;
  els.idHelp.textContent = "";
  els.attemptInfo.textContent = "";
  updateVerifyEnabled();
  setScreen("home");
}

function startStopwatch() {
  stopStopwatch();
  const tick = () => {
    els.stopwatch.textContent = ((performance.now() - state.t0) / 1000).toFixed(2);
    state.stopwatchRaf = requestAnimationFrame(tick);
  };
  tick();
}
function stopStopwatch() { cancelAnimationFrame(state.stopwatchRaf); }

// ============================================================
// Operator panel (press "o" when not typing)
// ============================================================
function setupOperator() {
  try { state.scenario = localStorage.getItem("kiosk.scenario") || "genuine"; } catch {}
  els.scenario.value = state.scenario;
  if (els.scenario.value !== state.scenario) { state.scenario = "genuine"; els.scenario.value = "genuine"; }
  els.scenario.addEventListener("change", () => {
    state.scenario = els.scenario.value;
    try { localStorage.setItem("kiosk.scenario", state.scenario); } catch {}
  });

  els.mockRows.hidden = !CONFIG.USE_MOCK;
  els.opBackend.textContent = CONFIG.USE_MOCK ? "MOCK" : (CONFIG.API_BASE || location.origin);
  els.opEncoder.textContent = encoder.name;

  const setOpen = (open) => {
    els.operator.hidden = !open;
    clearInterval(state.statusTimer);
    if (open) { refreshStatus(); state.statusTimer = setInterval(refreshStatus, CONFIG.STATUS_POLL_MS); }
  };
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t instanceof HTMLInputElement || t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement) {
      if (e.key === "Escape") setOpen(false);
      return;
    }
    if (e.key === "o" || e.key === "O") setOpen(els.operator.hidden);
    if (e.key === "Escape") setOpen(false);
  });
  els.mockMode.addEventListener("change", () => { if (!els.operator.hidden) refreshStatus(); });
}

/** GET /status -> status table. Shows only what the server returned; a failure shows as unreachable. */
async function refreshStatus() {
  const seq = ++state.statusSeq;
  let st = null, err = null;
  try { st = await api.status(); } catch (e) { err = e; }
  if (seq !== state.statusSeq) return;   // a newer poll already answered
  const m = st?.mode;
  // v4 /status fields, shown as returned; older servers fall back to the v3 fields.
  const passive = st?.passive_allow || "\u2014";
  const idSvc = st?.identity_service || (m === "ONLINE" ? "available" : m === "OFFLINE" ? "unavailable" : "unknown");
  const pol = st?.policy || { status: st?.policy_version ? "current" : "missing", version: st?.policy_version };
  const aq = st?.audit_queue || { pending: st?.pending_sync, synced: st?.synced };
  const rows = st ? [
    ["Passive-only approval", passive, String(passive).startsWith("ENABLED") ? "ok" : "bad"],
    ["Interactive challenge", st.challenge || "\u2014", "na"],
    ["Identity service", idSvc, idSvc === "available" ? "ok" : "bad"],
    ["Campus policy", `${pol.status || "?"}${pol.version ? ` \u00b7 ${pol.version}` : ""}`, pol.status === "current" ? "ok" : "bad"],
    ["Audit queue", `${aq.pending ?? "?"} pending \u00b7 ${aq.synced ?? "?"} synced`, aq.pending ? "na" : "ok"],
    ["Mode", m || "unknown", m === "ONLINE" ? "ok" : "bad"],
    ["Demo clock", `${st.demo_clock ? "ON" : "off"} \u00b7 ${st.kiosk_time || "?"}`, st.demo_clock ? "na" : "ok"],
  ] : [["Status", "unreachable", "bad"]];
  els.opStatus.replaceChildren(...rows.map(([k, v, tone]) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th"); th.textContent = k;
    const td = document.createElement("td"); td.textContent = v; td.className = `st-${tone}`;
    tr.append(th, td);
    return tr;
  }));
  els.opStatusNote.textContent = err ? String(err.message || err)
    : `GET /status \u00b7 ${new Date().toLocaleTimeString()}${CONFIG.USE_MOCK ? " \u00b7 MOCK" : ""}`;
}

// ============================================================
// Utils
// ============================================================
function sleep(ms) { return new Promise((r) => setTimeout(r, Math.max(0, ms))); }
function sleepUntil(t) { return sleep(t - performance.now()); }
function nextFrame() { return new Promise((r) => requestAnimationFrame(() => r())); }
function rand(a, b) { return a + Math.random() * (b - a); }
function shuffle(a) { return a.map((v) => [Math.random(), v]).sort((x, y) => x[0] - y[0]).map((x) => x[1]); }
function uuid() { return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36); }
function humanize(code) { const s = String(code).toLowerCase().replace(/_/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); }
/** value on a 0..scale range -> "87%" / "99.2%", or "—" when missing. */
function pct(v, scale) {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  const p = (v / scale) * 100;
  return `${p >= 99.5 && p < 100 ? p.toFixed(1) : Math.round(p * 10) / 10}%`;
}
function log(...a) { console.log("[kiosk]", ...a); }

function startClock() {
  const fmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  const update = () => { els.clock.textContent = fmt.format(new Date()); };
  update();
  setInterval(update, 10000);
}

// ============================================================
// Boot
// ============================================================
els.mockBadge.hidden = !CONFIG.USE_MOCK;
for (const id of CONFIG.ENROLLED_IDS) {
  const o = document.createElement("option");
  o.value = id;
  els.enrolledList.appendChild(o);
}
document.body.classList.toggle("identify", CONFIG.IDENTIFY_MODE);
els.userId.disabled = CONFIG.IDENTIFY_MODE;
els.idForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (CONFIG.IDENTIFY_MODE) return beginAttempt(null);
  const { id, error } = readUserId();
  if (error) { els.idHelp.textContent = error; els.userId.focus(); return; }
  beginAttempt(id);
});
els.continueBtn.addEventListener("click", runChallenge);
els.retryBtn.addEventListener("click", () => {
  if (CONFIG.IDENTIFY_MODE) beginAttempt(null);
  else if (state.userId) beginAttempt(state.userId);
  else goHome();
});
els.homeBtn.addEventListener("click", goHome);

setupOperator();
startClock();
setScreen("home");
updateVerifyEnabled();
startCamera();
