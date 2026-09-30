# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Face-redaction detection spike (round 1: faces only).

Runs MediaPipe's full-range face detector over test clips, links detections into
tracks, fills short gaps, and writes what a reviewer needs to judge recall:

  <out>/<run>/<clip>/frames/     sampled frames with the blur regions drawn
  <out>/<run>/<clip>/sheets/     the same frames as 2x2 contact sheets
  <out>/<run>/<clip>/stats.json  speed, detection and track counts
  <out>/<run>/<clip>/boxes.json  raw detections + final per-frame boxes
  <out>/<run>/<clip>/preview_540p.mp4   (only with --preview)

Green box = the detector found the face on that frame.
Orange box = the tracker filled it in (gap between detections, or lead-in/out).

Runs are never overwritten: every run needs a new --run name. See ../CLAUDE.md.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

WINGET_FFMPEG = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
                 / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0.1-full_build/bin/ffmpeg.exe")

GREEN = (0, 200, 0)
ORANGE = (0, 150, 255)


def find_ffmpeg():
    for cand in (os.environ.get("FFMPEG"), shutil.which("ffmpeg"), str(WINGET_FFMPEG)):
        if cand and Path(cand).exists():
            return cand
    sys.exit("ffmpeg not found - set the FFMPEG environment variable")


# ---------------------------------------------------------------- geometry

def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def iou(a, b):
    i = inter(a, b)
    return i / (area(a) + area(b) - i + 1e-9)


def contain(a, b):
    """Share of the smaller box that lies inside the other one."""
    return inter(a, b) / (min(area(a), area(b)) + 1e-9)


def centre(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def shift(b, dx, dy):
    return [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]


def lerp(a, b, r):
    return [a[k] + (b[k] - a[k]) * r for k in range(4)]


def pad(b, w, h):
    """Blur region: the detector's box is brow-to-chin, so grow it to cover hair and ears."""
    bw, bh = b[2] - b[0], b[3] - b[1]
    return [max(0, b[0] - 0.20 * bw), max(0, b[1] - 0.35 * bh),
            min(w, b[2] + 0.20 * bw), min(h, b[3] + 0.15 * bh)]


# ---------------------------------------------------------------- detection

def tile_starts(length, tile, overlap):
    if length <= tile:
        return [0]
    stride = max(1, int(tile * (1 - overlap)))
    starts = list(range(0, length - tile + 1, stride))
    if starts[-1] != length - tile:
        starts.append(length - tile)
    return starts


def merge(boxes, w, h, iou_thr=0.3, contain_thr=0.6):
    """Cluster overlapping boxes and keep each cluster's union.

    Union rather than best-score: a tile can cut a face in half, and for redaction
    covering too much is cheap while covering too little is the failure.
    """
    clean = []
    for x1, y1, x2, y2, s in boxes:
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 >= 2 and y2 - y1 >= 2:
            clean.append([x1, y1, x2, y2, s])
    clean.sort(key=lambda b: -b[4])
    merged = []
    for b in clean:
        for m in merged:
            if iou(b, m) > iou_thr or contain(b, m) > contain_thr:
                m[0], m[1] = min(m[0], b[0]), min(m[1], b[1])
                m[2], m[3] = max(m[2], b[2]), max(m[3], b[3])
                m[4] = max(m[4], b[4])
                break
        else:
            merged.append(list(b))
    return merged


MODELS = Path(__file__).resolve().parent.parent / "app" / "models"


class YuNetDetector:
    """OpenCV zoo YuNet (MIT weights, trained on WIDER FACE). Runs at full frame resolution."""

    def __init__(self, conf):
        self.det = cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "",
                                             (320, 320), conf, 0.3, 5000)
        self.size = None

    def detect(self, bgr):
        h, w = bgr.shape[:2]
        if self.size != (w, h):
            self.det.setInputSize((w, h))
            self.size = (w, h)
        _, faces = self.det.detect(bgr)
        if faces is None:
            return []
        return merge([[f[0], f[1], f[0] + f[2], f[1] + f[3], float(f[14])] for f in faces], w, h)


