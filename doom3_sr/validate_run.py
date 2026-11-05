#!/usr/bin/env python3
"""Validate a formal Doom SmallSR run against its original HR sources.

Writes a JSON report even for an invalid run.  The CLI exits nonzero when any
check fails; it performs no training, network, or platform operations.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from doom3_sr.train_doom import discover  # noqa: E402
from qrisp_sr.numpy_sr import SmallSR  # noqa: E402
from qrisp_sr.train_qrisp import metrics  # noqa: E402


PSNR_TOLERANCE_DB = 1e-4
SSIM_TOLERANCE = 1e-5


class ValidationError(Exception):
    pass


class Checks:
    def __init__(self):
        self.values = {}
        self.errors = []

    def require(self, name, condition, detail=""):
        entry = {"valid": bool(condition)}
        if detail:
            entry["detail"] = detail
        self.values[name] = entry
        if not condition:
            message = f"{name}: {detail}" if detail else name
            self.errors.append(message)
            raise ValidationError(message)

    def fatal(self, error):
        self.values["fatal"] = {"valid": False, "detail": str(error)}
        self.errors.append(f"fatal: {error}")


def _load_json(path):
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_path(run, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"invalid run-relative path: {relative!r}")
    path = (run / relative).resolve()
    if run.resolve() not in path.parents:
        raise ValueError(f"run path escapes its root: {relative}")
    return path


def _close(actual, expected, tolerance):
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    if math.isnan(actual) or math.isnan(expected):
        return False
    if math.isinf(actual) or math.isinf(expected):
        return actual == expected
    return abs(actual - expected) <= tolerance


def _frame_map(result, label):
    frames = result["frames"]
    paths = [item["relative_path"] for item in frames]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{label} has duplicate frame paths")
    return {item["relative_path"]: item for item in frames}


def _validate_aggregates(summary, checks):
    baseline = summary["baseline_bicubic"]
    results = {"baseline_bicubic": baseline, **summary["variants"]}
    baseline_frames = _frame_map(baseline, "baseline_bicubic")
    for name, result in results.items():
        frames = _frame_map(result, name)
        if set(frames) != set(baseline_frames):
            checks.require("aggregate_metrics", False, f"{name}: held-out frame paths differ")
        for field, mean_field in (
            ("rgb_psnr_db", "mean_rgb_psnr_db"),
            ("rgb_ssim_11x11", "mean_rgb_ssim_11x11"),
        ):
            expected = float(np.mean([item[field] for item in result["frames"]]))
            if not _close(result[mean_field], expected, 1e-12):
                checks.require("aggregate_metrics", False,
                               f"{name}: {mean_field} does not match its frames")
        if name == "baseline_bicubic":
            continue
        expected_deltas = []
        for path, frame in frames.items():
            expected_deltas.append(frame["rgb_psnr_db"] - baseline_frames[path]["rgb_psnr_db"])
        expected_values = {
            "delta_psnr_db_vs_bicubic": result["mean_rgb_psnr_db"] - baseline["mean_rgb_psnr_db"],
            "delta_ssim_vs_bicubic": result["mean_rgb_ssim_11x11"] - baseline["mean_rgb_ssim_11x11"],
            "psnr_improved_frames_vs_bicubic": sum(delta > 0 for delta in expected_deltas),
            "psnr_regressed_frames_vs_bicubic": sum(delta < 0 for delta in expected_deltas),
        }
        for field, expected in expected_values.items():
            tolerance = 1e-12 if isinstance(expected, float) else 0
            if not _close(result[field], expected, tolerance):
                checks.require("aggregate_metrics", False,
                               f"{name}: {field} does not match baseline comparison")
    checks.require("aggregate_metrics", True,
                   f"recomputed means and baseline deltas for {len(results)} results")


def _validate_npz_and_histories(run, summary, selection, checks):
    npz_files = {selection["model"]}
    steps = summary["steps_per_trained_variant"]
    history_errors = []
    for name, variant in summary["variants"].items():
        structure = variant.get("structure")
        if structure is None:
            continue  # The optional pretrained variant has no run-local NPZ/history.
        npz_files.add(structure["npz_file"])
        history_path = run / f"{name}-training-history.json"
        try:
            history = _load_json(history_path)["l1"]
            if structure.get("training_steps") != steps:
                history_errors.append(f"{name}: structure training_steps differs from {steps}")
            if len(history) != steps:
                history_errors.append(f"{name}: history length {len(history)} != {steps}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            history_errors.append(f"{name}: {error}")
    checks.require("training_histories", not history_errors, "; ".join(history_errors))

    finite_errors = []
    for relative in sorted(npz_files):
        path = _run_path(run, relative)
        try:
            with np.load(path, allow_pickle=False) as weights:
                for key in weights.files:
                    values = weights[key]
                    if not np.issubdtype(values.dtype, np.number) or not np.isfinite(values).all():
                        finite_errors.append(f"{relative}:{key}")
        except (OSError, ValueError, TypeError) as error:
            finite_errors.append(f"{relative}: {error}")
    checks.require("npz_weights_finite", not finite_errors,
                   "; ".join(finite_errors) if finite_errors else f"checked {len(npz_files)} NPZ files")


def _source_catalog(data, split):
    catalog = {}
    for item in discover(data, split):
        relative = item["relative_path"]
        if relative in catalog and catalog[relative]["path"] != item["path"]:
            raise ValueError(f"ambiguous source path {relative} in {data}")
        catalog[relative] = item
    return catalog


def _validate_sources(run, data, test_data, data_manifest, checks):
    sources = data_manifest["sources"]
    train = [item for item in sources if item.get("split") == "train"]
    test = [item for item in sources if item.get("split") == "test"]
    if len(train) + len(test) != len(sources):
        checks.require("source_sets_disjoint", False, "every source must be train or test")
    train_hashes = {item["hr_sha256"] for item in train}
    test_hashes = {item["hr_sha256"] for item in test}
    checks.require("source_sets_disjoint", train_hashes.isdisjoint(test_hashes),
                   f"{len(train_hashes)} train and {len(test_hashes)} test SHA-256 values")
    if len(train) != data_manifest["train_pairs"] or len(test) != data_manifest["test_pairs"]:
        checks.require("source_hashes", False, "source counts differ from train_pairs/test_pairs")

    train_catalog = _source_catalog(data, "train" if test_data else None)
    test_catalog = _source_catalog(test_data, "test") if test_data else train_catalog
    hash_errors = []
    resolved = {}
    for item in sources:
        relative = item["relative_path"]
        catalog = test_catalog if item["split"] == "test" else train_catalog
        descriptor = catalog.get(relative)
        if descriptor is None:
            hash_errors.append(f"{relative}: not found in supplied original data")
            continue
        actual = _sha256(descriptor["path"])
        if actual != item["hr_sha256"]:
            hash_errors.append(f"{relative}: original HR SHA-256 mismatch")
        resolved[(item["split"], relative)] = descriptor
    checks.require("source_hashes", not hash_errors,
                   "; ".join(hash_errors) if hash_errors else f"verified {len(sources)} original HR files")
    return test, resolved


def _validate_export(run, selection, checks):
    if selection.get("glsl_inc") != "doom_small_sr.inc" or selection.get("glsl_manifest") != "doom_small_sr.manifest.json":
        checks.require("export_hashes", False, "selection does not name the formal Doom GLSL artifacts")
    selected_path = _run_path(run, selection["model"])
    manifest_path = run / "doom_small_sr.manifest.json"
    inc_path = run / "doom_small_sr.inc"
    export_manifest = _load_json(manifest_path)
    source_ok = export_manifest.get("source_npz_sha256") == _sha256(selected_path)
    inc_ok = export_manifest.get("generated_inc_sha256") == _sha256(inc_path)
    checks.require("export_hashes", source_ok and inc_ok,
                   f"selected NPZ source SHA match={source_ok}; generated INC SHA match={inc_ok}")


def _decode_hr(descriptor):
    with Image.open(descriptor["path"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.load()
    return image


def _validate_predictions(model, test_sources, resolved, baseline, selected, checks):
    baseline_frames = _frame_map(baseline, "baseline_bicubic")
    selected_frames = _frame_map(selected, "selected variant")
    checked = []
    for source in test_sources:
        relative = source["relative_path"]
        descriptor = resolved[("test", relative)]
        image = _decode_hr(descriptor)
        width, height = image.size
        if [width, height] != [source["width"], source["height"]]:
            checks.require("heldout_predictions", False, f"{relative}: HR dimensions differ")
        if width < 16 or height < 16 or width % 2 or height % 2:
            checks.require("heldout_predictions", False, f"{relative}: invalid even HR dimensions")
        hr = np.asarray(image, dtype=np.uint8)
        lr_image = image.resize((width // 2, height // 2), Image.Resampling.BICUBIC)
        lr = np.asarray(lr_image, dtype=np.uint8)
        base_image = Image.fromarray(lr).resize((width, height), Image.Resampling.BICUBIC)
        base = np.asarray(base_image, dtype=np.uint8)
        prediction = model.predict(
            lr.astype(np.float32)[None] / 255,
            base.astype(np.float32)[None] / 255,
        )[0] * 255
        recomputed = {
            "selected": metrics(prediction, hr),
            "baseline_bicubic": metrics(base, hr),
        }
        stored = {
            "selected": selected_frames.get(relative),
            "baseline_bicubic": baseline_frames.get(relative),
        }
        for label in recomputed:
            if stored[label] is None:
                checks.require("heldout_predictions", False, f"{relative}: missing {label} metrics")
            for field, tolerance in (
                ("rgb_psnr_db", PSNR_TOLERANCE_DB),
                ("rgb_ssim_11x11", SSIM_TOLERANCE),
            ):
                actual = recomputed[label][field]
                expected = stored[label][field]
                if not _close(actual, expected, tolerance):
                    checks.require(
                        "heldout_predictions", False,
                        f"{relative}: {label} {field} recomputed {actual} != stored {expected}",
                    )
        checked.append(relative)
    checks.require("heldout_predictions", True,
                   f"independently recomputed selected and bicubic metrics for {len(checked)} frames")


def _validate(run, data, test_data, checks):
    required = [run / name for name in ("summary.json", "data-manifest.json", "selection.json")]
    missing = [path.name for path in required if not path.is_file()]
    checks.require("required_files", not missing,
                   f"missing: {', '.join(missing)}" if missing else "summary, data manifest, and selection exist")
    summary, data_manifest, selection = [_load_json(path) for path in required]

    _validate_npz_and_histories(run, summary, selection, checks)
    test_sources, resolved = _validate_sources(run, data, test_data, data_manifest, checks)
    _validate_aggregates(summary, checks)

    variants = summary["variants"]
    selected_name = selection["selected_variant"]
    if selected_name not in variants:
        checks.require("selected_max_mean_psnr", False, f"unknown variant {selected_name}")
    selected = variants[selected_name]
    best = max(float(item["mean_rgb_psnr_db"]) for item in variants.values())
    selection_matches = summary.get("selected_variant") == selected_name
    checks.require("selected_max_mean_psnr",
                   selection_matches and _close(selected["mean_rgb_psnr_db"], best, 0),
                   f"selected={selected_name}; summary selection match={selection_matches}; best mean PSNR={best}")

    _validate_export(run, selection, checks)
    expected_delta = selected["delta_psnr_db_vs_bicubic"]
    recommendation = selection.get("recommended_over_bicubic")
    checks.require(
        "selection_recommendation",
        _close(selection.get("delta_psnr_db_vs_bicubic"), expected_delta, 1e-12)
        and isinstance(recommendation, bool) and recommendation == bool(expected_delta > 0),
        f"delta={expected_delta}; recommendation={recommendation}",
    )

    structure = _load_json(_run_path(run, selection["structure"]))
    model = SmallSR.load(_run_path(run, selection["model"]), structure)
    _validate_predictions(model, test_sources, resolved, summary["baseline_bicubic"], selected, checks)


def validate(run, data, test_data, output):
    """Validate a run, write the report, and return it without raising on mismatch."""
    run = Path(run)
    checks = Checks()
    try:
        _validate(run, Path(data), Path(test_data) if test_data else None, checks)
    except ValidationError:
        pass
    except Exception as error:  # Convert corrupt downloads into a clear JSON failure.
        checks.fatal(error)
    report = {
        "schema": "hgamey/doom-sr-validation@1",
        "valid": not checks.errors,
        "checks": checks.values,
        "errors": checks.errors,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="downloaded formal training run directory")
    parser.add_argument("--data", required=True, help="original training HR directory or manifest")
    parser.add_argument("--test-data", help="original held-out HR directory or manifest")
    parser.add_argument("--output", required=True, help="validation report JSON")
    args = parser.parse_args(argv)
    report = validate(args.run, args.data, args.test_data, args.output)
    if report["valid"]:
        print(f"VALID {args.run}; report: {args.output}")
        return 0
    for error in report["errors"]:
        print(f"INVALID {error}", file=sys.stderr)
    print(f"Report: {args.output}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
