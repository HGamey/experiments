# QRISP small super-resolution

Trains a 2× RGB super-resolution model with NumPy on exact QRISP rendered pairs. No phone, GPU, TensorFlow, PyTorch, or global package installation is used.

## Data

Official source: <https://www.qualcomm.com/developer/software/qualcomm-rasterized-images-dataset/downloads>.

The formal run selects 16 pairs from each of three training scenes (`SunTempleBush`, `ScifiFacility`, `ScifiBaseNightStartStop`) and six pairs from the official held-out `SeaPort` scene. LR is native-rendered `540p/MipBiasMinus1`; HR is the same segment/frame under `1080p/Enhanced`. It is **not** HR downsampling.

`remote_zip.py` reads only ZIP metadata and selected entries with HTTP Range requests. Whole archives (6–9 GiB each) are never written. Decoded arrays stay in the task's macmini memory; source PNGs are not redistributed in model artifacts.

## Model and ablations

All variants use NHWC RGB input, `Conv2D same → ReLU → Conv2D same → ReLU → Conv2D same → depth_to_space(2)`.

| Variant | Feature channels | Bicubic residual | Parameters | MAC/LR pixel |
|---|---:|---|---:|---:|
| `base` | 16 | yes | 5,276 | 5,232 |
| `no_residual` | 16 | no | 5,276 | 5,232 |
| `half_channels` | 8 | yes | 2,068 | 2,040 |

At 960×540 input, the base convolutions execute 2,712,268,800 MACs per frame. The residual variant adds a Pillow bicubic 2× image to the network output. Weights are float32 NPZ files with per-layer JSON descriptions.

## Run

On macmini-3 through the platform (replace the commit with the intended immutable revision):

```sh
./bin/pf-node --experiment <COMMIT> \
  --research 253b68b9-f645-49af-9d6c-7288cd307c66 \
  --direction qrisp-single-frame-small-sr \
  --label 'QRISP 3场景训练 + SeaPort测试 + 2项消融' \
  --device-access none --max-runtime-seconds 21600 --detach \
  experiment qrisp_sr/train_qrisp.py --steps 10000
```

Local mechanism check with an existing eight-pair pilot:

```sh
python3 qrisp_sr/train_qrisp.py \
  --local-pilot /path/to/pilot --steps 30 --variants base
```

Training uses RGB values in [0,1], 32×32 random LR patches, batch 8, L1 loss, Adam (learning rate 0.001), and seed 20261106. All variants use identical steps and data.

## Inference

```sh
python3 qrisp_sr/infer_qrisp.py \
  --model base.npz --structure base-structure.json \
  --input lr.png --output prediction.png
```

The loader validates every weight key and shape, performs bicubic 2× plus the optional learned residual, and writes a 2× RGB PNG. The reported time is NumPy CPU time, not a phone measurement.

## Outputs and metrics

- `base.npz`, `no_residual.npz`, `half_channels.npz`: trained weights.
- `*-structure.json`: operators, channels, parameter counts, MACs, file bytes, and training time.
- `summary.json`: per-frame and mean PSNR/SSIM, bicubic deltas, and macmini CPU inference time.
- `data-manifest.json`: source paths, SHA-256, scene split, transfer and memory totals; no raw archive.
- `test-full-contact.jpg`, `test-crop-contact.jpg`: same-frame visual comparisons.

PSNR is full-frame RGB with 8-bit peak 255. SSIM is calculated per RGB channel with an 11×11 Gaussian window (σ=1.5), K1=0.01 and K2=0.03, then averaged over channels and valid pixels. These are QRISP image results, not phone latency or display-FPS claims.
