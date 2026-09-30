# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Build models/face_detection_yunet_2026may_u8.onnx from the dynamic YuNet export.

Why: on the GPU path the frame has to be copied to the card every time. YuNet takes
float32 CHW (25 MB for 1080p); the raw decoded frame is uint8 HWC (6 MB). This
wraps the network so it takes the raw frame and does the conversion on the card:

    image  uint8 [1, height, width, 3]  (BGR, as OpenCV decodes it)
      -> Transpose to [1, 3, height, width] -> Cast to float -> original network
    score_8/16/32 = sqrt(cls * obj)        (what OpenCV computes on the CPU; one
                                            download per anchor instead of two)
    bbox_8/16/32  unchanged

Weights are untouched. Build-time only: needs `pip install onnx`; the app itself
does not import onnx.

    .venv\\Scripts\\python.exe tools\\make_gpu_model.py
"""
from pathlib import Path

import onnx
from onnx import TensorProto, helper

MODELS = Path(__file__).resolve().parent.parent / "models"
SRC = Path(__file__).resolve().parent / "source_models" / "face_detection_yunet_2026may.onnx"
DST = MODELS / "face_detection_yunet_2026may_u8.onnx"

m = onnx.load(str(SRC))
g = m.graph
old = next(i for i in g.input if i.name == "input")
g.input.remove(old)
g.input.insert(0, helper.make_tensor_value_info("image", TensorProto.UINT8, [1, "height", "width", 3]))
pre = [
    helper.make_node("Transpose", ["image"], ["image_chw"], perm=[0, 3, 1, 2], name="u8_transpose"),
    helper.make_node("Cast", ["image_chw"], ["input"], to=TensorProto.FLOAT, name="u8_cast"),
]
for node in reversed(pre):
    g.node.insert(0, node)

cls_out = {o.name: o for o in g.output}
keep = []
for s in (8, 16, 32):
    g.node.extend([
        helper.make_node("Mul", [f"cls_{s}", f"obj_{s}"], [f"clsobj_{s}"], name=f"score_mul_{s}"),
        helper.make_node("Sqrt", [f"clsobj_{s}"], [f"score_{s}"], name=f"score_sqrt_{s}"),
    ])
    score = onnx.ValueInfoProto()
    score.CopyFrom(cls_out[f"cls_{s}"])  # same [1, anchors, 1] shape as the class output
    score.name = f"score_{s}"
    keep.append(score)
bboxes = [o for o in g.output if o.name.startswith("bbox_")]
del g.output[:]
g.output.extend(keep + bboxes)

onnx.checker.check_model(m)
onnx.save(m, str(DST))
print(f"wrote {DST.name}: inputs {[i.name for i in g.input]}, outputs {[o.name for o in g.output]}")
