# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Export: full-resolution redacted video with the original audio, plus a report.

The output always gets a new name beside the source (<name>_redacted.mp4, then
_redacted_2.mp4, ...); nothing is ever overwritten. The video is written to a .part
file, its frame count is checked and its report written, and only then is it given
its final name, so a failed or stopped export never leaves a file that looks finished.
"""
import html
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import __version__, media
from .analyze import Cancelled
from .project import Project
from .winjob import bind_to_app

log = logging.getLogger("redactor")

COPY_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}  # codecs MP4 carries as-is
STYLE_NAMES = {"blur": "Blur", "pixelate": "Pixelate", "solid": "Black box"}


def next_free(path: Path):
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.stem}_{n}{path.suffix}").exists():
        n += 1
    return path.with_name(f"{path.stem}_{n}{path.suffix}")


# ---------------------------------------------------------------- redaction styles

def _effect(roi, mode):
    h, w = roi.shape[:2]
    if mode == "solid":
        return np.zeros_like(roi)
    if mode == "pixelate":
        cells = max(1, min(w, h) // 8)
        small = cv2.resize(roi, (max(1, w // cells), max(1, h // cells)), interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    # Blur: average down to a coarse grid first, so no detail survives, smooth it there, then scale
    # back up. Smoothing the 10x-smaller grid (sigma / 10) looks like blurring at full size with
    # sigma = min(w, h) / 8, at a small fraction of the cost (research/RESULTS_r3.md).
    small = cv2.resize(roi, (max(1, w // 10), max(1, h // 10)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), max(0.1, min(w, h) / 80), borderType=cv2.BORDER_REPLICATE)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


_masks = {}


def _oval_mask(w, h):
    """Feathered oval weights for a w x h box, and their complement. Cached: sizes repeat across frames."""
    m = _masks.get((w, h))
    if m is None:
        mask = np.zeros((h, w), np.float32)
        cv2.ellipse(mask, (w // 2, h // 2), (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 1.0, -1)
        mask = np.clip(cv2.GaussianBlur(mask, (0, 0), max(0.5, min(w, h) / 30)) * 1.5, 0, 1)
        if len(_masks) > 4096:
            _masks.clear()
        m = _masks[(w, h)] = (mask, 1.0 - mask)
    return m


def redact(frame, items, mode="blur", shape="ellipse"):
    fh, fw = frame.shape[:2]
    for kind, box in items:
        x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
        x2, y2 = min(fw, int(np.ceil(box[2]))), min(fh, int(np.ceil(box[3])))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        roi = frame[y1:y2, x1:x2]
        eff = _effect(roi, mode)
        if kind == "face" and shape == "ellipse":
            mask, inv = _oval_mask(x2 - x1, y2 - y1)
            roi[:] = cv2.blendLinear(eff, roi, mask, inv)
        else:
            roi[:] = eff
    return frame


# ---------------------------------------------------------------- export job

class EncoderFailed(Exception):
    pass


_END = object()


def _audio_args(info):
    """All audio tracks, copied when MP4 can carry every one of them, else re-encoded as AAC."""
    codecs = info.get("audio_codecs") or ([info["audio_codec"]] if info.get("audio_codec") else [])
    if not codecs:
        return []
    keep = ["-c:a", "copy"] if all(c in COPY_AUDIO for c in codecs) else ["-c:a", "aac", "-b:a", "256k"]
    return ["-map", "1:a?", *keep]


def _encode(p, snap, src, part, mode, shape, encoder, prog, cancel):
    """Decode -> redact -> encode, each on its own thread so the CPU, the pipe and the
    (possibly hardware) encoder all stay busy. Returns redaction stats."""
    ffmpeg, _ = media.tools()
    info = p.data["probe"]
    w, h, n = p.data["width"], p.data["height"], snap["n_frames"]
    out_w, out_h = w + w % 2, h + h % 2
    filters = []
    if (out_w, out_h) != (w, h):
        # H.264 in the common 4:2:0 format needs even sizes: add one black line rather than lose a picture line.
        filters.append(f"pad={out_w}:{out_h}:0:0")
    sar = info.get("sar", "1:1")
    if sar != "1:1":  # raw frames carry no pixel shape: restore it so anamorphic footage isn't exported squashed
        filters.append(f"setsar={sar.replace(':', '/')}")
    vfilter = ["-vf", ",".join(filters)] if filters else []
    cmd = [ffmpeg, "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", info.get("rate") or str(snap["fps"]),
           "-i", "pipe:0", "-i", str(src), "-map", "0:v:0", *_audio_args(info), *vfilter,
           *encoder[2], "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(part)]
    stats = {"frames_redacted": 0, "boxes": 0, "size": [out_w, out_h]}
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog,
                                creationflags=media.NO_WINDOW)
        bind_to_app(proc)
        out_q, halt, write_error = queue.Queue(maxsize=6), threading.Event(), []

        def writer():
            while True:
                item = out_q.get()
                if item is _END:
                    return
                try:
                    proc.stdin.write(item.data)
                except OSError as e:  # the encoder died; its log says why
                    write_error.append(e)
                    halt.set()
                    return

        def put(item):
            while not halt.is_set():
                try:
                    out_q.put(item, timeout=0.25)
                    return True
                except queue.Full:
                    pass
            return False

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        try:
            with media.FrameReader(src) as frames:
                source = iter(frames)
                for f, items in p.frame_boxes(snap):
                    if cancel.is_set():
                        raise Cancelled
                    item = next(source, None)
                    if item is None:
                        break
                    frame = item[1]
                    if frame.shape[1] != w or frame.shape[0] != h:
                        raise RuntimeError(f"Frame {f} is {frame.shape[1]}x{frame.shape[0]}, expected {w}x{h}.")
                    if items:
                        redact(frame, items, mode, shape)
                        stats["frames_redacted"] += 1
                        stats["boxes"] += len(items)
                    if not put(frame):
                        break
                    if f % 10 == 0:
                        prog(f"Exporting · {encoder[1]} encoder", f / max(1, n))
            put(_END)
            writer_thread.join()
        except BaseException:
            proc.kill()
            raise
        finally:
            halt.set()
            while True:  # unblock the writer if it is waiting
                try:
                    out_q.get_nowait()
                except queue.Empty:
                    break
            writer_thread.join(timeout=5)
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait()  # always reap ffmpeg, so the .part file is released before anyone deletes it
        if proc.returncode != 0 or write_error:
            errlog.seek(0)
            lines = [ln.strip() for ln in errlog.read().decode("utf-8", "replace").splitlines() if ln.strip()]
            raise EncoderFailed(f"The {encoder[1]} video encoder ({encoder[0]}) stopped: "
                                + (" | ".join(lines[:4])[:600] or f"exit code {proc.returncode}"))
    return stats


def _remove(path, attempts=10):
    """Delete a file we created; Windows may hold it briefly after ffmpeg exits."""
    for i in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1 * (i + 1))
    log.warning("could not remove %s", path)


def export(p: Project, mode, shape, snap):
    """Export with the choices frozen in `snap` (taken when Export was clicked)."""
    cancel = p.cancel_export
    src = Path(p.data["source"])
    out = next_free(src.with_name(f"{src.stem}_redacted.mp4"))
    part = out.with_name(out.stem + ".part.mp4")
    reports = []
    with p.lock:
        job = p.data["export_job"]
        job.update(status="running", path=str(out), mode=mode, shape=shape, progress=0.0, stage="Exporting",
                   started=time.strftime("%Y-%m-%d %H:%M:%S"))
        p.save()

    def prog(stage, v):
        p.set_export_progress(job, stage, v)

    t_start = time.perf_counter()
    try:
        if cancel.is_set():
            raise Cancelled
        n = snap["n_frames"]
        encoder = media.pick_encoder()
        try:
            stats = _encode(p, snap, src, part, mode, shape, encoder, prog, cancel)
        except EncoderFailed as e:
            if encoder == media.CPU_ENCODER:
                raise RuntimeError(str(e)) from None
            # A hardware encoder can pass the startup test and still refuse this job
            # (session limit, odd size): redo it on the CPU rather than fail the export.
            log.warning("hardware encoder failed, retrying with x264: %s", e)
            _remove(part)
            encoder = media.CPU_ENCODER
            stats = _encode(p, snap, src, part, mode, shape, encoder, prog, cancel)
        encoder_name = f"{encoder[1]} ({encoder[0]})"

        prog("Checking the export", 0.99)
        out_frames = media.count_frames(part)
        if out.exists():  # something took the name meanwhile: pick the next free one, never overwrite
            out = next_free(out)
        prog("Writing the report", 0.995)
        reports = _write_report(p, snap, src, out, media.sha256(part), mode, shape, stats, out_frames, encoder_name)
        os.replace(part, out)
        with p.lock:
            job.update(status="done", progress=1.0, stage="Done", path=str(out), report=reports[1],
                       encoder=encoder_name, frames_ok=out_frames == n, out_frames=out_frames,
                       seconds=round(time.perf_counter() - t_start, 1),
                       finished=time.strftime("%Y-%m-%d %H:%M:%S"))
            p.data.setdefault("exports", []).append(dict(job))
            p.save()
    except Cancelled:
        with p.lock:
            job.update(status="cancelled")
            p.save()
        _remove(part)
        for r in reports:
            _remove(Path(r))
    except Exception as e:
        log.exception("export of %s failed", p.id)
        with p.lock:  # status first: a slow clean-up must never leave the job looking "running"
            job.update(status="error", error=str(e))
            p.save()
        _remove(part)
        for r in reports:
            _remove(Path(r))


# ---------------------------------------------------------------- report

def _tc(frame, fps):
    """hh:mm:ss.mmm, rounded once so 59.9996 s becomes 00:01:00.000, not 00:00:60.000."""
    ms = round(frame / fps * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


REPORT_CSS = ("body{font:14px system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1d1d1f}"
              "table{border-collapse:collapse;width:100%;margin:8px 0 24px}"
              "td,th{border-bottom:1px solid #ddd;padding:5px 8px;text-align:left;vertical-align:top}"
              "code{font-size:12px;word-break:break-all}.ok{color:#0a7f3f}.bad{color:#b3261e}")


def _write_report(p, snap, src, out, out_sha, mode, shape, stats, out_frames, encoder):
    """JSON + HTML record of what was hidden, from the same snapshot the video was made with.

    Contains no face images. The HTML shows file names only (full paths, which can include
    a Windows user name, are in the JSON for audit tools); every value is HTML-escaped.
    """
    fps, n = snap["fps"], snap["n_frames"]
    disabled = set(snap["disabled_tracks"])
    tracks = p.track_summary
    # "face" is the number the review screen shows (track id + 1), so the report and the app agree.
    rows = [{"face": t["id"] + 1, "track": t["id"], "from": _tc(t["start"], fps), "to": _tc(t["end"], fps),
             "hidden": t["id"] not in disabled, "detections": t["detections"]} for t in tracks]
    regions = [{"id": r["id"], "label": r["label"], "from": _tc(r["start"], fps), "to": _tc(r["end"], fps),
                "hidden": r.get("enabled", True)} for r in snap["regions"]]
    data = {
        "tool": f"Redactor {__version__}", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"path": str(src), "sha256": media.sha256(src), "frames": n, "fps": fps,
                   "width": p.data["width"], "height": p.data["height"]},
        "output": {"path": str(out), "sha256": out_sha, "frames": out_frames,
                   "frame_count_matches_source": out_frames == n,
                   "width": stats["size"][0], "height": stats["size"][1],
                   "padded_to_even_size": stats["size"] != [p.data["width"], p.data["height"]]},
        "settings": {"style": mode, "face_shape": shape, "video_encoder": encoder, **snap["settings"]},
        "summary": {"faces_found": len(tracks), "faces_hidden": sum(r["hidden"] for r in rows),
                    "faces_left_visible": sorted(i + 1 for i in disabled), "manual_regions": len(regions),
                    "frames_with_redaction": stats["frames_redacted"], "redaction_boxes": stats["boxes"]},
        "faces": rows, "regions": regions,
        "note": "Automatic detection can miss faces. Watch the output before publishing it.",
    }
    jpath = out.with_name(out.stem + "_report.json")
    hpath = out.with_name(out.stem + "_report.html")
    try:
        jpath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        hpath.write_text(_report_html(data, src, out, out_sha, mode, shape, out_frames, n, rows, regions),
                         encoding="utf-8")
    except BaseException:
        _remove(jpath)
        _remove(hpath)
        raise
    return [str(jpath), str(hpath)]


def _report_html(data, src, out, out_sha, mode, shape, out_frames, n, rows, regions):

    def e(value):
        return html.escape(str(value))

    s = data["summary"]
    shape_name = "ovals" if shape == "ellipse" else "rectangles"
    style = STYLE_NAMES[mode] + ("" if mode == "solid" else f", faces as {shape_name}")
    left = ", ".join(f"Face {i}" for i in s["faces_left_visible"]) or "none"
    match = out_frames == n
    o, sw, sh = data["output"], data["source"]["width"], data["source"]["height"]
    size = f"{o['width']}×{o['height']}"
    if o["padded_to_even_size"]:
        size = f"{sw}×{sh} source; one black line added ({size}) because H.264 needs even sizes"
    face_rows = "".join(
        f"<tr><td>Face {e(r['face'])}</td><td>{e(r['from'])}</td><td>{e(r['to'])}</td>"
        f"<td>{'Hidden' if r['hidden'] else '<b>Left visible</b>'}</td></tr>" for r in rows)
    region_rows = "".join(
        f"<tr><td>{e(r['label'])}</td><td>{e(r['from'])}</td><td>{e(r['to'])}</td>"
        f"<td>{'Hidden' if r['hidden'] else 'Off'}</td></tr>" for r in regions) or "<tr><td colspan=4>None</td></tr>"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Redaction report: {e(out.name)}</title><style>{REPORT_CSS}</style></head><body>
<h1>Redaction report</h1>
<p>{e(data['tool'])} &middot; {e(data['created'])}</p>
<h2>Files</h2>
<table>
<tr><th>Source</th><td>{e(src.name)}<br><code>SHA-256 {e(data['source']['sha256'])}</code></td></tr>
<tr><th>Output</th><td>{e(out.name)}<br><code>SHA-256 {e(out_sha)}</code></td></tr>
<tr><th>Frames</th><td class="{'ok' if match else 'bad'}">{e(out_frames)} written of {e(n)} in the source
{'(match)' if match else '(MISMATCH: check this file before using it)'}</td></tr>
<tr><th>Size</th><td>{e(size)}</td></tr>
<tr><th>Style</th><td>{e(style)}</td></tr>
</table>
<h2>Summary</h2>
<p>{e(s['faces_found'])} faces found, {e(s['faces_hidden'])} hidden; left visible on purpose: {e(left)}.
{e(s['manual_regions'])} manual region(s). {e(s['frames_with_redaction'])} of {e(n)} frames contain a redaction.</p>
<h2>Faces</h2>
<table><tr><th>Face</th><th>From</th><th>To</th><th>Result</th></tr>{face_rows}</table>
<h2>Manual regions</h2>
<table><tr><th>Region</th><th>From</th><th>To</th><th>Result</th></tr>{region_rows}</table>
<p><i>{e(data['note'])}</i></p>
</body></html>
"""
    return page
