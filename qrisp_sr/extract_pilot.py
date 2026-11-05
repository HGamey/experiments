#!/usr/bin/env python3
"""Extract eight exact QRISP LR/HR pairs through bounded HTTP range reads."""
import argparse
import binascii
import io
import json
import os
from pathlib import Path
import struct
import time
import zipfile
import zlib

import numpy as np
from PIL import Image, ImageDraw

from remote_zip import HTTPRangeFile


LOCAL_HEADER = struct.Struct("<4s5H3L2H")


def read_entry(remote, item):
    raw_header = remote.read_at(item.header_offset, LOCAL_HEADER.size)
    fields = LOCAL_HEADER.unpack(raw_header)
    if fields[0] != b"PK\x03\x04":
        raise RuntimeError(f"bad local header for {item.filename}")
    flags, compression, name_len, extra_len = fields[2], fields[3], fields[9], fields[10]
    if flags & 1:
        raise RuntimeError(f"encrypted entry is unsupported: {item.filename}")
    if compression != item.compress_type:
        raise RuntimeError(f"compression mismatch for {item.filename}")
    data_offset = item.header_offset + LOCAL_HEADER.size + name_len + extra_len
    payload = remote.read_at(data_offset, item.compress_size)
    if item.compress_type == zipfile.ZIP_DEFLATED:
        payload = zlib.decompress(payload, -15)
    elif item.compress_type != zipfile.ZIP_STORED:
        raise RuntimeError(f"unsupported compression {item.compress_type}: {item.filename}")
    if len(payload) != item.file_size or binascii.crc32(payload) != item.CRC:
        raise RuntimeError(f"size/CRC mismatch for {item.filename}")
    return payload


def save_image(payload, path):
    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGB")
        image.load()
    path.write_bytes(payload)
    return image


def make_sheet(rows, path):
    thumb_w, thumb_h, label_h = 384, 216, 24
    sheet = Image.new("RGB", (thumb_w * 3, (thumb_h + label_h) * len(rows)), "white")
    draw = ImageDraw.Draw(sheet)
    for row_index, (label, lr, bicubic, hr) in enumerate(rows):
        y = row_index * (thumb_h + label_h)
        for column, (kind, image) in enumerate(
            (("540p LR", lr), ("bicubic 1080p", bicubic), ("1080p HR", hr))
        ):
            x = column * thumb_w
            preview = image.resize((thumb_w, thumb_h), Image.Resampling.BILINEAR)
            sheet.paste(preview, (x, y + label_h))
            draw.text((x + 5, y + 5), f"{label} {kind}", fill="black")
    sheet.save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/suntemplebush.zip",
    )
    parser.add_argument("--segments", default="0000,0001,0002,0003,0004,0005,0006,0007")
    parser.add_argument("--frame", default="0015")
    args = parser.parse_args()
    segments = args.segments.split(",")
    if len(segments) != 8 or any(not x.isdigit() or len(x) != 4 for x in segments):
        raise ValueError("pilot requires eight four-digit segments")
    if not args.frame.isdigit() or len(args.frame) != 4:
        raise ValueError("--frame must be four digits")

    started = time.perf_counter()
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    remote = HTTPRangeFile(args.url)
    records = []
    sheet_rows = []
    with zipfile.ZipFile(remote) as archive:
        infos = {item.filename: item for item in archive.infolist() if not item.is_dir()}
        for segment in segments:
            lr_name = f"QRISP/SunTempleBush/540p/MipBiasMinus1/{segment}/{args.frame}.png"
            hr_name = f"QRISP/SunTempleBush/1080p/Enhanced/{segment}/{args.frame}.png"
            if lr_name not in infos or hr_name not in infos:
                raise RuntimeError(f"missing pair: {lr_name} / {hr_name}")
            lr_payload = read_entry(remote, infos[lr_name])
            hr_payload = read_entry(remote, infos[hr_name])
            stem = f"segment-{segment}-frame-{args.frame}"
            lr = save_image(lr_payload, frames_dir / f"{stem}-lr.png")
            hr = save_image(hr_payload, frames_dir / f"{stem}-hr.png")
            if lr.size != (960, 540) or hr.size != (1920, 1080):
                raise RuntimeError(f"unexpected dimensions: {lr.size} / {hr.size}")
            bicubic = lr.resize(hr.size, Image.Resampling.BICUBIC)
            bicubic.save(frames_dir / f"{stem}-bicubic.png")
            hr_array = np.asarray(hr, dtype=np.float32)
            bicubic_array = np.asarray(bicubic, dtype=np.float32)
            mse = float(np.mean((bicubic_array - hr_array) ** 2))
            psnr = float("inf") if mse == 0 else float(10 * np.log10(255.0**2 / mse))
            records.append(
                {
                    "segment": segment,
                    "frame": args.frame,
                    "lr_path": lr_name,
                    "hr_path": hr_name,
                    "lr_bytes": len(lr_payload),
                    "hr_bytes": len(hr_payload),
                    "lr_dimensions": list(lr.size),
                    "hr_dimensions": list(hr.size),
                    "bicubic_rgb_psnr_db": psnr,
                    "bicubic_rgb_mse_8bit_squared": mse,
                }
            )
            sheet_rows.append((f"{segment}/{args.frame}", lr, bicubic, hr))

    sheet_path = out / "pilot-contact-sheet.jpg"
    make_sheet(sheet_rows, sheet_path)
    report = {
        "schema": "hgamey/qrisp-pilot-pairs@1",
        "source_url": args.url,
        "scene": "SunTempleBush",
        "lr_modality": "540p/MipBiasMinus1 (native rendered LR, not downsampled HR)",
        "hr_modality": "1080p/Enhanced",
        "pair_count": len(records),
        "pairs": records,
        "bicubic_mean_rgb_psnr_db": float(
            np.mean([item["bicubic_rgb_psnr_db"] for item in records])
        ),
        "contact_sheet": sheet_path.name,
        "http_requests": remote.requests,
        "http_attempts": remote.attempts,
        "http_bytes_read": remote.bytes_read,
        "archive_bytes": remote.size,
        "archive_fraction_read": remote.bytes_read / remote.size,
        "model_trained": False,
        "phone_access": False,
        "wall_s": round(time.perf_counter() - started, 3),
    }
    (out / "pilot-pairs.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "pairs"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
