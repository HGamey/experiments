#!/usr/bin/env python3
"""Summarise one or more bench.py outputs into a Markdown table.

    python3 ondevice_cua/report.py <job_out_dir> [<job_out_dir> ...] > results.md

Each directory is a pf-node job output (contains results.json). Per phone and screenshot scale, numbers are
the median over repetitions; power is whole-device (USB in - battery charge), with the delta over idle.
"""
import json
import os
import statistics
import sys


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def fmt(v, nd=1):
    if v is None:
        return "-"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def main(dirs):
    rows, bench_rows, idle_rows, notes = [], [], [], []
    for d in dirs:
        p = os.path.join(d, "results.json")
        if not os.path.exists(p):
            notes.append(f"{d}: no results.json")
            continue
        r = json.load(open(p))
        dev = r["meta"].get("device", {})
        name = f"{dev.get('brand', '?')} {dev.get('model', '?')} ({dev.get('soc', '?')}, {round(dev.get('mem_total_kb', 0) / 1048576)} GB)"
        job = os.path.basename(os.path.normpath(d))
        idle = r.get("idle") or {}
        idle_rows.append((name, job, idle))
        lb = r.get("llama_bench") or {}
        for x in lb.get("results") or []:
            bench_rows.append((name, x))
        by = {}
        for s in r.get("steps", []):
            by.setdefault(s["scale"], []).append(s)
        for sc in sorted(by):
            ss = [s for s in by[sc] if s.get("rc") == 0]
            if not ss:
                notes.append(f"{name} scale 1/{sc}: no successful step ({[s.get('rc') for s in by[sc]]})")
                continue
            g = lambda k: med([s.get(k) for s in ss])
            gp = lambda k: med([(s.get("power") or {}).get(k) for s in ss])
            rows.append({
                "phone": name, "job": job, "scale": sc, "img": "x".join(map(str, ss[0]["image_wh"])), "n": len(ss),
                "img_tok": g("image_tokens"), "prompt_tok": g("prompt_tokens"), "enc_s": (g("vision_encode_ms") or 0) / 1000,
                "img_pf": g("image_prefill_tok_s"), "pf": g("prefill_tok_s"), "dec": g("decode_tok_s"), "dec_tok": g("decode_tokens"),
                "ttft_s": (g("ttft_ms") or 0) / 1000, "step_s": (g("step_ms") or 0) / 1000, "wall_s": g("wall_s"),
                "rss": g("peak_rss_mb"), "anon": g("peak_anon_mb"), "w": gp("sys_w_est"), "dw": gp("delta_w_vs_idle"),
                "cpu_max": med([(s.get("power") or {}).get("cpu_temp_max_c") for s in ss]),
                "batt_max": med([(s.get("power") or {}).get("batt_temp_max_c") for s in ss]),
                "tool": sum(1 for s in ss if s.get("tool_call")),
            })
    print("## Agent step (system prompt + screenshot + instruction -> reasoning + tool call), CPU, Q4_K_M + mmproj Q8_0\n")
    print("| Phone | Job | Screenshot | Image tok | Prompt tok | Vision encode s | Image prefill tok/s | Prefill tok/s | Decode tok/s | Gen tok | TTFT s | Step s | Peak RSS MB (anon) | Power W (Δ idle) | CPU max °C | Batt max °C | tool_call ok |")
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for x in rows:
        print(f"| {x['phone']} | {x['job']} | {x['img']} (1/{x['scale']}) | {fmt(x['img_tok'])} | {fmt(x['prompt_tok'])} | {fmt(x['enc_s'])} | "
              f"{fmt(x['img_pf'])} | {fmt(x['pf'])} | {fmt(x['dec'])} | {fmt(x['dec_tok'])} | {fmt(x['ttft_s'])} | {fmt(x['step_s'])} | "
              f"{fmt(x['rss'])} ({fmt(x['anon'])}) | {fmt(x['w'], 2)} ({fmt(x['dw'], 2)}) | {fmt(x['cpu_max'])} | {fmt(x['batt_max'])} | {x['tool']}/{x['n']} |")
    if bench_rows:
        print("\n## llama-bench, text only (Q4_K_M decoder)\n")
        print("| Phone | Threads | Test | tok/s | ± |")
        print("|---|---:|---|---:|---:|")
        for name, x in bench_rows:
            test = f"pp{x['n_prompt']}" if x.get("n_prompt") else f"tg{x['n_gen']}"
            print(f"| {name} | {x['n_threads']} | {test} | {fmt(x['avg_ts'], 2)} | {fmt(x['stddev_ts'], 2)} |")
    print("\n## Idle baseline (screen on, launcher, on USB)\n")
    print("| Phone | Job | Power W | USB in W | Battery in W | CPU °C | Battery °C | MemAvailable min MB | Charge status |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for name, job, i in idle_rows:
        print(f"| {name} | {job} | {fmt(i.get('sys_w_est'), 2)} | {fmt(i.get('usb_in_w'), 2)} | {fmt(i.get('batt_in_w'), 2)} | {fmt(i.get('cpu_temp_c'))} | "
              f"{fmt(i.get('batt_temp_c'))} | {fmt(i.get('mem_avail_min_mb'))} | {','.join(i.get('status') or [])} |")
    for n in notes:
        print(f"\n> {n}")


if __name__ == "__main__":
    main(sys.argv[1:])
