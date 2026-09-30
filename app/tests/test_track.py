# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""The tracker: joins per-frame detections into one track per face."""
import random
import sys
from pathlib import Path

from redactor.track import expand, link

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "research"))
from track_ref import link_reference  # noqa: E402  (the original, slower tracker kept for comparison)


def box(x, y, size=40, score=0.9):
    return [x, y, x + size, y + size, score]


def test_one_moving_face_is_one_track():
    frames = [[box(10 + 3 * f, 50)] for f in range(30)]
    tracks = link(frames, fps=25)
    assert len(tracks) == 1
    assert sorted(tracks[0]["boxes"]) == list(range(30))


def test_two_faces_stay_separate():
    frames = [[box(10 + f, 50), box(400 - f, 50)] for f in range(30)]
    tracks = link(frames, fps=25)
    assert len(tracks) == 2
    for t in tracks:
        xs = [t["boxes"][f][0] for f in sorted(t["boxes"])]
        assert xs == sorted(xs) or xs == sorted(xs, reverse=True)  # no swapping between the two faces


def test_short_gap_keeps_identity_long_gap_starts_new_track():
    fps = 25
    short = [[box(100, 100)]] * 10 + [[]] * 5 + [[box(100, 100)]] * 10  # 0.2 s missing
    assert len(link(short, fps)) == 1
    long = [[box(100, 100)]] * 10 + [[]] * 30 + [[box(100, 100)]] * 10  # 1.2 s missing
    assert len(link(long, fps)) == 2


def test_matches_reference_tracker():
    """The fast (matrix) tracker must give exactly the tracks of the original pair-by-pair one."""
    rng = random.Random(7)
    for _ in range(20):
        faces = [[rng.uniform(0, 1800), rng.uniform(0, 1000), rng.uniform(-6, 6), rng.uniform(-6, 6),
                  rng.uniform(20, 120)] for _ in range(rng.randint(1, 12))]
        frames = []
        for _f in range(60):
            dets = []
            for fc in faces:
                fc[0] += fc[2]
                fc[1] += fc[3]
                if rng.random() > 0.15:  # detector misses some frames
                    dets.append(box(fc[0], fc[1], fc[4], rng.uniform(0.5, 1)))
            if rng.random() < 0.1:
                dets.append(box(rng.uniform(0, 1800), rng.uniform(0, 1000), 30, 0.55))  # stray false alarm
            rng.shuffle(dets)
            frames.append(dets)
        assert link(frames, 25) == link_reference(frames, 25)


def test_expand_fills_gaps_and_stays_in_frame():
    t = {"id": 0, "boxes": {10: box(0, 0), 14: box(8, 0)}}
    boxes = expand(t, n_frames=30, fps=25, w=640, h=360)
    frames = sorted(boxes)
    assert set(range(10, 15)) <= set(frames)
    assert [boxes[f][1] for f in range(10, 15)] == ["det", "fill", "fill", "fill", "det"]  # gap 11-13 filled
    assert boxes[12][0][0] == 4  # halfway between x=0 and x=8
    assert frames[0] < 10 and frames[-1] > 14  # lead-in and lead-out
    for f in frames:
        x1, y1, x2, y2 = boxes[f][0]
        assert 0 <= x1 < x2 <= 640 and 0 <= y1 < y2 <= 360
