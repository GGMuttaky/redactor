# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Link per-frame detections into tracks and fill the frames between them.

Offline, so both ends of every gap are known: gaps are interpolated rather than
predicted. Each track also gets a short lead-in/out that follows its motion,
covering a face for a moment before it is first found and after it is last found.
"""
import itertools

import numpy as np


# Plain-Python geometry helpers, used by the tests and research scripts as the reference
# the vectorised code is checked against.
def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (area(a) + area(b) - inter + 1e-9)


def centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def shift(b, dx, dy):
    return [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]


def lerp(a, b, r):
    return [a[k] + (b[k] - a[k]) * r for k in range(4)]


def pad(b, w, h):
    """Redaction area: detector boxes are brow-to-chin, so grow them to cover hair and ears."""
    bw, bh = b[2] - b[0], b[3] - b[1]
    return [max(0.0, b[0] - 0.20 * bw), max(0.0, b[1] - 0.35 * bh),
            min(float(w), b[2] + 0.20 * bw), min(float(h), b[3] + 0.15 * bh)]


def _candidate_pairs(active, dets, f):
    """Every (score, track index, detection index) that may be the same face, best first.

    Score 1 + IoU when the prediction overlaps the detection (IoU > 0.1); otherwise, for
    small fast faces that barely overlap frame to frame, 1 - distance / (0.6 * size) when
    the centres are close. Computed as matrices: a crowd has ~50 faces and ~100 open
    tracks per frame, and pair-by-pair Python took almost as long as GPU detection itself
    (5.7 s vs ~6.5 s on a 5 s crowd clip; this version: 0.23 s). Order and ties match the
    original pair-by-pair version (research/track_ref.py), checked on all test clips.
    """
    last = np.array([t["boxes"][t["last"]][:4] for t in active], dtype=np.float64)
    vel = np.array([t["vel"] for t in active], dtype=np.float64)
    dt = np.array([f - t["last"] for t in active], dtype=np.float64)
    P = last + np.column_stack([vel[:, 0] * dt, vel[:, 1] * dt, vel[:, 0] * dt, vel[:, 1] * dt])
    D = np.array([d[:4] for d in dets], dtype=np.float64)
    ix = np.clip(np.minimum(P[:, None, 2], D[None, :, 2]) - np.maximum(P[:, None, 0], D[None, :, 0]), 0, None)
    iy = np.clip(np.minimum(P[:, None, 3], D[None, :, 3]) - np.maximum(P[:, None, 1], D[None, :, 1]), 0, None)
    inter = ix * iy
    area_p = np.clip(P[:, 2] - P[:, 0], 0, None) * np.clip(P[:, 3] - P[:, 1], 0, None)
    area_d = np.clip(D[:, 2] - D[:, 0], 0, None) * np.clip(D[:, 3] - D[:, 1], 0, None)
    o = inter / (area_p[:, None] + area_d[None, :] - inter + 1e-9)
    dx = (P[:, None, 0] + P[:, None, 2]) / 2 - (D[None, :, 0] + D[None, :, 2]) / 2
    dy = (P[:, None, 1] + P[:, None, 3]) / 2 - (D[None, :, 1] + D[None, :, 3]) / 2
    dist = (dx ** 2 + dy ** 2) ** 0.5
    size = np.maximum(np.maximum(P[:, 2] - P[:, 0], P[:, 3] - P[:, 1])[:, None],
                      np.maximum(D[:, 2] - D[:, 0], D[:, 3] - D[:, 1])[None, :])
    overlap = o > 0.1
    near = ~overlap & (dist < 0.6 * size)
    ii, jj = np.nonzero(overlap | near)
    if not len(ii):
        return []
    s = np.where(overlap[ii, jj], 1.0 + o[ii, jj], 1.0 - dist[ii, jj] / (0.6 * size[ii, jj]))
    order = np.lexsort((-jj, -ii, -s))  # score desc, then index desc: same as sorting tuples reversed
    return [(float(s[k]), int(ii[k]), int(jj[k])) for k in order]


def link(dets_per_frame, fps, max_gap_s=0.5):
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
        pairs = _candidate_pairs(active, dets, f) if active and dets else []
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


def _edge_velocity(boxes, frames):
    if len(frames) < 2:
        return 0.0, 0.0
    (x0, y0), (x1, y1) = centre(boxes[frames[0]]), centre(boxes[frames[-1]])
    dt = frames[-1] - frames[0]
    return (x1 - x0) / dt, (y1 - y0) / dt


def expand(track, n_frames, fps, w, h, lead_s=0.25):
    """Every frame a track covers: {frame: (box, kind)} with kind "det" or "fill".

    Boxes are clipped to the frame; a lead-in/out box that has left the frame is dropped.
    """
    lead = round(lead_s * fps)
    boxes = track["boxes"]
    fs = sorted(boxes)
    out = {}

    def put(f, box, kind):
        box = [max(0.0, box[0]), max(0.0, box[1]), min(float(w), box[2]), min(float(h), box[3])]
        if box[2] - box[0] >= 2 and box[3] - box[1] >= 2:
            out[f] = (box, kind)

    for a, b in itertools.pairwise(fs):
        put(a, boxes[a][:4], "det")
        for g in range(a + 1, b):
            put(g, lerp(boxes[a], boxes[b], (g - a) / (b - a)), "fill")
    put(fs[-1], boxes[fs[-1]][:4], "det")
    vx, vy = _edge_velocity(boxes, fs[:5])
    for g in range(max(0, fs[0] - lead), fs[0]):
        put(g, shift(boxes[fs[0]], vx * (g - fs[0]), vy * (g - fs[0])), "fill")
    vx, vy = _edge_velocity(boxes, fs[-5:])
    for g in range(fs[-1] + 1, min(n_frames, fs[-1] + lead + 1)):
        put(g, shift(boxes[fs[-1]], vx * (g - fs[-1]), vy * (g - fs[-1])), "fill")
    return out
