# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Original pure-Python tracker link() (app v0.1.0), kept as the reference for track_check.py."""
from redactor.track import centre, iou, shift


def link_reference(dets_per_frame, fps, max_gap_s=0.5):
    """Greedy frame-to-frame linking with constant-velocity prediction.

    Frames with no detection run (stride > 1) are simply empty lists. A track stays
    open for max_gap_s without a match, so dropped or skipped frames keep identity.
    Returns [{"id": int, "boxes": {frame: [x1, y1, x2, y2, score]}}].
    """
    max_gap = max(2, round(max_gap_s * fps))
    tracks, active = [], []
    for f, dets in enumerate(dets_per_frame):
        if not dets and not active:
            continue
        pairs = []
        for i, t in enumerate(active):
            dt = f - t["last"]
            last = t["boxes"][t["last"]]
            pred = shift(last, t["vel"][0] * dt, t["vel"][1] * dt)
            for j, d in enumerate(dets):
                o = iou(pred, d)
                if o > 0.1:
                    pairs.append((1.0 + o, i, j))
                    continue
                # Small, fast faces barely overlap frame to frame: fall back to centre distance.
                (px, py), (dx, dy) = centre(pred), centre(d)
                dist = ((px - dx) ** 2 + (py - dy) ** 2) ** 0.5
                size = max(pred[2] - pred[0], pred[3] - pred[1], d[2] - d[0], d[3] - d[1])
                if dist < 0.6 * size:
                    pairs.append((1.0 - dist / (0.6 * size), i, j))
        pairs.sort(reverse=True)
        used_t, used_d = set(), set()
        for _, i, j in pairs:
            if i in used_t or j in used_d:
                continue
            used_t.add(i)
            used_d.add(j)
            t, d = active[i], dets[j]
            dt = f - t["last"]
            (x0, y0), (x1, y1) = centre(t["boxes"][t["last"]]), centre(d)
            v = ((x1 - x0) / dt, (y1 - y0) / dt)
            t["vel"] = v if len(t["boxes"]) == 1 else (0.5 * t["vel"][0] + 0.5 * v[0], 0.5 * t["vel"][1] + 0.5 * v[1])
            t["boxes"][f] = list(d)
            t["last"] = f
        for j, d in enumerate(dets):
            if j not in used_d:
                t = {"id": len(tracks), "boxes": {f: list(d)}, "last": f, "vel": (0.0, 0.0)}
                tracks.append(t)
                active.append(t)
        active = [t for t in active if f - t["last"] < max_gap]
    return [{"id": t["id"], "boxes": t["boxes"]} for t in tracks]
