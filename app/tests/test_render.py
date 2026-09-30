# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Redaction drawing, output names, timecodes and small media helpers."""
import numpy as np
import pytest

from redactor import media, render


def noisy_frame(w=320, h=240, seed=1):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


# ---------------------------------------------------------------- redact

@pytest.mark.parametrize("mode", ["blur", "pixelate", "solid"])
def test_redact_changes_only_inside_the_box(mode):
    src = noisy_frame()
    out = src.copy()
    render.redact(out, [("region", [100, 80, 200, 160])], mode, "rect")
    assert np.array_equal(out[:80], src[:80]) and np.array_equal(out[160:], src[160:])
    assert np.array_equal(out[:, :100], src[:, :100]) and np.array_equal(out[:, 200:], src[:, 200:])
    inside = out[80:160, 100:200].astype(int)
    assert np.abs(inside - src[80:160, 100:200]).mean() > 30  # visibly different
    if mode == "solid":
        assert inside.max() == 0


def test_oval_face_leaves_box_corners_and_covers_the_centre():
    src = noisy_frame()
    out = src.copy()
    render.redact(out, [("face", [100, 80, 200, 160])], "solid", "ellipse")
    assert np.array_equal(out[80:84, 100:104], src[80:84, 100:104])  # corner of the box untouched
    assert out[115:125, 145:155].max() == 0  # centre hidden


def test_redact_survives_boxes_at_or_past_the_edges():
    src = noisy_frame()
    out = src.copy()
    render.redact(out, [("face", [-50, -50, 30, 30]), ("face", [300, 200, 400, 300]),
                        ("face", [10, 10, 11, 11]), ("region", [500, 500, 600, 600])], "blur", "ellipse")
    assert out.shape == src.shape
    assert np.array_equal(out[100:140, 100:220], src[100:140, 100:220])  # nothing outside the boxes changed


# ---------------------------------------------------------------- names and timecodes

def test_next_free_never_reuses_a_name(tmp_path):
    first = tmp_path / "clip_redacted.mp4"
    assert render.next_free(first) == first
    first.write_bytes(b"x")
    assert render.next_free(first).name == "clip_redacted_2.mp4"
    (tmp_path / "clip_redacted_2.mp4").write_bytes(b"x")
    assert render.next_free(first).name == "clip_redacted_3.mp4"


@pytest.mark.parametrize("frame,fps,expected", [
    (0, 25, "00:00:00.000"),
    (90, 30, "00:00:03.000"),
    (25 * 3600, 25, "01:00:00.000"),
    (1001, 30000 / 1001, "00:00:33.400"),
    (1, 1 / 59.9996, "00:01:00.000"),  # rounds up to a whole minute, never "00:00:60.000"
])
def test_timecode(frame, fps, expected):
    assert render._tc(frame, fps) == expected


def test_audio_is_copied_when_mp4_can_carry_it():
    assert render._audio_args({"audio_codecs": []}) == []
    assert render._audio_args({"audio_codecs": ["aac", "ac3"]}) == ["-map", "1:a?", "-c:a", "copy"]
    assert render._audio_args({"audio_codecs": ["aac", "pcm_s16le"]})[-4:] == ["-c:a", "aac", "-b:a", "256k"]


# ---------------------------------------------------------------- media helpers

@pytest.mark.parametrize("text,expected", [
    ("30000/1001", 29.97), ("25/1", 25.0), ("25", 25.0), ("0/0", 0.0), (None, 0.0), ("abc", 0.0),
])
def test_frame_rate_text(text, expected):
    assert media._rate(text) == pytest.approx(expected, abs=0.01)


def test_newest_ffmpeg_folder_sorts_first():
    paths = ["ffmpeg-9.0.1-full_build/bin/ffmpeg.exe", "ffmpeg-10.0-full_build/bin/ffmpeg.exe",
             "ffmpeg-9.1-full_build/bin/ffmpeg.exe"]
    assert sorted(paths, key=media._version_key, reverse=True)[0].startswith("ffmpeg-10.0")


def test_ffmpeg_problems_are_explained(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    (tmp_path / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setattr(media, "_tools", None)
    monkeypatch.setattr(media, "find_tool", lambda name: None)
    assert "not found" in media.tools_problem()
    monkeypatch.setattr(media, "find_tool", lambda name: str(fake))
    monkeypatch.setattr(media, "ffmpeg_version", lambda path: (4, 4))
    assert "4.4 is too old" in media.tools_problem()
    monkeypatch.setattr(media, "ffmpeg_version", lambda path: (9, 0))
    assert media.tools_problem() is None
