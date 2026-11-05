#!/usr/bin/env python3
"""Run 6-channel QRISP SR using current/previous LR and current EXR motion."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from exr import read_exr
from motion import align_previous
from numpy_sr import SmallSR


def read_rgb(path):
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.load()
    return image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--structure", required=True)
    parser.add_argument("--current", required=True, help="current LR RGB PNG")
    parser.add_argument("--previous", required=True, help="immediately previous LR RGB PNG")
    parser.add_argument("--motion", required=True, help="current MotionVectorsMipBiasMinus1 EXR")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    structure = json.loads(Path(args.structure).read_text())
    if structure.get("input_channels") != 6:
        raise ValueError("motion inference requires a six-channel model")
    model = SmallSR.load(args.model, structure)
    current_image = read_rgb(args.current)
    previous_image = read_rgb(args.previous)
    if previous_image.size != current_image.size:
        raise ValueError("current and previous LR dimensions differ")

    started = time.perf_counter()
    previous = np.asarray(previous_image, dtype=np.float32) / 255
    vectors, names, _ = read_exr(args.motion)
    if vectors.shape[:2] != (current_image.height, current_image.width):
        raise ValueError("motion and color dimensions differ")
    motion_rg = vectors[:, :, [names.index("R"), names.index("G")]]
    history, valid = align_previous(previous, motion_rg)
    history_s = time.perf_counter() - started

    current = np.asarray(current_image, dtype=np.float32)[None] / 255
    base_started = time.perf_counter()
    base_image = current_image.resize((current_image.width * 2, current_image.height * 2), Image.Resampling.BICUBIC)
    base = np.asarray(base_image, dtype=np.float32)[None] / 255
    bicubic_s = time.perf_counter() - base_started
    model_input = np.concatenate((current, history[None]), axis=3)
    model_started = time.perf_counter()
    prediction = model.predict(model_input, base)[0]
    model_s = time.perf_counter() - model_started
    output = np.clip(prediction * 255, 0, 255).astype(np.uint8)
    Image.fromarray(output).save(args.output)
    print(json.dumps({
        "output": args.output,
        "input_dimensions": list(current_image.size),
        "output_dimensions": [output.shape[1], output.shape[0]],
        "history_decode_and_warp_s": history_s,
        "bicubic_s": bicubic_s,
        "model_inference_s": model_s,
        "pipeline_s_excluding_file_io": history_s + bicubic_s + model_s,
        "history_valid_fraction": float(np.mean(valid)),
        "parameters": structure["parameters"],
        "macs_per_lr_pixel": structure["macs_per_lr_pixel"],
        "device": "NumPy CPU",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
