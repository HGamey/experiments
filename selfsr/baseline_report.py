#!/usr/bin/env python3
"""baseline_report.py — 汇总 baseline_cmp.sh 各组的 report.json, 出对比表与图 (本机跑)。

    python3 -I selfsr/baseline_report.py --job-out <srbaseline_目录> [--out <报告目录>]
读 hwc_bilinear/ sr_lowres/ srip_g7/ 三个子目录的 report.json (逐臂明细另读 arms.jsonl),
逐组列: 有效配对数与每指标 A/B 均值、diff%、p; 画逐指标 A/B 条形图 + 逐臂 seen_fps 散点。
"""
import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import glob as _glob  # noqa: E402
_cjk_done = False
for _pat in ("~/.local/share/fonts/gosim/NotoSansSC*.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK*.ttc"):
    for _f in _glob.glob(os.path.expanduser(_pat)):
        try:
            fm.fontManager.addfont(_f)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=_f).get_name(), "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            _cjk_done = True
            break
        except Exception:
            continue
    if _cjk_done:
        break

PAIRS = {"hwc_bilinear": ("native", "hwc:0.56 (低分+双线性)"),
         "sr_lowres": ("native", "sr:0.56 (低分+我们的超分)"),
         "srip_g7": ("native", "srip 第7组 (超分+插帧)")}
# 键 -> (中文名, 单位在名字里)
METRICS = [("seen_fps", "上屏帧率 fps"), ("game_fps", "游戏帧率 fps"),
           ("game_frame_p95_ms", "游戏帧时间 p95 ms"), ("gpu_active_ms", "GPU 活动时长 ms"),
           ("sharp_full", "整屏锐度"), ("sharp_hud", "HUD 锐度"),
           ("bat_temp_c", "电池温度 °C"), ("power_mw", "整机功耗 mW")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job-out", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or a.job_out
    os.makedirs(out, exist_ok=True)
    rows = []
    for d, (an, bn) in PAIRS.items():
        rp = os.path.join(a.job_out, d, "report.json")
        if os.path.exists(rp):
            rows.append((d, an, bn, json.load(open(rp))))
    if not rows:
        raise SystemExit("没找到 report.json")

    md = ["# 超分 vs 基线 性能对比", "",
          f"来源: `{a.job_out}`；每组冷启动 ABBA 配对 (selfsr_ab)。", ""]
    summary = {}
    for d, an, bn, r in rows:
        summary[d] = {"valid_pairs": r.get("valid_pairs"), "dropped": r.get("dropped_arms"),
                      "metrics": r.get("metrics")}
        md += [f"## {d}: {an} vs {bn}", "",
               f"有效配对 {r.get('valid_pairs')} / {r.get('rounds')}；作废臂 {r.get('dropped_arms') or '无'}", "",
               "| 指标 | A 均值 | B 均值 | 差% | p |", "|---|---|---|---|---|"]
        for key, name in METRICS:
            m = (r.get("metrics") or {}).get(key) or {}
            if "a_mean" not in m:
                continue
            p = m.get("p")
            md.append(f"| {name} | {m['a_mean']:.2f} | {m['b_mean']:.2f} | {m['diff_pct']:+.1f}% | "
                      f"{('%.3f' % p) if p is not None else '–'} |")
        md.append("")

    # 图 1: 每组每指标 A/B 条形
    keys = [k for k, _ in METRICS
            if any("a_mean" in ((r.get("metrics") or {}).get(k) or {}) for _, _, _, r in rows)]
    name_of = dict(METRICS)
    fig, axes = plt.subplots(max(1, len(keys)), 1, figsize=(8, 2.2 * max(1, len(keys))))
    if len(keys) == 1:
        axes = [axes]
    for ax, key in zip(axes, keys):
        sub = [(bn, (r.get("metrics") or {})[key]) for _, _, bn, r in rows
               if "a_mean" in ((r.get("metrics") or {}).get(key) or {})]
        names = [s[0] for s in sub]
        av = [s[1]["a_mean"] for s in sub]
        bv = [s[1]["b_mean"] for s in sub]
        x = list(range(len(sub)))
        ax.bar([i - 0.17 for i in x], av, 0.34, label="A native", color="#7f8c8d")
        ax.bar([i + 0.17 for i in x], bv, 0.34, label="B 方案", color="#2980b9")
        ax.set_xticks(x)
        ax.set_xticklabels(names, fontsize=8)
        ax.set_title(name_of[key], fontsize=10)
        ax.legend(fontsize=8)
    fig.tight_layout()
    p1 = os.path.join(out, "perf_metrics.png")
    fig.savefig(p1, dpi=110)
    plt.close(fig)

    # 图 2: 逐臂 seen_fps 散点
    p2 = None
    fig, ax = plt.subplots(figsize=(8, 3.4))
    plotted = False
    for i, (d, _an, _bn, _r) in enumerate(rows):
        apth = os.path.join(a.job_out, d, "arms.jsonl")
        if not os.path.exists(apth):
            continue
        for ln in open(apth):
            try:
                arm = json.loads(ln)
            except ValueError:
                continue
            fps = (arm.get("rec") or {}).get("seen_fps")
            if fps is None:
                continue
            ax.scatter(i, fps, color="#c0392b" if arm.get("arm") == "B" else "#7f8c8d", s=28)
            plotted = True
    if plotted:
        ax.set_xticks(range(len(rows)))
        ax.set_xticklabels([bn for _, _, bn, _ in rows], fontsize=8)
        ax.set_ylabel("seen_fps (A 灰 / B 红)")
        fig.tight_layout()
        p2 = os.path.join(out, "perf_arms.png")
        fig.savefig(p2, dpi=110)
    plt.close(fig)

    charts = [p for p in (p1, p2) if p]
    md += ["## 图", ""] + [f"![{os.path.basename(c)}]({os.path.basename(c)})" for c in charts] + [""]
    mp = os.path.join(out, "perf_report.md")
    open(mp, "w").write("\n".join(md) + "\n")
    json.dump(summary, open(os.path.join(out, "perf_summary.json"), "w"), ensure_ascii=False, indent=1)
    print(mp)
    print("\n".join(md[:34]))


if __name__ == "__main__":
    main()
