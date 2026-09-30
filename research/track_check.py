# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Fast (matrix) tracker vs the original pair-by-pair one: identical tracks? how much faster?

Uses every clip's real every-frame YuNet detections from a spike run.

    app\\.venv\\Scripts\\python.exe spike\\track_check.py spike\\out\\r2_yunet_c50
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from redactor.track import link  # noqa: E402
from track_ref import link_reference  # noqa: E402

print("| clip | faces/frame | tracks | identical | original | fast | speed-up |")
print("|---|---|---|---|---|---|---|")
for clip in sorted(p for p in Path(sys.argv[1]).iterdir() if p.is_dir()):
    raw = json.loads((clip / "boxes.json").read_text())["raw"]
    fps = json.loads((clip / "stats.json").read_text())["fps"]
    t0 = time.perf_counter(); a = link_reference(raw, fps); t1 = time.perf_counter()
    b = link(raw, fps); t2 = time.perf_counter()
    same = len(a) == len(b) and all(x["id"] == y["id"] and x["boxes"] == y["boxes"] for x, y in zip(a, b))
    n = len(raw)
    print(f"| {clip.name[:2]} | {sum(map(len, raw)) / n:.1f} | {len(a)} | {'yes' if same else 'NO'} | "
          f"{t1 - t0:.2f} s | {t2 - t1:.2f} s | {(t1 - t0) / max(1e-9, t2 - t1):.0f}x |", flush=True)
