# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Shared test set-up: every test run gets its own Redactor home, so real projects are never touched."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

_HOME = Path(tempfile.mkdtemp(prefix="redactor-test-"))
os.environ["REDACTOR_HOME"] = str(_HOME)

from redactor import media  # noqa: E402
from redactor.project import Project  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture
def dummy_video(tmp_path):
    """A file that stands in for a video where nothing reads its contents."""
    path = tmp_path / "Holiday Clip (1).mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def project(dummy_video):
    """A project with frame size and length set, as if analysis had run."""
    p = Project.create(dummy_video)
    p.update(status="ready", n_frames=100, fps=25.0, width=640, height=360)
    yield p
    with Project._cache_lock:  # a test may leave a job marked running, which delete() refuses
        Project._cache.pop(p.id, None)
    shutil.rmtree(p.dir, ignore_errors=True)


@pytest.fixture(scope="session")
def ffmpeg():
    if not media.tools_ok():
        pytest.skip("FFmpeg is not installed")
    return media.tools()[0]
