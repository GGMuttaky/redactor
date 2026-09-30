# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Whole engine on small generated clips: analyse -> hide a region -> export -> check the file.

Needs FFmpeg. The clips contain no faces, so these check the plumbing (frames, audio, timing,
pixel shape, report, file naming); tests/e2e.py does the same on real footage with faces.
"""
import json
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from redactor import analyze, media, render
from redactor.project import Project


@pytest.fixture(params=["cpu", "auto"])
def encoder(request, monkeypatch):
    """Run a test with the CPU encoder (what most PCs get) and with whatever this PC picks."""
    if request.param == "cpu":
        monkeypatch.setattr(media, "pick_encoder", lambda: media.CPU_ENCODER)
    return request.param


def make_clip(ffmpeg, path, seconds=2, size="320x240", extra_video=(), video_delay=0.0, audio=True,
              codec=("-c:v", "libx264", "-pix_fmt", "yuv420p")):
    cmd = [ffmpeg, "-v", "error", "-y"]
    if video_delay:
        cmd += ["-itsoffset", str(video_delay)]
    cmd += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += [*extra_video, *codec, "-g", "25"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "96k"]
    subprocess.run([*cmd, str(path)], check=True)
    return path


def export(p, mode="solid", shape="ellipse"):
    """What the server does when Export is clicked, run in this thread."""
    p.cancel_export = threading.Event()
    p.update(export_job={"status": "queued", "progress": 0, "stage": "Waiting"})
    render.export(p, mode, shape, p.snapshot())
    return p.data["export_job"]


def grab(ffmpeg, path, frame, w, h):
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame})",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def analysed(path):
    p = Project.create(path)
    analyze.analyze(p)
    assert p.data["status"] == "ready", p.data.get("error")
    return p


def test_export_hides_region_keeps_frames_and_audio(ffmpeg, tmp_path, encoder):
    src = make_clip(ffmpeg, tmp_path / "plain.mp4")
    before = media.sha256(src)
    p = analysed(src)
    n = p.data["n_frames"]
    assert n == 50 and len(p.times) == n
    assert media.count_frames(p.dir / "proxy.mp4") == n
    p.upsert_region({"start": 0, "end": n - 1, "keys": {"0": [40, 40, 120, 120]}, "label": "Screen"})

    job = export(p)
    assert job["status"] == "done", job.get("error")
    out = tmp_path / "plain_redacted.mp4"
    assert job["path"] == str(out) and job["frames_ok"] and job["out_frames"] == n
    info = media.probe(out)
    assert (info["width"], info["height"]) == (320, 240)
    assert info["audio_codecs"] == ["aac"]
    region = grab(ffmpeg, out, 10, 320, 240)[45:115, 45:115]
    assert region.max() < 30  # black box, allowing for compression
    assert grab(ffmpeg, src, 10, 320, 240)[45:115, 45:115].mean() > 60

    report = json.loads(out.with_name("plain_redacted_report.json").read_text(encoding="utf-8"))
    assert report["output"]["frame_count_matches_source"]
    assert not report["output"]["padded_to_even_size"]
    assert report["output"]["sha256"] == media.sha256(out)
    html = out.with_name("plain_redacted_report.html").read_text(encoding="utf-8")
    assert str(tmp_path) not in html  # file names only: folder paths can contain a user name
    assert media.sha256(src) == before  # the original is never touched

    second = export(p)
    assert second["status"] == "done" and second["path"].endswith("plain_redacted_2.mp4")
    assert media.sha256(out) == report["output"]["sha256"]  # first export left as it was
    assert not list(tmp_path.glob("*.part.mp4"))


def test_clip_without_audio(ffmpeg, tmp_path):
    p = analysed(make_clip(ffmpeg, tmp_path / "silent.mp4", audio=False))
    job = export(p, "blur")
    assert job["status"] == "done", job.get("error")
    assert media.probe(job["path"])["audio_codecs"] == []


def test_odd_size_is_padded_to_even(ffmpeg, tmp_path, encoder):
    """Screen recordings and crops can be 321x241; H.264 4:2:0 can't, so one black line is added."""
    src = make_clip(ffmpeg, tmp_path / "odd.mkv", extra_video=["-vf", "scale=321:241"], codec=("-c:v", "ffv1"))
    p = analysed(src)
    assert (p.data["width"], p.data["height"]) == (321, 241)
    job = export(p, "blur")
    assert job["status"] == "done", job.get("error")
    info = media.probe(job["path"])
    assert (info["width"], info["height"]) == (322, 242)
    report = json.loads(Path(job["report"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert report["output"]["padded_to_even_size"]
    frame = grab(ffmpeg, job["path"], 5, 322, 242)
    luma = frame @ np.array([0.299, 0.587, 0.114])  # colour bleeds a little into the line (4:2:0), brightness doesn't
    assert luma[:, 321].mean() < 30 and luma[241, :].mean() < 30  # the added line is black
    assert luma[:241, :321].mean() > 60  # the picture is all there


def test_rotated_phone_video_exports_upright(ffmpeg, tmp_path):
    plain = make_clip(ffmpeg, tmp_path / "plain.mp4")
    src = tmp_path / "phone.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-display_rotation:v:0", "90", "-i", str(plain), "-c", "copy",
                    str(src)], check=True)
    p = analysed(src)
    assert (p.data["width"], p.data["height"]) == (240, 320)
    job = export(p, "blur")
    assert job["status"] == "done", job.get("error")
    info = media.probe(job["path"])
    assert (info["width"], info["height"]) == (240, 320)


def test_anamorphic_pixels_keep_their_shape(ffmpeg, tmp_path):
    src = make_clip(ffmpeg, tmp_path / "wide.mp4", extra_video=["-vf", "setsar=4/3"])
    p = analysed(src)
    job = export(p, "blur")
    assert job["status"] == "done", job.get("error")
    assert media.probe(job["path"])["sar"] == "4:3"


def test_player_times_include_a_delayed_video_start(ffmpeg, tmp_path):
    """Camera files often start audio first; the player's clock then starts before frame 0."""
    src = make_clip(ffmpeg, tmp_path / "late.mp4", video_delay=0.2)
    p = analysed(src)
    assert p.times[0] == pytest.approx(0.2, abs=0.05)
    assert p.times[1] - p.times[0] == pytest.approx(0.04, abs=0.002)


def test_cancelled_export_leaves_no_files(ffmpeg, tmp_path):
    p = analysed(make_clip(ffmpeg, tmp_path / "stop.mp4"))
    p.cancel_export = threading.Event()
    p.cancel_export.set()
    p.update(export_job={"status": "queued", "progress": 0, "stage": "Waiting"})
    render.export(p, "blur", "ellipse", p.snapshot())
    assert p.data["export_job"]["status"] == "cancelled"
    assert sorted(f.name for f in tmp_path.iterdir()) == ["stop.mp4"]


def test_files_that_are_not_video_fail_with_a_clear_message(ffmpeg, tmp_path, dummy_video):
    song = tmp_path / "song.m4a"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(song)],
                   check=True)
    for path, words in ((song, "no video"), (dummy_video, "Could not read")):
        p = Project.create(path)
        analyze.analyze(p)
        assert p.data["status"] == "error"
        assert words in p.data["error"]


def test_one_frame_clip(ffmpeg, tmp_path):
    p = analysed(make_clip(ffmpeg, tmp_path / "one.mp4", seconds=0.04, audio=False))
    assert p.data["n_frames"] == 1
    job = export(p, "blur")
    assert job["status"] == "done" and job["frames_ok"], job.get("error")


def test_non_english_file_and_folder_names(ffmpeg, tmp_path):
    folder = tmp_path / "Café 東京 — ünïcode"
    folder.mkdir()
    p = analysed(make_clip(ffmpeg, folder / "Straße 동영상.mp4"))
    assert len(p.track_summary) == 0 and p.data["n_frames"] == 50
    job = export(p, "blur")
    assert job["status"] == "done", job.get("error")
    assert Path(job["path"]).name == "Straße 동영상_redacted.mp4" and Path(job["path"]).is_file()
    html = Path(job["report"]).read_text(encoding="utf-8")
    assert "Straße 동영상.mp4" in html
