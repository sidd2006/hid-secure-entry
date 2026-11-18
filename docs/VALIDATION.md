# Integration validation

Integrated main, liveness branch parallel challenge inference and enrollment, plus
the architecture diagram. API/kiosk and latency branch work is already in main.
Original teammate commits remain in history.

Ten local API tests pass using synthetic biometric signals. A delayed 350ms fake
inference is bounded by a 150ms test budget; the result stays REQUEST VERIFICATION
after the worker completes and permits challenge fallback. Same-origin kiosk and
enrollment routes are tested; `/.env` is inaccessible.

Outstanding: fresh integrated live camera trials, valid AWS identity match/mismatch
trials, browser latency median/p95 with sample counts, and print/phone/replay/both-mask
trials. Standalone passive voting outputs are not substitutes for this evidence.
