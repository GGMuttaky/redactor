# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Face detection with YuNet, on the graphics card when possible.

YuNet (opencv_zoo, MIT-licensed weights) runs at full frame resolution, so it
finds small, distant faces that close-range detectors miss. It was the only
detector in the spike that held up on crowds; see spike/RESULTS_r2.md.

Two backends, same model weights:
  - GPU: ONNX Runtime + DirectML (any DirectX 12 card: NVIDIA, AMD, Intel), using
    the dynamic-shape export `face_detection_yunet_2026may.onnx`. OpenCV's own
    FaceDetectorYN cannot use the GPU in the pip build, so decoding is done here,
    mirroring OpenCV's face_detect.cpp. spike/RESULTS_r3.md shows the two agree.
  - CPU: OpenCV's FaceDetectorYN with `face_detection_yunet_2023mar.onnx`.

Licence note: YuNet was trained on WIDER FACE (CC BY-NC-ND 4.0, non-commercial). Treat the
model as non-commercial until replaced; see THIRD_PARTY_NOTICES.md and docs/LICENSING.md.
"""
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(__file__).resolve().parent.parent / "models"
MODEL_CPU = MODELS / "face_detection_yunet_2023mar.onnx"
# Dynamic-shape export wrapped to take the raw uint8 frame (tools/make_gpu_model.py): a quarter of
# the bytes to copy to the card per frame, which is what limits speed when the link to the card is slow.
MODEL_DYN = MODELS / "face_detection_yunet_2026may_u8.onnx"
STRIDES = (8, 16, 32)


def _scaled(bgr, max_side):
    """Frames above max_side are shrunk first: 4K is slow and gains little, faces are big there."""
    h, w = bgr.shape[:2]
    scale = min(1.0, max_side / max(w, h))
    if scale == 1.0:
        return bgr, 1.0
    return cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA), scale


def _to_source(faces, scale, w, h):
    out = []
    for x, y, bw, bh, score in faces:
        x1, y1 = max(0.0, x / scale), max(0.0, y / scale)
        x2, y2 = min(float(w), (x + bw) / scale), min(float(h), (y + bh) / scale)
        if x2 - x1 >= 3 and y2 - y1 >= 3:
            out.append([x1, y1, x2, y2, float(score)])
    return out


class CpuDetector:
    """OpenCV FaceDetectorYN on the CPU."""

    name = "CPU"

    def __init__(self, conf=0.5, max_side=1920):
        if not MODEL_CPU.is_file():
            raise RuntimeError(f"Face model missing: {MODEL_CPU}")
        self.det = cv2.FaceDetectorYN.create(str(MODEL_CPU), "", (320, 320), conf, 0.3, 5000)
        self.max_side = max_side
        self.size = None

    def prepare(self, bgr):
        return _scaled(bgr, self.max_side) + (bgr.shape[1], bgr.shape[0])

    def detect_prepared(self, prepared):
        img, scale, w, h = prepared
        ih, iw = img.shape[:2]
        if self.size != (iw, ih):
            self.det.setInputSize((iw, ih))
            self.size = (iw, ih)
        _, faces = self.det.detect(img)
        rows = [] if faces is None else [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])) for f in faces]
        return _to_source(rows, scale, w, h)

    def detect(self, bgr):
        """[[x1, y1, x2, y2, score], ...] in the frame's own pixel coordinates."""
        return self.detect_prepared(self.prepare(bgr))


