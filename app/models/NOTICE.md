# Model files — licence notice

- `face_detection_yunet_2023mar.onnx` — YuNet, from opencv_zoo, unmodified. MIT licence,
  Copyright (c) 2020 Shiqi Yu. Used by the CPU path.
- `face_detection_yunet_2026may_u8.onnx` — the same weights (opencv_zoo's dynamic-size 2026may export),
  wrapped by `../tools/make_gpu_model.py` to take raw 8-bit frames. Used by the GPU path.

**Trained on WIDER FACE, whose licence (CC BY-NC-ND 4.0) does not allow commercial use.** Whether that
carries over to the trained model is unsettled, so treat these models as suitable for non-commercial use.
Checksums, full licence text and details: `../../THIRD_PARTY_NOTICES.md`.
