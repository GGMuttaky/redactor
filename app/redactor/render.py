# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Export: full-resolution redacted video with the original audio, plus a report.

The output always gets a new name beside the source (<name>_redacted.mp4, then
_redacted_2.mp4, ...); nothing is ever overwritten. After writing, the file is
probed and its frame count checked against the source before it is reported done.
"""
import hashlib
import html
import json
import os
import queue
import subprocess
import threading
import tempfile
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

from . import __version__, media
from .analyze import Cancelled
from .project import Project

COPY_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}  # codecs MP4 carries as-is


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
    # sigma = min(w, h) / 8, which was the original method, at a small fraction of the cost.
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


def _put(q, item, alive):
    """Queue put that gives up when the other side has died, instead of blocking forever."""
    while alive():
        try:
            q.put(item, timeout=0.25)
            return True
        except queue.Full:
            pass
    return False


def _encode(p, src, part, mode, shape, encoder, prog):
    """Decode -> redact -> encode, each on its own thread so the CPU, the pipe and the
    (possibly hardware) encoder all stay busy. Returns redaction stats."""
    ffmpeg, _ = media.tools()
    info = p.data["probe"]
    w, h, n = p.data["width"], p.data["height"], p.data["n_frames"]
    audio = ["-map", "1:a:0?"]
    if info.get("audio_codec"):
        audio += ["-c:a", "copy"] if info["audio_codec"] in COPY_AUDIO else ["-c:a", "aac", "-b:a", "256k"]
    cmd = [ffmpeg, "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", info.get("rate") or str(p.data["fps"]),
           "-i", "pipe:0", "-i", str(src), "-map", "0:v:0", *audio,
           *encoder[2], "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(part)]
    errlog = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog,
                            creationflags=media.NO_WINDOW)
    frames_q, out_q = queue.Queue(maxsize=6), queue.Queue(maxsize=6)
    halt, write_error = threading.Event(), []

    def reader():
        cap = cv2.VideoCapture(str(src))
        try:
            while not halt.is_set():
                ok, frame = cap.read()
                if not ok or not _put(frames_q, frame, lambda: not halt.is_set()):
                    break
        finally:
            cap.release()
            _put(frames_q, _END, lambda: not halt.is_set())

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

    threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=writer, daemon=True)]
    for t in threads:
        t.start()
    stats = {"frames_redacted": 0, "boxes": 0}
    alive = lambda: not halt.is_set()  # noqa: E731
    try:
        for f, items in p.frame_boxes():
            if p.cancel:
                raise Cancelled
            frame = None
            while alive() and frame is None:
                try:
                    frame = frames_q.get(timeout=0.25)
                except queue.Empty:
                    pass
            if frame is None or frame is _END:
                break
            if frame.shape[1] != w or frame.shape[0] != h:
                raise RuntimeError(f"Frame {f} is {frame.shape[1]}x{frame.shape[0]}, expected {w}x{h}.")
            if items:
                redact(frame, items, mode, shape)
                stats["frames_redacted"] += 1
                stats["boxes"] += len(items)
            if not _put(out_q, frame, alive):
                break
            if f % 10 == 0:
                prog(f"Exporting · {encoder[1]} encoder", f / max(1, n))
        _put(out_q, _END, alive)
        threads[1].join()
    except BaseException:
        proc.kill()
        raise
    finally:
        halt.set()
        for q in (frames_q, out_q):  # unblock any thread still waiting to put
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        for t in threads:
            t.join(timeout=5)
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
    proc.wait()
    if proc.returncode != 0 or write_error:
        errlog.seek(0)
        raise EncoderFailed(f"ffmpeg ({encoder[0]}) failed: {errlog.read().decode(errors='replace').strip()[-500:]}")
    return stats


def sha256(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def count_frames(path):
    _, ffprobe = media.tools()
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, creationflags=media.NO_WINDOW)
    # Files with stream groups (ffmpeg 9) print the count once per section, on separate lines.
    for token in out.stdout.replace(",", "\n").split():
        if token.isdigit():
            return int(token)
    return None


def export(p: Project, mode="blur", shape="ellipse"):
    p.cancel = False
    src = Path(p.data["source"])
    out = next_free(src.with_name(f"{src.stem}_redacted.mp4"))
    part = out.with_name(out.stem + ".part.mp4")  # renamed to `out` only once written and checked
    job = {"status": "running", "path": str(out), "mode": mode, "shape": shape,
           "started": time.strftime("%Y-%m-%d %H:%M:%S"), "progress": 0.0, "stage": "Exporting"}
    with p.lock:
        p.data["export_job"] = job
        p.save()

    def prog(stage, v):
        with p.lock:
            job.update(stage=stage, progress=round(v, 4))
            if int(v * 100) != getattr(p, "_exp_pct", -1):
                p._exp_pct = int(v * 100)
                p.save()

    t_start = time.perf_counter()
    try:
        n = p.data["n_frames"]
        encoder = media.pick_encoder()
        try:
            stats = _encode(p, src, part, mode, shape, encoder, prog)
        except EncoderFailed as e:
            if encoder == media.CPU_ENCODER:
                raise RuntimeError(str(e)) from None
            # A hardware encoder can pass the startup test and still refuse this job
            # (session limit, odd size): redo it on the CPU rather than fail the export.
            part.unlink(missing_ok=True)
            encoder = media.CPU_ENCODER
            stats = _encode(p, src, part, mode, shape, encoder, prog)
        job["encoder"] = f"{encoder[1]} ({encoder[0]})"

        prog("Checking the export", 0.99)
        out_frames = count_frames(part)
        if out.exists():
            raise RuntimeError(f"{out.name} appeared while exporting; kept the new file as {part.name}.")
        os.replace(part, out)
        prog("Checksumming", 0.995)
        report = _report(p, out, mode, shape, stats, out_frames, job["encoder"])
        with p.lock:
            job.update(status="done", progress=1.0, stage="Done", report=report["html"],
                       frames_ok=out_frames == n, out_frames=out_frames,
                       seconds=round(time.perf_counter() - t_start, 1),
                       finished=time.strftime("%Y-%m-%d %H:%M:%S"))
            p.data.setdefault("exports", []).append(dict(job))
            p.save()
    except Cancelled:
        with p.lock:
            job.update(status="cancelled")
            p.save()
        part.unlink(missing_ok=True)  # the partial file this job created; never a finished export
    except Exception as e:
        traceback.print_exc()
        part.unlink(missing_ok=True)
        with p.lock:
            job.update(status="error", error=str(e))
            p.save()


def _tc(frame, fps):
    s = frame / fps
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def _report(p, out, mode, shape, stats, out_frames, encoder):
    """JSON + HTML record of what was hidden. Deliberately contains no face images."""
    d, fps, n = p.data, p.data["fps"], p.data["n_frames"]
    disabled = set(d["disabled_tracks"])
    tracks = p.track_summary
    # "face" is the number the review screen shows (track id + 1), so the report and the app agree.
    rows = [{"face": t["id"] + 1, "track": t["id"], "from": _tc(t["start"], fps), "to": _tc(t["end"], fps),
             "redacted": t["id"] not in disabled, "detections": t["detections"]} for t in tracks]
    regions = [{"id": r["id"], "label": r["label"], "from": _tc(r["start"], fps), "to": _tc(r["end"], fps),
                "redacted": r.get("enabled", True)} for r in d["regions"]]
    data = {
        "tool": f"Redactor {__version__}", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"path": d["source"], "sha256": sha256(d["source"]), "frames": n, "fps": fps,
                   "width": d["width"], "height": d["height"]},
        "output": {"path": str(out), "sha256": sha256(out), "frames": out_frames,
                   "frame_count_matches_source": out_frames == n},
        "settings": {"style": mode, "face_shape": shape, "video_encoder": encoder, **d["settings"]},
        "summary": {"faces_found": len(tracks), "faces_redacted": sum(r["redacted"] for r in rows),
                    "faces_left_visible": sorted(i + 1 for i in disabled), "manual_regions": len(regions),
                    "frames_with_redaction": stats["frames_redacted"], "redaction_boxes": stats["boxes"]},
        "faces": rows, "regions": regions,
        "note": "Automatic detection can miss faces. Watch the output before publishing it.",
    }
    jpath = out.with_name(out.stem + "_report.json")
    hpath = out.with_name(out.stem + "_report.html")
    jpath.write_text(json.dumps(data, indent=2))
    e = html.escape
    left = ", ".join(f"Face {i + 1}" for i in sorted(disabled)) or "none"
    face_rows = "".join(f"<tr><td>Face {r['face']}</td><td>{r['from']}</td><td>{r['to']}</td><td>{'Blurred' if r['redacted'] else '<b>Left visible</b>'}</td></tr>" for r in rows)
    reg_rows = "".join(f"<tr><td>{e(r['label'])}</td><td>{r['from']}</td><td>{r['to']}</td><td>{'Blurred' if r['redacted'] else 'Off'}</td></tr>" for r in regions)
    s = data["summary"]
    hpath.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>Redaction report</title>
<style>body{{font:14px system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1d1d1f}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid #ddd;padding:5px 8px;text-align:left}}
code{{font-size:12px;word-break:break-all}}.ok{{color:#0a7f3f}}.bad{{color:#b3261e}}</style></head><body>
<h1>Redaction report</h1><p>{e(data['tool'])} &middot; {e(data['created'])}</p>
<h2>Files</h2><table>
<tr><th>Source</th><td>{e(d['source'])}<br><code>SHA-256 {data['source']['sha256']}</code></td></tr>
<tr><th>Output</th><td>{e(str(out))}<br><code>SHA-256 {data['output']['sha256']}</code></td></tr>
<tr><th>Frames</th><td class="{'ok' if out_frames == n else 'bad'}">{out_frames} written of {n} in the source
{'(match)' if out_frames == n else '(MISMATCH - check this file)'}</td></tr>
<tr><th>Style</th><td>{ {'blur': 'Blur', 'pixelate': 'Pixelate', 'solid': 'Black box'}[mode] }{'' if mode == 'solid' else ', faces as ' + ('ovals' if shape == 'ellipse' else 'rectangles')}</td></tr></table>
<h2>Summary</h2><p>{s['faces_found']} faces found, {s['faces_redacted']} blurred; left visible on purpose: {left}.
{s['manual_regions']} manual region(s). {s['frames_with_redaction']} of {n} frames contain a redaction.</p>
<h2>Faces</h2><table><tr><th>Face</th><th>From</th><th>To</th><th>Result</th></tr>{face_rows}</table>
<h2>Manual regions</h2><table><tr><th>Region</th><th>From</th><th>To</th><th>Result</th></tr>{reg_rows or '<tr><td colspan=4>None</td></tr>'}</table>
<p><i>{e(data['note'])}</i></p></body></html>""")
    return {"json": str(jpath), "html": str(hpath)}
