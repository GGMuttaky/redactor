# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Export speed: old vs new redaction, CPU (x264) vs hardware encoder.

1. Redaction alone, per frame, old method (full-size Gaussian, per-frame masks) vs new
   (blur the 10x-smaller grid, cached masks), on the office clip's real boxes. Also writes
   a side-by-side still of both methods on the same frame, to check the look.
2. Whole export (decode -> redact -> encode, threaded) with libx264 and with the hardware
   encoder; frames/s, frame count, output size.

    app\\.venv\\Scripts\\python.exe spike\\export_bench.py <project id> <still.jpg>
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from redactor import media, render  # noqa: E402
from redactor.project import Project  # noqa: E402


def old_effect(roi):
    h, w = roi.shape[:2]
    small = cv2.resize(roi, (max(1, w // 10), max(1, h // 10)), interpolation=cv2.INTER_AREA)
    big = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.GaussianBlur(big, (0, 0), max(1.0, min(w, h) / 8))


def old_redact(frame, items):
    fh, fw = frame.shape[:2]
    for kind, box in items:
        x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
        x2, y2 = min(fw, int(np.ceil(box[2]))), min(fh, int(np.ceil(box[3])))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        roi = frame[y1:y2, x1:x2]
        eff = old_effect(roi)
        if kind == "face":
            h, w = roi.shape[:2]
            mask = np.zeros((h, w), np.float32)
            cv2.ellipse(mask, (w // 2, h // 2), (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 1.0, -1)
            mask = np.clip(cv2.GaussianBlur(mask, (0, 0), max(0.5, min(w, h) / 30)) * 1.5, 0, 1)[..., None]
            roi[:] = (eff * mask + roi * (1 - mask)).astype(np.uint8)
        else:
            roi[:] = eff


p = Project.get(sys.argv[1])
still = Path(sys.argv[2])
src = p.data["source"]
cap = cv2.VideoCapture(src)
frames = []
for f, items in p.frame_boxes():
    ok, fr = cap.read()
    if not ok:
        break
    frames.append((fr, items))
cap.release()

t_old = t_new = 0.0
for fr, items in frames:
    a, b = fr.copy(), fr.copy()
    t0 = time.perf_counter(); old_redact(a, items); t1 = time.perf_counter()
    render.redact(b, items, "blur", "ellipse"); t2 = time.perf_counter()
    t_old += t1 - t0
    t_new += t2 - t1
n = len(frames)
print(f"redaction per frame ({n} frames, {sum(len(i) for _, i in frames) / n:.1f} boxes/frame): "
      f"old {t_old / n * 1000:.1f} ms, new {t_new / n * 1000:.1f} ms")

# Side by side on the frame with the largest box, cropped around it.
fr, items = max(frames, key=lambda x: max(((b[2] - b[0]) for _, b in x[1]), default=0))
a, b = fr.copy(), fr.copy()
old_redact(a, items)
render.redact(b, items, "blur", "ellipse")
big = max(items, key=lambda it: it[1][2] - it[1][0])[1]
x1, y1 = max(0, int(big[0]) - 60), max(0, int(big[1]) - 60)
x2, y2 = min(fr.shape[1], int(big[2]) + 60), min(fr.shape[0], int(big[3]) + 60)
pair = np.hstack([a[y1:y2, x1:x2], np.full((y2 - y1, 8, 3), 255, np.uint8), b[y1:y2, x1:x2]])
cv2.imwrite(str(still), pair)
print(f"still: {still} (left old, right new)")

for enc in (media.CPU_ENCODER, media.pick_encoder()):
    part = Path(src).with_name(f"_bench_{enc[0]}.mp4")
    t0 = time.perf_counter()
    stats = render._encode(p, Path(src), part, "blur", "ellipse", enc, lambda *a: None)
    dt = time.perf_counter() - t0
    frames_out = render.count_frames(part)
    print(f"export {enc[1]:>6} ({enc[0]}): {n / dt:.1f} fps, {dt:.1f} s, frames {frames_out}/{n}, "
          f"{part.stat().st_size / 1e6:.1f} MB")
    part.unlink()
