#!/usr/bin/env python3
"""Draw dependency-free SVG figures from frozen platform artifacts.

The script reads only the four files passed by the caller and writes to --out.
It never accesses a device, the network, or platform-internal job directories.
"""

import argparse
import csv
import hashlib
import html
import json
import math
import re
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path


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
        if side not in ("A", "B"):
            raise ValueError(f"invalid side: {side}")
        if side in pairs.setdefault(pair, {}):
            raise ValueError(f"duplicate arm: pair={pair}, side={side}")
        pairs[pair][side] = row
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
            temp_match = re.search(r"temperature: (\d+)", row["bat"])
            if not temp_match:
                continue
            temp = finite(temp_match.group(1), "temperature") / 10
            t = finite(row["t"], "t")
        except (KeyError, TypeError, ValueError):
            continue
        result.append({"t": t, "hz": hz, "temp": temp, "reassert": row.get("reassert") == "1"})
    if not result:
        raise ValueError(f"no complete samples in {path}")
    return result


def esc(value):
    return html.escape(str(value), quote=True)


def text(x, y, value, size=14, fill="#1f2937", anchor="start", weight="normal", rotate=None):
    transform = f' transform="rotate({rotate} {x} {y})"' if rotate is not None else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}"{transform}>{esc(value)}</text>')


def line(x1, y1, x2, y2, stroke="#94a3b8", width=1, dash=None):
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"{dashed}/>')


def rect(x, y, width, height, fill):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" fill="{fill}" rx="2"/>'


def polyline(points, stroke, width=2, dash=None):
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<polyline points="{coords}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"{dashed}/>')


def circle(x, y, radius, fill):
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}"/>'


def write_svg(path, width, height, title, body):
    content = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
               f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
               f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">\n'
               f'<title>{esc(title)}</title>\n'
               '<rect width="100%" height="100%" fill="#ffffff"/>\n'
               '<g font-family="Inter,Helvetica,Arial,sans-serif">\n'
               + "\n".join(body) + "\n</g>\n</svg>\n")
    path.write_text(content, encoding="utf-8")


def paired_plot(pairs, metric, ylabel, title, note, output):
    x = list(range(1, 7))
    a = [finite(pairs[i]["A"][metric], metric) for i in x]
    b = [finite(pairs[i]["B"][metric], metric) for i in x]
    width, height = 1200, 680
    left, top, plot_w, plot_h = 100, 105, 1040, 420
    bottom = top + plot_h
    y_max = max(a + b) * 1.18
    step = 100 if y_max > 600 else 20
    y_max = math.ceil(y_max / step) * step

    def sy(value):
        return bottom - value / y_max * plot_h

    body = [text(55, 45, title, 24, weight="bold"),
            rect(105, 66, 20, 12, "#718096"), text(134, 78, "Native (A)", 14),
            rect(280, 66, 20, 12, "#087f5b"), text(309, 78, "SelfSR + interpolation (B)", 14)]
    for tick in range(6):
        value = y_max * tick / 5
        y = sy(value)
        body += [line(left, y, left + plot_w, y, "#e2e8f0"),
                 text(left - 12, y + 5, f"{value:.0f}", 12, anchor="end")]
    group_w = plot_w / 6
    bar_w = 54
    for index, pair in enumerate(x):
        center = left + group_w * (index + .5)
        for offset, values, color in ((-bar_w / 2 - 3, a, "#718096"), (bar_w / 2 + 3, b, "#087f5b")):
            value = values[index]
            y = sy(value)
            bx = center + offset - bar_w / 2
            body += [rect(bx, y, bar_w, bottom - y, color),
                     text(center + offset, y - 7, f"{value:.1f}", 11, anchor="middle")]
        body.append(text(center, bottom + 28, str(pair), 13, anchor="middle"))
    for values, color in ((a, "#4a5568"), (b, "#087f5b")):
        mean = sum(values) / 6
        body.append(line(left, sy(mean), left + plot_w, sy(mean), color, 1.2, "7 5"))
    body += [line(left, top, left, bottom, "#64748b", 1.2),
             line(left, bottom, left + plot_w, bottom, "#64748b", 1.2),
             text(left + plot_w / 2, bottom + 58, "ABBA pair", 15, anchor="middle"),
             text(28, top + plot_h / 2, ylabel, 15, anchor="middle", rotate=-90),
             text(left + plot_w - 5, top + 20, "Dashed lines: six-arm means", 12, fill="#64748b", anchor="end")]
    wrapped = textwrap.wrap(note, 135)
    for index, part in enumerate(wrapped):
        body.append(text(60, 610 + index * 20, part, 12, fill="#4a5568"))
    write_svg(output, width, height, title, body)
    return {"a_mean": sum(a) / 6, "b_mean": sum(b) / 6, "a": a, "b": b}


def first_drop(trace):
    return next((row for row in trace if row["hz"] < 90), None)