class CenterFaceDetector:
    """CenterFace as shipped with deface (MIT weights, trained on WIDER FACE)."""

    def __init__(self, conf, scale=1.0):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.sess = ort.InferenceSession(str(Path(__file__).resolve().parent / "models" / "centerface.onnx"), opts, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name
        self.conf = conf
        self.scale = scale

    def detect(self, bgr):
        h, w = bgr.shape[:2]
        wn, hn = int(np.ceil(w * self.scale / 32) * 32), int(np.ceil(h * self.scale / 32) * 32)
        blob = cv2.dnn.blobFromImage(bgr, 1.0, (wn, hn), (0, 0, 0), swapRB=True, crop=False)
        heat, scale, offset, _ = self.sess.run(None, {self.input: blob})
        heat = heat[0, 0]
        ys, xs = np.where(heat > self.conf)
        if len(ys) == 0:
            return []
        s0 = np.exp(scale[0, 0, ys, xs]) * 4
        s1 = np.exp(scale[0, 1, ys, xs]) * 4
        cx = (xs + offset[0, 1, ys, xs] + 0.5) * 4
        cy = (ys + offset[0, 0, ys, xs] + 0.5) * 4
        sx, sy = w / wn, h / hn
        boxes = [[(cx[i] - s1[i] / 2) * sx, (cy[i] - s0[i] / 2) * sy, (cx[i] + s1[i] / 2) * sx,
                  (cy[i] + s0[i] / 2) * sy, float(heat[ys[i], xs[i]])] for i in range(len(ys))]
        return nms(boxes, w, h)


def nms(boxes, w, h, thr=0.3):
    """Plain score-ordered NMS (the heatmap gives several peaks per face)."""
    boxes = sorted(([max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3]), b[4]] for b in boxes),
                   key=lambda b: -b[4])
    keep = []
    for b in boxes:
        if b[2] - b[0] >= 2 and b[3] - b[1] >= 2 and all(iou(b, k) <= thr for k in keep):
            keep.append(b)
    return keep


def make_detector(args):
    if args.detector == "yunet":
        return YuNetDetector(args.conf)
    if args.detector == "centerface":
        return CenterFaceDetector(args.conf, args.scale)
    return FaceDetector(args.conf, args.tile)


class FaceDetector:
    """MediaPipe full-range (Apache-2.0, Google-trained). Optional overlapping tiles for small faces."""

    def __init__(self, conf, tile, overlap=0.25):
        fd = mp.solutions.face_detection
        self.full = fd.FaceDetection(model_selection=1, min_detection_confidence=conf)
        self.tiles = fd.FaceDetection(model_selection=1, min_detection_confidence=conf) if tile else None
        self.tile = tile
        self.overlap = overlap

    @staticmethod
    def _run(det, rgb, ox, oy):
        h, w = rgb.shape[:2]
        res = det.process(rgb)
        out = []
        for d in res.detections or []:
            bb = d.location_data.relative_bounding_box
            x1, y1 = ox + bb.xmin * w, oy + bb.ymin * h
            out.append([x1, y1, x1 + bb.width * w, y1 + bb.height * h, float(d.score[0])])
        return out

    def detect(self, bgr):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        boxes = self._run(self.full, rgb, 0, 0)
        if self.tile:
            for y in tile_starts(h, self.tile, self.overlap):
                for x in tile_starts(w, self.tile, self.overlap):
                    crop = np.ascontiguousarray(rgb[y:y + self.tile, x:x + self.tile])
                    boxes += self._run(self.tiles, crop, x, y)
        return merge(boxes, w, h)


# ---------------------------------------------------------------- tracking

def link_tracks(dets_per_frame, fps, max_gap_s=0.5):
    """Greedy frame-to-frame linking with constant-velocity prediction.

    A track stays open for max_gap_s without a detection, so a face the detector
    drops for a few frames keeps its identity and gets its gap filled later.
    """
    max_gap = max(1, round(max_gap_s * fps))
    tracks, active = [], []
    for f, dets in enumerate(dets_per_frame):
        pairs = []
        for i, t in enumerate(active):
            dt = f - t["last"]
            pred = shift(t["boxes"][t["last"]], t["vel"][0] * dt, t["vel"][1] * dt)
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
            t["boxes"][f] = d[:4]
            t["last"] = f
        for j, d in enumerate(dets):
            if j not in used_d:
                t = {"id": len(tracks), "boxes": {f: d[:4]}, "last": f, "vel": (0.0, 0.0)}
                tracks.append(t)
                active.append(t)
        active = [t for t in active if f - t["last"] < max_gap]
    return tracks


def edge_velocity(t, frames):
    """Mean centre velocity (px/frame) across a run of a track's detected frames."""
    if len(frames) < 2:
        return 0.0, 0.0
    (x0, y0), (x1, y1) = centre(t["boxes"][frames[0]]), centre(t["boxes"][frames[-1]])
    dt = frames[-1] - frames[0]
    return (x1 - x0) / dt, (y1 - y0) / dt


