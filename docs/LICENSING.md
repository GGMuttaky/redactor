# Licensing: where Redactor stands, and how to make it commercially clean

## Today (v0.2.0)

| Part | Licence | Commercial use |
|---|---|---|
| Redactor's code | GPL-3.0-or-later | Yes, under GPL terms |
| OpenCV, NumPy, ONNX Runtime | Apache-2.0 / BSD-3 / MIT | Yes |
| FFmpeg (external, not bundled) | LGPL / GPL | Yes, as a separate program |
| **YuNet face model** | **MIT file, trained on WIDER FACE (CC BY-NC-ND 4.0)** | **Unsettled → treat as no** |

So Redactor is **free for non-commercial use** as shipped. The only thing standing between it and a
product that can be sold, or used by companies without doubt, is the face model.

## Routes to a commercially clean model

### 1. Retrain the same detector on commercially usable data (recommended)

- **Training code:** [libfacedetection.train](https://github.com/ShiqiYu/libfacedetection.train),
  BSD-3-Clause, the official YuNet trainer. Out of the box it trains on WIDER FACE, so the data
  loader has to be pointed at another dataset.
- **Data:** [Open Images V7](https://storage.googleapis.com/openimages/web/factsfigures_v7.html),
  class "Human face". Annotations: CC BY 4.0 (Google). Images: "listed as having a CC BY 2.0
  license"; Google gives no warranty, so filter to images whose recorded licence is CC BY and keep
  the attribution list. CC BY allows commercial use with attribution.
- **Known risk:** Open Images faces are mostly larger than WIDER FACE's crowd faces, so small-face
  recall may drop. Mitigate with downscale/mosaic augmentation.
- **Gate before switching:** the new model must match the current one on this repo's test clips
  (`spike/` method: recall on the hand-counted frames, `spike/gpu_bench.py`-style agreement, and the
  crowd clip). If it doesn't, it doesn't ship.
- **Cost:** tens of GB of downloads and a few days of GPU training on a mid-range card.
- **Attribution to add** once used: "Face model trained on Open Images V7 (annotations CC BY 4.0 by
  Google LLC; images CC BY 2.0 by their respective authors)".

### 2. Ask for permission

Write to the WIDER FACE authors (contact details on the
[dataset page](http://shuoyang1213.me/WIDERFACE/)) asking for written permission to use a model
trained on WIDER FACE in a commercial product. One email, but may go unanswered or be refused. A
yes should be kept on file and referenced here.

### 3. Legal advice

A lawyer's opinion on whether trained weights are a derivative of the dataset in the markets you sell
to. This settles the question for you; it does not change the licences.

## Keeping the option to sell

Redactor's author holds the copyright to its code, so besides the GPL release they can also sell the
code under other terms (dual licensing). That only stays possible if outside contributions are made
under an agreement that allows it — see `CONTRIBUTING.md`.
