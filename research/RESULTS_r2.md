# Round 2 — YuNet vs MediaPipe (2026-09-28)

> **Outcome:** YuNet adopted; it is the detector in the app (v0.1.0 on). Runs are in `out/`, which is
> not published (frames of real faces). Written for the CPU-only app; round 3 added the GPU path.

Detector: OpenCV zoo YuNet `face_detection_yunet_2023mar.onnx` (MIT file, 232,589 bytes,
SHA-256 `8f2383e4…52fa4`) via `cv2.FaceDetectorYN`, full frame resolution, score threshold 0.5.
Same clips, tracker and sampled frames as round 1 (`RESULTS_r1.md`). Run: `out/r2_yunet_c50`.

CenterFace (as shipped by deface, 7,304,518 bytes) was also downloaded. Its ONNX file has a fixed
10×3×32×32 input, which ONNX Runtime rejects for other frame sizes, so the `CenterFaceDetector` in
`detector_runs.py` could not complete a run (the two Tokyo attempts in `out/` stopped before writing
results). Loaded instead through OpenCV DNN in a one-off check, it took ~0.9 s per 1080p frame
(~1.1 fps), too slow to be practical here, so it was not taken further. It is not used by the app.

## Faces found per frame (all frames)

| clip | MediaPipe tiled 640 | YuNet | YuNet detect fps |
|---|---|---|---|
| 01 Tokyo crowd | 2.58 | 46.44 | 5.8 |
| 02 Manchester | 1.16 | 6.66 | 6.4 |
| 03 London profiles | 1.47 | 9.00 | 9.3 (720p) |
| 04 Hong Kong masks | 0.53 | 7.96 | 9.8 (720p) |
| 05 Berlin car park | 1.57 | 3.37 | 3.6 (1440p; the app caps detection at 1920 px) |
| 06 night plate | 0.00 | 0.00 (no false alarms) | 5.2 |
| 07 office | 3.65 | 4.15 | 5.3 |
| 08 talking head | 1.00 | 1.00 | ~5 (measured while another job ran) |

## Recall on the round-1 frames (hand-counted)

Car park corrected on 2026-09-30 from "9/11": each of the 4 frames has a driver, a passenger and a
pedestrian (12 faces); round 1 had not counted the pedestrian's partly hidden head in f506.

| clip | frames | MediaPipe tiled | YuNet |
|---|---|---|---|
| 01 Tokyo | f229 at full resolution | ~25 % | no clear miss found in the crowd band |
| 02 Manchester | sheet 03 | 7/8 foreground, distant missed | 8/8 foreground, most distant faces boxed |
| 03 London | sheet 03 | ~50 % | masked + profile faces covered; background mostly covered |
| 04 Hong Kong | sheet 03 | near 0 | masked profiles and edge faces covered |
| 05 car park | sheet 05 (4 frames) | 10/12 (passenger missed twice) | 12/12 |
| 07 office | sheet 02 (4 frames) | 15/16 | 16/16 |
| 08 talking head | all | 100 % | 100 % |

False alarms seen with YuNet: hands holding paper (07), a headlight (05), backs of bald heads (04),
a hand and a jacket close to the lens (03), a food picture on a sign (01). Printed faces on
billboards and posters are detected too (01, 02, 05) — arguably correct for redaction.

## Detecting every Nth frame (`compare_stride.py`)

Reference = every-frame YuNet detections. For the frames a strided run skips, share of reference
faces at least 80 % inside a box that run would redact (app tracker, `app/redactor/track.py`):

| clip | every 2nd | every 3rd |
|---|---|---|
| 01 | 98.7 % | 97.7 % |
| 02 | 95.6 % | 91.0 % |
| 03 | 91.9 % | 84.4 % |
| 04 | 89.3 % | 85.4 % |
| 05 | 99.8 % | 99.4 % |
| 07 | 99.4 % | 99.0 % |
| 08 | 100 % | 99.7 % |

Some of the reference faces a strided run misses are one-frame flickers (often false alarms), so
this overstates the loss somewhat — but fast, brief faces (03, 04) clearly suffer.
**Decision: the app defaults to every frame; "Faster" (every 2nd) is offered with that warning.**

## Decision

YuNet replaces MediaPipe in the app. Licence risk (WIDER FACE training data) recorded in
`app/README.md` and `THIRD_PARTY_NOTICES.md`. Speed on the test PC's CPU (~5–6 fps at 1080p, ~5 h per hour of 1080p30) is the
main weakness; GPU inference (ONNX Runtime DirectML) is the obvious next improvement.
