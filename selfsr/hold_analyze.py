#!/usr/bin/env python3
"""hold_analyze.py — 汇总 hold 探测的 traces/P*_hold.tsv：掉落时刻、时长、干预效果。

    python3 -I selfsr/hold_analyze.py <hold 输出目录>

每个 TSV 列（hold_probe 写出）：wall t ctrl_fps mode mperiod active_hz range bat focus tap reassert ok。
输出：每阶段的掉落段列表（开始/结束/持续）、首掉延迟、120Hz 占比、ctrl/range 与掉落的一致性、
电池温度与掉落时间的相关线索、点屏/重设标记对齐。"""
import json
import os
import re
import sys


def hz_of(mperiod):
    m = re.search(r"\(([0-9.]+) fps\)", mperiod or "")
    return float(m.group(1)) if m else None


def main(d):
    summary = {}
    for fn in sorted(os.listdir(os.path.join(d, "traces"))):
        if not fn.endswith("_hold.tsv"):
            continue
        name = fn[:-len("_hold.tsv")]
        rows = []
        with open(os.path.join(d, "traces", fn)) as f:
            next(f)
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 12:
                    rows.append(p)
        eps, cur = [], None
        for p in rows:
            t, ok = int(p[1]), p[11]
            if ok == "":
                continue   # 无目标 panel 的阶段
            if ok == "0" and cur is None:
                cur = [t, t]
            elif ok == "0":
                cur[1] = t
            elif cur is not None:
                eps.append(tuple(cur) + (cur[1] - cur[0] + 1,))
                cur = None
        if cur is not None:
            eps.append(tuple(cur) + (cur[1] - cur[0] + 1,))
        n = len(rows)
        ok_n = sum(1 for p in rows if p[11] == "1")
        hzs = [hz_of(p[4]) for p in rows]
        hzs = [h for h in hzs if h is not None]
        # ctrl_fps / range 与掉落的一致性: 掉落行里 ctrl==60 且 range 收窄的比例
        drop_rows = [p for p in rows if p[11] == "0"]
        ctrl60 = sum(1 for p in drop_rows if p[2].split(".")[0] == "60") if drop_rows else None
        rng_single = sum(1 for p in drop_rows if re.fullmatch(r"60(\.0+)?, 60(\.0+)?", p[6].strip())) if drop_rows else None
        taps = [int(p[1]) for p in rows if p[9] == "1"]
        reas = [int(p[1]) for p in rows if p[10] == "1"]
        bats = [(int(p[1]), p[7]) for p in rows if p[7]]
        summary[name] = {
            "rows": n, "ok120_ratio": round(ok_n / n, 3) if n else None,
            "first_drop_s": eps[0][0] if eps else None,
            "drop_episodes": [{"from_s": a, "to_s": b, "dur_s": c} for a, b, c in eps],
            "hz_min": min(hzs) if hzs else None, "hz_max": max(hzs) if hzs else None,
            "drop_rows_ctrl60": f"{ctrl60}/{len(drop_rows)}" if drop_rows else None,
            "drop_rows_range_60_only": f"{rng_single}/{len(drop_rows)}" if drop_rows else None,
            "taps_at_s": taps, "reasserts_at_s": reas,
            "bat_first": bats[0][1] if bats else None, "bat_last": bats[-1][1] if bats else None,
            "focus_sample": next((p[8] for p in rows if p[8]), None),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    # 人的读法
    for name, s in summary.items():
        eps = ", ".join(f"{e['from_s']}–{e['to_s']}s({e['dur_s']}s)" for e in s["drop_episodes"]) or "无"
        print(f"{name}: 120Hz 占比 {s['ok120_ratio']} 首掉 {s['first_drop_s']}s 掉落段 {eps} "
              f"ctrl60 {s['drop_rows_ctrl60']} range单60 {s['drop_rows_range_60_only']} "
              f"点屏@{s['taps_at_s'][:8]} 重设@{s['reasserts_at_s'][:12]}")


if __name__ == "__main__":
    main(sys.argv[1])
