# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""GPU detector: does it agree with OpenCV, and how much faster is it?

For every frame of each clip, runs
  ref  = OpenCV FaceDetectorYN, CPU, 2023mar model   (what the app used so far)
  ocpu = our decoder, ONNX Runtime CPU, 2026may model
  dml  = our decoder, ONNX Runtime DirectML (GPU), 2026may model
Agreement: detections matched one-to-one by IoU >= 0.8 against ref; reports matched share,
extras on each side, and the largest coordinate difference among matches.
Speed: detector time only (decode excluded), frames per second.

    app\\.venv\\Scripts\\python.exe research\\gpu_bench.py testclips\\01_*.mp4 ...
"""
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from redactor.detect import CpuDetector, GpuDetector  # noqa: E402
from redactor.track import iou  # noqa: E402


def match(a, b, thr=0.8):
    pairs = sorted(((iou(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b)), reverse=True)
    used_a, used_b, diffs = set(), set(), []
    for o, i, j in pairs:
        if o < thr:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        diffs.append(max(abs(a[i][k] - b[j][k]) for k in range(4)))
    return len(used_a), len(a) - len(used_a), len(b) - len(used_b), max(diffs, default=0.0)


if __name__ == "__main__":
    dets = {"ref": CpuDetector(0.5), "ocpu": GpuDetector(0.5, provider="CPUExecutionProvider"), "dml": GpuDetector(0.5)}
    print("| clip | res | frames | OpenCV CPU fps | ORT CPU fps | GPU fps | GPU vs OpenCV: matched "
          "| only OpenCV | only GPU | max px diff |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for path in sys.argv[1:]:
        cap = cv2.VideoCapture(path)
        t = {k: 0.0 for k in dets}
        n, m_tot, a_only, b_only, maxdiff, ref_total, w, h = 0, 0, 0, 0, 0.0, 0, 0, 0
        ocpu_agree = [0, 0]
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            out = {}
            for k, d in dets.items():
                t0 = time.perf_counter()
                out[k] = d.detect(frame)
                t[k] += time.perf_counter() - t0
            m, ao, bo, md = match(out["ref"], out["dml"])
            m_tot, a_only, b_only, maxdiff = m_tot + m, a_only + ao, b_only + bo, max(maxdiff, md)
            ref_total += len(out["ref"])
            m2, _, _, _ = match(out["ref"], out["ocpu"])
            ocpu_agree[0] += m2
            ocpu_agree[1] += len(out["ref"])
            n += 1
        cap.release()
        share = f"{m_tot / ref_total:.2%}" if ref_total else "n/a"
        print(f"| {Path(path).name[:2]} | {w}x{h} | {n} | {n / t['ref']:.1f} | {n / t['ocpu']:.1f} | "
              f"{n / t['dml']:.1f} | {share} ({m_tot}/{ref_total}) | {a_only} | {b_only} | {maxdiff:.2f} |", flush=True)
        print(f"  (ORT CPU vs OpenCV matched {ocpu_agree[0]}/{ocpu_agree[1]})", flush=True)
