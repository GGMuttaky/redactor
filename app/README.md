# Redactor

Blur faces in video, entirely on your own computer. Open a video, check what was
found, switch individual faces off, hide number plates or screens by hand, and
export a redacted copy with a report of what was hidden.

Nothing is uploaded. There are no accounts.

## Run it

Double-click **`Redactor.bat`**. It opens in your browser at `http://127.0.0.1:<port>/`.
Close the console window to quit. Starting it again while it runs just opens the running copy.

First run only: the batch file creates a private Python environment in `.venv\` and installs OpenCV,
NumPy and ONNX Runtime with DirectML — about 100 MB, needs **Python 3.12, 3.13 or 3.14** and an internet
connection once. It runs setup again by itself when `requirements.txt` changes (after an update).
If setup fails with a "path too long" error, move the folder somewhere short, such as `C:\redactor`.

Needs **FFmpeg 5.1 or newer** (reads and writes the video). Install with `winget install Gyan.FFmpeg`,
or put `ffmpeg.exe` and `ffprobe.exe` in `bin\` next to this file.

### Options

`Redactor.bat` passes its arguments on; so does `python run.py` from any folder.

| | |
|---|---|
| `--port N` | listen on port N instead of any free port |
| `--no-browser` | don't open the browser |
| `--version` | print the version and exit |

| Environment variable | |
|---|---|
| `REDACTOR_HOME` | where projects and the log are kept (default `%LOCALAPPDATA%\Redactor`) |
| `FFMPEG`, `FFPROBE` | full paths to the FFmpeg programs, if they aren't found on their own |

Problems are logged to `%LOCALAPPDATA%\Redactor\redactor.log`.

## How it works

1. **Analyse** — runs the YuNet face detector on every frame at full resolution (capped at 1920 px
   on the long side) — on the graphics card when DirectML can use it, otherwise on the CPU — while a
   540p preview copy for smooth review is made alongside. It links detections into tracks. Short gaps
   between detections are interpolated; each track gets a quarter-second lead-in and lead-out that
   follows its motion, so a face is covered as it turns or enters.
2. **Review** — the player shows the result live ("Preview result") or the boxes ("Show boxes").
   Every face track is listed with a thumbnail; switch any off to leave that person visible.
   Draw regions over anything else; move or resize a region at another moment and it follows
   between those positions.
3. **Export** — full resolution H.264 with every audio track (copied when MP4 can carry it, otherwise
   AAC). Uses the graphics card's video encoder when one works (NVENC tested and tuned to match x264
   CRF 18; AMF and QSV untested), else x264 CRF 18. Written beside the source as `<name>_redacted.mp4`;
   if that exists, `_redacted_2.mp4`, and so on. The source is never modified and no earlier export is
   overwritten. The export uses the choices as they were when you clicked Export, and the frame count
   is checked against the source afterwards.
4. **Report** — `<name>_redacted_report.html` and `.json`: SHA-256 of source and output, settings,
   every face and region with its time range and whether it was hidden. The report contains
   **no images** (face thumbnails would defeat the point of redacting).

Project data (preview copy, thumbnails, choices) lives in `%LOCALAPPDATA%\Redactor\projects\`.
**Thumbnails there are unredacted faces**: delete the project from the start screen (the bin icon next to it)
when you are done. That removes only Redactor's working files, never your video, exports or reports.

## Shortcuts

`Space` play/pause · `←` `→` one frame · `Shift`+`←` `→` one second · `Home` `End` first/last frame · `B` preview/boxes ·
`X` switch the selected face on/off · `Delete` remove the selected region · `Esc` cancel/deselect

## Measured on the test clips (one mid-range desktop PC with an NVIDIA graphics card)

Method and per-clip numbers: `../research/RESULTS_r2.md` (detector choice) and `../research/RESULTS_r3.md` (GPU).
YuNet finds faces the Apache-licensed MediaPipe detector missed entirely (crowds, masks, profiles,
distance). Finding faces runs at ~50 fps for 1080p and ~108 fps for 720p on the GPU (8–20 fps on
the CPU). Extrapolated from the short test clips, an hour of 1080p30 would take roughly 40 minutes to
analyse on the test PC with the GPU. GPU and CPU find the same faces (99.76–100 % matched).
Export runs at ~96 fps with NVENC and ~23 fps with x264 at 1080p.
"Faster" mode checks every 2nd frame and relies on tracking in between; on the skipped frames it
covers 0–11 % fewer faces depending on the clip, so every frame is the default.

## Limits — read before relying on it

- **Detection is not perfect.** Faces turned fully away, heavily occluded, very blurred by motion,
  or smaller than about 10 px can be missed. Always watch the export before publishing.
- **Number plates, screens and documents are not detected automatically** in this version. Use regions.
- Variable-frame-rate sources (some phone recordings) are exported at their average frame rate;
  audio can drift slightly on long clips.
- 10-bit and HDR sources are exported as 8-bit SDR H.264.
- Odd frame sizes (e.g. 321×241) get one black line on the right/bottom, because H.264 needs even
  sizes; the report says so.
- Tested on Windows with NVIDIA graphics only. AMD/Intel graphics (DirectML, AMF, QSV) should work through
  the same code and fall back to the CPU if they fail, but have not been tried.

## Licensing

- Redactor's code: **GNU GPL v3.0 or later** (`../LICENSE`).
- Dependencies: OpenCV (Apache-2.0), NumPy (BSD-3-Clause), ONNX Runtime (MIT). FFmpeg is called as a
  separate program and is not bundled.
- **Face model: non-commercial use.** The YuNet files are MIT-licensed by their author, but the model
  was trained on WIDER FACE, published under CC BY-NC-ND 4.0 (non-commercial). Whether that restricts
  the trained weights is legally unsettled, so treat the app as **free for non-commercial use** until
  the model is replaced. Details: `../THIRD_PARTY_NOTICES.md`; plan: `../docs/LICENSING.md`.

## Development

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check . ..\research
```

`pytest` runs unit tests and the whole pipeline on small generated clips (needs FFmpeg). To check real
footage with faces: `.venv\Scripts\python.exe tests\e2e.py <clips> --home <scratch dir> --stills <dir>`
(uses its own project folder; writes stills to look at and removes its exports).

## Building the GPU model

`models/face_detection_yunet_2026may_u8.onnx` is generated, not downloaded: install the development
requirements (above, includes `onnx==1.23.0`), then run `.venv\Scripts\python.exe tools\make_gpu_model.py`.
It wraps opencv_zoo's dynamic-shape YuNet (`tools/source_models/`) to take raw 8-bit frames, so a
quarter of the data crosses to the graphics card per frame. The weights are unchanged and the rebuild
is byte-identical.