def fill_tracks(tracks, n_frames, fps, lead_s=0.25):
    """Per-frame boxes: detections, interpolated gaps, and a short lead-in/out.

    The lead-in/out covers a face for a moment before the detector first finds it
    and after it last does (turning towards camera, entering frame). It carries
    the track's motion at that end, so the box follows a walking face instead of
    staying where the face was.
    """
    lead = round(lead_s * fps)
    out = [[] for _ in range(n_frames)]
    for t in tracks:
        fs = sorted(t["boxes"])
        for a, b in zip(fs, fs[1:]):
            out[a].append((t["boxes"][a], "det", t["id"]))
            for g in range(a + 1, b):
                out[g].append((lerp(t["boxes"][a], t["boxes"][b], (g - a) / (b - a)), "fill", t["id"]))
        out[fs[-1]].append((t["boxes"][fs[-1]], "det", t["id"]))
        vx, vy = edge_velocity(t, fs[:5])
        for g in range(max(0, fs[0] - lead), fs[0]):
            out[g].append((shift(t["boxes"][fs[0]], vx * (g - fs[0]), vy * (g - fs[0])), "fill", t["id"]))
        vx, vy = edge_velocity(t, fs[-5:])
        for g in range(fs[-1] + 1, min(n_frames, fs[-1] + lead + 1)):
            out[g].append((shift(t["boxes"][fs[-1]], vx * (g - fs[-1]), vy * (g - fs[-1])), "fill", t["id"]))
    return out


# ---------------------------------------------------------------- output

