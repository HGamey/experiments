#!/usr/bin/env python3
"""reroll_report.py — srip 重掷缓解验证分析 (本机跑, 纯标准库)。

    python3 selfsr/reroll_report.py --set-dir <srbaseline_.../srip_g7> [--out <目录>]
读 arms.jsonl + traces/<臂>_probe.txt (+ <臂>#rN_probe.txt):
  - 每支 B 臂: 重掷轨迹 (reroll_dropped 每掷 fps/p95/耗时)、最终探测 (reroll_probe)、
    窗口 sclat 8:17 直方分组 (口径同 cadence_report.py: <5:1 症状 / >20:1 干净)
  - 汇总: 最终症状率 vs srip-cadence 历史 18 臂 ~50% (g7 1-2/6、性能轮 3/6、cadence-r1 4/6);
    重掷次数分布; 每掷耗时 (验证「秒级代价」声明——实现为整臂重走)
判定: 症状 0/6 (或 ≤1/6 且该臂 attempts=4) = 重掷有效 (每掷独立同分布);
      症状率与 50% 无差或被丢弃尝试连续同档 = 定型相关, 重掷不可靠, 需修正缓解声明。
"""
import argparse
import json
import os
import re


def sclat_gaps(path):
    rows = []
    for ln in open(path):
        f = ln.split()
        if len(f) == 3 and all(re.fullmatch(r"\d+", x) for x in f):
            rows.append([int(x) for x in f])
    act = [r[1] for r in rows if r[1] > 0]
    return [(b - a) / 1e6 for a, b in zip(act, act[1:]) if 0 < (b - a) / 1e6 < 40]


def gap_stats(gaps):
    n8 = sum(1 for g in gaps if 6.5 <= g <= 10.5)
    n17 = sum(1 for g in gaps if 14.5 <= g <= 19.5)
    ratio = n8 / n17 if n17 else float("inf")
    return {"n": len(gaps), "n8": n8, "n17": n17, "ratio8_17": round(ratio, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set-dir", required=True, help="srip_g7 目录 (含 arms.jsonl 与 traces/)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or a.set_dir
    os.makedirs(out, exist_ok=True)

    arms = []
    for ln in open(os.path.join(a.set_dir, "arms.jsonl")):
        try:
            arms.append(json.loads(ln))
        except ValueError:
            pass
    rows = []
    for arm in arms:
        if arm.get("side") != "B":
            continue
        name = arm["arm"]
        probe = arm.get("reroll_probe") or {}
        spath = os.path.join(a.set_dir, "traces", f"{name}_sclat.txt")
        gs, grp = {"n": 0, "n8": 0, "n17": 0, "ratio8_17": None}, "no_data"
        if os.path.exists(spath):
            gs = gap_stats(sclat_gaps(spath))
            grp = "symptom" if gs["ratio8_17"] < 5 else ("clean" if gs["ratio8_17"] > 20 else "middle")
        rows.append({"arm": name, "attempts": arm.get("reroll_attempts", 1),
                     "dropped": arm.get("reroll_dropped") or [], "probe_fps": probe.get("fps"),
                     "probe_p95": probe.get("interval_p95_ms"), "probe_clean": probe.get("clean"),
                     "seen_fps": arm.get("seen_fps"), "error": arm.get("error"),
                     **gs, "group": grp})

    valid = [r for r in rows if not r.get("error")]
    sym = [r for r in valid if r["group"] == "symptom"]
    mid = [r for r in valid if r["group"] == "middle"]
    all_took = [d.get("took_s") for r in rows for d in r["dropped"] if d.get("took_s") is not None]
    md = ["# srip 重掷缓解验证分析", "", f"来源: `{a.set_dir}`", "",
          "## 每臂轨迹", "",
          "| 臂 | 尝试 | 丢弃轨迹 (fps@p95ms, 耗时s) | 最终探测 fps | 窗口 8:17 | 组 | seen | err |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        drops = "; ".join(f"{d.get('fps')}@{d.get('interval_p95_ms')}ms,{d.get('took_s')}s" for d in r["dropped"])
        md.append(f"| {r['arm']} | {r['attempts']} | {drops or '—'} | {r['probe_fps']} "
                  f"| {r['ratio8_17']} | {r['group']} | {r['seen_fps']} | {r['error'] or ''} |")
    md += ["", "## 判定", "",
           f"- 有效臂 {len(valid)}/{len(rows)}；最终症状 {len(sym)}/{len(valid)}"
           f"（中间档 {len(mid)}）",
           f"- 历史对照 (srip-cadence 18 臂 lead25): 8-9/18 ≈ 50%",
           f"- 重掷次数: " + ", ".join(f"{r['arm']}={r['attempts']}" for r in rows),
           f"- 每掷耗时: n={len(all_took)}"
           + (f", 均值 {sum(all_took)/len(all_took):.0f}s, 范围 {min(all_took):.0f}-{max(all_took):.0f}s" if all_took else ""),
           f"- 被丢弃尝试的档位序列: "
           + "; ".join(f"{r['arm']}: " + "/".join(str(d.get('fps')) for d in r['dropped']) for r in rows if r["dropped"])]
    n_ok = len(valid)
    verdict = "pending"
    if n_ok:
        rate = len(sym) / n_ok
        if rate == 0 or (rate <= 1 / n_ok and all(r["attempts"] >= 4 for r in sym)):
            verdict = "reroll_effective"
        elif rate >= 0.34:
            verdict = "reroll_ineffective (症状率与历史无差 → 定型相关, 需修正缓解声明)"
        else:
            verdict = "mixed (见逐臂轨迹)"
    md += ["", f"结论: {verdict}", ""]
    report = "\n".join(md)
    open(os.path.join(out, "reroll_report.md"), "w").write(report)
    print(report)


if __name__ == "__main__":
    main()
