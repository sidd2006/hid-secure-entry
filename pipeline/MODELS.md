# Models used by /pipeline

## Silent-Face-Anti-Spoofing (passive anti-spoof)

| | |
|---|---|
| Source | https://github.com/minivision-ai/Silent-Face-Anti-Spoofing |
| Commit | `b6d5f04ad78778917853b25c778acef6d5626d15` (2020-08-05, "Merge remote-tracking branch 'origin/master'") |
| License | Apache License 2.0 (`LICENSE` in the clone) |
| Location | `pipeline/third_party/Silent-Face-Anti-Spoofing` (gitignored; `python -m pipeline.fetch_third_party` clones and pins it) |
| Anti-spoof weights | `resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth` (scale 2.7), `resources/anti_spoof_models/4_0_0_80x80_MiniFASNetV1SE.pth` (scale 4.0) |
| Face detector | `resources/detection_model/Widerface-RetinaFace.caffemodel` + `deploy.prototxt` (OpenCV DNN, conf 0.6) |

### How we run it (`pipeline/silentface.py`)
Reproduces upstream `test.py` exactly, except both models are loaded once:
BGR image as read by cv2 -> upstream `Detection.get_bbox` -> for each model, upstream
`CropImage.crop` at that model's scale to 80x80 -> upstream `ToTensor` transform ->
softmax -> sum over the two models -> argmax.

Note: the upstream `ToTensor` does **not** divide by 255 (`src/data_io/functional.py`,
"modify by zkx"). We use the upstream transform, so inputs are 0-255 floats as the
models expect.

### Which class means real: index 1 (verified)
- `test.py`: `label = np.argmax(prediction)`; `if label == 1: "Real Face"` else "Fake Face".
- Checked on the repo's own samples: `image_T1.jpg` -> label 1, live 0.994;
  `image_F1.jpg` / `image_F2.jpg` -> label 2 (live 0.072 / 0.181). Upstream `test.py`
  on the same files prints Real 0.99 / Fake 0.73 / Fake 0.82, consistent with ours.
- Classes 0 and 2 are both spoof.

