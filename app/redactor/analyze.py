# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Analysis job: preview copy, face detection, tracking, thumbnails.

Jobs (analyses and exports) run one at a time on a single worker thread. Both lean
on the same decoder, graphics card and encoder, so two at once would only make each
slower and harder to reason about.
"""
import logging
import os
import queue
import shutil
import threading
import time

import cv2
import numpy as np

from . import media, track
from .detect import CpuDetector, make_detector
from .project import BOX_DTYPE, Project

log = logging.getLogger("redactor")

_jobs = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def submit(fn, *args):
    global _worker
    _jobs.put((fn, args))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_jobs, daemon=True, name="redactor-jobs")
            _worker.start()


def _run_jobs():
    while True:
        fn, args = _jobs.get()
        try:
            fn(*args)
        except Exception:  # each job records its own error on its project; never kill the worker
            log.exception("job %s failed", getattr(fn, "__name__", fn))


class Cancelled(Exception):
    pass


def analyze(p: Project):
    cancel = p.cancel_analysis  # the flag set for this job when it was queued
    proxy_halt = threading.Event()  # stops the preview copy if detection fails

    def stop():
        return cancel.is_set()

    def stop_proxy():
        return cancel.is_set() or proxy_halt.is_set()

    try:
        if stop():
            raise Cancelled
        src = p.data["source"]
        p.update(status="analyzing", error=None)
        p.set_progress("Reading file", 0)
        info = media.probe(src)
        p.update(probe=info)

        # The preview copy is made on a second thread while faces are found: with detection on
        # the graphics card the CPU has room for it, and the two no longer run back to back.
        proxy, tmp = p.dir / "proxy.mp4", p.dir / "proxy.part.mp4"
        proxy_job = {"value": 0.0, "error": None}

        def make_proxy():
            try:
                media.make_proxy(src, tmp, info["duration"], lambda v: proxy_job.update(value=v), stop_proxy)
            except Exception as e:  # reported after detection, where the job can fail cleanly
                proxy_job["error"] = e

        proxy_thread = None
        if not proxy.is_file():
            proxy_thread = threading.Thread(target=make_proxy, daemon=True)
            proxy_thread.start()
        try:
            dets, fps, (w, h) = _detect(p, src, info, stop)
            if proxy_thread:
                while proxy_thread.is_alive():
                    p.set_progress("Finishing the preview copy", proxy_job["value"])
                    proxy_thread.join(0.5)
                if stop():
                    raise Cancelled
                if proxy_job["error"]:
                    raise proxy_job["error"]
                os.replace(tmp, proxy)
        finally:
            proxy_halt.set()  # no-op when the copy is already finished
            if proxy_thread:
                proxy_thread.join()
            tmp.unlink(missing_ok=True)

        n = len(dets)
        p.set_progress("Tracking", 0)
        tracks = track.link(dets, fps)
        summary, boxes = _summarise(tracks, n, fps, w, h)
        _thumbnails(p, src, summary, stop)
        times = _player_times(proxy, src, n, fps)
        p.store_results(summary, boxes, times)
        p.update(status="ready", n_frames=n, fps=fps, width=w, height=h, sar=info["sar_value"])
        p.set_progress("Done", 1)
    except Cancelled:
        p.update(status="cancelled")
    except Exception as e:
        log.exception("analysis of %s failed", p.id)
        p.update(status="error", error=str(e))


def _summarise(tracks, n, fps, w, h):
    """Per-frame redaction boxes for every track, and one summary row per track for the list."""
    rows, summary = [], []
    for t in tracks:
        covered = track.expand(t, n, fps, w, h)
        for f, (box, kind) in covered.items():
            rows.append((f, t["id"], *track.pad(box, w, h), 1 if kind == "det" else 0))
        det_frames = sorted(t["boxes"])
        best = max(det_frames, key=lambda f: t["boxes"][f][4])
        heights = sorted(t["boxes"][f][3] - t["boxes"][f][1] for f in det_frames)
        summary.append({
            "id": t["id"], "start": min(covered), "end": max(covered),
            "detections": len(det_frames), "score": round(t["boxes"][best][4], 3),
            "best_frame": best, "best_box": [round(v, 1) for v in t["boxes"][best][:4]],
            "size": round(heights[len(heights) // 2]),
            # One or two low-confidence hits: often not a face. Still hidden by default.
            "weak": len(det_frames) <= 2 and t["boxes"][best][4] < 0.7,
        })
    boxes = np.array(rows, dtype=BOX_DTYPE)
    boxes.sort(order=["f", "t"])
    return summary, boxes


def _player_times(proxy, src, n, fps):
    """Time of each frame as the browser will report it while playing the preview copy."""
    times = media.frame_times(proxy, from_zero=False)
    if len(times) == n:
        return times
    log.warning("preview copy has %d frame times for %d frames; using the source's", len(times), n)
    times = media.frame_times(src)
    return times if len(times) == n else [round(i / fps, 6) for i in range(n)]


def _detect(p, src, info, stop):
    settings = p.data["settings"]
    stride = max(1, int(settings.get("stride", 1)))
    detector, note = make_detector(conf=settings["conf"], prefer_gpu=settings.get("gpu", True),
                                   frame_size=(info["width"], info["height"]))
    where = "on the graphics card" if detector.name == "GPU" else "on the CPU"
    p.update(detector=detector.name, detector_note=note)
    fps = info["fps"] or 30.0
    total = info["nb_frames"] or max(1, round(info["duration"] * fps))
    dets, size, fell_back, t0 = [], None, False, time.perf_counter()
    with media.FrameReader(src, stride, detector.prepare) as reader:
        for f, frame, prepared in reader:
            if stop():
                raise Cancelled
            if frame is None:
                dets.append([])
                continue
            size = (frame.shape[1], frame.shape[0])
            if fell_back:
                dets.append(detector.detect(frame))  # frames are still prepared for the card: start over
            else:
                try:
                    dets.append(detector.detect_prepared(prepared))
                except Exception as e:
                    if detector.name != "GPU":
                        raise
                    # The card failed mid-run (driver reset, out of memory): finish on the CPU.
                    log.warning("GPU detection failed at frame %d, continuing on the CPU: %s", f, e)
                    detector, fell_back, where = CpuDetector(settings["conf"]), True, "on the CPU"
                    p.update(detector=detector.name,
                             detector_note=f"Graphics card stopped ({str(e)[:160]}); finished on the CPU.")
                    dets.append(detector.detect(frame))
            if len(dets) % 5 == 0:
                rate = len(dets) / max(1e-6, time.perf_counter() - t0)
                p.set_progress(f"Finding faces {where} · {rate:.0f} frames/s", min(0.999, len(dets) / total))
    if stop():
        raise Cancelled
    p.update(detect_fps=round(len(dets) / max(1e-6, time.perf_counter() - t0), 1))
    if not dets or size is None:
        raise RuntimeError("No frames could be decoded from this file.")
    return dets, fps, size


def _thumbnails(p, src, summary, stop):
    """Crop each track's clearest detection. Kept inside the project folder only:
    they are unredacted faces, so they never go into exports or reports."""
    tdir = p.dir / "thumbs"
    shutil.rmtree(tdir, ignore_errors=True)  # a re-analysis must not leave faces of tracks that are gone
    tdir.mkdir()
    wanted = {}
    for s in summary:
        wanted.setdefault(s["best_frame"], []).append(s)
    if not wanted:
        return
    last = max(wanted)
    cap = cv2.VideoCapture(src)
    try:
        f = 0
        while f <= last and cap.grab():
            if stop():
                raise Cancelled
            if f in wanted:
                ok, frame = cap.retrieve()
                if ok:
                    for s in wanted[f]:
                        _write_thumbnail(tdir / f"{s['id']}.jpg", frame, s["best_box"])
            f += 1
            if f % 25 == 0:
                p.set_progress("Making thumbnails", min(0.999, f / (last + 1)))
    finally:
        cap.release()


def _write_thumbnail(path, frame, box):
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = box
    cx, cy, half = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * 0.8
    crop = frame[max(0, int(cy - half)):min(fh, int(cy + half)), max(0, int(cx - half)):min(fw, int(cx + half))]
    if not crop.size:
        return
    crop = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA if crop.shape[0] > 96 else cv2.INTER_CUBIC)
    # imencode + write_bytes rather than imwrite: imwrite fails silently on non-ASCII paths.
    ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if ok:
        path.write_bytes(jpg.tobytes())
