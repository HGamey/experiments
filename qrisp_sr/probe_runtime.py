#!/usr/bin/env python3
"""Inventory QRISP training prerequisites without installing anything.

This is a host/runtime and HTTP reachability probe. It does not download a
scene, train on QRISP, access a phone, or produce a quality result.
"""
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
import time
import urllib.error
import urllib.request


URLS = {
    "train_scififacility": "https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/scififacility.zip",
    "test_seaport": "https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/seaport.zip",
}
MODULES = (
    "numpy",
    "PIL",
    "torch",
    "tensorflow",
    "jax",
    "mlx",
    "h5py",
    "onnx",
    "coremltools",
)


def probe_url(url):
    result = {"url": url}
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "QRISP-research-runtime-probe/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result.update(
                status=response.status,
                final_url=response.url,
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower()
                    in {
                        "content-length",
                        "content-type",
                        "content-range",
                        "accept-ranges",
                        "content-disposition",
                    }
                },
            )
            return result
    except Exception as exc:
        result["head_error"] = f"{type(exc).__name__}: {exc}"[:1000]

    # Some download services reject HEAD. A bounded GET distinguishes that
    # case without consuming the scene body; never retry an unbounded read.
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "QRISP-research-runtime-probe/1",
            "Range": "bytes=0-63",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(64)
            result.update(
                status=response.status,
                final_url=response.url,
                range_probe_bytes=len(body),
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower()
                    in {
                        "content-length",
                        "content-type",
                        "content-range",
                        "accept-ranges",
                        "content-disposition",
                    }
                },
            )
    except Exception as exc:
        result["get_error"] = f"{type(exc).__name__}: {exc}"[:1000]
    return result


def main():
    started = time.perf_counter()
    modules = {}
    for name in MODULES:
        installed = importlib.util.find_spec(name) is not None
        item = {"installed": installed}
        if installed:
            try:
                item["version"] = importlib.metadata.version(
                    {"PIL": "Pillow"}.get(name, name)
                )
            except importlib.metadata.PackageNotFoundError:
                item["version"] = None
        modules[name] = item

    report = {
        "schema": "hgamey/qrisp-runtime-probe@1",
        "host": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": sys.version,
        "python_executable": sys.executable,
        "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
        "modules": modules,
        "official_downloads": {name: probe_url(url) for name, url in URLS.items()},
        "scene_bytes_downloaded": 0,
        "qrisp_data_used_for_training": False,
        "quality_claim": False,
        "phone_access": False,
        "global_environment_changed": False,
    }
    report["wall_s"] = round(time.perf_counter() - started, 3)
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / "qrisp-runtime.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