class GpuDetector:
    """YuNet through ONNX Runtime + DirectML, decoded the way OpenCV decodes it."""

    def __init__(self, conf=0.5, max_side=1920, nms=0.3, top_k=5000, provider="DmlExecutionProvider", frame_size=None):
        """frame_size=(w, h) fixes the session to one video's frame size.

        DirectML optimises the network for the first input size it runs; a different size
        later runs about 3.5x slower (measured: 11 ms vs 39 ms per 720p frame). So each
        video gets its own session, built for that video's padded size.
        """
        import onnxruntime as ort
        if provider not in ort.get_available_providers():
            raise RuntimeError(f"{provider} is not available")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        if provider == "DmlExecutionProvider":
            opts.enable_mem_pattern = False  # required by DirectML
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.max_side = max_side
        self.pad_size = None
        if frame_size:
            w, h = frame_size
            s = min(1.0, max_side / max(w, h))
            iw, ih = round(w * s), round(h * s)
            self.pad_size = ((iw - 1) // 32 * 32 + 32, (ih - 1) // 32 * 32 + 32)
            opts.add_free_dimension_override_by_name("width", self.pad_size[0])
            opts.add_free_dimension_override_by_name("height", self.pad_size[1])
        self.sess = ort.InferenceSession(str(MODEL_DYN), opts, providers=[provider])
        if self.sess.get_providers()[0] != provider:
            raise RuntimeError(f"{provider} could not be started")
        self.name = "GPU" if provider == "DmlExecutionProvider" else "CPU (ONNX Runtime)"
        # score = sqrt(cls * obj) is computed on the card; landmarks are never copied back.
        self.outputs = [f"{k}_{s}" for k in ("score", "bbox") for s in STRIDES]
        self.conf, self.nms, self.top_k = conf, nms, top_k

    def warm_up(self):
        """One run at the session's own size: compiles the graph and fails fast on a broken driver."""
        pw, ph = self.pad_size or (64, 64)
        self.sess.run(self.outputs, {"image": np.zeros((1, ph, pw, 3), np.uint8)})

    def prepare(self, bgr):
        """CPU-side work (resize, pad to x32): cheap, and safe to run on a reader thread."""
        img, scale = _scaled(bgr, self.max_side)
        ih, iw = img.shape[:2]
        pw, ph = (iw - 1) // 32 * 32 + 32, (ih - 1) // 32 * 32 + 32  # OpenCV pads right/bottom to x32
        if (pw, ph) != (iw, ih):
            img = cv2.copyMakeBorder(img, 0, ph - ih, 0, pw - iw, cv2.BORDER_CONSTANT, value=0)
        return img[None], scale, bgr.shape[1], bgr.shape[0]  # uint8 BGR [1, H, W, 3]

    def detect_prepared(self, prepared):
        image, scale, w, h = prepared
        ph, pw = image.shape[1:3]
        outs = self.sess.run(self.outputs, {"image": image})
        boxes, scores = [], []
        for i, s in enumerate(STRIDES):
            cols = pw // s
            score, bb = outs[i].reshape(-1), outs[3 + i].reshape(-1, 4)
            idx = np.flatnonzero(score >= self.conf)
            if not len(idx):
                continue
            r, c = idx // cols, idx % cols
            cx, cy = (c + bb[idx, 0]) * s, (r + bb[idx, 1]) * s
            bw, bh = np.exp(bb[idx, 2]) * s, np.exp(bb[idx, 3]) * s
            boxes.append(np.stack([cx - bw / 2, cy - bh / 2, bw, bh], 1))
            scores.append(score[idx])
        if not boxes:
            return []
        boxes, scores = np.concatenate(boxes), np.concatenate(scores)
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), self.conf, self.nms, 1.0, self.top_k)
        keep = np.array(keep).reshape(-1)
        return _to_source([(*boxes[k], scores[k]) for k in keep], scale, w, h)

    def detect(self, bgr):
        return self.detect_prepared(self.prepare(bgr))


_gpu_ok = None


def gpu_available():
    """Whether a DirectML session starts and runs here (checked once per app run)."""
    global _gpu_ok
    if _gpu_ok is None:
        try:
            GpuDetector(0.5, frame_size=(64, 64)).warm_up()
            _gpu_ok = True
        except Exception:
            _gpu_ok = False
    return _gpu_ok


def make_detector(conf=0.5, prefer_gpu=True, frame_size=None):
    """GPU if it starts and passes a warm-up run, otherwise CPU. Returns (detector, note).

    Pass the video's frame_size (w, h): the GPU session is built for exactly that size.
    """
    if prefer_gpu and MODEL_DYN.is_file():
        try:
            det = GpuDetector(conf, frame_size=frame_size)
            if frame_size:
                det.warm_up()
            return det, None
        except Exception as e:  # any GPU problem falls back to the CPU path, never stops the job
            return CpuDetector(conf), f"Graphics card not used ({str(e)[:160]}); using the CPU."
    return CpuDetector(conf), None


# Kept for callers that predate make_detector().
FaceDetector = CpuDetector
