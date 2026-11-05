#!/usr/bin/env python3
"""Compare matched native, bicubic, and small-SR device screenshots."""
import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qrisp_sr"))
from train_qrisp import metrics as qrisp_metrics  # noqa: E402


METRIC_KEYS = ("rgb_psnr_db", "rgb_ssim_11x11")
SOURCES = ("native", "bicubic", "sr")


def _resolve_image(root, value, index, source):
    if not isinstance(value, str) or not value:
        raise ValueError(f"item {index}: {source} must be a nonempty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"item {index}: {source} path must be relative")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"item {index}: {source} path escapes manifest directory") from exc
    return resolved


def _read_rgb(path):
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.load()
    if image.width <= 0 or image.height <= 0:
        raise ValueError(f"image has zero dimensions: {path}")
    return image


def _crop_box(value, size, index):
    width, height = size
    if value is None:
        crop_width, crop_height = min(256, width), min(256, height)
        return [(width - crop_width) // 2, (height - crop_height) // 2,
                crop_width, crop_height]
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"item {index}: crop must be [x, y, width, height]")
    if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
        raise ValueError(f"item {index}: crop values must be integers")
    x, y, crop_width, crop_height = value
    if x < 0 or y < 0 or crop_width <= 0 or crop_height <= 0:
        raise ValueError(f"item {index}: crop origin/size is invalid")
    if x + crop_width > width or y + crop_height > height:
        raise ValueError(f"item {index}: crop exceeds image bounds")
    return value.copy()


def _quality(prediction, native):
    values = qrisp_metrics(np.asarray(prediction), np.asarray(native))
    return {key: float(values[key]) for key in METRIC_KEYS}


def _delta(sr, bicubic):
    return {key: sr[key] - bicubic[key] for key in METRIC_KEYS}


def _fps_summary(native, bicubic, sr):
    result = {}
    for name, value in (("native", native), ("bicubic", bicubic), ("sr", sr)):
        if value is None:
            continue
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"FPS {name} must be finite and positive")
        result[name] = value
    if bicubic is not None and sr is not None:
        result["sr_minus_bicubic"] = result["sr"] - result["bicubic"]
        result["sr_over_bicubic"] = result["sr"] / result["bicubic"]
    return result


def _thumbnail(image):
    result = image.copy()
    result.thumbnail((320, 180), Image.Resampling.LANCZOS)
    return result


def _write_sheet(path, rows, labels):
    cell_width = max(image.width for row in rows for image in row)
    cell_height = max(image.height for row in rows for image in row)
    header_height, label_height = 24, 20
    row_height = cell_height + label_height
    sheet = Image.new("RGB", (3 * cell_width, header_height + len(rows) * row_height),
                      (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    for column, title in enumerate(("native", "bicubic", "SR")):
        draw.text((column * cell_width + 4, 5), title, fill="white")
    for index, (row, label) in enumerate(zip(rows, labels)):
        y = header_height + index * row_height
        draw.text((4, y + 3), label, fill="white")
        for column, image in enumerate(row):
            x = column * cell_width + (cell_width - image.width) // 2
            image_y = y + label_height + (cell_height - image.height) // 2
            sheet.paste(image, (x, image_y))
    sheet.save(path)


def compare(manifest, out, fps_native=None, fps_bicubic=None, fps_sr=None,
            full_thumbnails=False):
    """Validate a manifest, write comparison artifacts, and return the summary."""
    manifest = Path(manifest)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("manifest must be an object with an items list")
    if not document["items"]:
        raise ValueError("manifest must contain at least one item")
    fps = _fps_summary(fps_native, fps_bicubic, fps_sr)
    root = manifest.parent.resolve()
    items = []
    crop_rows, thumbnail_rows, labels = [], [], []

    for index, entry in enumerate(document["items"]):
        if not isinstance(entry, dict):
            raise ValueError(f"item {index}: expected an object")
        paths = {source: _resolve_image(root, entry.get(source), index, source)
                 for source in SOURCES}
        images = {source: _read_rgb(paths[source]) for source in SOURCES}
        size = images["native"].size
        if any(images[source].size != size for source in ("bicubic", "sr")):
            raise ValueError(f"item {index}: native/bicubic/sr dimensions must match")
        crop = _crop_box(entry.get("crop"), size, index)
        label = entry.get("label", f"item {index + 1}")
        if not isinstance(label, str):
            raise ValueError(f"item {index}: label must be a string")

        bicubic_quality = _quality(images["bicubic"], images["native"])
        sr_quality = _quality(images["sr"], images["native"])
        item = {
            "label": label,
            "paths": {source: entry[source] for source in SOURCES},
            "image_size": list(size),
            "crop": crop,
            "bicubic_vs_native": bicubic_quality,
            "sr_vs_native": sr_quality,
            "sr_minus_bicubic": _delta(sr_quality, bicubic_quality),
        }
        items.append(item)
        labels.append(label)
        x, y, width, height = crop
        box = (x, y, x + width, y + height)
        crop_rows.append([images[source].crop(box) for source in SOURCES])
        if full_thumbnails:
            thumbnail_rows.append([_thumbnail(images[source]) for source in SOURCES])

    mean = {}
    for comparison in ("bicubic_vs_native", "sr_vs_native", "sr_minus_bicubic"):
        mean[comparison] = {
            key: float(np.mean([item[comparison][key] for item in items]))
            for key in METRIC_KEYS
        }
    sheets = {"crops": "contact_sheet.png"}
    if full_thumbnails:
        sheets["full"] = "contact_sheet_full.png"
    summary = {"items": items, "mean": mean, "fps": fps, "sheets": sheets}

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    _write_sheet(out / sheets["crops"], crop_rows, labels)
    if full_thumbnails:
        _write_sheet(out / sheets["full"], thumbnail_rows, labels)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                      encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fps-native", type=float)
    parser.add_argument("--fps-bicubic", type=float)
    parser.add_argument("--fps-sr", type=float)
    parser.add_argument("--full-thumbnails", action="store_true",
                        help="also write a downscaled full-frame contact sheet")
    args = parser.parse_args(argv)
    try:
        summary = compare(args.manifest, args.out, args.fps_native, args.fps_bicubic,
                          args.fps_sr, args.full_thumbnails)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for name in ["summary.json", *summary["sheets"].values()]:
        print(args.out / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
