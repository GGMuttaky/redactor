# Research

The measurements behind Redactor's design: which detector, whether the GPU path finds the same faces,
and how fast each stage is. Everything was run on one mid-range desktop PC with an NVIDIA graphics card, on the public clips
listed in `../testclips/SOURCES.md`. Speeds on other PCs will differ.

## Results

| File | Question | Outcome |
|---|---|---|
| [`RESULTS_r1.md`](RESULTS_r1.md) | Is MediaPipe (cleanest licence) good enough? | No: ~25 % of faces in a crowd, near 0 behind masks |
| [`RESULTS_r2.md`](RESULTS_r2.md) | YuNet instead? Can we skip frames? | YuNet adopted; every frame by default |
| [`RESULTS_r3.md`](RESULTS_r3.md) | GPU detection, faster tracking and export | 5–5.5× faster detection, same faces; shipped in v0.2.0 |

## Scripts

Run from the repository root with the app's environment (`app\.venv\Scripts\python.exe research\<script>`).

| Script | What it does | Extra needs |
|---|---|---|
| `detector_runs.py` | Runs a detector (MediaPipe, YuNet, CenterFace) over clips; writes boxes, stats and contact sheets to `out/<run>/` for hand-counting | `mediapipe==0.10.14` for MediaPipe; `models/centerface.onnx` from [deface](https://github.com/ORB-HD/deface) for CenterFace |
| `compare_stride.py` | How much coverage detecting every 2nd/3rd frame loses | a YuNet run in `out/` |
| `gpu_bench.py` | GPU vs OpenCV CPU detections: agreement (IoU ≥ 0.8) and speed | — |
| `pipeline_bench.py` | The app's detection stage, CPU vs GPU, decode included | — |
| `track_check.py` | Fast tracker vs the original (`track_ref.py`): identical tracks? speed? | a YuNet run in `out/` |
| `export_bench.py` | Redaction cost per frame (old vs new) and export speed per encoder | an analysed Redactor project |

## Not published

- `out/` — thousands of frames and contact sheets of real people's faces from the test clips, plus
  their detections. Regenerate with `detector_runs.py`.
- `models/` — the CenterFace file (third-party, not needed by the app).
- Run logs.

The test clips themselves are not redistributed either; `../testclips/SOURCES.md` has their Pexels
pages and direct links.
