# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""End-to-end check of the engine, no UI: analyse -> edit -> export (twice) -> verify.

    .venv\\Scripts\\python.exe tests\\e2e.py <video> [<video> ...] --home <scratch dir> --stills <dir>

Uses its own REDACTOR_HOME so it never touches real projects. Exits non-zero on any failed check.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("videos", nargs="+")
ap.add_argument("--home", required=True)
ap.add_argument("--stills", required=True, help="folder for frames pulled from each export, to look at")
ap.add_argument("--mode", default="blur")
args = ap.parse_args()
os.environ["REDACTOR_HOME"] = args.home
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redactor import analyze, media, render  # noqa: E402
from redactor.project import Project  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def streams(path):
    _, ffprobe = media.tools()
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(out.stdout)["streams"]


stills = Path(args.stills)
stills.mkdir(parents=True, exist_ok=True)
for v in args.videos:
    v = Path(v).resolve()
    print(f"\n== {v.name}")
    src_hash = render.sha256(v)
    p = Project.create(v, "normal", 1)
    t0 = time.perf_counter()
    analyze.analyze(p)
    t_an = time.perf_counter() - t0
    d = p.data
    check(d["status"] == "ready", f"analysis finished (status={d['status']}, error={d.get('error')}) in {t_an:.1f}s")
    if d["status"] != "ready":
        continue
    n = d["n_frames"]
    check((p.dir / "proxy.mp4").is_file(), "preview copy exists")
    check(render.count_frames(p.dir / "proxy.mp4") == n, f"preview copy has all {n} frames")
    check(len(p.times) == n, "one timestamp per frame")
    tracks = p.track_summary
    print(f"  info {n} frames, {len(tracks)} face tracks, {len(p.boxes)} boxes, "
          f"{sum(1 for t in tracks if t['weak'])} weak")
    check(all((p.dir / 'thumbs' / f"{t['id']}.jpg").is_file() for t in tracks), "every track has a thumbnail")

    # Edits: leave the first track visible, add one moving region.
    if tracks:
        p.set_tracks_enabled([tracks[0]["id"]], False)
    w, h = d["width"], d["height"]
    reg = p.upsert_region({"keys": {0: [0, 0, w * 0.2, h * 0.2], n - 1: [w * 0.8, h * 0.8, w, h]}, "start": 0, "end": n - 1})
    check(reg["id"] == 1, "region saved")

    outs = []
    for i in range(2):
        render.export(p, args.mode, "ellipse")
        job = p.data["export_job"]
        check(job["status"] == "done", f"export {i + 1} finished ({job.get('error')})")
        if job["status"] != "done":
            break
        outs.append(Path(job["path"]))
        check(job["frames_ok"], f"export {i + 1}: {job['out_frames']} frames written of {n}")
        kinds = {s["codec_type"] for s in streams(job["path"])}
        src_kinds = {s["codec_type"] for s in streams(v)}
        check(("audio" in kinds) == ("audio" in src_kinds), f"export {i + 1}: audio present={('audio' in kinds)} "
              f"matches source={('audio' in src_kinds)}")
        rep = json.loads(Path(job["report"].replace(".html", ".json")).read_text())
        check(rep["output"]["frame_count_matches_source"], f"export {i + 1}: report records matching frame count")
        check(rep["summary"]["faces_left_visible"] == ([tracks[0]["id"] + 1] if tracks else []),
              f"export {i + 1}: report lists the face left visible")
    if len(outs) == 2:
        check(outs[0] != outs[1] and outs[1].name.endswith("_redacted_2.mp4"),
              f"second export got a new name ({outs[1].name}), first kept")
    check(render.sha256(v) == src_hash, "source file unchanged")

    # Frames to look at: a third, two thirds, and the frame each of two busy tracks is clearest.
    ffmpeg, _ = media.tools()
    picks = sorted({n // 3, 2 * n // 3} | {t["best_frame"] for t in sorted(tracks, key=lambda t: -t["detections"])[:2]})
    for f in picks:
        if outs:
            sel = f"select=eq(n\\,{f})"
            for tag, path in (("src", v), ("out", outs[0])):
                subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(path), "-vf", sel, "-frames:v", "1",
                                str(stills / f"{v.stem}_f{f:05d}_{tag}.jpg")])
    for o in outs:  # test exports sit beside the test clips; remove them so the clip folder stays clean
        o.unlink(missing_ok=True)
        for suffix in ("_report.json", "_report.html"):
            o.with_name(o.stem + suffix).unlink(missing_ok=True)

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