def redact(img, box):
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return
    h, w = roi.shape[:2]
    small = cv2.resize(roi, (max(1, w // 10), max(1, h // 10)), interpolation=cv2.INTER_AREA)
    big = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    roi[:] = cv2.GaussianBlur(big, (0, 0), max(1.0, min(w, h) / 8))


def draw(img, boxes, label):
    h, w = img.shape[:2]
    th = max(2, w // 640)
    for box, kind, _ in boxes:
        x1, y1, x2, y2 = (int(round(v)) for v in pad(box, w, h))
        cv2.rectangle(img, (x1, y1), (x2, y2), GREEN if kind == "det" else ORANGE, th)
    scale = w / 1600
    cv2.putText(img, label, (10, int(40 * scale) + 10), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), th + 3)
    cv2.putText(img, label, (10, int(40 * scale) + 10), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), th)


def write_sheets(frames, sheet_dir, cell_w=960):
    sheet_dir.mkdir()
    cells = []
    for img in frames:
        h, w = img.shape[:2]
        cells.append(cv2.resize(img, (cell_w, round(h * cell_w / w)), interpolation=cv2.INTER_AREA))
    for n in range(0, len(cells), 4):
        group = cells[n:n + 4]
        while len(group) < 4:
            group.append(np.zeros_like(cells[0]))
        sheet = np.vstack([np.hstack(group[:2]), np.hstack(group[2:])])
        cv2.imwrite(str(sheet_dir / f"sheet_{n // 4 + 1:02d}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])


def open_preview(ffmpeg, path, w, h, fps):
    ow = round(w * 540 / h / 2) * 2
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x540",
           "-r", f"{fps:.5f}", "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-threads", "2", str(path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE), ow


# ---------------------------------------------------------------- driver

def process_clip(path, run_dir, args, ffmpeg):
    clip_dir = run_dir / path.stem
    (clip_dir / "frames").mkdir(parents=True)
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS)

    if args.reuse:
        # Tracking/fill experiments: take the raw detections from an earlier run.
        src = Path(args.out) / args.reuse / path.stem
        dets = json.loads((src / "boxes.json").read_text())["raw"]
        t_detect = json.loads((src / "stats.json").read_text())["detect_seconds"]
        if args.stride > 1:  # simulate detecting only every Nth frame
            dets = [d if f % args.stride == 0 else [] for f, d in enumerate(dets)]
            t_detect /= args.stride
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    else:
        # Pass 1: detect on every frame.
        detector = make_detector(args)
        dets, t_detect, w, h = [], 0.0, None, None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            t0 = time.perf_counter()
            dets.append(detector.detect(frame))
            t_detect += time.perf_counter() - t0
    cap.release()
    n = len(dets)

    tracks = link_tracks(dets, fps)
    per_frame = fill_tracks(tracks, n, fps)

    # Pass 2: draw sampled frames, optionally write the redacted preview.
    samples = sorted({round(i * (n - 1) / max(1, args.samples - 1)) for i in range(args.samples)})
    drawn = []
    preview, ow = (open_preview(ffmpeg, clip_dir / "preview_540p.mp4", w, h, fps) if args.preview else (None, 0))
    cap = cv2.VideoCapture(str(path))
    for f in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        if f in samples:
            img = frame.copy()
            n_det = sum(1 for b in per_frame[f] if b[1] == "det")
            draw(img, per_frame[f], f"{path.stem[:2]}  f{f}  t={f / fps:.2f}s  det={n_det}  fill={len(per_frame[f]) - n_det}")
            cv2.imwrite(str(clip_dir / "frames" / f"f{f:05d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            drawn.append(img)
        if preview:
            small = cv2.resize(frame, (ow, 540), interpolation=cv2.INTER_AREA)
            s = 540 / h
            for box, _, _ in per_frame[f]:
                redact(small, [v * s for v in pad(box, w, h)])
            preview.stdin.write(small.tobytes())
    cap.release()
    if preview:
        preview.stdin.close()
        preview.wait()
    write_sheets(drawn, clip_dir / "sheets")

    det_counts = [len(d) for d in dets]
    stats = {
        "clip": path.name, "width": w, "height": h, "fps": round(fps, 3), "frames": n,
        "detections_reused_from": args.reuse,
        "detect_seconds": round(t_detect, 2), "detect_fps": round(n / t_detect, 2) if t_detect else None,
        "realtime_factor": round((n / fps) / t_detect, 3) if t_detect else None,
        "detections": sum(det_counts), "mean_det_per_frame": round(sum(det_counts) / max(1, n), 2),
        "frames_with_det": sum(1 for c in det_counts if c),
        "tracks": len(tracks), "tracks_single_det": sum(1 for t in tracks if len(t["boxes"]) == 1),
        "fill_boxes": sum(1 for fr in per_frame for b in fr if b[1] == "fill"),
        "sampled_frames": samples,
    }
    (clip_dir / "stats.json").write_text(json.dumps(stats, indent=2))
    (clip_dir / "boxes.json").write_text(json.dumps({
        "raw": [[[round(float(v), 1) for v in b[:4]] + [round(float(b[4]), 3)] for b in d] for d in dets],
        "final": [[[round(float(v), 1) for v in b[0]] + [b[1], b[2]] for b in fr] for fr in per_frame],
    }))
    print(f"{path.name}: {n} frames, detect {stats['detect_fps']} fps "
          f"({stats['realtime_factor']}x realtime), {stats['tracks']} tracks", flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--run", required=True, help="run name; must not exist yet")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    ap.add_argument("--tile", type=int, default=0, help="also detect on overlapping tiles of this size (px); 0 = whole frame only")
    ap.add_argument("--detector", choices=["mediapipe", "yunet", "centerface"], default="mediapipe")
    ap.add_argument("--scale", type=float, default=1.0, help="centerface: resize factor before detection")
    ap.add_argument("--conf", type=float, default=0.3, help="detector confidence threshold")
    ap.add_argument("--samples", type=int, default=40, help="frames to draw per clip")
    ap.add_argument("--preview", action="store_true", help="also write a 540p redacted preview")
    ap.add_argument("--reuse", help="skip detection: reuse raw detections from this earlier run (tracking experiments)")
    ap.add_argument("--stride", type=int, default=1, help="with --reuse: keep detections on every Nth frame only")
    args = ap.parse_args()

    run_dir = Path(args.out) / args.run
    if run_dir.exists():
        sys.exit(f"{run_dir} already exists - runs are never overwritten, pick a new --run")
    run_dir.mkdir(parents=True)
    (run_dir / "args.json").write_text(json.dumps(vars(args), indent=2))
    ffmpeg = find_ffmpeg() if args.preview else None

    rows = [process_clip(Path(c), run_dir, args, ffmpeg) for c in args.clips]

    lines = ["| clip | res | frames | detect fps | x realtime | det/frame | tracks | 1-det tracks | fill boxes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in rows:
        lines.append(f"| {s['clip']} | {s['width']}x{s['height']} | {s['frames']} | {s['detect_fps']} | "
                     f"{s['realtime_factor']} | {s['mean_det_per_frame']} | {s['tracks']} | "
                     f"{s['tracks_single_det']} | {s['fill_boxes']} |")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
