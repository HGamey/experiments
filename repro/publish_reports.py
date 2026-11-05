#!/usr/bin/env python3
"""Publish an explicitly curated set of text reports as ordinary job artifacts.

The caller supplies reports through the platform's small input snapshot. Nothing
is captured, replayed or measured by this operation; original bytes are preserved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=os.environ.get("PF_JOB_OUT"))
    args = parser.parse_args()
    if args.out is None:
        parser.error("--out or PF_JOB_OUT is required")
    args.out.mkdir(parents=True, exist_ok=True)
    files = []
    for source in sorted(args.reports.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(args.reports)
        destination = args.out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append({"path": relative.as_posix(), "bytes": destination.stat().st_size,
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    manifest = {"schema": "hgamey/report-import@1", "operation": "historical_reports_only",
                "raw_data_uploaded": False, "new_device_experiments": False,
                "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"), "files": files,
                "report_bytes": sum(item["bytes"] for item in files)}
    (args.out / "publish-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report_files": len(files), "report_bytes": manifest["report_bytes"],
                      "raw_data_uploaded": False}), flush=True)


if __name__ == "__main__":
    main()
