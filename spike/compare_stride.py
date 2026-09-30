# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""How much does detecting every Nth frame lose?

Takes the every-frame YuNet detections from a spike run as the reference. For each
stride N, keeps only every Nth frame's detections, runs the app's own tracker
(app/redactor/track.py), and checks every reference face on the *skipped* frames:
is it at least 80 % inside one of the boxes the strided run would redact?

    python compare_stride.py out/r2_yunet_c50
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from redactor import track  # noqa: E402

run = Path(sys.argv[1])
rows = []
for clip in sorted(p for p in run.iterdir() if p.is_dir()):
    stats = json.loads((clip / "stats.json").read_text())
    raw = json.loads((clip / "boxes.json").read_text())["raw"]
    fps, w, h, n = stats["fps"], stats["width"], stats["height"], stats["frames"]
    row = {"clip": clip.name[:2], "faces/frame": stats["mean_det_per_frame"]}
    for stride in (1, 2, 3):
        dets = [d if f % stride == 0 else [] for f, d in enumerate(raw)]
        per_frame = [[] for _ in range(n)]
        for t in track.link(dets, fps):
            for f, (box, _) in track.expand(t, n, fps, w, h).items():
                per_frame[f].append(track.pad(box, w, h))
        total = covered = 0
        for f, ref in enumerate(raw):
            if stride > 1 and f % stride == 0:
                continue  # detected frames are covered by construction; judge the skipped ones
            for r in ref:
                a = track.area(r)
                if a <= 0:
                    continue
                total += 1
                best = max((track.area([max(r[0], b[0]), max(r[1], b[1]), min(r[2], b[2]), min(r[3], b[3])]) / a
                            for b in per_frame[f]), default=0)
                covered += best >= 0.8
        row[f"x{stride}"] = f"{covered / total:.1%}" if total else "n/a"
        row[f"x{stride}_n"] = total
    rows.append(row)

print("| clip | faces/frame | every frame (self-check) | every 2nd | every 3rd | faces judged (x2) |")
print("|---|---|---|---|---|---|")
for r in rows:
    print(f"| {r['clip']} | {r['faces/frame']} | {r['x1']} | {r['x2']} | {r['x3']} | {r['x2_n']} |")
