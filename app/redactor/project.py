# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Project storage: one folder per source video.

    <home>/projects/<id>/
        project.json   settings, status, per-track on/off, manual regions, export history
        tracks.json    every track's detected boxes + a summary row per track (for the list)
        boxes.npy      the redaction area of every track on every frame it covers
        times.json     presentation time of every frame (maps player time <-> frame)
        proxy.mp4      540p review copy
        thumbs/<track>.jpg

boxes.npy is what both the review screen and the export read, so what you see in
review is what gets exported.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

BOX_DTYPE = np.dtype([("f", "<i4"), ("t", "<i4"), ("x1", "<f4"), ("y1", "<f4"),
                      ("x2", "<f4"), ("y2", "<f4"), ("k", "u1")])  # k: 1 detected, 0 filled

SENSITIVITY = {"high": 0.35, "normal": 0.5, "low": 0.7}


def home():
    root = Path(os.environ.get("REDACTOR_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Redactor")
    (root / "projects").mkdir(parents=True, exist_ok=True)
    return root


def project_id(source):
    st = Path(source).stat()
    digest = hashlib.sha1(f"{Path(source).resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(source).stem).strip("-").lower()[:40] or "video"
    return f"{slug}-{digest}"


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def interpolate_region(region, f):
    """Box of a manual region at frame f, or None. Linear between keyframes, held outside them."""
    if not region.get("enabled", True) or f < region["start"] or f > region["end"]:
        return None
    keys = sorted((int(k), v) for k, v in region["keys"].items())
    if not keys:
        return None
    if f <= keys[0][0]:
        return keys[0][1]
    for (fa, a), (fb, b) in zip(keys, keys[1:]):
        if fa <= f <= fb:
            r = (f - fa) / (fb - fa)
            return [a[i] + (b[i] - a[i]) * r for i in range(4)]
    return keys[-1][1]


class Project:
    _cache = {}
    _cache_lock = threading.Lock()

    def __init__(self, pid):
        self.id = pid
        self.dir = home() / "projects" / pid
        self.lock = threading.RLock()
        self.data = json.loads((self.dir / "project.json").read_text())
        self._boxes = None
        self._times = None
        self._tracks = None
        self.cancel = False

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def get(cls, pid):
        with cls._cache_lock:
            if pid not in cls._cache:
                if not (home() / "projects" / pid / "project.json").is_file():
                    raise KeyError(pid)
                cls._cache[pid] = cls(pid)
            return cls._cache[pid]

    @classmethod
    def create(cls, source, sensitivity="normal", stride=1):
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
    def recover_interrupted(cls):
        """At startup nothing is running: a job still marked running was cut off by the app closing."""
        for path in (home() / "projects").glob("*/project.json"):
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            changed = False
            if d.get("status") in ("queued", "analyzing"):
                d["status"], d["error"], changed = "cancelled", "Interrupted when the app closed.", True
            job = d.get("export_job") or {}
            if job.get("status") == "running":
                job.update(status="cancelled", error="Interrupted when the app closed.")
                changed = True
            if changed:
                _write_json(path, d)

    @classmethod
    def list_all(cls):
        rows = []
        for p in sorted((home() / "projects").glob("*/project.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text())
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

    def set_progress(self, stage, value):
        with self.lock:
            self.data["progress"] = {"stage": stage, "value": round(float(value), 4)}
            # Progress ticks are frequent; persist only stage changes and whole percents.
            if getattr(self, "_last_saved", None) != (stage, int(value * 100)):
                self._last_saved = (stage, int(value * 100))
                self.save()

    # ---------------------------------------------------------------- analysis results

    def store_results(self, tracks, summary, boxes, times):
        _write_json(self.dir / "tracks.json", {"tracks": [
            {"id": t["id"], "boxes": {str(f): [round(v, 2) for v in b] for f, b in t["boxes"].items()}} for t in tracks
        ], "summary": summary})
        np.save(self.dir / "boxes.npy", boxes)
        _write_json(self.dir / "times.json", times)
        self._boxes, self._tracks, self._times = boxes, None, times

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
            self._times = json.loads(path.read_text()) if path.is_file() else []
        return self._times

    @property
    def track_summary(self):
        if self._tracks is None:
            path = self.dir / "tracks.json"
            self._tracks = json.loads(path.read_text())["summary"] if path.is_file() else []
        return self._tracks

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

    def frame_boxes(self):
        """Iterate (frame, [redaction boxes]) in frame order for export: enabled tracks + regions."""
        b = self.boxes
        disabled = set(self.data["disabled_tracks"])
        regions = [r for r in self.data["regions"] if r.get("enabled", True)]
        i, n = 0, len(b)
        for f in range(self.data["n_frames"]):
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

    def upsert_region(self, region):
        with self.lock:
            regions = self.data["regions"]
            if not region.get("id"):
                region["id"] = max([r["id"] for r in regions], default=0) + 1
            region.setdefault("enabled", True)
            region.setdefault("label", f"Region {region['id']}")
            region["keys"] = {str(int(k)): [float(v) for v in box] for k, box in region["keys"].items()}
            region["start"], region["end"] = int(region["start"]), int(region["end"])
            self.data["regions"] = [r for r in regions if r["id"] != region["id"]] + [region]
            self.data["regions"].sort(key=lambda r: r["id"])
            self.save()
            return region

    def delete_region(self, rid):
        with self.lock:
            self.data["regions"] = [r for r in self.data["regions"] if r["id"] != rid]
            self.save()
