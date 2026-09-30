# Model files — licence notice

- `face_detection_yunet_2023mar.onnx` — YuNet, from opencv_zoo, unmodified. MIT licence,
  Copyright (c) 2020 Shiqi Yu. Used by the CPU path.
- `face_detection_yunet_2026may_u8.onnx` — the same weights, wrapped by `../tools/make_gpu_model.py`
  to take raw 8-bit frames. Used by the GPU path.

**Trained on WIDER FACE, which is licensed for non-commercial research only.** Treat these models as
suitable for non-commercial use. Full text and details: `../../THIRD_PARTY_NOTICES.md`.
