# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Detection stage of the app, as the app runs it (threaded decode + detector), CPU vs GPU.

Wall-clock frames per second, including decoding. Also checks the GPU run finds the
same faces as the CPU run (IoU >= 0.8 one-to-one matches).

    app\\.venv\\Scripts\\python.exe spike\\pipeline_bench.py testclips\\0*.mp4
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from redactor import analyze, media  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gpu_bench import match  # noqa: E402  (importing runs nothing: the bench loop needs argv clips)


class FakeProject:
    def __init__(self, gpu):
        self.data = {"settings": {"conf": 0.5, "stride": 1, "gpu": gpu}}

    def update(self, **kw):
        self.data.update(kw)

    def set_progress(self, *a):
        pass


print("| clip | res | frames | CPU fps | GPU fps | speed-up | GPU vs CPU faces matched |")
print("|---|---|---|---|---|---|---|")
for path in sys.argv[1:]:
    info = media.probe(path)
    runs = {}
    for gpu in (False, True):
        p = FakeProject(gpu)
        t0 = time.perf_counter()
        dets, fps, size = analyze._detect(p, path, info, lambda: False)
        runs[gpu] = (dets, len(dets) / (time.perf_counter() - t0), p.data["detector"])
    cpu, gpu = runs[False], runs[True]
    m = tot = 0
    for a, b in zip(cpu[0], gpu[0]):
        m += match(a, b)[0]
        tot += len(a)
    print(f"| {Path(path).name[:2]} | {info['width']}x{info['height']} | {len(cpu[0])} | {cpu[1]:.1f} ({cpu[2]}) | "
          f"{gpu[1]:.1f} ({gpu[2]}) | {gpu[1] / cpu[1]:.1f}x | {m}/{tot} ({m / tot:.2%}) |" if tot else
          f"| {Path(path).name[:2]} | {info['width']}x{info['height']} | {len(cpu[0])} | {cpu[1]:.1f} | {gpu[1]:.1f} | "
          f"{gpu[1] / cpu[1]:.1f}x | no faces |", flush=True)
