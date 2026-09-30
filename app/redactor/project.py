# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Project storage: one folder per source video.

    <home>/projects/<id>/
        project.json   settings, status, per-track on/off, manual regions, export history
        tracks.json    one summary row per face track (for the review list)
        boxes.npy      the redaction area of every track on every frame it covers
        times.json     presentation time of every frame in the preview copy (player time <-> frame)
        proxy.mp4      540p review copy (unredacted)
        thumbs/<track>.jpg  (unredacted faces)

boxes.npy is what both the review screen and the export read, so what you see in
review is what gets exported. The folder holds unredacted material, which is why
projects can be deleted from the home screen.
"""
import copy
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

BOX_DTYPE = np.dtype([("f", "<i4"), ("t", "<i4"), ("x1", "<f4"), ("y1", "<f4"),
                      ("x2", "<f4"), ("y2", "<f4"), ("k", "u1")])  # k: 1 detected, 0 filled

SENSITIVITY = {"high": 0.35, "normal": 0.5, "low": 0.7}
STYLE_MODES = ("blur", "pixelate", "solid")
STYLE_SHAPES = ("ellipse", "rect")
PID_RE = re.compile(r"^[a-z0-9-]{1,48}-[0-9a-f]{8}$")
MAX_LABEL = 120


def home():
    root = Path(os.environ.get("REDACTOR_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Redactor")
    (root / "projects").mkdir(parents=True, exist_ok=True)
    return root


def project_id(source):
    st = Path(source).stat()
    digest = hashlib.sha1(f"{Path(source).resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(source).stem).strip("-").lower()[:40] or "video"
    return f"{slug}-{digest}"


def _write_json(path, data, attempts=5):
    """Atomic write. Retries briefly: antivirus scanners on Windows can hold a file for a moment."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def interpolate_region(region, f):
    """Box of a manual region at frame f, or None. Linear between keyframes, held outside them."""
    if not region.get("enabled", True) or f < region["start"] or f > region["end"]:
        return None
    keys = sorted((int(k), v) for k, v in region["keys"].items())
    if not keys:
        return None
    if f <= keys[0][0]:
        return keys[0][1]
    for (fa, a), (fb, b) in itertools.pairwise(keys):
        if fa <= f <= fb:
            r = (f - fa) / (fb - fa)
            return [a[i] + (b[i] - a[i]) * r for i in range(4)]
    return keys[-1][1]


