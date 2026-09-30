# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Projects: ids, regions, the export snapshot, clean-up after a crash."""
import json

import numpy as np
import pytest

from redactor.project import BOX_DTYPE, PID_RE, Project, clean_region, interpolate_region, project_id

REGION = {"start": 10, "end": 20, "keys": {"10": [0, 0, 100, 100], "20": [100, 0, 200, 100]}}


# ---------------------------------------------------------------- regions

def test_region_moves_linearly_between_keyframes():
    assert interpolate_region(REGION, 15) == [50, 0, 150, 100]


def test_region_holds_outside_keyframes_and_stops_outside_its_range():
    r = dict(REGION, start=5, end=25)
    assert interpolate_region(r, 7) == [0, 0, 100, 100]
    assert interpolate_region(r, 23) == [100, 0, 200, 100]
    assert interpolate_region(r, 4) is None
    assert interpolate_region(r, 26) is None
    assert interpolate_region(dict(r, enabled=False), 15) is None


def test_clean_region_clamps_to_the_frame_and_orders_corners():
    r = clean_region({"start": -5, "end": 500, "keys": {"3": [700, 400, -10, 50]}, "label": "  Plate  "},
                     n_frames=100, width=640, height=360)
    assert r["keys"] == {"3": [0.0, 50.0, 640.0, 360.0]}
    assert (r["start"], r["end"]) == (0, 99)
    assert r["label"] == "Plate"
    assert r["id"] is None and r["enabled"] is True


def test_clean_region_limits_label_length():
    r = clean_region(dict(REGION, label="x" * 1000), 100, 640, 360)
    assert len(r["label"]) == 120


@pytest.mark.parametrize("body", [
    "not an object",
    {"start": 0, "end": 5},                                        # no keyframes
    {"start": 0, "end": 5, "keys": {}},
    {"start": "a", "end": 5, "keys": {"0": [0, 0, 9, 9]}},
    {"start": 0, "end": 5, "keys": {"0": [0, 0, 9]}},              # three coordinates
    {"start": 0, "end": 5, "keys": {"0": [0, 0, "NaN", 9]}},
    {"start": 0, "end": 5, "keys": {"0": [5, 5, 5.5, 50]}},        # under a pixel wide
    {"start": 0, "end": 5, "keys": {"x": [0, 0, 9, 9]}},           # frame is not a number
])
def test_clean_region_rejects_bad_input(body):
    with pytest.raises(ValueError):
        clean_region(body, 100, 640, 360)


def test_new_regions_get_the_next_free_id(project):
    a = project.upsert_region(REGION)
    b = project.upsert_region(REGION)
    assert (a["id"], b["id"]) == (1, 2)
    assert a["label"] == "Region 1"
    project.delete_region(1)
    c = project.upsert_region(REGION)
    assert c["id"] == 3  # ids are never reused while higher ones exist
    edited = project.upsert_region(dict(REGION, id=2, label="Screen"))
    assert edited["id"] == 2
    assert [r["label"] for r in project.data["regions"]] == ["Screen", "Region 3"]


# ---------------------------------------------------------------- ids

def test_project_id_is_safe_for_urls_and_folders(dummy_video):
    pid = project_id(dummy_video)
    assert PID_RE.match(pid)
    assert pid.startswith("holiday-clip-1-")


@pytest.mark.parametrize("bad", ["../../windows", "ABC-12345678", "x-1234567", "", None, "a" * 60 + "-12345678"])
def test_bad_project_ids_are_refused(bad):
    with pytest.raises(KeyError):
        Project.get(bad)


def test_create_validates_settings(dummy_video):
    with pytest.raises(ValueError):
        Project.create(dummy_video, sensitivity="extreme")
    with pytest.raises(ValueError):
        Project.create(dummy_video, stride=3)
    with pytest.raises(FileNotFoundError):
        Project.create(dummy_video.with_name("missing.mp4"))


# ---------------------------------------------------------------- export snapshot

def _two_tracks(p):
    boxes = np.zeros(4, BOX_DTYPE)
    boxes[:] = [(0, 0, 1, 1, 50, 50, 1), (0, 1, 60, 60, 90, 90, 1), (1, 0, 2, 2, 52, 52, 1), (1, 1, 61, 61, 91, 91, 0)]
    p.store_results([{"id": 0}, {"id": 1}], boxes, [0.0, 0.04])
    p.update(n_frames=2)


def test_frame_boxes_skips_hidden_faces_and_adds_regions(project):
    _two_tracks(project)
    project.set_tracks_enabled([1], False)
    project.upsert_region({"start": 1, "end": 1, "keys": {"1": [100, 100, 200, 200]}})
    frames = dict(project.frame_boxes(project.snapshot()))
    assert frames[0] == [("face", [1.0, 1.0, 50.0, 50.0])]
    assert frames[1] == [("face", [2.0, 2.0, 52.0, 52.0]), ("region", [100.0, 100.0, 200.0, 200.0])]


def test_snapshot_is_not_changed_by_later_edits(project):
    _two_tracks(project)
    snap = project.snapshot()
    project.set_tracks_enabled([0, 1], False)
    project.upsert_region(REGION)
    assert snap["disabled_tracks"] == [] and snap["regions"] == []
    assert len(dict(project.frame_boxes(snap))[0]) == 2


# ---------------------------------------------------------------- lifecycle

def test_delete_refuses_while_exporting(project):
    project.update(export_job={"status": "running"})
    with pytest.raises(RuntimeError):
        Project.delete(project.id)
    project.update(export_job={"status": "done"})
    Project.delete(project.id)
    assert not project.dir.exists()
    with pytest.raises(KeyError):
        Project.get(project.id)


def test_recover_interrupted_marks_jobs_and_removes_half_written_files(project, tmp_path):
    out = tmp_path / "clip_redacted.mp4"
    part = tmp_path / "clip_redacted.part.mp4"
    part.write_bytes(b"half")
    (project.dir / "proxy.part.mp4").write_bytes(b"half")
    project.update(status="analyzing", export_job={"status": "running", "path": str(out)})
    Project.recover_interrupted()
    d = json.loads((project.dir / "project.json").read_text(encoding="utf-8"))
    assert d["status"] == "cancelled"
    assert d["export_job"]["status"] == "cancelled"
    assert not part.exists() and not (project.dir / "proxy.part.mp4").exists()