`live_score = prediction[1] / 2` (sum of the two models' softmax for class 1, averaged).
It is a model score, not a calibrated probability. Never show it as "% secure".

Needs `opencv-python` **4.x** (OpenCV 5 dropped `cv2.dnn.readNetFromCaffe`) and `torch` (CPU).

## Step 2 functions (for /api)

```python
from pipeline import quality, select_frames, passive_liveness, warmup
warmup()                                   # once at startup (~1 s first torch call)
q = quality(frames)                        # frames: list of JPEG bytes (or BGR arrays)
idx = select_frames(frames, ts_ms, q)      # <= 4 usable, time-spaced, by quality only
p = passive_liveness([frames[i] for i in idx])
```

- `quality` fail reasons are CONTRACT codes: `QUALITY_DARK`, `NO_FACE`, `MULTI_FACE`,
  `QUALITY_TOO_SMALL`, `QUALITY_BLUR`, `TRACKING_LOST` (and `LIVENESS_UNAVAILABLE` if the detector is missing).
- `passive_liveness`: LIVE only if every frame scored and min >= `PASSIVE_HIGH`; SPOOF if
  median <= `PASSIVE_LOW`; else UNCERTAIN; UNAVAILABLE if the model can't load or run.
- All thresholds are in `pipeline/config.py` and can be overridden by env vars
  (`PASSIVE_HIGH`, `PASSIVE_LOW`, `Q_MIN_FACE_PX`, `Q_MIN_BLUR`, `Q_MIN_BRIGHTNESS`, `Q_MIN_IOU`, ...).
  `PASSIVE_HIGH/LOW` are placeholders (0.80/0.30) until the Step 1 gate run sets them.

## MediaPipe Face Landmarker (pass-2 challenge)

| | |
|---|---|
| Model | `face_landmarker.task` (float16, v1), 478 landmarks + 52 blendshapes |
| Source | https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task |
| License | Apache License 2.0 (MediaPipe) |
| Location | `pipeline/third_party/face_landmarker.task` (gitignored; `python -m pipeline.fetch_third_party` downloads it) |
| Package | `mediapipe` (Tasks API, `vision.FaceLandmarker`, IMAGE mode, `output_face_blendshapes=True`) |

Install note: `pip install mediapipe` pulls `opencv-contrib-python` 5.x, which breaks the Silent-Face
detector. After installing, force 4.x: `pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python`
then `pip install "opencv-contrib-python>=4.8,<5"`. Check: `python -c "import cv2; print(cv2.__version__, hasattr(cv2.dnn, 'readNetFromCaffe'))"` should print `4.x True`.
On Linux the MediaPipe Tasks library also needs `libEGL.so.1` (`apt install libegl1 libgles2`).

## Step 3: challenge_check

```python
from pipeline import challenge_check
r = challenge_check(frames, ts_ms, prompt_ts_ms, ["SMILE", "TURN_LEFT"])
# {status: PASS|FAIL|UNCERTAIN, reason, per_action:[{action, baseline, threshold, peak, onset_ms, done, pre_prompt}],
#  passive:{...passive_liveness on quality-selected challenge frames; SPOOF holds back PASS...}, tracking:{ok, single_face_all, min_iou}, ms}
```
Deltas, timeout, hold frames, minimum reaction time and mirroring are in `config.py` (`CH_*`, env-overridable).
TURN_LEFT means the visitor's own left on raw (unmirrored) frames; set `CH_MIRRORED=1` if the browser sends mirrored frames.

## Pass 1: flat_sheet (no model)

```python
from pipeline import flat_sheet
r = flat_sheet(frames, ts_ms)   # same burst as quality(); OpenCV optical flow only
# {status: FLAT|NOT_FLAT|UNCERTAIN|NO_MOTION|UNAVAILABLE, attached_ratio, face_motion, ...}
```
FLAT = the area around the face moves with the face (held photo/phone): spoof evidence.
NOT_FLAT is not proof of liveness, NO_MOTION is not a spoof. Thresholds `FS_*` in config.py.

## Step 1 gate result (2026-11-14): NO-GO for passive-only

Decision: passive anti-spoof alone may never ALLOW. Every ALLOW requires a
`challenge_check` PASS (`REQUIRE_CHALLENGE=1` in config.py; `passive_liveness` returns
`can_allow: False, can_deny: False`, `challenge_check` returns `allow_eligible`).
Passive never ALLOWs and never DENYs (genuine faces fell below threshold 4/10 times).
A passive SPOOF turns a passed challenge into UNCERTAIN (reception), never FAIL.

Calibration counts (kiosk webcam, confirmed props, 30 still frames; threshold-setting
data, NOT measured evaluation results):

| Class | n | Outcome at the chosen threshold |
|---|---|---|
| genuine | 10 | 6 passed |
| printed photo | 10 | 6 caught |
| phone photo | 10 | 7 caught |

All three are below the 9/10 target; 7/20 spoof frames passed the passive threshold.

Off-domain reference (not the kiosk camera): 45 CelebA-Spoof images from a Hugging Face
copy: 10/15 genuine passed, 20/30 spoofs caught, 9.3 ms/frame on a Ryzen 7 9800X3D.

Challenge smoke check: one genuine live webcam attempt, 2/2 actions passed across 49 frames.

## Live challenge test (webcam, one command per attempt)

```
python -m pipeline.live_challenge --label genuine --who sonu
python -m pipeline.live_challenge --label print   --who print1
python -m pipeline.live_challenge --tally
```
Labels: genuine, print, phone, replay, mask, mask_cutout. Each attempt: ~0.8 s baseline,
then 2 random actions on screen, frames captured until `CH_TIMEOUT_MS`, then
`challenge_check`. One row per attempt is appended to `logs/live_challenge.csv` (gitignored)
(no images saved). `--tally` prints counts per label x status and challenge ms.
For spoofs, PASS = the attack got through; FAIL/UNCERTAIN = stopped (never ALLOW).
`--actions SMILE` forces the action list (1-action quick path); `--note steady` tags the
condition. `--tally` also prints a table per label x note x action count, where `act_seen`
counts attempts in which every asked action was seen (the action check alone).
`challenge_check` thins its input to ~`CH_CHECK_FPS` (10) frames per second by timestamp.
