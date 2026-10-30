#!/usr/bin/env python3
"""cadence_report.py — srip 插帧呈现节律双峰分析 (本机跑, 纯标准库)。

    python3 selfsr/cadence_report.py --set-dir <srbaseline_.../srip_g7> [--out <目录>]
读 arms.jsonl + traces/<臂>_sclat.txt (+ _atrace.txt 若有):
  - 每支 B 臂: 上屏 fps、SF latency 间隔直方 (8/17ms 两族)、8:17 比例 → 症状(<5:1)/干净(>20:1) 分组
  - 有 atrace 的臂: fence unsignal / onCommitNotComposited / releasePendingBuffer 每秒计数 (组间对比)
  - 判定: 两类臂 atrace 无差异而节律不同 → 锁相自持; 症状臂 fence 周期性出现 → 供给不足
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
    n8 = sum(1 for g in gaps if 6.5 <= g <= 10.5)     # 120Hz 命中
    n17 = sum(1 for g in gaps if 14.5 <= g <= 19.5)   # 漏一个 vsync
    n25 = sum(1 for g in gaps if 22.5 <= g <= 27.5)   # 漏两个
    ratio = n8 / n17 if n17 else float("inf")
    return {"n": len(gaps), "n8": n8, "n17": n17, "n25": n25, "ratio8_17": round(ratio, 1)}


def atrace_marks(path):
    if not os.path.exists(path):
        return {}
    txt = open(path, errors="replace").read()
    # sf_atrace() 用 3s 窗口; 文本行数/时长只作粗略归一
    return {"fence_unsignal": txt.count("fence unsignaled"),
            "not_composited": txt.count("onCommitNotComposited"),
            "release_pending": txt.count("releasePendingBuffer"),
            "bytes": len(txt)}


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
        gaps = sclat_gaps(os.path.join(a.set_dir, "traces", f"{name}_sclat.txt"))
        gs = gap_stats(gaps)
        grp = "symptom" if gs["ratio8_17"] < 5 else ("clean" if gs["ratio8_17"] > 20 else "middle")
        marks = atrace_marks(os.path.join(a.set_dir, "traces", f"{name}_atrace.txt"))
        rows.append({"arm": name, "seen_fps": arm.get("seen_fps"), "sc_fps": arm.get("display_fps_sc"),
                     "sc_p95": arm.get("sc_interval_p95_ms"), "missed": arm.get("missed_frames"),
                     "game_fps": arm.get("game_fps"), **gs, "group": grp, "atrace": marks})

    md = ["# srip 呈现节律双峰分析", "", f"来源: `{a.set_dir}`", "",
          "| 臂 | seen fps | sc p95 | 8ms | 17ms | 8:17 | 组 | SF丢帧 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['arm']} | {r['seen_fps']} | {r['sc_p95']} | {r['n8']} | {r['n17']} "
                  f"| {r['ratio8_17'] if r['ratio8_17'] != float('inf') else 'inf'} | {r['group']} | {r['missed']} |")

    for grp in ("symptom", "clean"):
        sub = [r for r in rows if r["group"] == grp]
        if not sub:
            continue
        md += ["", f"## {grp} ({len(sub)} 臂)", ""]
        if any(r["atrace"] for r in sub):
            md += ["| 臂 | fence unsignal | notComposited | releasePending |", "|---|---|---|---|"]
            for r in sub:
                at = r["atrace"] or {}
                md.append(f"| {r['arm']} | {at.get('fence_unsignal', '–')} "
                          f"| {at.get('not_composited', '–')} | {at.get('release_pending', '–')} |")
            fu = [ (r['atrace'] or {}).get('fence_unsignal', 0) for r in sub ]
            nc = [ (r['atrace'] or {}).get('not_composited', 0) for r in sub ]
            md += ["", f"fence unsignal 均值 {sum(fu)/len(fu):.1f}、notComposited 均值 {sum(nc)/len(nc):.1f}（3s 窗口内计数）"]
        else:
            md.append("（本轮无 atrace）")

    verdict = []
    sym = [r for r in rows if r["group"] == "symptom"]
    cln = [r for r in rows if r["group"] == "clean"]
    if sym and cln and all(r["atrace"] for r in rows):
        sm = sum((r["atrace"] or {}).get("fence_unsignal", 0) for r in sym) / len(sym)
        cm = sum((r["atrace"] or {}).get("fence_unsignal", 0) for r in cln) / len(cln)
        verdict.append(f"症状臂 fence unsignal {sm:.1f}/3s vs 干净臂 {cm:.1f}/3s——"
                       + ("差异显著, 指向供给不足（fence 等待）" if abs(sm - cm) > 3
                          else "无显著差异, 指向锁相自持（调度相位）"))
    md += ["", "## 判定", ""] + (verdict or ["（数据不足: 需同轮同时出现症状与干净臂且带 atrace）"]) + [""]
    open(os.path.join(out, "cadence_report.md"), "w").write("\n".join(md) + "\n")
    json.dump(rows, open(os.path.join(out, "cadence_rows.json"), "w"), ensure_ascii=False, indent=1)
    print("\n".join(md))


if __name__ == "__main__":
    main()
