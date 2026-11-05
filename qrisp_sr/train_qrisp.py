#!/usr/bin/env python3
"""Train and ablate small NumPy SR models on exact QRISP rendered pairs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import time
import zipfile

import numpy as np
from PIL import Image, ImageDraw

from extract_pilot import read_entry
from numpy_sr import Adam, SmallSR
from remote_zip import HTTPRangeFile


BASE_URL = "https://softwarecenter.qualcomm.com/api/download/software/dataset/AIDataset/Qualcomm_Rasterized_Images/"
SCENES = {
    "suntemplebush": ("QRISP/SunTempleBush", "train"),
    "scififacility": ("QRISP/ScifiFacility", "train"),
    "scifibasenightstartstop": ("QRISP/ScifiBaseNightStartStop", "train"),
    "seaport": ("QRISP/TestSet/SeaPort", "test"),
}
VARIANTS = {
    "base": {"channels": 16, "residual": True},
    "no_residual": {"channels": 16, "residual": False},
    "half_channels": {"channels": 8, "residual": True},
    "double_channels": {"channels": 32, "residual": True},
}


def decode(payload):
    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGB")
        image.load()
    if image.size != (1920, 1080) and image.size != (960, 540):
        raise RuntimeError(f"unexpected image dimensions: {image.size}")
    return image


def make_record(scene, segment, frame, lr_payload, hr_payload, lr_path, hr_path):
    lr_image = decode(lr_payload)
    hr_image = decode(hr_payload)
    if lr_image.size != (960, 540) or hr_image.size != (1920, 1080):
        raise RuntimeError("LR/HR must be 960x540 and 1920x1080")
    base_image = lr_image.resize(hr_image.size, Image.Resampling.BICUBIC)
    return {
        "scene": scene,
        "segment": segment,
        "frame": frame,
        "lr": np.asarray(lr_image, dtype=np.uint8),
        "hr": np.asarray(hr_image, dtype=np.uint8),
        "base": np.asarray(base_image, dtype=np.uint8),
        "source": {
            "lr_path": lr_path,
            "hr_path": hr_path,
            "lr_sha256": hashlib.sha256(lr_payload).hexdigest(),
            "hr_sha256": hashlib.sha256(hr_payload).hexdigest(),
        },
    }


def choose_indices(items, count):
    if len(items) < count:
        raise RuntimeError(f"need {count} items, found {len(items)}")
    return sorted(set(int(x) for x in np.linspace(0, len(items) - 1, count)))


def load_remote_scene(scene, pair_count):
    folder, split = SCENES[scene]
    url = BASE_URL + scene + ".zip"
    remote = HTTPRangeFile(url)
    records = []
    with zipfile.ZipFile(remote) as archive:
        infos = {item.filename: item for item in archive.infolist() if not item.is_dir()}
        lr_prefix = f"{folder}/540p/MipBiasMinus1/"
        hr_prefix = f"{folder}/1080p/Enhanced/"
        segments = sorted(
            {
                name[len(lr_prefix) :].split("/")[0]
                for name in infos
                if name.startswith(lr_prefix) and name.endswith(".png")
            }
        )
        if split == "test":
            segment = segments[0]
            frames = sorted(
                name[len(lr_prefix) + len(segment) + 1 : -4]
                for name in infos
                if name.startswith(f"{lr_prefix}{segment}/") and name.endswith(".png")
            )
            selected = [(segment, frames[i]) for i in choose_indices(frames, pair_count)]
        else:
            selected = []
            per_segment = pair_count // 8
            for segment_index in choose_indices(segments, 8):
                segment = segments[segment_index]
                frames = sorted(
                    name[len(lr_prefix) + len(segment) + 1 : -4]
                    for name in infos
                    if name.startswith(f"{lr_prefix}{segment}/") and name.endswith(".png")
                )
                for frame_index in choose_indices(frames, per_segment):
                    selected.append((segment, frames[frame_index]))
        if len(selected) != pair_count:
            raise RuntimeError(f"{scene}: selected {len(selected)} pairs, wanted {pair_count}")
        for index, (segment, frame) in enumerate(selected):
            lr_path = f"{lr_prefix}{segment}/{frame}.png"
            hr_path = f"{hr_prefix}{segment}/{frame}.png"
            if lr_path not in infos or hr_path not in infos:
                raise RuntimeError(f"missing pair: {lr_path} / {hr_path}")
            record = make_record(
                scene,
                segment,
                frame,
                read_entry(remote, infos[lr_path]),
                read_entry(remote, infos[hr_path]),
                lr_path,
                hr_path,
            )
            record["source"]["archive_url"] = url
            records.append(record)
            print(f"DATA {scene} {index + 1}/{pair_count} {segment}/{frame}", flush=True)
    return records, {
        "scene": scene,
        "archive_url": url,
        "archive_bytes": remote.size,
        "http_attempts": remote.attempts,
        "http_bytes_read": remote.bytes_read,
    }


def load_local_pilot(directory):
    records = []
    for lr_path in sorted(Path(directory).glob("frames/*-lr.png")):
        stem = lr_path.name[:-7]
        hr_path = lr_path.with_name(stem + "-hr.png")
        segment = stem.split("-")[1]
        frame = stem.split("-")[3]
        record = make_record(
            "suntemplebush",
            segment,
            frame,
            lr_path.read_bytes(),
            hr_path.read_bytes(),
            lr_path.name,
            hr_path.name,
        )
        records.append(record)
    if len(records) != 8:
        raise RuntimeError(f"local pilot requires 8 pairs, found {len(records)}")
    return records[:6], records[6:]


def gaussian_blur(x):
    radius = 5
    kernel = np.exp(-np.arange(-radius, radius + 1) ** 2 / (2 * 1.5**2)).astype(np.float32)
    kernel /= kernel.sum()
    padded = np.pad(x, ((0, 0), (radius, radius), (0, 0)), mode="reflect")
    horizontal = np.zeros_like(x, dtype=np.float32)
    for index, weight in enumerate(kernel):
        horizontal += weight * padded[:, index : index + x.shape[1], :]
    padded = np.pad(horizontal, ((radius, radius), (0, 0), (0, 0)), mode="reflect")
    result = np.zeros_like(horizontal, dtype=np.float32)
    for index, weight in enumerate(kernel):
        result += weight * padded[index : index + x.shape[0], :, :]
    return result


def metrics(pred, target):
    pred = np.clip(pred, 0, 255).astype(np.float32)
    target = target.astype(np.float32)
    mse = float(np.mean((pred - target) ** 2))
    psnr = float("inf") if mse == 0 else float(10 * np.log10(255.0**2 / mse))
    mu_x = gaussian_blur(pred)
    mu_y = gaussian_blur(target)
    var_x = gaussian_blur(pred * pred) - mu_x * mu_x
    var_y = gaussian_blur(target * target) - mu_y * mu_y
    covariance = gaussian_blur(pred * target) - mu_x * mu_y
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    ssim_map = ((2 * mu_x * mu_y + c1) * (2 * covariance + c2)) / (
        (mu_x * mu_x + mu_y * mu_y + c1) * (var_x + var_y + c2)
    )
    return {"rgb_psnr_db": psnr, "rgb_ssim_11x11": float(np.mean(ssim_map[5:-5, 5:-5])), "rgb_mse": mse}


def evaluate(model, records):
    results = []
    for record in records:
        lr = record["lr"].astype(np.float32)[None] / 255
        base = record["base"].astype(np.float32)[None] / 255
        started = time.perf_counter()
        prediction = model.predict(lr, base)[0] * 255
        elapsed = time.perf_counter() - started
        item = {
            "scene": record["scene"],
            "segment": record["segment"],
            "frame": record["frame"],
            "inference_s_macmini_cpu": elapsed,
            **metrics(prediction, record["hr"]),
        }
        results.append(item)
        record.setdefault("predictions", {})[str(id(model))] = np.clip(prediction, 0, 255).astype(np.uint8)
    return {
        "frames": results,
        "mean_rgb_psnr_db": float(np.mean([x["rgb_psnr_db"] for x in results])),
        "mean_rgb_ssim_11x11": float(np.mean([x["rgb_ssim_11x11"] for x in results])),
        "mean_inference_s_macmini_cpu": float(np.mean([x["inference_s_macmini_cpu"] for x in results])),
    }


def random_batch(records, rng, batch_size=8, patch=32):
    lrs, bases, targets = [], [], []
    for _ in range(batch_size):
        record = records[int(rng.integers(len(records)))]
        y = int(rng.integers(record["lr"].shape[0] - patch + 1))
        x = int(rng.integers(record["lr"].shape[1] - patch + 1))
        lrs.append(record["lr"][y : y + patch, x : x + patch])
        bases.append(record["base"][2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)])
        targets.append(record["hr"][2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)])
    return tuple(np.stack(values).astype(np.float32) / 255 for values in (lrs, bases, targets))


def make_sheets(records, variant_models, out):
    names = list(variant_models)
    for mode in ("full", "crop"):
        width, height = (320, 180) if mode == "full" else (256, 256)
        label_h = 24
        columns = ["bicubic"] + names + ["HR"]
        sheet = Image.new("RGB", (width * len(columns), (height + label_h) * len(records)), "white")
        draw = ImageDraw.Draw(sheet)
        for row, record in enumerate(records):
            arrays = [record["base"]] + [record["predictions"][str(id(variant_models[name]))] for name in names] + [record["hr"]]
            for column, (label, array) in enumerate(zip(columns, arrays)):
                image = Image.fromarray(array)
                if mode == "full":
                    image = image.resize((width, height), Image.Resampling.BILINEAR)
                else:
                    left, top = (1920 - 512) // 2, (1080 - 512) // 2
                    image = image.crop((left, top, left + 512, top + 512)).resize(
                        (width, height), Image.Resampling.BILINEAR
                    )
                x, y = column * width, row * (height + label_h)
                sheet.paste(image, (x, y + label_h))
                draw.text((x + 4, y + 5), f"{record['segment']}/{record['frame']} {label}", fill="black")
        sheet.save(out / f"test-{mode}-contact.jpg", quality=92)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--variants", default="base,no_residual,half_channels,double_channels")
    parser.add_argument("--local-pilot")
    parser.add_argument("--train-pairs-per-scene", type=int, default=16)
    parser.add_argument("--test-pairs", type=int, default=6)
    args = parser.parse_args()
    if args.steps < 1 or args.train_pairs_per_scene < 8 or args.train_pairs_per_scene % 8:
        raise ValueError("steps>=1 and train pairs per scene must be a positive multiple of 8")
    names = args.variants.split(",")
    if not names or any(name not in VARIANTS for name in names):
        raise ValueError(f"unknown variants: {names}")
    started = time.perf_counter()
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    if args.local_pilot:
        train_records, test_records = load_local_pilot(args.local_pilot)
        data_report = {"mode": "local_pilot", "train_pairs": len(train_records), "test_pairs": len(test_records)}
    else:
        train_records, test_records, data_report = [], [], {"mode": "official_remote_ranges", "scenes": []}
        for scene in ("suntemplebush", "scififacility", "scifibasenightstartstop"):
            records, scene_report = load_remote_scene(scene, args.train_pairs_per_scene)
            train_records.extend(records)
            data_report["scenes"].append(scene_report)
        test_records, scene_report = load_remote_scene("seaport", args.test_pairs)
        data_report["scenes"].append(scene_report)
        data_report.update(train_pairs=len(train_records), test_pairs=len(test_records))
    data_report["decoded_array_bytes"] = sum(
        sum(record[key].nbytes for key in ("lr", "hr", "base")) for record in train_records + test_records
    )
    data_report["raw_archives_written"] = 0
    data_report["sources"] = [record["source"] | {"scene": record["scene"], "segment": record["segment"], "frame": record["frame"]} for record in train_records + test_records]
    (out / "data-manifest.json").write_text(json.dumps(data_report, indent=2) + "\n")
    print(f"DATA_READY train={len(train_records)} test={len(test_records)} array_bytes={data_report['decoded_array_bytes']}", flush=True)

    baseline_metrics = []
    for record in test_records:
        t0 = time.perf_counter()
        resized = Image.fromarray(record["lr"]).resize((1920, 1080), Image.Resampling.BICUBIC)
        elapsed = time.perf_counter() - t0
        baseline_metrics.append({"scene": record["scene"], "segment": record["segment"], "frame": record["frame"], "inference_s_macmini_cpu": elapsed, **metrics(np.asarray(resized), record["hr"])})
    baseline = {
        "frames": baseline_metrics,
        "mean_rgb_psnr_db": float(np.mean([x["rgb_psnr_db"] for x in baseline_metrics])),
        "mean_rgb_ssim_11x11": float(np.mean([x["rgb_ssim_11x11"] for x in baseline_metrics])),
        "mean_inference_s_macmini_cpu": float(np.mean([x["inference_s_macmini_cpu"] for x in baseline_metrics])),
    }
    summary = {
        "schema": "hgamey/qrisp-numpy-sr-training@1",
        "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
        "host": platform.node(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "steps_per_variant": args.steps,
        "batch_size": 8,
        "lr_patch": 32,
        "loss": "L1 RGB [0,1]",
        "optimizer": "Adam lr=0.001 beta1=0.9 beta2=0.999 grad_clip=1",
        "seed": 20261106,
        "train_pairs": len(train_records),
        "test_pairs": len(test_records),
        "test_scene": "official held-out SeaPort" if not args.local_pilot else "SunTempleBush segments 0006-0007",
        "baseline_bicubic": baseline,
        "variants": {},
        "raw_archives_written": 0,
        "phone_access": False,
    }
    models = {}
    for name in names:
        config = VARIANTS[name]
        model = SmallSR(**config, seed=20261106)
        optimizer = Adam(model.params)
        rng = np.random.default_rng(20261106)
        history = []
        t0 = time.perf_counter()
        for step in range(1, args.steps + 1):
            loss = model.train_step(*random_batch(train_records, rng), optimizer)
            if not np.isfinite(loss):
                raise RuntimeError(f"{name}: non-finite loss at step {step}")
            history.append(loss)
            if step == 1 or step % 100 == 0 or step == args.steps:
                print(f"TRAIN {name} step={step}/{args.steps} loss={loss:.7f} elapsed={time.perf_counter() - t0:.2f}s", flush=True)
        train_s = time.perf_counter() - t0
        model_path = out / f"{name}.npz"
        model.save(model_path)
        structure = model.structure()
        structure.update({
            "npz_file": model_path.name,
            "npz_bytes": model_path.stat().st_size,
            "training_wall_s": train_s,
            "training_steps_per_s": args.steps / train_s,
            "final_100_step_l1": float(np.mean(history[-100:])),
        })
        (out / f"{name}-structure.json").write_text(json.dumps(structure, indent=2) + "\n")
        (out / f"{name}-training-history.json").write_text(json.dumps({"l1": history}, indent=2) + "\n")
        result = evaluate(model, test_records)
        result["delta_psnr_db_vs_bicubic"] = result["mean_rgb_psnr_db"] - baseline["mean_rgb_psnr_db"]
        result["delta_ssim_vs_bicubic"] = result["mean_rgb_ssim_11x11"] - baseline["mean_rgb_ssim_11x11"]
        result["structure"] = structure
        summary["variants"][name] = result
        models[name] = model
        print(f"VARIANT_RESULT {name} psnr={result['mean_rgb_psnr_db']:.5f} delta={result['delta_psnr_db_vs_bicubic']:+.5f} ssim={result['mean_rgb_ssim_11x11']:.6f} train_s={train_s:.2f}", flush=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    make_sheets(test_records, models, out)
    summary["wall_s"] = time.perf_counter() - started
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"DONE wall_s={summary['wall_s']:.2f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
