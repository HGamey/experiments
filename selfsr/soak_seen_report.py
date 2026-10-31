#!/usr/bin/env python3
"""soak30 HOLD_SEEN 抽样分析: 判定长稳窗口内插帧子图层呈现节律档位 (双峰复发检测)。

用法: python3 selfsr/soak_seen_report.py --set-dir <任务out目录> [--seen-name P2_seen.tsv]

读 traces/<名>_seen.tsv (wall, t, fps, interval_p50_ms, interval_p95_ms, 每行=一次 2s 抽样)
与 traces/<名>_hold.tsv (逐秒面板), 输出 JSON 摘要与逐点档位表。标准库实现。

档位判据 (srip-cadence 定案的双稳/中间档, lead_ns=25ms):
  clean   : fps>=110            (帧帧命中 vsync, 117-119)
  middle  : 105<=fps<110        (漏 ~5% 槽位, 111-114)
  symptom : fps<105 且 p95>=14  (90fps 吸引子, [8,8,17]ms, p95≈17)
  unknown : 其余 (采样不足/ERROR 行)
"""
import argparse
import json
import os
import re
import sys


def load_seen(path):
    rows, errs = [], 0
    if not os.path.exists(path):
        return rows, errs
    for ln in open(path):
        p = ln.rstrip("\n").split("\t")
        if not p or p[0] in ("wall", ""):
            continue
        try:
            rows.append({"wall": int(p[0]), "t": int(p[1]),
                         "fps": float(p[2]) if p[2] not in ("", "ERROR") else None,
                         "p50": float(p[3]) if len(p) > 3 and p[3] not in ("", "ERROR") else None,
                         "p95": float(p[4]) if len(p) > 4 and p[4] not in ("", "ERROR") else None})
        except (ValueError, IndexError):
            errs += 1
    return rows, errs


def classify(r):
    if r["fps"] is None:
        return "error"
    if r["fps"] >= 110:
        return "clean"
    if r["fps"] >= 105:
        return "middle"
    if r["p95"] is not None and r["p95"] >= 14:
        return "symptom"
    return "unknown"


def load_hold_minutes(path):
    """面板逐秒 → 分钟均值/最低 + 凹陷/重设统计 + 电池温度曲线。"""
    if not os.path.exists(path):
        return {}
    buckets, temps, dips, reasserts, coverage = {}, [], 0, 0, 0
    for ln in open(path):
        p = ln.rstrip("\n").split("\t")
        if not p or p[0] == "wall":
            continue
        try:
            t = int(p[1])
            hz = float(p[5]) if len(p) > 5 and p[5] else 0.0
            coverage = max(coverage, t + 1)
        except (ValueError, IndexError):
            continue
        m = re.search(r"temperature: (\d+)", p[7]) if len(p) > 7 else None
        if m:
            temps.append((t, int(m.group(1)) / 10))
        if hz:
            if hz < 100:
                dips += 1
            b = buckets.setdefault(t // 60, {"hz": [], "t": []})
            b["hz"].append(hz)
            if m:
                b["t"].append(int(m.group(1)) / 10)
        if len(p) > 10 and p[10] and p[10] != "0":
            reasserts += 1
    minutes = []
    for mth in sorted(buckets):
        b = buckets[mth]
        minutes.append({"minute": mth,
                        "hz_mean": round(sum(b["hz"]) / len(b["hz"]), 2),
                        "temp_mean": round(sum(b["t"]) / len(b["t"]), 1) if b["t"] else None})
    last10 = [tc for tt, tc in temps if tt >= coverage - 600]
    return {"coverage_s": coverage, "dip_seconds": dips, "reassert_events": reasserts,
            "minutes": minutes,
            "temp_first": temps[0][1] if temps else None,
            "temp_last": temps[-1][1] if temps else None,
            "temp_max": max((t for _, t in temps), default=None),
            "temp_last10_mean": round(sum(last10) / len(last10), 1) if last10 else None,
            "min_hz_minutes_ge_115": sum(1 for m in minutes if (m["hz_mean"] or 0) >= 115)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-dir", required=True, help="任务 out 目录 (含 traces/)")
    ap.add_argument("--seen-name", default=None, help="seen TSV 文件名, 缺省扫 traces/*_seen.tsv")
    ap.add_argument("--json-out", default=None, help="摘要 JSON 输出路径")
    a = ap.parse_args()
    tr = os.path.join(a.set_dir, "traces")
    res = {"set_dir": a.set_dir, "arms": []}
    names = ([a.seen_name] if a.seen_name
             else sorted(os.path.basename(p) for p in os.listdir(tr) if p.endswith("_seen.tsv"))) if os.path.isdir(tr) else []
    for nm in names:
        rows, errs = load_seen(os.path.join(tr, nm))
        pts = [{"t": r["t"], "fps": r["fps"], "p50_ms": r["p50"], "p95_ms": r["p95"], "class": classify(r)}
               for r in rows]
        cls = [p["class"] for p in pts]
        hold = nm.replace("_seen.tsv", "_hold.tsv")
        res["arms"].append({
            "seen_tsv": nm, "samples": len(pts), "parse_errors": errs,
            "classes": {c: cls.count(c) for c in ("clean", "middle", "symptom", "unknown", "error")},
            "fps_min": min((p["fps"] for p in pts if p["fps"] is not None), default=None),
            "fps_max": max((p["fps"] for p in pts if p["fps"] is not None), default=None),
            "p95_max_ms": max((p["p95_ms"] for p in pts if p["p95_ms"] is not None), default=None),
            "first_transition": next(({"at_s": pts[i - 1]["t"], "from": cls[i - 1], "to": cls[i]}
                                      for i in range(1, len(cls)) if cls[i] != cls[0]), None),
            "hold": load_hold_minutes(os.path.join(tr, hold)),
            "points": pts})
        arm = res["arms"][-1]
        first_tr = arm["first_transition"]
        print(f"[{nm}] 样本 {len(pts)} (解析错 {errs})  档位 " +
              " ".join(f"{c}={cls.count(c)}" for c in ("clean", "middle", "symptom", "unknown", "error") if cls.count(c)) +
              f"  fps {arm['fps_min']}-{arm['fps_max']}" +
              (f"  首次档位切换 {first_tr}" if first_tr else "  无档位切换"))
        h = res["arms"][-1]["hold"]
        if h:
            print(f"  面板: 覆盖 {h['coverage_s']}s 凹陷 {h['dip_seconds']}s 重设 {h['reassert_events']} 次  "
                  f"分钟Hz≥115: {h['min_hz_minutes_ge_115']}/{len(h['minutes'])}  "
                  f"温度 {h['temp_first']}→{h['temp_last']}°C (max {h['temp_max']}, 末10min均值 {h['temp_last10_mean']})")
    if not names:
        print("无 seen TSV (该臂未开 HOLD_SEEN 或未跑)", file=sys.stderr)
    if a.json_out:
        json.dump(res, open(a.json_out, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