def trace_panel(body, trace, title, panel_x, panel_y=120, panel_w=400, panel_h=390):
    max_t = max(row["t"] for row in trace)
    min_temp = math.floor(min(min(row["temp"] for row in trace), 43) - 1)
    max_temp = math.ceil(max(max(row["temp"] for row in trace), 43) + 1)
    bottom = panel_y + panel_h

    def sx(t):
        return panel_x + t / max_t * panel_w

    def sy_hz(hz):
        return bottom - (hz - 50) / 80 * panel_h

    def sy_temp(temp):
        return bottom - (temp - min_temp) / (max_temp - min_temp) * panel_h

    body.append(text(panel_x + panel_w / 2, panel_y - 24, title, 15, anchor="middle", weight="bold"))
    for hz in (60, 90, 120):
        y = sy_hz(hz)
        body += [line(panel_x, y, panel_x + panel_w, y, "#e2e8f0"),
                 text(panel_x - 10, y + 4, str(hz), 11, anchor="end")]
    for fraction, label in ((0, "0"), (.5, f"{max_t / 2:.0f}"), (1, f"{max_t:.0f}")):
        x = panel_x + panel_w * fraction
        body += [line(x, bottom, x, bottom + 5, "#64748b"),
                 text(x, bottom + 23, label, 11, anchor="middle")]
    temp_ticks = sorted(set((min_temp, 43, max_temp)))
    for temp in temp_ticks:
        body.append(text(panel_x + panel_w + 9, sy_temp(temp) + 4, f"{temp}°", 10, fill="#c2410c"))
    body += [polyline([(sx(row["t"]), sy_temp(row["temp"])) for row in trace], "#e8590c", 1.6),
             line(panel_x, sy_temp(43), panel_x + panel_w, sy_temp(43), "#e8590c", 1, "5 4")]
    for row in trace:
        if row["reassert"]:
            body.append(line(sx(row["t"]), panel_y, sx(row["t"]), bottom, "#7048e8", 1, "3 3"))
    body += [polyline([(sx(row["t"]), sy_hz(row["hz"])) for row in trace], "#1864ab", 2),
             line(panel_x, panel_y, panel_x, bottom, "#64748b", 1.1),
             line(panel_x, bottom, panel_x + panel_w, bottom, "#64748b", 1.1),
             text(panel_x + panel_w / 2, bottom + 51, "Time in phase (s)", 13, anchor="middle"),
             text(panel_x - 47, panel_y + panel_h / 2, "Hz", 12, anchor="middle", rotate=-90),
             text(panel_x + panel_w + 43, panel_y + panel_h / 2, "Battery °C", 11, fill="#c2410c", anchor="middle", rotate=90)]
    drop = first_drop(trace)
    if drop:
        x, y = sx(drop["t"]), sy_hz(drop["hz"])
        body += [circle(x, y, 5, "#c92a2a"),
                 text(min(x + 12, panel_x + panel_w - 120), y - 18,
                      f"drop {drop['t']:.0f}s / {drop['temp']:.1f}°C", 11, fill="#c92a2a")]


def mitigation_plot(crossing, reassert, warm, output):
    width, height = 1500, 650
    body = [text(55, 45, "RedMagic 120→60 Hz drop and validated mitigations", 24, weight="bold"),
            line(70, 78, 100, 78, "#1864ab", 2), text(110, 83, "Panel refresh (Hz)", 13),
            line(330, 78, 360, 78, "#e8590c", 2), text(370, 83, "Battery temperature (°C)", 13),
            line(650, 78, 680, 78, "#7048e8", 1, "3 3"), text(690, 83, "refresh_rate_mode reset", 13)]
    trace_panel(body, crossing, "A  Upward crossing: no intervention", 70)
    trace_panel(body, reassert, "B  Cool start + reassert", 555)
    trace_panel(body, warm, "C  Set panel after warm crossing", 1040)
    body.append(text(60, 610, "One-second device samples. Warm-set starts above the 43°C crossing; it has zero drop samples.", 12, fill="#4a5568"))
    write_svg(output, width, height, "RedMagic 120 to 60 Hz drop and mitigations", body)
    return {
        "crossing_first_drop": first_drop(crossing),
        "reassert_first_drop": first_drop(reassert),
        "reassert_events": sum(row["reassert"] for row in reassert),
        "warm_drops": sum(row["hz"] < 90 for row in warm),
    }


def svg_size(path):
    root = ET.parse(path).getroot()
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError(f"not an SVG document: {path}")
    width, height = int(root.attrib["width"]), int(root.attrib["height"])
    graphics = (root.findall(".//{http://www.w3.org/2000/svg}polyline")
                + root.findall(".//{http://www.w3.org/2000/svg}rect"))
    if width <= 0 or height <= 0 or len(graphics) < 2:
        raise ValueError(f"empty or invalid chart: {path}")
    return [width, height]


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
        "format": "svg",
        "dependencies": "Python standard library only",
        "sources": {
            "arms": "job:1744980d63a3:selfsr_20261028-124028/7_sr_interp/arms.jsonl",
            "crossing": "job:0f93d527296a:hold120_20261028-111539/traces/P1_hold.tsv",
            "reassert": "job:c954fcb2ae96:hold120_20261028-115822/traces/P1_hold.tsv",
            "warm": "job:c954fcb2ae96:hold120_20261028-115822/traces/P2_hold.tsv",
        },
        "seen_fps": paired_plot(
            pairs, "seen_fps", "Seen frames per second", "Six valid ABBA pairs: seen frame rate",
            "B1 is the disclosed post-window sampling-position artifact; B3 has the disclosed cold-start game stall. No arms are excluded.",
            out / "ab6_seen_fps.svg"),
        "hud_sharpness": paired_plot(
            pairs, "sharp_hud", "Laplacian variance (HUD crop)", "Six valid ABBA pairs: HUD sharpness",
            "Higher is sharper for the fixed HUD crop. Dashed lines show the six-arm means.",
            out / "ab6_hud_sharpness.svg"),
        "mitigation": mitigation_plot(crossing, reassert, warm, out / "refresh_43c_mitigation.svg"),
        "figures": [],
    }
    for name in ("ab6_seen_fps.svg", "ab6_hud_sharpness.svg", "refresh_43c_mitigation.svg"):
        path = out / name
        manifest["figures"].append({
            "path": name,
            "size": path.stat().st_size,
            "dimensions": svg_size(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    (out / "figure_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
