#!/usr/bin/env python3
"""Read-only probe of a frame directory: count images and report near-duplicate MAD stats."""
import argparse
import json
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_doom import IMAGE_SUFFIXES, EXCLUDED_PARTS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--max-thumbnails", type=int, default=80)
    args = parser.parse_args()
    root = Path(args.data)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part.lower() in EXCLUDED_PARTS for part in relative.parts[:-1]):
            continue
        files.append(path)
    thumbnails = []
    for path in files[: args.max_thumbnails]:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("L").resize((64, 64), Image.Resampling.BILINEAR)
        thumbnails.append(np.asarray(image, dtype=np.float32))
    mads = [float(np.mean(np.abs(thumbnails[i] - thumbnails[i - 1]))) for i in range(1, len(thumbnails))]
    sizes = {}
    sample = files[0] if files else None
    if sample is not None:
        with Image.open(sample) as source:
            sizes["first_image"] = list(source.size)
    report = {
        "data": str(root),
        "readable": True,
        "image_count": len(files),
        "first_image_size": sizes.get("first_image"),
        "thumbnails_computed": len(thumbnails),
        "adjacent_mad_8bit": None if not mads else {
            "min": min(mads), "mean": float(np.mean(mads)),
            "p50": float(np.percentile(mads, 50)), "p90": float(np.percentile(mads, 90)),
            "max": max(mads),
            "kept_at_0.5": int(sum(value >= 0.5 for value in [np.inf] + mads)),
        },
        "out_dir": os.environ.get("PF_JOB_OUT", "out"),
    }
    out = Path(report["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
