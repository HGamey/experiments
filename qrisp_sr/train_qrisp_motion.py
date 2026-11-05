#!/usr/bin/env python3
"""Train 3-channel and motion-aligned 6-channel QRISP SR models."""
import argparse
import json
import os
from pathlib import Path
import platform
import time

import numpy as np
from PIL import Image, ImageDraw

from numpy_sr import Adam, SmallSR
from train_qrisp import (
    SCENES,
    load_local_pilot,
    load_remote_scene,
    make_sheets,
    metrics,
)


def add_local_history(records):
    # Mechanism smoke only: no consecutive local frames are claimed.
    for record in records:
        record["history"] = np.zeros(record["lr"].shape, dtype=np.float32)
        record["history_valid"] = np.zeros(record["lr"].shape[:2], dtype=bool)
        record["history_prepare_s"] = 0.0
        record["source"]["has_real_history"] = False


def random_batch(records, rng, motion, batch_size=8, patch=32):
    lrs, histories, bases, targets = [], [], [], []
    for _ in range(batch_size):
        record = records[int(rng.integers(len(records)))]
        y = int(rng.integers(record["lr"].shape[0] - patch + 1))
        x = int(rng.integers(record["lr"].shape[1] - patch + 1))
        lr = record["lr"][y : y + patch, x : x + patch].astype(np.float32) / 255
        if motion:
            history = record["history"][y : y + patch, x : x + patch]
            lr = np.concatenate((lr, history), axis=2)
        lrs.append(lr)
        bases.append(record["base"][2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)])
        targets.append(record["hr"][2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)])
    return (
        np.stack(lrs).astype(np.float32),
        np.stack(bases).astype(np.float32) / 255,
        np.stack(targets).astype(np.float32) / 255,
    )


def evaluate(model, records, motion):
    frames = []
    for record in records:
        lr = record["lr"].astype(np.float32)[None] / 255
        if motion:
            lr = np.concatenate((lr, record["history"][None]), axis=3)
        base = record["base"].astype(np.float32)[None] / 255
        started = time.perf_counter()
        prediction = model.predict(lr, base)[0] * 255
        elapsed = time.perf_counter() - started
        item = {
            "scene": record["scene"],
            "segment": record["segment"],
            "frame": record["frame"],
            "has_real_history": bool(record["source"]["has_real_history"]),
            "model_inference_s_macmini_cpu": elapsed,
            "history_prepare_s_macmini_cpu": record["history_prepare_s"],
            **metrics(prediction, record["hr"]),
        }
        frames.append(item)
        record.setdefault("predictions", {})[str(id(model))] = np.clip(prediction, 0, 255).astype(np.uint8)
    return frames


def bicubic_frames(records):
    frames = []
    for record in records:
        started = time.perf_counter()
        resized = Image.fromarray(record["lr"]).resize((1920, 1080), Image.Resampling.BICUBIC)
        elapsed = time.perf_counter() - started
        frames.append({
            "scene": record["scene"],
            "segment": record["segment"],
            "frame": record["frame"],
            "has_real_history": bool(record["source"]["has_real_history"]),
            "bicubic_s_macmini_cpu": elapsed,
            **metrics(np.asarray(resized), record["hr"]),
        })
    return frames


def metric_means(frames):
    if not frames:
        return {"mean_rgb_psnr_db": None, "mean_rgb_ssim_11x11": None}
    return {
        "mean_rgb_psnr_db": float(np.mean([x["rgb_psnr_db"] for x in frames])),
        "mean_rgb_ssim_11x11": float(np.mean([x["rgb_ssim_11x11"] for x in frames])),
    }


def summarize(frames, model=True):
    real = [x for x in frames if x["has_real_history"]]
    result = {"frames": frames, **metric_means(frames), "with_real_history": metric_means(real)}
    if model:
        result["mean_model_inference_s_macmini_cpu"] = float(np.mean([x["model_inference_s_macmini_cpu"] for x in frames]))
        result["mean_history_prepare_s_macmini_cpu"] = float(np.mean([x["history_prepare_s_macmini_cpu"] for x in real])) if real else 0.0
    else:
        result["mean_bicubic_s_macmini_cpu"] = float(np.mean([x["bicubic_s_macmini_cpu"] for x in frames]))
    return result


def add_deltas(result, baseline):
    result["delta_psnr_db_vs_bicubic"] = result["mean_rgb_psnr_db"] - baseline["mean_rgb_psnr_db"]
    result["delta_ssim_vs_bicubic"] = result["mean_rgb_ssim_11x11"] - baseline["mean_rgb_ssim_11x11"]
    if result["with_real_history"]["mean_rgb_psnr_db"] is not None:
        result["delta_psnr_db_with_real_history_vs_bicubic"] = (
            result["with_real_history"]["mean_rgb_psnr_db"] - baseline["with_real_history"]["mean_rgb_psnr_db"]
        )
        result["delta_ssim_with_real_history_vs_bicubic"] = (
            result["with_real_history"]["mean_rgb_ssim_11x11"] - baseline["with_real_history"]["mean_rgb_ssim_11x11"]
        )


