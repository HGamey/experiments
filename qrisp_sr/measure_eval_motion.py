#!/usr/bin/env python3
"""Measure adjacent-frame QRISP SeaPort motion-vector magnitude in LR pixels."""
import json
from pathlib import Path
import zipfile

import numpy as np

from exr import decode_exr
from extract_pilot import read_entry
from remote_zip import HTTPRangeFile


URL = "https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/seaport.zip"
FRAMES = ("0059", "0119", "0179", "0239", "0299")
PREFIX = "QRISP/TestSet/SeaPort/540p/MotionVectorsMipBiasMinus1/0000"


def main():
    remote = HTTPRangeFile(URL)
    frames = []
    with zipfile.ZipFile(remote) as archive:
        infos = {item.filename: item for item in archive.infolist() if not item.is_dir()}
        for frame in FRAMES:
            path = f"{PREFIX}/{frame}.exr"
            payload = read_entry(remote, infos[path])
            vectors, names, _ = decode_exr(payload)
            height, width = vectors.shape[:2]
            dx = vectors[:, :, names.index("R")] * width
            dy = vectors[:, :, names.index("G")] * height
            magnitude = np.hypot(dx, dy)
            item = {
                "frame": frame,
                "previous_frame": f"{int(frame) - 1:04d}",
                "width": width,
                "height": height,
                "mean_magnitude_lr_pixels": float(np.mean(magnitude)),
                "median_magnitude_lr_pixels": float(np.median(magnitude)),
                "p95_magnitude_lr_pixels": float(np.percentile(magnitude, 95)),
                "fraction_below_1_lr_pixel": float(np.mean(magnitude < 1.0)),
            }
            frames.append(item)
            print(f"MOTION {frame} median={item['median_magnitude_lr_pixels']:.4f} "
                  f"p95={item['p95_magnitude_lr_pixels']:.4f} "
                  f"below1={item['fraction_below_1_lr_pixel']:.4f}", flush=True)
    all_medians = [item["median_magnitude_lr_pixels"] for item in frames]
    result = {
        "schema": "hgamey/qrisp-eval-motion@1",
        "source": URL,
        "paths": [f"{PREFIX}/{frame}.exr" for frame in FRAMES],
        "definition": "hypot(MV_R * LR_width, MV_G * LR_height); adjacent frames; all pixels",
        "frames": frames,
        "mean_of_frame_medians_lr_pixels": float(np.mean(all_medians)),
        "mean_of_frame_p95s_lr_pixels": float(np.mean([item["p95_magnitude_lr_pixels"] for item in frames])),
        "mean_fraction_below_1_lr_pixel": float(np.mean([item["fraction_below_1_lr_pixel"] for item in frames])),
        "http_bytes_read": remote.bytes_read,
        "raw_exr_files_written": 0,
    }
    output = Path("eval-motion-summary.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "frames"}, indent=2))


if __name__ == "__main__":
    main()
