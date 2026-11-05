#!/usr/bin/env python3
"""Snapshot a selected historical study without replaying its experiments.

The private input manifest supplies roots and a file allowlist. Outputs are ordinary
job artifacts: bounded gzip/tar volumes, per-file hashes, and selected loose reports.
This script never accesses devices or registers historical jobs as new measurements.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class HashReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        block = self.source.read(size)
        self.digest.update(block)
        return block


def batches(files, max_bytes, max_files):
    group, size = [], 0
    for entry in files:
        if group and (size + entry["bytes"] > max_bytes or len(group) >= max_files):
            yield group
            group, size = [], 0
        group.append(entry)
        size += entry["bytes"]
    if group:
        yield group


def archive(manifest, out, max_bytes=256 << 20, max_files=2500):
    out.mkdir(parents=True, exist_ok=True)
    roots = {key: Path(value) for key, value in manifest["roots"].items()}
    records, volumes = [], []
    loose = set(manifest.get("loose", []))
    for index, group in enumerate(batches(manifest["files"], max_bytes, max_files), 1):
        name = f"doom-history-{index:03d}.tar.gz"
        target = out / name
        if target.exists():
            raise FileExistsError(f"Use a fresh output directory; refusing to overwrite {name}")
        start = len(records)
        with target.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=1, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as tar:
                for entry in group:
                    rel = Path(entry["path"])
                    if rel.is_absolute() or ".." in rel.parts:
                        raise ValueError("Manifest paths must stay inside the selected root")
                    source = roots[entry["root"]] / rel
                    arcname = entry["root"] + "/" + rel.as_posix()
                    with source.open("rb") as stream:
                        before = os.fstat(stream.fileno())
                        if before.st_size != entry["bytes"] or before.st_mtime_ns != entry["mtime_ns"]:
                            raise ValueError(f"Source changed since inventory: {arcname}")
                        reader = HashReader(stream)
                        info = tarfile.TarInfo(arcname)
                        info.size, info.mode = before.st_size, before.st_mode & 0o777
                        info.mtime = before.st_mtime
                        tar.addfile(info, reader)
                        after = os.fstat(stream.fileno())
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError(f"Source changed during snapshot: {arcname}")
                    records.append({"path": arcname, "bytes": before.st_size,
                                    "sha256": reader.digest.hexdigest(), "volume": name})
                    if arcname in loose:
                        destination = out / "browse" / arcname
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        volumes.append({"path": name, "bytes": target.stat().st_size, "sha256": digest,
                        "files": len(group), "source_bytes": sum(e["bytes"] for e in group)})
        save(out / "archive-progress.json", {"volumes": volumes, "files_archived": len(records)})
        print(json.dumps({"volume": name, "files": len(records) - start,
                          "compressed_bytes": target.stat().st_size}), flush=True)
    result = {"schema": "hgamey/historical-study-archive@1", "study": manifest["study"],
              "operation": "historical_import_not_new_measurement", "files": records,
              "file_count": len(records), "source_bytes": sum(r["bytes"] for r in records),
              "volumes": volumes, "excluded": manifest.get("excluded", []),
              "symlinks": manifest.get("symlinks", []), "gaps": manifest.get("gaps", []),
              "execution_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
              "research_id": os.environ.get("PF_JOB_RESEARCH"),
              "restore": "Extract all volumes into one empty directory; consult symlinks for relocated source-tree links."}
    save(out / "archive-manifest.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--prepared", help="JSON containing a prepared archive directory and its file allowlist")
    parser.add_argument("--out", default=os.environ.get("PF_JOB_OUT"))
    args = parser.parse_args()
    if not args.out:
        parser.error("--out or PF_JOB_OUT is required")
    if args.prepared:
        prepared = json.loads(Path(args.prepared).read_text())
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for item in prepared["files"]:
            rel = Path(item["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("Prepared paths must stay inside the archive directory")
            origin, destination = Path(prepared["directory"]) / rel, out / rel
            if origin.stat().st_size != item["bytes"]:
                raise ValueError(f"Prepared archive changed: {rel}")
            if destination.exists():
                raise FileExistsError(f"Refusing to overwrite {rel}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
        result = json.loads((out / "archive-manifest.json").read_text())
        save(out / "import-provenance.json", {
            "operation": "historical_import_not_new_measurement",
            "execution_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
            "research_id": os.environ.get("PF_JOB_RESEARCH"),
            "original_observations": "See original timestamps and task IDs inside the archive",
        })
    else:
        result = archive(json.loads(Path(args.manifest).read_text()), Path(args.out))
    print(json.dumps({"files": result["file_count"], "source_bytes": result["source_bytes"],
                      "archive_bytes": sum(v["bytes"] for v in result["volumes"])}), flush=True)


if __name__ == "__main__":
    main()