def alignment_sheet(records, out):
    candidates = [x for x in records if "previous_lr" in x]
    if not candidates:
        return
    record = candidates[0]
    arrays = [
        np.clip(record["previous_lr"] * 255, 0, 255).astype(np.uint8),
        np.clip(record["history"] * 255, 0, 255).astype(np.uint8),
        record["lr"],
    ]
    labels = ["previous LR", "MV-aligned previous", "current LR"]
    left, top = (960 - 256) // 2, (540 - 256) // 2
    sheet = Image.new("RGB", (256 * 3, 256 + 24), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, array) in enumerate(zip(labels, arrays)):
        crop = Image.fromarray(array).crop((left, top, left + 256, top + 256))
        sheet.paste(crop, (index * 256, 24))
        draw.text((index * 256 + 4, 5), f"{record['segment']}/{record['frame']} {label}", fill="black")
    sheet.save(out / "motion-alignment-crop.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--variants", default="base_3ch,motion_6ch")
    parser.add_argument("--local-pilot")
    parser.add_argument("--train-pairs-per-scene", type=int, default=16)
    parser.add_argument("--test-pairs", type=int, default=6)
    args = parser.parse_args()
    names = args.variants.split(",")
    if args.steps < 1 or any(name not in ("base_3ch", "motion_6ch") for name in names):
        raise ValueError("steps>=1 and variants must be base_3ch,motion_6ch")
    started = time.perf_counter()
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)

    if args.local_pilot:
        train_records, test_records = load_local_pilot(args.local_pilot)
        add_local_history(train_records + test_records)
        data_report = {"mode": "local_motion_mechanism_smoke", "scenes": []}
    else:
        train_records, test_records = [], []
        data_report = {"mode": "official_remote_ranges_motion", "scenes": []}
        for scene in ("suntemplebush", "scififacility", "scifibasenightstartstop"):
            records, scene_report = load_remote_scene(scene, args.train_pairs_per_scene, motion=True)
            train_records.extend(records)
            data_report["scenes"].append(scene_report)
        test_records, scene_report = load_remote_scene("seaport", args.test_pairs, motion=True)
        data_report["scenes"].append(scene_report)
    all_records = train_records + test_records
    data_report.update({
        "train_pairs": len(train_records),
        "test_pairs": len(test_records),
        "real_history_train_pairs": sum(bool(x["source"]["has_real_history"]) for x in train_records),
        "real_history_test_pairs": sum(bool(x["source"]["has_real_history"]) for x in test_records),
        "decoded_array_bytes": sum(
            sum(record[key].nbytes for key in ("lr", "hr", "base", "history", "history_valid", "previous_lr") if key in record)
            for record in all_records
        ),
        "raw_archives_written": 0,
        "sources": [record["source"] | {"scene": record["scene"], "segment": record["segment"], "frame": record["frame"]} for record in all_records],
    })
    (out / "data-manifest.json").write_text(json.dumps(data_report, indent=2) + "\n")
    print(f"DATA_READY train={len(train_records)} test={len(test_records)} real_history_test={data_report['real_history_test_pairs']} array_bytes={data_report['decoded_array_bytes']}", flush=True)
    alignment_sheet(test_records, out)

    baseline = summarize(bicubic_frames(test_records), model=False)
    summary = {
        "schema": "hgamey/qrisp-numpy-motion-sr-training@1",
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
        "real_history_train_pairs": data_report["real_history_train_pairs"],
        "real_history_test_pairs": data_report["real_history_test_pairs"],
        "history_input": "previous native 540p LR aligned by current MotionVectorsMipBiasMinus1 R/G, current_pixel - MV*(width,height); zeros without real history",
        "timing_scope": "model inference excludes bicubic and history preparation; history preparation includes PNG/EXR decode and warp, excludes network",
        "baseline_bicubic": baseline,
        "variants": {},
        "raw_archives_written": 0,
        "phone_access": False,
    }
    models = {}
    for name in names:
        motion = name == "motion_6ch"
        model = SmallSR(channels=16, residual=True, seed=20261106, input_channels=6 if motion else 3)
        optimizer = Adam(model.params)
        rng = np.random.default_rng(20261106)
        history = []
        t0 = time.perf_counter()
        for step in range(1, args.steps + 1):
            loss = model.train_step(*random_batch(train_records, rng, motion), optimizer)
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
        result = summarize(evaluate(model, test_records, motion))
        add_deltas(result, baseline)
        result["structure"] = structure
        summary["variants"][name] = result
        models[name] = model
        real_psnr = result["with_real_history"]["mean_rgb_psnr_db"]
        real_text = f"{real_psnr:.5f}" if real_psnr is not None else "none"
        print(f"VARIANT_RESULT {name} real_history_psnr={real_text} all6_psnr={result['mean_rgb_psnr_db']:.5f} train_s={train_s:.2f}", flush=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if (
        "base_3ch" in summary["variants"]
        and "motion_6ch" in summary["variants"]
        and summary["variants"]["base_3ch"]["with_real_history"]["mean_rgb_psnr_db"] is not None
    ):
        base_result = summary["variants"]["base_3ch"]
        motion_result = summary["variants"]["motion_6ch"]
        summary["motion_vs_base_with_real_history"] = {
            "delta_psnr_db": motion_result["with_real_history"]["mean_rgb_psnr_db"] - base_result["with_real_history"]["mean_rgb_psnr_db"],
            "delta_ssim": motion_result["with_real_history"]["mean_rgb_ssim_11x11"] - base_result["with_real_history"]["mean_rgb_ssim_11x11"],
            "extra_parameters": motion_result["structure"]["parameters"] - base_result["structure"]["parameters"],
            "extra_macs_per_lr_pixel": motion_result["structure"]["macs_per_lr_pixel"] - base_result["structure"]["macs_per_lr_pixel"],
        }
        print(f"MOTION_VS_BASE {json.dumps(summary['motion_vs_base_with_real_history'])}", flush=True)
    real_records = [x for x in test_records if x["source"]["has_real_history"]]
    if real_records:
        make_sheets(real_records, models, out)
    summary["wall_s"] = time.perf_counter() - started
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"DONE wall_s={summary['wall_s']:.2f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
