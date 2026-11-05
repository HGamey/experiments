# Doom 3 small 2× super-resolution

Trains the QRISP 5,276-parameter residual network on Doom 3 phone captures using NumPy on macmini-3. The capture task supplies native HR frames; this script creates LR with Pillow bicubic 0.5×. It does not access a phone.

## Train and export

Input can be an HR-only directory, or a JSON/JSONL manifest whose items contain `hr` (also accepts `path`/`file`) and optional `scene`, `frame`, `group`, `device`, and `split`. Image paths in a manifest are relative to that manifest. Exact duplicate files are removed first. By default, a frame is also skipped when its 64×64 luma thumbnail differs from the last kept frame by less than **0.5 8-bit levels mean absolute difference**; set `--min-frame-mad 0` to disable this near-duplicate filter. Counts and rejected paths are recorded in `data-manifest.json`. With no explicit split, the script prefers a deterministic group-level holdout; a frame-level fallback is labelled because near-duplicates may remain.

```sh
python3 doom3_sr/train_doom.py \
  --data /path/to/doom-hr \
  --test-data /path/to/independent-doom-hr \
  --pretrained /path/to/base.npz \
  --pretrained-structure /path/to/base-structure.json \
  --steps 10000
```

`--test-data` is recommended. Without it, 10% of the supplied frames (at least two, at most 24) are held out. Training compares random initialization at learning rate 0.001 with QRISP initialization at 0.0002. Both use the same batches, 32×32 LR patches, batch 8, RGB [0,1], L1, and Adam. The selected model has the highest held-out mean RGB PSNR; selection is not based on training loss.

For a platform run, the same local paths are uploaded by `pf-node` and rewritten to task inputs:

```sh
./bin/pf-node --experiment <COMMIT> \
  --research 89bf53ee-f502-4d60-98af-017bc96a1f1d \
  --label 'Doom HR首批：scratch与QRISP微调对照' \
  --device-access none --max-runtime-seconds 7200 --detach \
  experiment doom3_sr/train_doom.py \
  --data /absolute/path/to/doom-hr \
  --pretrained /absolute/path/to/base.npz \
  --pretrained-structure /absolute/path/to/base-structure.json \
  --steps 10000
```

Outputs include `summary.json`, per-frame metrics, data/source SHA-256, training histories, NPZ research weights, comparison sheets, and stable `selected.npz` / `selected-structure.json` names. `selection.json` records the choice. Raw captures are not copied into the public repository.

## GLSL weight interface

Every training run also exports `doom_small_sr.inc` and `doom_small_sr.manifest.json`. To export an existing selected model:

```sh
python3 doom3_sr/export_glsl.py \
  --model selected.npz --structure selected-structure.json \
  --output-prefix doom_small_sr
```

The include is weights only, for GLSL ES 3.x:

```glsl
#include "doom_small_sr.inc" // Adapt to the engine's include mechanism.
```

- Arrays: `W1[1200]`, `B1[16]`, `W2[2304]`, `B2[16]`, `W3[1728]`, `B3[12]`; 5,276 float32 values = 21,104 bytes.
- Logical activations are NHWC. Weights are flattened in C-order HWIO: `(((ky*K + kx)*Cin + ci)*Cout + co)`. The include provides `W1_INDEX`, `W2_INDEX`, and `W3_INDEX` macros.
- `conv1`: 5×5, RGB→16, same zero padding, ReLU. `conv2`: 3×3, 16→16, same zero padding, ReLU. `conv3`: 3×3, 16→12, same zero padding, no ReLU.
- Pixel shuffle: `conv3_channel = (sub_y*2 + sub_x)*3 + rgb`; `HR_xy = LR_xy*2 + (sub_x,sub_y)`.
- Add the caller-provided 2× bicubic RGB residual. The include does **not** implement bicubic sampling or final display conversion. Clamp the final display RGB as appropriate.
- The manifest records each shape and length, exact source-NPZ and generated-include SHA-256, and total float32 bytes. Nine-significant-digit decimal literals round-trip every float32 value.

For real-time integration, avoid blindly evaluating all three convolutions independently at every HR fragment: that repeats conv1/conv2 for all four subpixels. Compute/cache the 16-channel LR feature map once per LR pixel, then evaluate only the three conv3 channels needed by each HR fragment, or use equivalent multi-pass feature textures. The full network is 5,232 MAC/LR pixel (2.71 GMAC at 960×540), excluding bicubic, ReLU, pixel shuffle, and display conversion.

## Device screenshot and FPS comparison

Prepare a JSON manifest with an `items` list. Each item contains relative `native`, `bicubic`, and `sr` image paths plus an optional `label` and `crop: [x,y,width,height]`. All three images must show the same game state and have identical dimensions. The tool writes per-frame and mean RGB PSNR/SSIM, SR-minus-bicubic deltas, a native-resolution crop contact sheet, and optional FPS deltas supplied by the device owner:

```sh
python3 doom3_sr/compare_device.py \
  --manifest /path/to/device-comparison.json --out /path/to/device-results \
  --fps-native 60.0 --fps-bicubic 45.0 --fps-sr 40.0 \
  --full-thumbnails
```

FPS values above are syntax examples only, not measurements. Omit all FPS arguments until real matched measurements are available. `summary.json` preserves which values were supplied and never substitutes macmini timing.

## Validation and metric boundary

```sh
python3 -m unittest doom3_sr.test_export_glsl doom3_sr.test_train_doom doom3_sr.test_compare_device -v
```

The tests use synthetic temporary data only. They check every emitted float and array layout, hashes, invalid weights, exact duplicate removal, group splitting, and that interior training patches exactly match full-frame Pillow bicubic resize. A 20-step end-to-end smoke also checks training, selection, and export; it is not quality evidence.

PSNR is full-frame RGB with 8-bit peak 255. SSIM is per-channel 11×11 Gaussian (σ=1.5), K1=0.01 and K2=0.03. Because LR is downsampled HR, these are simulated-LR offline results, not native-rendered LR pairs. macmini NumPy inference time is not phone FPS. Device FPS and visual on/off conclusions require the separate engine integration and matched phone captures.
