#!/usr/bin/env python3
"""Run locally: plot HONOR refresh AB task CSVs and apply the frozen rule."""
import argparse
import csv
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ARMS = (("smart", "Smart", "#1864ab"), ("high", "High", "#c92a2a"))


def number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def read_arm(indir, arm):
    with open(indir / f"{arm}_samples.csv", encoding="utf-8", newline="") as handle:
        rows = [{key: number(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    summary = json.loads((indir / f"{arm}_analysis.json").read_text(encoding="utf-8"))
    if not rows:
        raise ValueError(f"no samples for {arm}")
    start = rows[0]["wall"]
    for row in rows:
        row["minutes"] = (row["wall"] - start) / 60
    return rows, summary


def rolling_median(values, window=9):
    result = []
    for index in range(len(values)):
        neighborhood = [value for value in values[max(0, index - window // 2):index + window // 2 + 1]
                        if value is not None]
        result.append(statistics.median(neighborhood) if neighborhood else None)
    return result


def plot_series(indir, outdir):
    data = {arm: read_arm(indir, arm) for arm, _, _ in ARMS}
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    for ax, (arm, label, color) in zip(axes, ARMS):
        rows, _ = data[arm]
        x = [row["minutes"] for row in rows]
        fps = [row["fps"] for row in rows]
        ax.plot(x, fps, color=color, alpha=.18, linewidth=.7, label="Game FPS (samples)")
        ax.plot(x, rolling_median(fps), color=color, linewidth=1.8, label="Game FPS (rolling median)")
        ax.set(ylabel="Game FPS", title=f"{label}: actual game-layer presentation", ylim=(0, 200))
        ax.grid(alpha=.2)
        active = [row["active_hz"] for row in rows]
        ax2 = ax.twinx()
        ax2.step(x, active, where="post", color="#495057", linewidth=1, alpha=.8, label="Panel active Hz")
        ax2.set(ylabel="Panel Hz", ylim=(0, 200), yticks=[0, 60, 90, 120, 185, 200])
        lines = ax.get_lines() + ax2.get_lines()
        ax.legend(lines, [line.get_label() for line in lines], loc="lower left", fontsize=8, ncol=3)
    axes[-1].set_xlabel("Time in arm (minutes)")
    fig.suptitle("HONOR Genshin: game FPS is not panel refresh rate", fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, .96))
    fig.savefig(outdir / "fps_refresh.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    for ax, (arm, label, color) in zip(axes, ARMS):
        rows, _ = data[arm]
        x = [row["minutes"] for row in rows]
        for field, name, line_color in (("battery_hal_c", "Battery", "#e8590c"),
                                        ("skin_c", "Skin", "#c92a2a"),
                                        ("cpu_max_c", "CPU max", "#1864ab"),
                                        ("gpu_max_c", "GPU max", "#7048e8")):
            ax.plot(x, [row[field] for row in rows], label=name, color=line_color, linewidth=1.3)
        ax.axhline(37, color="#c92a2a", linestyle="--", linewidth=.8, alpha=.6, label="Skin light threshold 37°C")
        ax.set(ylabel="Temperature (°C)", title=f"{label}: Thermal HAL current temperatures", ylim=(25, 65))
        ax.grid(alpha=.2)
        ax.legend(loc="upper left", fontsize=8, ncol=3)
    axes[-1].set_xlabel("Time in arm (minutes)")
    fig.suptitle("HONOR Genshin: temperature over each 20-minute arm", fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, .96))
    fig.savefig(outdir / "temperature.png", dpi=170, bbox_inches="tight")
    plt.close(fig)
    return data


def decide(data):
    smart, high = data["smart"][1], data["high"][1]
    result = {"valid": bool(smart["comparison_valid"] and high["comparison_valid"])}
    if not result["valid"]:
        result.update({"recommendation": None, "reason": "At least one arm has insufficient valid foreground/FPS samples."})
        return result
    fps_gain = (high["fps_p50"] / smart["fps_p50"] - 1) * 100
    p05_drop = (1 - high["fps_p05"] / smart["fps_p05"]) * 100
    skin_delta = high["skin_c"]["max"] - smart["skin_c"]["max"]
    thermal_worse = high["thermal_status_max"] > smart["thermal_status_max"]
    high_ok = fps_gain >= 10 and p05_drop <= 10 and skin_delta <= 3 and not thermal_worse
    result.update({
        "recommendation": "高" if high_ok else "智能",
        "fps_p50_gain_pct": fps_gain, "fps_p05_drop_pct": p05_drop,
        "skin_max_delta_c": skin_delta, "thermal_status_worse": thermal_worse,
        "reason": (f"High FPS median gain {fps_gain:.1f}% (need ≥10%), p05 drop {p05_drop:.1f}% (allow ≤10%), "
                   f"skin peak delta {skin_delta:+.1f}°C (allow ≤3°C), thermal worse={thermal_worse}."),
    })
    return result


def write_report(data, decision, outdir):
    lines = ["# HONOR 原神：智能 vs 高（各 20 分钟）", "",
             "| 指标 | 智能 | 高 |", "|---|---:|---:|"]
    for label, getter in (
        ("有效样本比例", lambda s: f"{100*s['valid_ratio']:.1f}%"),
        ("FPS 均值", lambda s: f"{s['fps_mean']:.2f}"),
        ("FPS 中位数", lambda s: f"{s['fps_p50']:.2f}"),
        ("FPS p05", lambda s: f"{s['fps_p05']:.2f}"),
        ("帧间隔 p95 (ms)", lambda s: f"{s['frame_interval_p95_ms']:.2f}"),
        ("Skin 平均/峰值 (°C)", lambda s: f"{s['skin_c']['mean']:.2f} / {s['skin_c']['max']:.2f}"),
        ("电池平均/峰值 (°C)", lambda s: f"{s['battery_hal_c']['mean']:.2f} / {s['battery_hal_c']['max']:.2f}"),
        ("Thermal Status 最大值", lambda s: str(s["thermal_status_max"])),
    ):
        lines.append(f"| {label} | {getter(data['smart'][1])} | {getter(data['high'][1])} |")
    lines += ["", f"**推荐：{decision['recommendation'] or '无法推荐（有效样本不足）'}**", "", decision["reason"], "",
              "图：`fps_refresh.png`（游戏 FPS 与面板 Hz）、`temperature.png`（HAL 当前温度）。"]
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (outdir / "recommendation.json").write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    data = plot_series(Path(args.indir), outdir)
    decision = decide(data)
    write_report(data, decision, outdir)
    print(json.dumps(decision, indent=2))


if __name__ == "__main__":
    main()
