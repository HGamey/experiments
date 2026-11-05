#!/usr/bin/env python3
"""Run a trained NumPy QRISP model on one RGB image."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from numpy_sr import SmallSR


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="trained NPZ weights")
    parser.add_argument("--structure", required=True, help="matching *-structure.json")
    parser.add_argument("--input", required=True, help="LR RGB image")
    parser.add_argument("--output", required=True, help="2x output PNG")
    args = parser.parse_args()
    structure = json.loads(Path(args.structure).read_text())
    model = SmallSR.load(args.model, structure)
    with Image.open(args.input) as source:
        lr_image = source.convert("RGB")
        lr_image.load()
    lr = np.asarray(lr_image, dtype=np.float32)[None] / 255
    started = time.perf_counter()
    base_image = lr_image.resize(
        (lr_image.width * 2, lr_image.height * 2), Image.Resampling.BICUBIC
    )
    base = np.asarray(base_image, dtype=np.float32)[None] / 255
    prediction = model.predict(lr, base)[0]
    elapsed = time.perf_counter() - started
    output = np.clip(prediction * 255, 0, 255).astype(np.uint8)
    Image.fromarray(output).save(args.output)
    print(json.dumps({
        "model": args.model,
        "input": args.input,
        "output": args.output,
        "input_dimensions": list(lr_image.size),
        "output_dimensions": [output.shape[1], output.shape[0]],
        "inference_s": elapsed,
        "parameters": structure["parameters"],
        "macs_per_lr_pixel": structure["macs_per_lr_pixel"],
        "device": "NumPy CPU",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
