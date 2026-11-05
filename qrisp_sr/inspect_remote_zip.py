#!/usr/bin/env python3
"""Read a public QRISP ZIP central directory without downloading scene data."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
import zipfile

from remote_zip import HTTPRangeFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/suntemplebush.zip",
    )
    parser.add_argument("--name", default="suntemplebush")
    args = parser.parse_args()
    started = time.perf_counter()
    remote = HTTPRangeFile(args.url)
    with zipfile.ZipFile(remote) as archive:
        entries = [
            {
                "filename": item.filename,
                "file_size": item.file_size,
                "compress_size": item.compress_size,
                "compress_type": item.compress_type,
                "CRC": item.CRC,
                "header_offset": item.header_offset,
            }
            for item in archive.infolist()
            if not item.is_dir()
        ]
    bad_path = "../" in args.name or args.name.startswith("/") or not args.name
    if bad_path:
        raise ValueError("--name must be a simple output basename")
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    listing_path = out / f"{args.name}-central-directory.json"
    listing = {
        "schema": "hgamey/qrisp-remote-zip-directory@1",
        "source_url": args.url,
        "archive_bytes": remote.size,
        "entries": entries,
    }
    listing_path.write_text(
        json.dumps(listing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "schema": "hgamey/qrisp-remote-zip-inspect@1",
        "source_url": args.url,
        "archive_bytes": remote.size,
        "file_entries": len(entries),
        "uncompressed_bytes": sum(item["file_size"] for item in entries),
        "compressed_entry_bytes": sum(item["compress_size"] for item in entries),
        "extensions": dict(Counter(Path(item["filename"]).suffix for item in entries)),
        "top_level": dict(
            Counter(item["filename"].split("/", 1)[0] for item in entries)
        ),
        "sample_paths": [item["filename"] for item in entries[:40]],
        "central_directory_file": listing_path.name,
        "central_directory_bytes": listing_path.stat().st_size,
        "central_directory_sha256": hashlib.sha256(listing_path.read_bytes()).hexdigest(),
        "http_requests": remote.requests,
        "http_bytes_read": remote.bytes_read,
        "scene_payload_downloaded": False,
        "phone_access": False,
        "wall_s": round(time.perf_counter() - started, 3),
    }
    (out / "remote-zip-inspect.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
