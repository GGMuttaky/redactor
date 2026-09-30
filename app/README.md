# Redactor

Blur faces in video, entirely on your own computer. Open a video, check what was
found, switch individual faces off, hide number plates or screens by hand, and
export a redacted copy with a report of what was hidden.

Nothing is uploaded. There are no accounts.

## Run it

Double-click **`Redactor.bat`**. It opens in your browser at `http://127.0.0.1:<port>/`.
Close the console window to quit.

First run only: the batch file creates a Python environment and installs OpenCV, NumPy and
ONNX Runtime with DirectML (needs Python 3.12 and an internet connection once).

Needs **ffmpeg** (reads and writes the video). Install with `winget install Gyan.FFmpeg`, or put
`ffmpeg.exe` and `ffprobe.exe` in `bin\` next to this file.

## How it works

1. **Analyse** — runs the YuNet face detector on every frame at full resolution (capped at 1920 px
   on the long side) — on the graphics card when there is one (DirectML: NVIDIA, AMD or Intel),
   otherwise on the CPU — while a 540p preview copy for smooth review is made alongside. It links detections
   into tracks. Short gaps between detections are interpolated; each track gets a quarter-second
   lead-in and lead-out that follows its motion, so a face is covered as it turns or enters.
2. **Review** — the player shows the result live ("Preview result") or the boxes ("Show boxes").
   Every face track is listed with a thumbnail; switch any off to leave that person visible.
   Draw regions over anything else; move or resize a region at another moment and it follows
   between those positions.
3. **Export** — full resolution H.264, original audio copied when MP4 can carry it. Uses the graphics
   card's video encoder when available (NVENC / AMF / QSV, tuned to match x264 CRF 18), else x264 CRF 18.
   Written beside the source as `<name>_redacted.mp4`; if that exists, `_redacted_2.mp4`, and so on.
   The source is never modified and no earlier export is overwritten. After writing, the frame
   count is checked against the source.
4. **Report** — `<name>_redacted_report.html` and `.json`: SHA-256 of source and output, settings,
   every face and region with its time range and whether it was hidden. The report contains
   **no images** (face thumbnails would defeat the point of redacting).

Project data (preview copies, thumbnails, choices) lives in `%LOCALAPPDATA%\Redactor\projects\`.
Thumbnails there are unredacted faces: delete a project folder when you are done with it.

## Shortcuts

`Space` play/pause · `←` `→` one frame · `Shift`+`←` `→` one second · `Home` `End` first/last frame · `B` preview/boxes ·
`X` switch the selected face on/off · `Delete` remove the selected region · `Esc` cancel/deselect

## Measured on the test clips (one mid-range desktop PC with an NVIDIA graphics card)

Method and per-clip numbers: `../spike/RESULTS_r2.md` (detector choice) and `../spike/RESULTS_r3.md` (GPU).
YuNet finds faces the Apache-licensed MediaPipe detector missed entirely (crowds, masks, profiles,
distance). Finding faces runs at ~50 fps for 1080p and ~110 fps for 720p on the GPU (9–20 fps on
the CPU), so an hour of 1080p30 takes roughly 40 minutes to analyse on the test PC with the GPU.
GPU and CPU find the same faces (99.8–100 % matched). Export runs at ~80 fps with NVENC.
"Faster" mode checks every 2nd frame and relies on tracking in between (loses 1–11 % coverage on
the skipped frames; rarely needed now).

## Limits — read before relying on it

- **Detection is not perfect.** Faces turned fully away, heavily occluded, very blurred by motion,
  or smaller than about 10 px can be missed. Always watch the export before publishing.
- **Number plates, screens and documents are not detected automatically** in this version. Use regions.
- Variable-frame-rate sources (some phone recordings) are exported at their average frame rate;
  audio can drift slightly on long clips.
- 10-bit and HDR sources are exported as 8-bit SDR H.264.

## Licensing

- Redactor's code: **GNU GPL v3.0 or later** (`../LICENSE`).
- Dependencies: OpenCV (Apache-2.0), NumPy (BSD-3-Clause), ONNX Runtime (MIT). FFmpeg is called as a
  separate program and is not bundled.
- **Face model: non-commercial use.** The YuNet files are MIT-licensed by their author, but the model
  was trained on WIDER FACE, published under CC BY-NC-ND 4.0 (non-commercial). Whether that restricts
  the trained weights is legally unsettled, so treat the app as **free for non-commercial use** until
  the model is replaced. Details: `../THIRD_PARTY_NOTICES.md`; plan: `../docs/LICENSING.md`.

## Building the GPU model

`models/face_detection_yunet_2026may_u8.onnx` is generated, not downloaded:
`.venv\Scripts\python.exe -m pip install onnx` then `.venv\Scripts\python.exe tools\make_gpu_model.py`.
It wraps opencv_zoo's dynamic-shape YuNet (`tools/source_models/`) to take raw 8-bit frames, so a
quarter of the data crosses to the graphics card per frame. The rebuild is byte-identical.