def clean_region(body, n_frames, width, height):
    """A region from the browser, checked and normalised; raises ValueError with a readable reason."""
    if not isinstance(body, dict):
        raise ValueError("A region must be an object.")
    last = max(0, int(n_frames) - 1)
    try:
        rid = body.get("id")
        rid = int(rid) if rid not in (None, "", 0) else None
        start, end = int(body["start"]), int(body["end"])
        raw_keys = body["keys"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("A region needs whole-number start and end frames and keyframes.") from None
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise ValueError("A region needs at least one keyframe.")
    keys = {}
    for k, box in raw_keys.items():
        try:
            f = int(k)
            x1, y1, x2, y2 = (float(v) for v in box)
        except (TypeError, ValueError):
            raise ValueError("Each keyframe must be a frame number with four coordinates.") from None
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
            raise ValueError("Keyframe coordinates must be numbers.")
        x1, x2 = sorted((min(max(x1, 0.0), width), min(max(x2, 0.0), width)))
        y1, y2 = sorted((min(max(y1, 0.0), height), min(max(y2, 0.0), height)))
        if x2 - x1 < 1 or y2 - y1 < 1:
            raise ValueError("A region must be at least one pixel wide and tall.")
        keys[str(min(max(f, 0), last))] = [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]
    start, end = min(max(start, 0), last), min(max(end, 0), last)
    if start > end:
        start, end = end, start
    label = body.get("label")
    label = str(label).strip()[:MAX_LABEL] if label is not None else ""
    return {"id": rid, "label": label, "enabled": bool(body.get("enabled", True)),
            "start": start, "end": end, "keys": keys}


class Project:
    _cache = {}
    _cache_lock = threading.Lock()

    def __init__(self, pid):
        self.id = pid
        self.dir = home() / "projects" / pid
        self.lock = threading.RLock()
        self.data = _read_json(self.dir / "project.json")
        self._boxes = None
        self._times = None
        self._summary = None
        self._last_saved_progress = None
        self._last_saved_export = None
        # One flag per kind of job, so stopping one never stops the other. The server
        # replaces a flag when it queues that job, so a stop pressed while queued counts.
        self.cancel_analysis = threading.Event()
        self.cancel_export = threading.Event()

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def get(cls, pid):
        if not isinstance(pid, str) or not PID_RE.match(pid):
            raise KeyError(pid)
        with cls._cache_lock:
            if pid not in cls._cache:
                if not (home() / "projects" / pid / "project.json").is_file():
                    raise KeyError(pid)
                cls._cache[pid] = cls(pid)
            return cls._cache[pid]

    @classmethod
    def create(cls, source, sensitivity="normal", stride=1):
        if sensitivity not in SENSITIVITY:
            raise ValueError("Unknown sensitivity.")
        stride = int(stride)
        if stride not in (1, 2):
            raise ValueError("Speed must check every frame (1) or every 2nd frame (2).")
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"No such file: {source}")
        pid = project_id(source)
        pdir = home() / "projects" / pid
        if (pdir / "project.json").is_file():
            return cls.get(pid)  # same file, unchanged: reopen the existing review
        pdir.mkdir(parents=True, exist_ok=True)
        _write_json(pdir / "project.json", {
            "id": pid, "version": 1, "source": str(source.resolve()), "name": source.name,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "settings": {"sensitivity": sensitivity, "conf": SENSITIVITY[sensitivity], "stride": stride},
            "status": "new", "progress": {"stage": "Queued", "value": 0.0}, "error": None,
            "probe": None, "n_frames": 0, "fps": 0,
            "disabled_tracks": [], "regions": [], "exports": [],
            "style": {"mode": "blur", "shape": "ellipse"},
        })
        return cls.get(pid)

    @classmethod
    def delete(cls, pid):
        """Remove a project's folder (preview copy, thumbnails, choices). Exports beside the source stay."""
        p = cls.get(pid)
        with p.lock:
            job = p.data.get("export_job") or {}
            if p.data.get("status") in ("queued", "analyzing") or job.get("status") == "running":
                raise RuntimeError("Stop the running job before deleting this project.")
            with cls._cache_lock:
                cls._cache.pop(pid, None)
            shutil.rmtree(p.dir)

    @classmethod
    def recover_interrupted(cls):
        """At startup nothing is running: a job still marked running was cut off by the app closing.

        Also removes the half-written files such a job leaves behind.
        """
        for path in (home() / "projects").glob("*/project.json"):
            try:
                d = _read_json(path)
            except (OSError, ValueError):
                continue
            changed = False
            if d.get("status") in ("queued", "analyzing"):
                d["status"], d["error"], changed = "cancelled", "Interrupted when the app closed.", True
            (path.parent / "proxy.part.mp4").unlink(missing_ok=True)
            job = d.get("export_job") or {}
            if job.get("status") in ("running", "queued"):
                job.update(status="cancelled", error="Interrupted when the app closed.")
                changed = True
                if job.get("path"):
                    out = Path(job["path"])
                    out.with_name(out.stem + ".part.mp4").unlink(missing_ok=True)
            if changed:
                _write_json(path, d)

    @classmethod
    def list_all(cls):
        rows = []
        paths = []
        for p in (home() / "projects").glob("*/project.json"):
            try:
                paths.append((p.stat().st_mtime, p))
            except OSError:
                continue
        for _, p in sorted(paths, reverse=True):
            try:
                d = _read_json(p)
                rows.append({k: d.get(k) for k in ("id", "name", "source", "status", "created", "n_frames", "fps")})
            except (OSError, ValueError):
                continue
        return rows

    def save(self):
        with self.lock:
            _write_json(self.dir / "project.json", self.data)

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.save()

    def to_json(self):
        with self.lock:  # the job thread edits this dict while a request serialises it
            return json.dumps(self.data)

    def _save_quietly(self):
        """Progress saves are best effort: losing one must never stop a long job."""
        try:
            self.save()
        except OSError:
            pass

    def set_progress(self, stage, value):
        with self.lock:
            self.data["progress"] = {"stage": stage, "value": round(float(value), 4)}
            # Ticks are frequent; persist only stage changes and whole percents.
            mark = (stage, int(value * 100))
            if mark != self._last_saved_progress:
                self._last_saved_progress = mark
                self._save_quietly()

    def set_export_progress(self, job, stage, value):
        with self.lock:
            job.update(stage=stage, progress=round(float(value), 4))
            mark = (stage, int(value * 100))
            if mark != self._last_saved_export:
                self._last_saved_export = mark
                self._save_quietly()

    # ---------------------------------------------------------------- analysis results

    def store_results(self, summary, boxes, times):
        (self.dir / "tracks.json").unlink(missing_ok=True)
        np.save(self.dir / "boxes.npy", boxes)
        _write_json(self.dir / "times.json", times)
        _write_json(self.dir / "tracks.json", {"summary": summary})
        self._boxes, self._summary, self._times = boxes, summary, times

    @property
    def boxes(self):
        if self._boxes is None:
            path = self.dir / "boxes.npy"
            self._boxes = np.load(path) if path.is_file() else np.zeros(0, BOX_DTYPE)
        return self._boxes

    @property
    def times(self):
        if self._times is None:
            path = self.dir / "times.json"
            self._times = _read_json(path) if path.is_file() else []
        return self._times

    @property
    def track_summary(self):
        if self._summary is None:
            path = self.dir / "tracks.json"
            self._summary = _read_json(path)["summary"] if path.is_file() else []
        return self._summary

    # ---------------------------------------------------------------- queries used by review and export

    def window(self, start, end):
        """Redaction boxes for frames [start, end): {frame: [[track, x1, y1, x2, y2, detected], ...]}."""
        b = self.boxes
        lo, hi = np.searchsorted(b["f"], start, "left"), np.searchsorted(b["f"], end, "left")
        out = {}
        for r in b[lo:hi]:
            out.setdefault(int(r["f"]), []).append([int(r["t"]), round(float(r["x1"]), 1), round(float(r["y1"]), 1),
                                                    round(float(r["x2"]), 1), round(float(r["y2"]), 1), int(r["k"])])
        return out

    def density(self, buckets=1000):
        """Enabled redactions per slice of the timeline, for the timeline strip."""
        n = max(1, self.data.get("n_frames") or 1)
        b = self.boxes
        if len(b) == 0:
            return [0] * buckets
        mask = ~np.isin(b["t"], np.array(self.data["disabled_tracks"], dtype=np.int32))
        idx = np.minimum((b["f"][mask].astype(np.int64) * buckets) // n, buckets - 1)
        return np.bincount(idx, minlength=buckets).tolist()

    def snapshot(self):
        """Frozen copy of everything an export depends on.

        The export and its report both read this one copy, so switching a face or editing
        a region while an export runs (another tab, a reload) cannot make the report
        disagree with the video.
        """
        with self.lock:
            return copy.deepcopy({
                "disabled_tracks": self.data["disabled_tracks"],
                "regions": self.data["regions"],
                "settings": self.data["settings"],
                "n_frames": self.data["n_frames"], "fps": self.data["fps"],
            })

    def frame_boxes(self, snap):
        """Iterate (frame, [redaction boxes]) in frame order: enabled tracks + regions, per `snap`."""
        b = self.boxes
        disabled = set(snap["disabled_tracks"])
        regions = [r for r in snap["regions"] if r.get("enabled", True)]
        i, n = 0, len(b)
        for f in range(snap["n_frames"]):
            items = []
            while i < n and b["f"][i] < f:
                i += 1
            while i < n and b["f"][i] == f:
                r = b[i]
                if int(r["t"]) not in disabled:
                    items.append(("face", [float(r["x1"]), float(r["y1"]), float(r["x2"]), float(r["y2"])]))
                i += 1
            for reg in regions:
                box = interpolate_region(reg, f)
                if box:
                    items.append(("region", box))
            yield f, items

    # ---------------------------------------------------------------- edits from the review screen

    def set_tracks_enabled(self, ids, enabled):
        with self.lock:
            off = set(self.data["disabled_tracks"])
            off = off - set(ids) if enabled else off | set(ids)
            self.data["disabled_tracks"] = sorted(off)
            self.save()

    def set_style(self, mode, shape):
        if mode not in STYLE_MODES or shape not in STYLE_SHAPES:
            raise ValueError("Unknown style.")
        self.update(style={"mode": mode, "shape": shape})

    def upsert_region(self, body):
        region = clean_region(body, self.data.get("n_frames") or 0,
                              self.data.get("width") or 0, self.data.get("height") or 0)
        with self.lock:
            regions = self.data["regions"]
            if region["id"] is None:
                region["id"] = max([r["id"] for r in regions], default=0) + 1
            region["label"] = region["label"] or f"Region {region['id']}"
            self.data["regions"] = sorted([r for r in regions if r["id"] != region["id"]] + [region],
                                          key=lambda r: r["id"])
            self.save()
            return region

    def delete_region(self, rid):
        with self.lock:
            self.data["regions"] = [r for r in self.data["regions"] if r["id"] != rid]
            self.save()
