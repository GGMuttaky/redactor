# Round 3 — GPU speed-up (2026-09-28)

> **Outcome:** shipped in v0.2.0. "v0.1.0" below means the first, CPU-only build (not published).

Machine: one mid-range desktop PC with an NVIDIA graphics card. Every time below was measured on the test clips, apart from the
one extrapolation that is marked as such.

## What was done

1. **Detection on the graphics card**: ONNX Runtime 1.24.4 + DirectML (`onnxruntime-directml`).
   OpenCV's FaceDetectorYN cannot use the GPU in the pip build, so YuNet's output decoding and NMS
   were re-implemented in `app/redactor/detect.py`, mirroring OpenCV's `face_detect.cpp`.
   Model: `face_detection_yunet_2026may.onnx` (opencv_zoo, MIT) — the 2023mar weights re-exported with
   dynamic H/W (weight tensors compared: identical) — wrapped by `app/tools/make_gpu_model.py` to take the raw uint8 frame and output
   `sqrt(cls*obj)` scores (weights unchanged; rebuild is byte-identical, SHA-256 `fa83e393…50d8`).
2. **Threaded pipeline**: decoding on a reader thread while the card works.
3. **Per-video GPU session**: DirectML optimises for the first input size it sees; a smoke test at
   64×64 made every real 720p frame 3.5× slower (39 ms vs 11 ms). Sessions are now built for each
   video's padded size (`add_free_dimension_override_by_name`), warmed up at that size.
   (OpenCV's own GPU route was not an option: the pip build has no CUDA backend; its OpenCL backend
   was not evaluated.)
4. **Tracker as matrices** (`track.py`): identical tracks on all 8 clips (`track_check.py`), 25× faster
   on the Tokyo crowd (5.74 s → 0.23 s).
5. **Export**: blur computed on the 10×-smaller grid and oval masks cached (84 ms → 7.3 ms per frame on
   the office clip, same look — side-by-side still checked); decode / redact / encode on separate
   threads; hardware encoder when present (NVENC here), x264 fallback if it fails.
6. **Preview copy** made on a second thread during detection, with NVENC and 2 decode threads.

## Finding: copying frames to the card is a large part of the cost

On the test PC, moving a 1080p frame to the graphics card as 32-bit floats (25 MB) took longer than
detecting faces in it, so the frame is now sent as 8-bit values (6 MB) and converted on the card.

## Agreement with the CPU detector (`gpu_bench.py`, `pipeline_bench.py`)

| clip | faces matched GPU vs OpenCV CPU (IoU ≥ 0.8) | max coordinate difference |
|---|---|---|
| 08 talking head | 530 / 530 (100 %) | 0.00 px |
| 01 Tokyo crowd | 14864 / 14900 (99.76 %) | 3.11 px |
| 04 Hong Kong | 5035 / 5046 (99.78 %) | — |
| 05 car park | 3313 / 3313 (100 %) | — |
| 07 office | 1689 / 1689 (100 %) | — |

The ONNX Runtime *CPU* path shows the same 36 differences on Tokyo: they are numeric rounding at the
0.5 score threshold, not a GPU effect.

## Detection speed (app pipeline, decode included)

| clip | res | original app (OpenCV CPU) | CPU, threaded | GPU | GPU vs original |
|---|---|---|---|---|---|
| 08 | 1080p | 6.8 fps | 9.4 | 49.1 | 7.2× |
| 01 | 1080p | 5.8 | 9.6 | 49.2 | 8.5× |
| 04 | 720p | 9.8 | 19.7 | 108.5 | 11× |
| 05 | 1440p (detected at 1920) | 3.6 | 8.0 | 44.0 | 12× |
| 07 | 1080p | 5.3 | 9.5 | 52.0 | 9.8× |

"Original app" column: detector-only fps from `gpu_bench.py` for 08 and 01, and from the round-2
research run for 04, 05, 07. Clip 05 ran at full 2560 px in that run while the app caps detection at
1920 px, so its 12× overstates; the like-for-like CPU → GPU figure there is 8.0 → 44.0 fps (5.5×).
Summary across clips: **GPU is 5.1–5.5× the threaded CPU path and 7–11× v0.1.0 at 1080p/720p.**
Extrapolation: at ~50 fps, an hour of 1080p30 (108,000 frames) needs ~36 minutes of detection.

## Whole analysis (preview + detection + tracking + thumbnails), wall clock

| clip | before (v0.1.0) | now |
|---|---|---|
| 09 talking head + audio, 21 s | 94 s | 14.5 s |
| 01 Tokyo, 5.4 s | 77 s | 11.3 s |
| 03 London, 28.7 s | — | 13.8 s |
| 05 car park 1440p60, 18.3 s | — | 34.6 s |

## Export (office clip, 407 frames 1080p, 5 boxes/frame)

| | fps | time | frames | size |
|---|---|---|---|---|
| v0.1.0 (sequential, old blur, x264) | ~8 | ~50 s | 407/407 | 10.2 MB |
| threaded, new blur, x264 | 17.4 | 23.4 s | 407/407 | 10.2 MB |
| threaded, new blur, NVENC | 82.6 | 4.9 s | 407/407 | 18.2 MB at CQ 19 |
| v0.3.0, x264 CRF 18 (2026-09-30) | 22.8 | 17.9 s | 407/407 | 10.2 MB |
| v0.3.0, NVENC CQ 23 (2026-09-30) | 96.4 | 4.2 s | 407/407 | 10.3 MB |

The last two rows are `export_bench.py` after v0.3.0's blend change (`cv2.blendLinear`): redaction
alone went from 71.4 ms per frame (v0.1.0 method) to 3.4 ms on the same frames, same look.

NVENC quality tuned against a lossless encode of the same redacted frames:

| encoder | PSNR | SSIM | size |
|---|---|---|---|
| x264 CRF 18 | 47.78 dB | 0.9922 | 10.2 MB |
| NVENC CQ 19 | 50.32 | 0.9941 | 18.2 MB |
| NVENC CQ 21 | 49.40 | 0.9930 | 13.5 MB |
| **NVENC CQ 23 (chosen)** | 48.80 | 0.9923 | 10.3 MB |
| NVENC CQ 25 | 47.71 | 0.9907 | 7.2 MB |

UI export of the London clip (860 frames, 770 face tracks): 15 s end to end, all frames checked.

## Tests

`app/tests/e2e.py` passed on clips 01, 03, 04, 05, 09 after the change (frames, audio, no overwrite,
source unchanged, report). It caught one real bug on the way: the frame counter misread ffprobe output
for files with ffmpeg 9 stream groups (clip 05), now fixed.

## Not tested

AMD (AMF) and Intel (QSV) encoders and DirectML on non-NVIDIA cards — no such hardware here. Both paths
fall back to the CPU automatically if they fail.
