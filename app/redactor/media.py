# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""ffmpeg / ffprobe discovery and the few media operations the app needs."""
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

# Keep child processes from flashing console windows when the app runs windowless.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _candidates(name):
    exe = f"{name}.exe" if sys.platform == "win32" else name
    if os.environ.get(name.upper()):
        yield os.environ[name.upper()]
    yield str(APP_DIR / "bin" / exe)
    yield shutil.which(name)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # winget installs Gyan.FFmpeg here but its PATH shim does not always resolve.
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", exe)
        yield from sorted(glob.glob(pattern, recursive=True), reverse=True)


def find_tool(name):
    for cand in _candidates(name):
        if cand and Path(cand).is_file():
            return cand
    return None


def tools():
    """Both tools or a clear error; the app cannot make previews or exports without them."""
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg was not found. Install it (for example `winget install Gyan.FFmpeg`) "
                           "or put ffmpeg.exe and ffprobe.exe in the app's bin folder.")
    return ffmpeg, ffprobe


def _rate(text):
    num, _, den = (text or "0/1").partition("/")
    try:
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


def probe(path):
    _, ffprobe = tools()
    out = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"Could not read this file as video: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError("This file has no video stream.")
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(side["rotation"])
    w, h = int(video["width"]), int(video["height"])
    if rotation % 180:
        w, h = h, w  # decoders auto-rotate, so work in display orientation
    return {
        "width": w, "height": h,
        "fps": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "rate": video.get("avg_frame_rate") if _rate(video.get("avg_frame_rate")) else video.get("r_frame_rate"),
        "duration": float(info.get("format", {}).get("duration") or video.get("duration") or 0),
        "nb_frames": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        "video_codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "size_bytes": int(info.get("format", {}).get("size") or 0),
    }


def frame_times(path):
    """Presentation time of every video frame, from packet timestamps (no decoding, so fast)."""
    _, ffprobe = tools()
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, creationflags=NO_WINDOW)
    times = sorted(float(t) for t in out.stdout.split() if t.strip() and t.strip() != "N/A")
    return [round(t - times[0], 6) for t in times] if times else []


def run_ffmpeg(args, duration, progress=None, should_stop=None, stdin=None):
    """Run ffmpeg, reporting 0..1 progress from its -progress output."""
    ffmpeg, _ = tools()
    cmd = [ffmpeg, "-v", "error", "-y", "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=stdin,
                            text=True, creationflags=NO_WINDOW)
    for line in proc.stdout:
        if should_stop and should_stop():
            proc.kill()
            break
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and value.isdigit() and duration and progress:
            progress(min(1.0, int(value) / 1e6 / duration))
    proc.wait()
    err = proc.stderr.read()
    if proc.returncode not in (0, None) and not (should_stop and should_stop()):
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-500:]}")


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
_encoder_cache = None


def pick_encoder():
    """First hardware encoder that really works here (listed and able to encode a test clip), else x264.

    A listed encoder can still fail (no such card, old driver), so each is tried on a
    tiny generated clip once per run of the app.
    """
    global _encoder_cache
    if _encoder_cache is None:
        ffmpeg, _ = tools()
        listed = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                creationflags=NO_WINDOW).stdout
        _encoder_cache = CPU_ENCODER
        for name, vendor, args in HW_ENCODERS:
            if f" {name} " not in listed:
                continue
            test = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=0.5",
                                   *args, "-pix_fmt", "yuv420p", "-f", "null", "-"],
                                  capture_output=True, text=True, creationflags=NO_WINDOW)
            if test.returncode == 0:
                _encoder_cache = (name, vendor, args)
                break
    return _encoder_cache


def make_proxy(src, dst, duration, progress=None, should_stop=None):
    """Small H.264 copy for review: every source frame kept, short GOP for quick scrubbing.

    Made while detection runs, so it is kept light on the CPU: two decode threads, and the
    graphics card's encoder when there is one (quality matters little for a 540p preview).
    """
    name, _, _ = pick_encoder()
    venc = (["-c:v", name, "-preset", "p1", "-rc", "vbr", "-cq", "28", "-b:v", "0"] if name == "h264_nvenc" else
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-threads", "2"])
    run_ffmpeg(["-threads", "2", "-i", str(src),
                "-vf", "scale='if(gte(iw,ih),-2,540)':'if(gte(iw,ih),540,-2)'",
                "-fps_mode", "passthrough", *venc,
                "-g", "12", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "+faststart", str(dst)],
               duration, progress, should_stop)
