#!/usr/bin/env python3
"""Draw report figures from frozen platform artifacts; no device access.

Example (inside a platform experiment job):
  python3 selfsr/platform_figures.py \
    --arms "$PF_JOB_IN/arms.jsonl" \
    --crossing "$PF_JOB_IN/crossing.tsv" \
    --reassert "$PF_JOB_IN/reassert.tsv" \
    --warm "$PF_JOB_IN/warm.tsv" --out "$PF_JOB_OUT"
"""

import argparse
import csv
import hashlib
import json
import math
import re
import struct
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} is not finite: {value!r}")
    return number


def read_arms(path):
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    pairs = {}
    for row in rows:
        pair = int(row["pair"])
        side = row["side"]
        if side not in ("A", "B") or pair in pairs and side in pairs[pair]:
            raise ValueError(f"invalid or duplicate arm: pair={pair}, side={side}")
        pairs.setdefault(pair, {})[side] = row
    if sorted(pairs) != list(range(1, 7)) or any(set(arms) != {"A", "B"} for arms in pairs.values()):
        raise ValueError("expected complete A/B arms for pairs 1..6")
    for pair, arms in pairs.items():
        for side, row in arms.items():
            if row.get("dropped"):
                raise ValueError(f"dropped arm must not be plotted as valid: {side}{pair}")
            finite(row["seen_fps"], f"{side}{pair}.seen_fps")
            finite(row["sharp_hud"], f"{side}{pair}.sharp_hud")
    return pairs


def read_trace(path):
    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        raise ValueError(f"empty trace: {path}")
    result = []
    for row in rows:
        try:
            hz = finite(row["active_hz"], "active_hz")
            temp = finite(re.search(r"temperature: (\d+)", row["bat"]).group(1), "temperature") / 10
            t = finite(row["t"], "t")
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        result.append({"t": t, "hz": hz, "temp": temp, "reassert": row.get("reassert") == "1"})
    if not result:
        raise ValueError(f"no complete samples in {path}")
    return result


