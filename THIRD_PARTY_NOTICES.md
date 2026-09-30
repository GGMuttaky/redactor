# Third-party notices

Redactor's own code is licensed under the GNU General Public License v3.0 or later (see `LICENSE`).
The components below keep their own licences.

## Bundled in this repository

### YuNet face detection model — MIT, with a training-data restriction

Source: [opencv_zoo/models/face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
("All files in this directory are licensed under MIT License").

| File | Origin | Bytes | SHA-256 |
|---|---|---|---|
| `app/models/face_detection_yunet_2023mar.onnx` | opencv_zoo, unmodified | 232,589 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `app/tools/source_models/face_detection_yunet_2026may.onnx` | opencv_zoo, unmodified (dynamic input size) | 229,738 | `ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0` |
| `app/models/face_detection_yunet_2026may_u8.onnx` | generated from the 2026may file by `app/tools/make_gpu_model.py` | 229,903 | `fa83e393325d95c35ea1b82a9ecaf7d3e161c1236da69776b1d7f042e2d750d8` |

All three hold the same trained weights: the 112 weight tensors (53,121 values) were compared and are
identical. The generated file only adds nodes that convert the 8-bit input and combine the scores.

Licence of the model files, verbatim from [opencv_zoo](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE):

```
Copyright (c) 2020 Shiqi Yu <shiqi.yu@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial
portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

> **Commercial use: read this.** YuNet was trained on the
> [WIDER FACE](http://shuoyang1213.me/WIDERFACE/) dataset, which is published by CUHK under
> [CC BY-NC-ND 4.0](https://huggingface.co/datasets/CUHK-CSE/wider_face) — non-commercial, no
> derivatives — and described as for academic research. Whether a model trained on it counts as a
> derivative is legally unsettled. The model files themselves are MIT-licensed by their author, and
> OpenCV distributes them openly — but **if you use Redactor commercially, you take on that question.**
> The Redactor project treats the bundled model as **suitable for non-commercial use** until it is
> replaced by a model trained on commercially usable data (see `docs/LICENSING.md`).

## Installed at setup (not in this repository)

| Package | Licence | Used for |
|---|---|---|
| [OpenCV](https://opencv.org/) (`opencv-python-headless`) | Apache-2.0 | decoding video, CPU face detection, image processing |
| [NumPy](https://numpy.org/) | BSD-3-Clause | arrays |
| [ONNX Runtime](https://onnxruntime.ai/) (`onnxruntime-directml`) | MIT | face detection on the graphics card |
| [ONNX](https://onnx.ai/) (`onnx`) | Apache-2.0 | only `app/tools/make_gpu_model.py` (development), not the app |
| [pytest](https://pytest.org/), [Ruff](https://docs.astral.sh/ruff/) | MIT | development only: tests and lint |

ONNX Runtime also installs its own small dependencies (for example `protobuf`, BSD-3-Clause; `flatbuffers`,
Apache-2.0; `sympy`, BSD-3-Clause), each under its own licence.

## Called as a separate program (not bundled)

[FFmpeg](https://ffmpeg.org/) reads and writes the video. Users install it themselves; Redactor runs
`ffmpeg.exe` / `ffprobe.exe` as external processes and does not link to or redistribute it. FFmpeg's
licence (LGPL or GPL depending on the build) applies to FFmpeg itself.

## Demo media

The demo GIF in `docs/` is made from stock footage by **Cheng** on [Pexels](https://www.pexels.com/video/a-group-of-people-walking-through-a-crowded-city-street-18662635/),
and the review-screen screenshot from footage by **RDNE Stock project** on
[Pexels](https://www.pexels.com/video/people-working-on-office-using-computer-7581202/), both
used under the [Pexels licence](https://www.pexels.com/license/) (free to use and modify). The
original clips are not redistributed here; `testclips/SOURCES.md` lists them for anyone reproducing
the tests.
