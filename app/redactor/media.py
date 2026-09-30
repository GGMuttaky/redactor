# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Everything that talks to ffmpeg / ffprobe, plus a threaded frame reader.

All child processes go through here so that three things hold everywhere:
text is decoded as UTF-8 (file names and metadata can be in any language), ffmpeg's
error output goes to a temporary file rather than a pipe (a full pipe would stall
ffmpeg forever), and long-running ffmpeg processes die with the app (winjob).
"""
import glob
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import cv2

from .winjob import bind_to_app

APP_DIR = Path(__file__).resolve().parent.parent
MIN_FFMPEG = (5, 1)  # -fps_mode, used for the preview copy, arrived in FFmpeg 5.1

# Keep child processes from flashing console windows when the app runs windowless.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_tools = None
_tools_lock = threading.Lock()


# ---------------------------------------------------------------- finding ffmpeg

def _version_key(path):
    """Sort key so that ffmpeg-10.0 ranks above ffmpeg-9.0.1 (plain text sorting gets this wrong)."""
    return [int(n) for n in re.findall(r"\d+", str(path))]


def _candidates(name):
    exe = f"{name}.exe" if sys.platform == "win32" else name
    if os.environ.get(name.upper()):
        yield os.environ[name.upper()]
    yield str(APP_DIR / "bin" / exe)
    yield shutil.which(name)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # winget installs Gyan.FFmpeg here, but its PATH shim does not always resolve.
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", exe)
        yield from sorted(glob.glob(pattern, recursive=True), key=_version_key, reverse=True)


def find_tool(name):
    for cand in _candidates(name):
        if cand and Path(cand).is_file():
            return cand
    return None


def ffmpeg_version(ffmpeg):
    """(major, minor) from `ffmpeg -version`, or None for builds that don't say (e.g. git snapshots)."""
    out = run([ffmpeg, "-hide_banner", "-version"])
    m = re.match(r"ffmpeg version n?(\d+)\.(\d+)", out.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def tools():
    """(ffmpeg, ffprobe) or a clear error. Found once per app run.

    ffprobe is taken from the same folder as ffmpeg when it is there, so the two
    always come from the same installation.
    """
    global _tools
    with _tools_lock:
        if _tools is None:
            ffmpeg = find_tool("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("FFmpeg was not found. Install it (for example `winget install Gyan.FFmpeg`) "
                                   "or put ffmpeg.exe and ffprobe.exe in the app's bin folder.")
            sibling = Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe"))
            ffprobe = str(sibling) if sibling.is_file() else find_tool("ffprobe")
            if not ffprobe:
                raise RuntimeError("ffprobe was not found next to ffmpeg. Reinstall FFmpeg (it ships both).")
            version = ffmpeg_version(ffmpeg)
            if version and version < MIN_FFMPEG:
                raise RuntimeError(f"FFmpeg {version[0]}.{version[1]} is too old; Redactor needs "
                                   f"{MIN_FFMPEG[0]}.{MIN_FFMPEG[1]} or newer (for example `winget upgrade Gyan.FFmpeg`).")
            _tools = (ffmpeg, ffprobe)
        return _tools


def tools_problem():
    """None when FFmpeg is usable, else the reason (missing, too old, no ffprobe) in plain words."""
    try:
        tools()
        return None
    except RuntimeError as e:
        return str(e)


def tools_ok():
    return tools_problem() is None


# ---------------------------------------------------------------- running processes

def run(cmd):
    """Short command, output captured as UTF-8 text."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)


def run_ffmpeg(args, duration=None, progress=None, should_stop=None):
    """Run a long ffmpeg job, reporting 0..1 progress, stoppable at any time.

    Progress lines are read on a helper thread and the stop flag is polled on a timer,
    so a job that goes quiet can still be cancelled. Errors go to a temp file.
    """
    ffmpeg, _ = tools()
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-progress", "pipe:1", "-nostats", *args],
                                stdout=subprocess.PIPE, stderr=errlog, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        bind_to_app(proc)

        def read_progress():
            for raw in proc.stdout:
                key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
                if key == "out_time_us" and value.isdigit() and duration and progress:
                    progress(min(1.0, int(value) / 1e6 / duration))

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        stopped = False
        while proc.poll() is None:
            if should_stop and should_stop():
                proc.kill()
                stopped = True
                break
            time.sleep(0.25)
        proc.wait()
        reader.join(timeout=5)
        if proc.returncode != 0 and not stopped:
            errlog.seek(0)
            raise RuntimeError(f"ffmpeg failed: {errlog.read().decode('utf-8', 'replace').strip()[-500:]}")
    return not stopped


# ---------------------------------------------------------------- reading media

def _rate(text):
    num, _, den = (text or "0/1").partition("/")
    try:
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


def probe(path):
    _, ffprobe = tools()
    out = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"Could not read this file as video: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    # An album-art picture in an audio file is a "video" stream of one frame: not a video.
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        raise RuntimeError("This file has no video stream.")
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(side["rotation"])
    w, h = int(video["width"]), int(video["height"])
    if rotation % 180:
        w, h = h, w  # decoders auto-rotate, so work in display orientation
    sar = video.get("sample_aspect_ratio") or "1:1"
    if sar in ("0:1", "N/A"):
        sar = "1:1"
    num, _, den = sar.partition(":")
    sar_value = float(num) / float(den) if den and float(den) else 1.0
    if rotation % 180 and sar_value:
        sar_value, sar = 1.0 / sar_value, f"{den}:{num}"
    return {
        "width": w, "height": h,
        "sar": sar, "sar_value": round(sar_value, 6),  # pixel shape; 1:1 unless anamorphic
        "fps": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "rate": video.get("avg_frame_rate") if _rate(video.get("avg_frame_rate")) else video.get("r_frame_rate"),
        "duration": float(info.get("format", {}).get("duration") or video.get("duration") or 0),
        "nb_frames": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        "video_codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "audio_codec": audio[0].get("codec_name") if audio else None,
        "audio_codecs": [a.get("codec_name") for a in audio],
        "size_bytes": int(info.get("format", {}).get("size") or 0),
    }


def frame_times(path, from_zero=True):
    """Presentation time of every video frame, from packet timestamps (no decoding, so fast).

    from_zero=False keeps the file's own timeline. Use that for the preview copy: the
    browser reports its times, and they include any delay before the first video frame
    (common in camera files whose audio starts first).
    """
    _, ffprobe = tools()
    out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
               "-of", "csv=p=0", str(path)])
    times = sorted(float(t) for t in out.stdout.replace(",", "\n").split() if t.strip() not in ("", "N/A"))
    if not times:
        return []
    base = times[0] if from_zero else 0.0
    return [round(t - base, 6) for t in times]


def count_frames(path):
    """Video frames actually in a file (counts packets; files with stream groups print it twice)."""
    _, ffprobe = tools()
    out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)])
    for token in out.stdout.replace(",", "\n").split():
        if token.isdigit():
            return int(token)
    return None


def sha256(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


_END = object()


class FrameReader:
    """Decodes a video on a background thread so decoding overlaps with the caller's work.

    Iterate to get (frame_index, frame, extra) where extra = prepare(frame) or None;
    with stride > 1 only every stride-th frame is decoded and the others arrive as
    (index, None, None). Always use as a context manager: leaving the block stops the
    thread even if the caller broke out early or raised.
    """

    def __init__(self, path, stride=1, prepare=None, depth=6):
        self.path, self.stride, self.prepare = str(path), max(1, int(stride)), prepare
        self.q = queue.Queue(maxsize=depth)
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.halt.set()
        while self.thread.is_alive():  # unblock a reader waiting on a full queue
            try:
                self.q.get(timeout=0.2)
            except queue.Empty:
                pass
        return False

    def _put(self, item):
        while not self.halt.is_set():
            try:
                self.q.put(item, timeout=0.25)
                return True
            except queue.Full:
                pass
        return False

    def _run(self):
        cap = cv2.VideoCapture(self.path)
        try:
            f = 0
            while not self.halt.is_set() and cap.grab():
                if f % self.stride == 0:
                    ok, frame = cap.retrieve()
                    if not ok:
                        break
                    item = (f, frame, self.prepare(frame) if self.prepare else None)
                else:
                    item = (f, None, None)
                if not self._put(item):
                    return
                f += 1
        except Exception as e:  # hand decoder errors to the consumer instead of dying silently
            self._put(e)
        finally:
            cap.release()
            self._put(_END)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item


# ---------------------------------------------------------------- encoding

# Hardware H.264 encoders, best first, with settings aimed at the same visual quality as x264 CRF 18.
# NVENC CQ 23 measured against a lossless encode of the same redacted office clip (2026-09-28):
# PSNR 48.8 dB / SSIM 0.9923 at 10.3 MB vs x264 CRF 18 at 47.8 dB / 0.9922 / 10.2 MB.
# AMF and QSV settings are untested (no such hardware here); they fall back to x264 if they fail.
HW_ENCODERS = [
    ("h264_nvenc", "NVIDIA", ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                              "-cq", "23", "-b:v", "0", "-profile:v", "high"]),
    ("h264_amf", "AMD", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                         "-qp_i", "18", "-qp_p", "20", "-qp_b", "22"]),
    ("h264_qsv", "Intel", ["-c:v", "h264_qsv", "-preset", "slower", "-global_quality", "20"]),
]
CPU_ENCODER = ("libx264", "CPU", ["-c:v", "libx264", "-preset", "fast", "-crf", "18"])
_encoder = None
_encoder_lock = threading.Lock()


def pick_encoder():
    """First hardware encoder that really works here (listed and able to encode a test clip), else x264.

    A listed encoder can still fail (no such card, old driver), so each is tried on a
    tiny generated clip, once per app run. Callers arriving meanwhile wait for the answer.
    """
    global _encoder
    with _encoder_lock:
        if _encoder is None:
            ffmpeg, _ = tools()
            listed = run([ffmpeg, "-hide_banner", "-encoders"]).stdout
            chosen = CPU_ENCODER
            for name, vendor, args in HW_ENCODERS:
                if f" {name} " not in listed:
                    continue
                test = run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=0.5",
                            *args, "-pix_fmt", "yuv420p", "-f", "null", "-"])
                if test.returncode == 0:
                    chosen = (name, vendor, args)
                    break
            _encoder = chosen
        return _encoder


def make_proxy(src, dst, duration, progress=None, should_stop=None):
    """Small H.264 copy for review: every source frame kept, short GOP for quick scrubbing.

    Made while faces are being found, so it is kept light on the CPU: two decode threads,
    and NVENC when the card has it (quality matters little for a 540p preview). The pixel
    shape (SAR) of anamorphic sources is kept, so the browser shows it at the right width.
    """
    name, _, _ = pick_encoder()
    venc = (["-c:v", name, "-preset", "p1", "-rc", "vbr", "-cq", "28", "-b:v", "0"] if name == "h264_nvenc" else
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-threads", "2"])
    return run_ffmpeg(["-threads", "2", "-i", str(src),
                       "-vf", "scale='if(gte(iw,ih),-2,540)':'if(gte(iw,ih),540,-2)'",
                       "-fps_mode", "passthrough", *venc,
                       "-g", "12", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                       "-movflags", "+faststart", str(dst)],
                      duration, progress, should_stop)