def paired_plot(pairs, metric, ylabel, title, note, output):
    x = list(range(1, 7))
    a = [finite(pairs[i]["A"][metric], metric) for i in x]
    b = [finite(pairs[i]["B"][metric], metric) for i in x]
    width = 0.36
    fig, ax = plt.subplots(figsize=(10, 5.6))
    bars_a = ax.bar([i - width / 2 for i in x], a, width, label="Native (A)", color="#718096")
    bars_b = ax.bar([i + width / 2 for i in x], b, width, label="SelfSR + interpolation (B)", color="#087f5b")
    ax.bar_label(bars_a, fmt="%.1f", padding=3, fontsize=9)
    ax.bar_label(bars_b, fmt="%.1f", padding=3, fontsize=9)
    ax.axhline(sum(a) / 6, color="#4a5568", linestyle="--", linewidth=1, alpha=.8)
    ax.axhline(sum(b) / 6, color="#087f5b", linestyle="--", linewidth=1, alpha=.8)
    ax.set(title=title, xlabel="ABBA pair", ylabel=ylabel, xticks=x, xticklabels=[str(i) for i in x])
    ax.grid(axis="y", alpha=.2)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left")
    ax.text(.01, -.17, note, transform=ax.transAxes, fontsize=9, color="#4a5568")
    ax.margins(y=.16)
    fig.tight_layout()
    fig.savefig(output, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return {"a_mean": sum(a) / 6, "b_mean": sum(b) / 6, "a": a, "b": b}


def first_drop(trace):
    return next((row for row in trace if row["hz"] < 90), None)


def trace_panel(ax, trace, title, show_temp=False):
    times = [row["t"] for row in trace]
    hz = [row["hz"] for row in trace]
    ax.plot(times, hz, color="#1864ab", linewidth=1.6, label="Panel refresh (Hz)")
    ax.set(title=title, xlabel="Time in phase (s)", ylabel="Refresh rate (Hz)", ylim=(50, 130))
    ax.set_yticks([60, 90, 120])
    ax.grid(alpha=.2)
    if show_temp:
        temp_ax = ax.twinx()
        temp_ax.plot(times, [row["temp"] for row in trace], color="#e8590c", linewidth=1.4, label="Battery (°C)")
        temp_ax.axhline(43, color="#e8590c", linestyle="--", linewidth=1, alpha=.8)
        temp_ax.set_ylabel("Battery temperature (°C)", color="#c2410c")
        temp_ax.tick_params(axis="y", colors="#c2410c")
        lines = ax.get_lines() + temp_ax.get_lines()[:1]
        ax.legend(lines, [line.get_label() for line in lines], loc="lower left", fontsize=8)
    drop = first_drop(trace)
    if drop:
        ax.annotate(f"drop @ {drop['t']:.0f}s, {drop['temp']:.1f}°C", (drop["t"], drop["hz"]),
                    xytext=(12, 24), textcoords="offset points", fontsize=8,
                    arrowprops={"arrowstyle": "->", "color": "#c92a2a"}, color="#c92a2a")
    for row in trace:
        if row["reassert"]:
            ax.axvline(row["t"], color="#7048e8", alpha=.22, linewidth=.8)


def mitigation_plot(crossing, reassert, warm, output):
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.3))
    trace_panel(axes[0], crossing, "A  Upward crossing: no intervention", show_temp=True)
    trace_panel(axes[1], reassert, "B  Cool start + reassert", show_temp=True)
    trace_panel(axes[2], warm, "C  Set panel after warm crossing", show_temp=True)
    fig.suptitle("RedMagic 120→60 Hz drop and the two validated mitigations", fontsize=14)
    fig.text(.01, .01, "Purple lines in B: refresh_rate_mode reset events. All panels use one-second device samples.", fontsize=9, color="#4a5568")
    fig.tight_layout(rect=(0, .04, 1, .94))
    fig.savefig(output, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return {
        "crossing_first_drop": first_drop(crossing),
        "reassert_first_drop": first_drop(reassert),
        "reassert_events": sum(row["reassert"] for row in reassert),
        "warm_drops": sum(row["hz"] < 90 for row in warm),
    }


def png_size(path):
    with open(path, "rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"not a complete PNG: {path}")
    return list(struct.unpack(">II", header[16:24]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", required=True)
    parser.add_argument("--crossing", required=True)
    parser.add_argument("--reassert", required=True)
    parser.add_argument("--warm", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pairs = read_arms(args.arms)
    crossing = read_trace(args.crossing)
    reassert = read_trace(args.reassert)
    warm = read_trace(args.warm)
    manifest = {
        "schema": "selfsr/platform-figures@1",
        "sources": {
            "arms": "job:1744980d63a3:selfsr_20261028-124028/7_sr_interp/arms.jsonl",
            "crossing": "job:0f93d527296a:hold120_20261028-111539/traces/P1_hold.tsv",
            "reassert": "job:c954fcb2ae96:hold120_20261028-115822/traces/P1_hold.tsv",
            "warm": "job:c954fcb2ae96:hold120_20261028-115822/traces/P2_hold.tsv",
        },
        "seen_fps": paired_plot(
            pairs, "seen_fps", "Seen frames per second", "Six valid ABBA pairs: seen frame rate",
            "B1 is the disclosed post-window sampling-position artifact; B3 has the disclosed cold-start game stall. No arms are excluded here.",
            out / "ab6_seen_fps.png"),
        "hud_sharpness": paired_plot(
            pairs, "sharp_hud", "Laplacian variance (HUD crop)", "Six valid ABBA pairs: HUD sharpness",
            "Higher is sharper for the fixed HUD crop. Dashed lines show the six-arm means.",
            out / "ab6_hud_sharpness.png"),
        "mitigation": mitigation_plot(crossing, reassert, warm, out / "refresh_43c_mitigation.png"),
        "figures": [],
    }
    for name in ("ab6_seen_fps.png", "ab6_hud_sharpness.png", "refresh_43c_mitigation.png"):
        path = out / name
        manifest["figures"].append({
            "path": name,
            "size": path.stat().st_size,
            "dimensions": png_size(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    (out / "figure_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
