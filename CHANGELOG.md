# Changelog

## 0.3.0 — 2026-09-30

A review of the whole project for reliability, security and honest documentation.

### Fixed

- **Exports of videos with odd frame sizes** (e.g. 321×241) failed on PCs without an NVIDIA encoder.
  One black line is now added to reach an even size, and the report says so.
- **The report could disagree with the video** if a face was switched or a region edited while an export
  ran. Each export now uses a snapshot taken when Export is clicked, for both the video and the report.
- **Preview timing on camera files whose audio starts first**: face boxes could sit a few frames off
  in the review player. Frame times now come from the preview copy the player actually shows.
- **Anamorphic video** (non-square pixels) was exported squashed; the pixel shape is now kept.
- **Videos with more than one audio track** lost all but the first; all are kept now.
- A hardware-encoder failure mid-export now retries on the CPU instead of failing.
- If the graphics card failed during analysis, the remaining frames could be skipped; they now go to the CPU.
- FFmpeg could stall on long jobs (a full error pipe) and was left running if the app was closed; both fixed.
- File names and paths with non-English characters are handled everywhere.
- Stop now stops only the job you meant (analysis or export), including while it is queued.
- Half-written files are cleaned up after a crash or when the app is closed mid-job.
- Timecodes like 59.9996 s now read `00:01:00.000`, not `00:00:60.000`.
- Album-art pictures in audio files are no longer mistaken for video.

### Security and privacy

- Stricter local server: exact `Host` check, a Content-Security-Policy that blocks outside scripts and
  framing, request size limit, validation of every input, and only the app's own files served.
- Projects can be deleted from the start screen, removing the unblurred thumbnails and preview copy.
  Re-analysing clears old thumbnails.
- The HTML report shows file names only (full paths, which can contain a user name, stay in the JSON).
- Only one copy of Redactor runs at a time; starting it again opens the running one.

### Changed

- The start screen shows the version, and a clear message when FFmpeg is missing or older than 5.1.
- Confirmations before deleting a region or a project; errors appear as messages instead of failing silently.
- `Redactor.bat` checks the Python version (3.12–3.14), re-runs setup after an update, and explains
  common setup failures (no internet, path too long).
- Uses `opencv-python-headless` (no GUI parts needed). Python 3.12 or newer is required.
- `--version` option; errors are logged to `%LOCALAPPDATA%\Redactor\redactor.log`.

### Added

- Automated tests (75: tracking, projects, drawing, the local server, and the whole pipeline on generated
  clips), lint, and a GitHub Actions workflow running both.
- `SECURITY.md`, `research/README.md`, this changelog.

### Documentation

- Numbers re-checked against the measurements, with sample sizes.
  The car-park count in the research notes was corrected (MediaPipe 10/12, not 9/11).
- `spike/` renamed to `research/`.

## 0.2.0 — 2026-09-30

First public release: GPU detection through DirectML (~50 frames/s at 1080p), hardware video encoding,
vectorised tracker, faster export, before/after demo, GPL-3.0 licence and third-party notices.

## 0.1.0 — 2026-09-28

First working version (not published): CPU detection with YuNet, tracking, review screen, regions, export and report.
