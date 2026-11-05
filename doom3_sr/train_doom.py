#!/usr/bin/env python3
"""Train small 2x SR on Doom HR captures; LR is bicubic-downsampled HR."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qrisp_sr"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from numpy_sr import Adam, SmallSR  # noqa: E402
from train_qrisp import metrics  # noqa: E402
from export_glsl import export as export_glsl  # noqa: E402


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
EXCLUDED_PARTS = {"lr", "bicubic", "prediction", "predictions", "crop", "crops", "contact", "sheet", "thumbnail", "thumbnails"}


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entries(path):
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    document = json.loads(path.read_text())
    if isinstance(document, list):
        return document
    for key in ("frames", "pairs", "images", "items"):
        if key in document and isinstance(document[key], list):
            return document[key]
    raise ValueError(f"no frame list in {path}")


def discover(data, explicit_split=None):
    """Return HR descriptors from a manifest or an HR-only directory tree."""
    data = Path(data)
    manifest = data if data.is_file() else None
    if data.is_dir():
        for name in ("doom-manifest.jsonl", "doom-manifest.json", "manifest.jsonl", "manifest.json"):
            if (data / name).is_file():
                manifest = data / name
                break
    descriptors = []
    if manifest:
        root = manifest.parent
        for index, item in enumerate(manifest_entries(manifest)):
            relative = item.get("hr") or item.get("path") or item.get("file")
            if not relative:
                raise ValueError(f"manifest item {index} lacks hr/path/file")
            path = Path(relative)
            if path.is_absolute():
                raise ValueError("manifest image paths must be relative")
            resolved = (root / path).resolve()
            if root.resolve() not in resolved.parents:
                raise ValueError("manifest image escapes its root")
            split = explicit_split or item.get("split")
            group = item.get("group") or item.get("sequence") or item.get("capture") or item.get("scene") or item.get("map")
            descriptors.append({
                "path": resolved,
                "relative_path": str(path),
                "group": str(group or path.parent),
                "split": split if split in {"train", "test"} else None,
                "scene": str(item.get("scene") or item.get("map") or "Doom3"),
                "frame": str(item.get("frame") or path.stem),
                "device": item.get("device"),
            })
    else:
        if not data.is_dir():
            raise FileNotFoundError(data)
        root = data / "hr" if (data / "hr").is_dir() else data
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part.lower() in EXCLUDED_PARTS for part in relative.parts[:-1]):
                continue
            if any(part.lower() in EXCLUDED_PARTS for part in path.stem.split("-")):
                continue
            descriptors.append({
                "path": path.resolve(),
                "relative_path": str(relative),
                "group": str(relative.parent),
                "split": explicit_split,
                "scene": relative.parts[0] if len(relative.parts) > 1 else "Doom3",
                "frame": path.stem,
                "device": None,
            })
    if not descriptors:
        raise RuntimeError(f"no Doom HR images found under {data}")
    return descriptors


def deduplicate(descriptors):
    seen = set()
    result = []
    for item in descriptors:
        payload = item["path"].read_bytes()
        digest = sha256_bytes(payload)
        if digest in seen:
            continue
        seen.add(digest)
        result.append(item | {"hr_sha256": digest, "hr_bytes": len(payload)})
    return result


def assign_split(descriptors, test_fraction):
    explicit_test = [item for item in descriptors if item["split"] == "test"]
    if explicit_test:
        train = [item for item in descriptors if item["split"] != "test"]
        test = explicit_test
        method = "manifest_explicit"
    else:
        train = list(descriptors)
        test = []
        wanted = min(24, max(2, int(round(len(descriptors) * test_fraction))))
        if wanted >= len(descriptors):
            wanted = len(descriptors) - 1
        groups = sorted({item["group"] for item in descriptors})
        if len(groups) >= 3:
            ranked = sorted(groups, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
            test_groups = set()
            for group in ranked:
                if len(test) >= wanted or len(train) <= sum(item["group"] == g for item in descriptors for g in [group]):
                    break
                members = [item for item in train if item["group"] == group]
                if len(train) - len(members) < 1:
                    continue
                test_groups.add(group)
                test.extend(members)
                train = [item for item in train if item["group"] != group]
            method = "group_sha256"
            if test:
                for item in train + test:
                    item["test_group_selected"] = item["group"] in test_groups
        if not test:
            ranked = sorted(train, key=lambda item: hashlib.sha256(item["relative_path"].encode()).hexdigest())
            test = ranked[:wanted]
            selected = {id(item) for item in test}
            train = [item for item in train if id(item) not in selected]
            method = "frame_sha256_possible_near_duplicates"
    if not train or not test:
        raise RuntimeError(f"need nonempty train/test, got {len(train)}/{len(test)}")
    for item in train:
        item["split"] = "train"
    for item in test:
        item["split"] = "test"
    return train, test, method


def decode_record(item):
    with Image.open(item["path"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.load()
    width, height = image.size
    if width < 16 or height < 16 or width % 2 or height % 2:
        raise ValueError(f"HR dimensions must be even and >=16: {item['path']} {image.size}")
    lr_image = image.resize((width // 2, height // 2), Image.Resampling.BICUBIC)
    lr = np.asarray(lr_image, dtype=np.uint8)
    return item | {
        "hr": np.asarray(image, dtype=np.uint8),
        "lr": lr,
        "lr_padded": np.pad(lr, ((4, 4), (4, 4), (0, 0)), mode="edge"),
        "width": width,
        "height": height,
    }


def base_patch(record, x, y, patch):
    # Four LR pixels cover Pillow bicubic support. Edge padding matches resize.
    expanded = Image.fromarray(record["lr_padded"]).crop((x, y, x + patch + 8, y + patch + 8))
    resized = expanded.resize((2 * (patch + 8), 2 * (patch + 8)), Image.Resampling.BICUBIC)
    return np.asarray(resized, dtype=np.uint8)[8 : 8 + 2 * patch, 8 : 8 + 2 * patch]


def random_batch(records, rng, patch, batch_size):
    lrs, bases, targets = [], [], []
    for _ in range(batch_size):
        record = records[int(rng.integers(len(records)))]
        lr = record["lr"]
        # Pillow normalizes truncated bicubic filters at outer image borders.
        # Keep a two-LR-pixel margin so patch resize is exactly full-frame resize.
        max_y = lr.shape[0] - patch - 2
        max_x = lr.shape[1] - patch - 2
        if max_y < 2 or max_x < 2:
            raise ValueError(f"LR image needs two-pixel border around patch {patch}: {record['relative_path']}")
        y = int(rng.integers(2, max_y + 1))
        x = int(rng.integers(2, max_x + 1))
        lrs.append(lr[y : y + patch, x : x + patch])
        bases.append(base_patch(record, x, y, patch))
        targets.append(record["hr"][2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)])
    return tuple(np.stack(values).astype(np.float32) / 255 for values in (lrs, bases, targets))


def evaluate(model, records, label):
    results = []
    for record in records:
        base_image = Image.fromarray(record["lr"]).resize((record["width"], record["height"]), Image.Resampling.BICUBIC)
        base = np.asarray(base_image, dtype=np.uint8)
        if model is None:
            prediction = base.astype(np.float32)
            elapsed = 0.0
        else:
            lr = record["lr"].astype(np.float32)[None] / 255
            base_float = base.astype(np.float32)[None] / 255
            started = time.perf_counter()
            prediction = model.predict(lr, base_float)[0] * 255
            elapsed = time.perf_counter() - started
        item = {
            "relative_path": record["relative_path"],
            "scene": record["scene"],
            "frame": record["frame"],
            "device": record["device"],
            "width": record["width"],
            "height": record["height"],
            "inference_s_macmini_cpu": elapsed,
            **metrics(prediction, record["hr"]),
        }
        results.append(item)
        print(f"EVAL {label} {record['relative_path']} psnr={item['rgb_psnr_db']:.5f}", flush=True)
    return {
        "frames": results,
        "mean_rgb_psnr_db": float(np.mean([item["rgb_psnr_db"] for item in results])),
        "mean_rgb_ssim_11x11": float(np.mean([item["rgb_ssim_11x11"] for item in results])),
        "mean_inference_s_macmini_cpu": float(np.mean([item["inference_s_macmini_cpu"] for item in results])),
    }


def add_baseline_comparison(result, baseline):
    baseline_by_path = {item["relative_path"]: item for item in baseline["frames"]}
    deltas = [item["rgb_psnr_db"] - baseline_by_path[item["relative_path"]]["rgb_psnr_db"] for item in result["frames"]]
    result["delta_psnr_db_vs_bicubic"] = result["mean_rgb_psnr_db"] - baseline["mean_rgb_psnr_db"]
    result["delta_ssim_vs_bicubic"] = result["mean_rgb_ssim_11x11"] - baseline["mean_rgb_ssim_11x11"]
    result["psnr_improved_frames_vs_bicubic"] = int(sum(delta > 0 for delta in deltas))
    result["psnr_regressed_frames_vs_bicubic"] = int(sum(delta < 0 for delta in deltas))
    return result


def make_sheets(records, model, out):
    for mode in ("full", "crop"):
        width, height = (320, 180) if mode == "full" else (256, 256)
        label_h = 24
        sheet = Image.new("RGB", (width * 3, (height + label_h) * len(records)), "white")
        draw = ImageDraw.Draw(sheet)
        for row, record in enumerate(records):
            base = np.asarray(Image.fromarray(record["lr"]).resize((record["width"], record["height"]), Image.Resampling.BICUBIC))
            prediction = model.predict(record["lr"].astype(np.float32)[None] / 255, base.astype(np.float32)[None] / 255)[0]
            arrays = [base, np.clip(prediction * 255, 0, 255).astype(np.uint8), record["hr"]]
            for column, (label, array) in enumerate(zip(("bicubic", "selected model", "HR"), arrays)):
                image = Image.fromarray(array)
                if mode == "full":
                    image = image.resize((width, height), Image.Resampling.BILINEAR)
                else:
                    left = max(0, (record["width"] - width) // 2)
                    top = max(0, (record["height"] - height) // 2)
                    image = image.crop((left, top, left + width, top + height))
                x, y = column * width, row * (height + label_h)
                sheet.paste(image, (x, y + label_h))
                draw.text((x + 4, y + 5), f"{record['frame']} {label}", fill="black")
        sheet.save(out / f"test-{mode}-contact.jpg", quality=94)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="HR directory or JSON/JSONL manifest; auto-uploaded by pf-node")
    parser.add_argument("--test-data", help="optional independent HR directory or manifest")
    parser.add_argument("--pretrained", help="QRISP base.npz")
    parser.add_argument("--pretrained-structure", help="QRISP base-structure.json")
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--patch", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261106)
    parser.add_argument("--test-fraction", type=float, default=0.1)
    parser.add_argument("--scratch-lr", type=float, default=0.001)
    parser.add_argument("--finetune-lr", type=float, default=0.0002)
    args = parser.parse_args()
    if args.steps < 1 or args.patch < 4 or args.batch_size < 1:
        raise ValueError("steps/batch size must be positive and patch>=4")
    if bool(args.pretrained) != bool(args.pretrained_structure):
        raise ValueError("--pretrained and --pretrained-structure are required together")
    started = time.perf_counter()
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)

    descriptors = deduplicate(discover(args.data, "train" if args.test_data else None))
    if args.test_data:
        train_desc = descriptors
        test_desc = deduplicate(discover(args.test_data, "test"))
        train_hashes = {item["hr_sha256"] for item in train_desc}
        test_desc = [item for item in test_desc if item["hr_sha256"] not in train_hashes]
        split_method = "separate_test_data_exact_duplicates_removed"
    else:
        train_desc, test_desc, split_method = assign_split(descriptors, args.test_fraction)
    if len(train_desc) < 4 or len(test_desc) < 2:
        raise RuntimeError(f"need at least 4 unique train and 2 unique test frames, got {len(train_desc)}/{len(test_desc)}")
    train_records = [decode_record(item) for item in train_desc]
    test_records = [decode_record(item) for item in test_desc]
    manifest = {
        "schema": "hgamey/doom-sr-data@1",
        "lr_generation": "Pillow bicubic HR 0.5x, RGB uint8",
        "bicubic_baseline": "Pillow bicubic LR 2x, RGB uint8",
        "split_method": split_method,
        "train_pairs": len(train_records),
        "test_pairs": len(test_records),
        "exact_duplicate_files_removed": len(discover(args.data)) - len(descriptors),
        "sources": [
            {key: item[key] for key in ("relative_path", "group", "scene", "frame", "device", "split", "hr_sha256", "hr_bytes", "width", "height")}
            for item in train_records + test_records
        ],
    }
    (out / "data-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"DATA_READY train={len(train_records)} test={len(test_records)} split={split_method}", flush=True)

    baseline = evaluate(None, test_records, "bicubic")
    summary = {
        "schema": "hgamey/doom-numpy-sr-training@1",
        "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
        "host": platform.node(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "steps_per_trained_variant": args.steps,
        "batch_size": args.batch_size,
        "lr_patch": args.patch,
        "loss": "L1 RGB [0,1]",
        "seed": args.seed,
        "train_pairs": len(train_records),
        "test_pairs": len(test_records),
        "split_method": split_method,
        "lr_is_hr_downsample": True,
        "baseline_bicubic": baseline,
        "variants": {},
        "phone_access": False,
    }
    pretrained = None
    if args.pretrained:
        structure = json.loads(Path(args.pretrained_structure).read_text())
        pretrained = SmallSR.load(args.pretrained, structure)
        result = evaluate(pretrained, test_records, "qrisp_pretrained")
        add_baseline_comparison(result, baseline)
        summary["variants"]["qrisp_pretrained"] = result
        summary["pretrained_sha256"] = sha256_file(args.pretrained)

    configs = [("doom_scratch", None, args.scratch_lr)]
    if pretrained is not None:
        configs.append(("doom_finetune", pretrained, args.finetune_lr))
    models = {}
    for name, initial, learning_rate in configs:
        if initial is None:
            model = SmallSR(channels=16, residual=True, seed=args.seed)
        else:
            model = SmallSR(channels=16, residual=True, seed=args.seed)
            model.params = {key: value.copy() for key, value in initial.params.items()}
        optimizer = Adam(model.params, lr=learning_rate)
        rng = np.random.default_rng(args.seed)
        history = []
        train_started = time.perf_counter()
        for step in range(1, args.steps + 1):
            loss = model.train_step(*random_batch(train_records, rng, args.patch, args.batch_size), optimizer)
            if not np.isfinite(loss):
                raise RuntimeError(f"{name}: non-finite loss at {step}")
            history.append(loss)
            if step == 1 or step % 500 == 0 or step == args.steps:
                print(f"TRAIN {name} step={step}/{args.steps} loss={loss:.7f} elapsed={time.perf_counter() - train_started:.2f}s", flush=True)
        training_s = time.perf_counter() - train_started
        model.save(out / f"{name}.npz")
        structure = model.structure()
        structure.update({
            "npz_file": f"{name}.npz",
            "training_steps": args.steps,
            "training_wall_s": training_s,
            "optimizer": f"Adam lr={learning_rate} beta1=0.9 beta2=0.999 grad_clip=1",
            "initialization": "QRISP base" if initial is not None else "seeded random",
            "final_100_step_l1": float(np.mean(history[-100:])),
        })
        (out / f"{name}-structure.json").write_text(json.dumps(structure, indent=2) + "\n")
        (out / f"{name}-training-history.json").write_text(json.dumps({"l1": history}, indent=2) + "\n")
        result = evaluate(model, test_records, name)
        add_baseline_comparison(result, baseline)
        result["structure"] = structure
        summary["variants"][name] = result
        models[name] = model
        print(f"VARIANT_RESULT {name} psnr={result['mean_rgb_psnr_db']:.5f} delta={result['delta_psnr_db_vs_bicubic']:+.5f} ssim={result['mean_rgb_ssim_11x11']:.6f}", flush=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    candidates = dict(models)
    if pretrained is not None:
        candidates["qrisp_pretrained"] = pretrained
    selected = max(candidates, key=lambda name: summary["variants"][name]["mean_rgb_psnr_db"])
    selected_model = candidates[selected]
    # Stable selected names let deployment use one interface across runs.
    if selected == "qrisp_pretrained":
        selected_model.save(out / "selected.npz")
        selected_structure = selected_model.structure()
    else:
        (out / "selected.npz").write_bytes((out / f"{selected}.npz").read_bytes())
        selected_structure = json.loads((out / f"{selected}-structure.json").read_text())
    selected_structure["npz_file"] = "selected.npz"
    (out / "selected-structure.json").write_text(json.dumps(selected_structure, indent=2) + "\n")
    inc_path, glsl_manifest_path = export_glsl(out / "selected.npz", out / "selected-structure.json", out / "doom_small_sr")
    selected_delta = summary["variants"][selected]["delta_psnr_db_vs_bicubic"]
    selection = {
        "selected_variant": selected,
        "model": "selected.npz",
        "structure": "selected-structure.json",
        "glsl_inc": inc_path.name,
        "glsl_manifest": glsl_manifest_path.name,
        "reason": "highest held-out mean RGB PSNR among model variants",
        "delta_psnr_db_vs_bicubic": selected_delta,
        "recommended_over_bicubic": bool(selected_delta > 0),
    }
    (out / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    summary["selected_variant"] = selected
    make_sheets(test_records[:3], selected_model, out)
    summary["wall_s"] = time.perf_counter() - started
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"SELECTED {selected}; DONE wall_s={summary['wall_s']:.2f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
