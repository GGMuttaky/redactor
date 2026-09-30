# Spike round 1 — MediaPipe face detection (2026-09-28)

Detector: MediaPipe 0.10.14 legacy `face_detection`, full-range model (`model_selection=1`), conf 0.3.
Apache-2.0, trained on Google data — the cleanest licence available. Tracker: greedy IoU/centre-distance
linking, gaps <= 0.5 s interpolated, 0.25 s lead-in/out following the track's motion.

Runs (all in `out/`, never overwritten):

| run | what |
|---|---|
| `smoke_01` | talking head only, 8 samples — pipeline check |
| `r1_full` | whole frame only |
| `r1_tile640` | whole frame + 640 px tiles, 25 % overlap |
| `r1_tile640_fill2` | same detections as `r1_tile640` (`--reuse`), lead-in/out now follows motion |

## Speed (CPU only)

| mode | detect fps (1080p) | x realtime | 1 h of 1080p30 takes |
|---|---|---|---|
| whole frame | 77–90 | 1.2–3.4 | ~20–50 min |
| tiled 640 | ~10 (5 at 1440p) | 0.09–0.40 | ~2.5–3.5 h |

## Recall — tiled mode, hand-counted on sampled frames

"Visible" = a face (frontal or profile, not back of head) roughly 20 px or taller in the source.
Covered = inside a drawn (padded) box. Counts are from the sheets named; crowd clips are estimates
because they are too dense to count exactly at sheet scale.

| clip | frames checked | covered / visible | notes |
|---|---|---|---|
| 01 Tokyo night crowd | 4 (sheet 08) | ~6 of ~22 per frame (~25 %) | close faces fine; mid-distance crowd missed |
| 02 Manchester | 4 (sheet 03) | 7 / 8 foreground | distant faces (< 20 px) all missed; false boxes on a building window |
| 03 London profiles | 4 (sheet 03) | ~50 % | masked profiles half caught; background faces missed |
| 04 Hong Kong masks | 4 (sheet 03) | near 0 of the few visible faces | boxes land on backs of bald heads instead |
| 05 Berlin car park | 12 (sheets 03, 05, 08) | 31 / 33 (94 %) | passenger missed on 2 frames |
| 06 night plate | — | no faces; 0 detections, 0 false positives | |
| 07 office | 8 (sheets 02, 08) | 26 / 28 (93 %) | one face half-covered, one partial |
| 08 talking head | all frames | 529 / 529 (100 %) | |

Whole-frame mode is far worse (0.35 vs 2.58 detections/frame on Tokyo; misses the car park faces entirely).

## Verdict

**Fails the 99 % bar on every clip except the talking head.** MediaPipe's model is built for faces near
the camera; tiling helps but does not fix crowds, masks or distance, and costs ~8x speed.

False positives seen (harmless for redaction, but the review UI must let users switch them off):
a monitor (frames 75–77 of clip 07), a building window (02), motorbike parts (05), backs of heads (04).

## Next options

1. Test small-face detectors on the same clips: YuNet (OpenCV zoo, MIT, 233 KB) and CenterFace
   (as used by `deface`, MIT, 7.3 MB). **Licence risk:** both trained on WIDER FACE, whose official
   terms forbid commercial use of the images "and any portion of derived data" (as quoted from the WIDER Challenge 2018
   terms page in a search result on 2026-09-28 — never verified on the page itself, which returned 404 on
   2026-09-30. CUHK's dataset card lists CC BY-NC-ND 4.0).
2. Person-detector safety net: blur the head region of every detected person (catches crowds and
   back-of-head cases; over-blurs).
3. Narrow the product to controlled footage (interviews, office, vlogs, property walk-throughs),
   where tiled MediaPipe already reaches 93–100 % and a review step catches the rest.
