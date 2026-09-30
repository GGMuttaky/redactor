# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Analysis job: preview copy, face detection, tracking, thumbnails.

Jobs run one at a time on a single worker thread: detection already uses every
CPU core, so two at once would only make both slower.
"""
import os
import queue
import threading
import time
import traceback

import cv2
import numpy as np

from . import media, track
from .detect import CpuDetector, make_detector
from .project import BOX_DTYPE, Project

_jobs = queue.Queue()
_worker = None


def submit(fn, *args):
    global _worker
    _jobs.put((fn, args))
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_jobs, daemon=True)
        _worker.start()


def _run_jobs():
    while True:
        fn, args = _jobs.get()
        try:
            fn(*args)
        except Exception:  # the job records its own error on the project; never kill the worker
            traceback.print_exc()


class Cancelled(Exception):
    pass


def analyze(p: Project):
    p.cancel = False

    def stop():
        return p.cancel

    try:
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
                media.make_proxy(src, tmp, info["duration"], lambda v: proxy_job.update(value=v), stop)
            except Exception as e:  # reported after detection, where the job can fail cleanly
                proxy_job["error"] = e

        proxy_thread = None
        if not proxy.is_file():
            proxy_thread = threading.Thread(target=make_proxy, daemon=True)
            proxy_thread.start()

        dets, fps, (w, h) = _detect(p, src, info, stop)
        n = len(dets)

        if proxy_thread:
            while proxy_thread.is_alive():
                p.set_progress("Finishing the preview copy", proxy_job["value"])
                proxy_thread.join(0.5)
            if stop():
                raise Cancelled
            if proxy_job["error"]:
                raise proxy_job["error"]
            os.replace(tmp, proxy)

        p.set_progress("Tracking", 0)
        tracks = track.link(dets, fps)
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
                # One or two low-confidence hits: probably not a face. Still redacted by default.
                "weak": len(det_frames) <= 2 and t["boxes"][best][4] < 0.7,
            })
        boxes = np.array(rows, dtype=BOX_DTYPE)
        boxes.sort(order=["f", "t"])

        _thumbnails(p, src, summary, stop)

        times = media.frame_times(src)
        if len(times) != n:
            times = [round(i / fps, 6) for i in range(n)]
        p.store_results(tracks, summary, boxes, times)
        p.update(status="ready", n_frames=n, fps=fps, width=w, height=h)
        p.set_progress("Done", 1)
    except Cancelled:
        p.update(status="cancelled")
    except Exception as e:
        traceback.print_exc()
        p.update(status="error", error=str(e))


_END = object()


def _reader(src, stride, prepare, stop, q):
    """Decode (and prepare) frames on this thread while the detector works on the previous ones."""
    cap = cv2.VideoCapture(src)
    try:
        f = 0
        while not stop() and cap.grab():
            if f % stride == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                q.put((f, frame, prepare(frame)))
            else:
                q.put((f, None, None))
            f += 1
    except Exception as e:  # hand decoder errors to the consumer instead of dying silently
        q.put(e)
    finally:
        cap.release()
        q.put(_END)


def _detect(p, src, info, stop):
    settings = p.data["settings"]
    stride = max(1, int(settings.get("stride", 1)))
    detector, note = make_detector(conf=settings["conf"], prefer_gpu=settings.get("gpu", True),
                                   frame_size=(info["width"], info["height"]))
    where = "on the graphics card" if detector.name == "GPU" else "on the CPU"
    p.update(detector=detector.name, detector_note=note)
    fps = info["fps"] or 30.0
    total = info["nb_frames"] or max(1, round(info["duration"] * fps))
    q = queue.Queue(maxsize=8)
    halt = threading.Event()
    reader = threading.Thread(target=_reader, args=(src, stride, detector.prepare, lambda: stop() or halt.is_set(), q),
                              daemon=True)
    reader.start()
    try:
        return _consume(p, q, detector, where, stop, settings, fps, total)
    finally:
        halt.set()  # on any exit, let the reader finish instead of blocking on a full queue
        while reader.is_alive():
            try:
                q.get(timeout=0.2)
            except queue.Empty:
                pass


def _consume(p, q, detector, where, stop, settings, fps, total):
    dets, size, t0 = [], None, time.perf_counter()
    while True:
        item = q.get()
        if item is _END:
            break
        if isinstance(item, Exception):
            raise item
        f, frame, prepared = item
        if frame is None:
            dets.append([])
        else:
            size = (frame.shape[1], frame.shape[0])
            try:
                dets.append(detector.detect_prepared(prepared))
            except Exception as e:
                if detector.name != "GPU":
                    raise
                # The card failed mid-run (driver reset, out of memory): finish on the CPU.
                detector = CpuDetector(settings["conf"])
                where = "on the CPU"
                p.update(detector=detector.name, detector_note=f"Graphics card stopped ({str(e)[:160]}); finished on the CPU.")
                dets.append(detector.detect(frame))
        n = len(dets)
        if n % 5 == 0:
            rate = n / max(1e-6, time.perf_counter() - t0)
            p.set_progress(f"Finding faces {where} · {rate:.0f} frames/s", min(0.999, n / total))
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
    tdir.mkdir(exist_ok=True)
    wanted = {}
    for s in summary:
        wanted.setdefault(s["best_frame"], []).append(s)
    if not wanted:
        return
    last = max(wanted)
    cap = cv2.VideoCapture(src)
    f = 0
    while f <= last and cap.grab():
        if stop():
            cap.release()
            raise Cancelled
        if f in wanted:
            ok, frame = cap.retrieve()
            if ok:
                fh, fw = frame.shape[:2]
                for s in wanted[f]:
                    x1, y1, x2, y2 = s["best_box"]
                    cx, cy, half = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * 0.8
                    crop = frame[max(0, int(cy - half)):min(fh, int(cy + half)), max(0, int(cx - half)):min(fw, int(cx + half))]
                    if crop.size:
                        crop = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA if crop.shape[0] > 96 else cv2.INTER_CUBIC)
                        cv2.imwrite(str(tdir / f"{s['id']}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        f += 1
        if f % 25 == 0:
            p.set_progress("Making thumbnails", min(0.999, f / (last + 1)))
    cap.release()
