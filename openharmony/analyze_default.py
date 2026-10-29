#!/usr/bin/env python3
"""Analyze a raw OpenHarmony SmartPerf CSV and draw a local report."""
import argparse
import csv
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TEMP_FIELDS = {
    "Battery": "Battery", "soc_thermal": "SoC thermal", "gpu": "GPU",
    "shell_back": "Shell back", "shell_frame": "Shell frame",
    "shell_front": "Shell front", "system_h": "System H", "cluster0": "Cluster 0",
}


def number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * q)))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--package", default="com.miHoYo.Yuanshen")
    parser.add_argument("--version", default="7.1.0_48052158_48145775")
    parser.add_argument("--duration-s", type=float, default=None,
                        help="analyze only the first N seconds; raw CSV remains complete")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(args.csv, encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            row = {key: number(value) for key, value in raw.items() if key}
            row["fpsJitters_raw"] = raw.get("fpsJitters", "")
            rows.append(row)
    if not rows:
        raise ValueError("no SmartPerf rows")
    total_rows = len(rows)
    start = rows[0]["timestamp"]
    if args.duration_s is not None:
        rows = [row for row in rows if (row["timestamp"] - start) / 1000 <= args.duration_s]
    if not rows:
        raise ValueError("no rows inside the requested duration")
    for row in rows:
        row["seconds"] = (row["timestamp"] - start) / 1000
    intervals = []
    for row in rows:
        intervals.extend(number(part) / 1e6 for part in row["fpsJitters_raw"].split(";;") if number(part) and number(part) > 0)
    fps = [row["fps"] for row in rows if row["fps"] is not None]
    summary = {
        "package": args.package, "version": args.version,
        "samples": len(rows), "duration_s": rows[-1]["seconds"],
        "raw_rows_total": total_rows, "requested_window_s": args.duration_s,
        "fps_source": "SmartPerf app fps",
        "fps_mean": statistics.mean(fps) if fps else None,
        "fps_p50": percentile(fps, .5), "fps_p05": percentile(fps, .05),
        "fps_min": min(fps) if fps else None, "fps_max": max(fps) if fps else None,
        "frame_interval_p50_ms": percentile(intervals, .5),
        "frame_interval_p95_ms": percentile(intervals, .95),
        "frame_interval_p99_ms": percentile(intervals, .99),
        "jank_over_50ms_pct": 100 * sum(value > 50 for value in intervals) / len(intervals) if intervals else None,
        "refreshrate_values": sorted({row["refreshrate"] for row in rows if row["refreshrate"] is not None}),
        "temperature": {},
    }
    for field in TEMP_FIELDS:
        values = [row[field] for row in rows if row.get(field) is not None]
        summary["temperature"][field] = {
            "mean": statistics.mean(values) if values else None,
            "max": max(values) if values else None,
            "last": values[-1] if values else None,
        }
    (out / "analysis.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    x = [(row["timestamp"] - start) / 60_000 for row in rows]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax = axes[0]
    ax.plot(x, [row["fps"] for row in rows], color="#1864ab", linewidth=1.5, label="Game FPS")
    ax.set(ylabel="Game FPS", title="OpenHarmony Genshin: actual app FPS and panel refresh rate")
    ax.grid(alpha=.2)
    ax2 = ax.twinx()
    ax2.step(x, [row["refreshrate"] for row in rows], where="post", color="#495057", linewidth=1.1, label="Panel refresh Hz")
    ax2.set(ylabel="Panel Hz")
    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [line.get_label() for line in lines], loc="best")
    ax = axes[1]
    for field, color in (("Battery", "#e8590c"), ("soc_thermal", "#c92a2a"), ("gpu", "#7048e8"), ("shell_back", "#2b8a3e")):
        ax.plot(x, [row[field] for row in rows], linewidth=1.2, label=TEMP_FIELDS[field], color=color)
    ax.set(ylabel="Temperature (°C)", xlabel="Time (minutes)", title="Temperature over the 20-minute default run")
    ax.grid(alpha=.2)
    ax.legend(ncol=4, fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "fps_temperature.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(intervals, bins=40, color="#1864ab", alpha=.85)
    for q, color, label in ((.5, "#2b8a3e", "p50"), (.95, "#e8590c", "p95"), (.99, "#c92a2a", "p99")):
        ax.axvline(percentile(intervals, q), color=color, linestyle="--", label=f"{label}={percentile(intervals, q):.2f} ms")
    ax.set(xlabel="Frame interval (ms)", ylabel="Frames", title="Frame interval distribution from SmartPerf fpsJitters")
    ax.grid(alpha=.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "frame_intervals.png", dpi=170, bbox_inches="tight")
    plt.close(fig)

    lines = ["# HUAWEI OpenHarmony 原神：默认设置 20 分钟", "",
             f"- 设备/游戏：`{args.package}` {args.version}，默认设置不变。",
             f"- 样本：{summary['samples']} 条，时长 {summary['duration_s']:.1f} 秒。", "",
             "| 指标 | 值 |", "|---|---:|",
             f"| FPS 均值 / 中位 / p05 | {summary['fps_mean']:.2f} / {summary['fps_p50']:.2f} / {summary['fps_p05']:.2f} |",
             f"| 帧间隔 p50 / p95 / p99 (ms) | {summary['frame_interval_p50_ms']:.2f} / {summary['frame_interval_p95_ms']:.2f} / {summary['frame_interval_p99_ms']:.2f} |",
             f"| >50ms 帧间隔占比 | {summary['jank_over_50ms_pct']:.2f}% |",
             f"| 面板刷新率 | {summary['refreshrate_values']} Hz |",
             f"| 电池平均 / 峰值 / 末值 (°C) | {summary['temperature']['Battery']['mean']:.2f} / {summary['temperature']['Battery']['max']:.2f} / {summary['temperature']['Battery']['last']:.2f} |",
             f"| SoC 平均 / 峰值 / 末值 (°C) | {summary['temperature']['soc_thermal']['mean']:.2f} / {summary['temperature']['soc_thermal']['max']:.2f} / {summary['temperature']['soc_thermal']['last']:.2f} |",
             f"| GPU 平均 / 峰值 / 末值 (°C) | {summary['temperature']['gpu']['mean']:.2f} / {summary['temperature']['gpu']['max']:.2f} / {summary['temperature']['gpu']['last']:.2f} |",
             "", "原始 CSV 与 `analysis.json` 保留；图由本机生成。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
