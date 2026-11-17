#!/usr/bin/env python3
"""gi_manifest.py — 把 gi_capture.py 采的一个场景写成 pf-node results register 的清单 (phonefarm/result@1, 带 dataset)。

    pf-node out <任务> <gi_场景_时间>/report.json
    python3 -I gi_manifest.py <任务ID> <report.json> "<场景名>" "<场景说明>" ["<本场景另外的局限>" ...] \\
        [--prefix gi-seq] [--supersedes gi-seq-beach-20261113 ...] > manifest.json
    pf-node results register manifest.json

result_id 是 <prefix>-<场景>-<采集日期>，dataset.version 是采集日期；研究 ID、方向 ID、设备型号按需改下面的 labels。
一档多段 (--segments K) 时按段登记: 只收运动达标 (fd_motion: 角色在跑、画面在变) 且连续 (0 漏帧、完整帧 ≥240) 的段,
其余的段写进 claims 但不算进 frames / size。
每段的帧数、完整帧数、漏掉的 present、每百帧耗时、运动量都作为 metrics 指向 report.json 里的值，登记时中心会逐项核对。
"""
import argparse
import json
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("report")
ap.add_argument("name")
ap.add_argument("desc")
ap.add_argument("extra", nargs="*", help="本场景另外的局限")
ap.add_argument("--prefix", default="gi-seq")
ap.add_argument("--supersedes", nargs="*", default=[], help="被本数据集取代的旧 result_id")
ap.add_argument("--supersedes-why", help="取代的理由 (默认: 旧数据达不到运动标准)")
ap.add_argument("--tag", help="result_id 末尾再加一段 (同一场景同一天分几个任务采时区分, 比如 540p)")
a = ap.parse_args()
job, rp = a.job, a.report
r = json.load(open(rp))
root = os.path.basename(os.path.dirname(rp))   # gi_<场景>_<时间>

# 每个可登记单元: (档序号, 段序号或 None, 档名, 相对目录, 段记录)
units, dropped = [], []
for i, t in enumerate(r["tiers"]):
    segs = t.get("segments") or []
    if len(segs) > 1:
        for k, sg in enumerate(segs):
            d = f"{t['tier']}/s{k + 1}"
            mo = (sg.get("convert") or {}).get("motion") or {}
            seq = sg.get("seq") or {}
            cont = sg.get("convert") and seq.get("frames_missed") == 0 and sg["convert"]["complete"] >= 240
            if sg.get("convert") and mo.get("ok") and cont:
                units.append((i, k, t["tier"], d, sg))
            elif sg.get("convert") and mo.get("ok"):
                dropped.append(f"{d}（不连续：漏 {seq.get('frames_missed')} 帧，完整 {sg['convert']['complete']} 帧）")
            else:
                dropped.append(f"{d}（{mo.get('why') or sg.get('error') or '没采完'}）")
    elif t.get("convert"):
        units.append((i, None, t["tier"], t["tier"], t))

frames = sum(u[4]["convert"]["frames"] for u in units)
complete = sum(u[4]["convert"]["complete"] for u in units)
size = sum(u[4].get("disk_bytes") or 0 for u in units)
t0 = r["steps"][0]["t"] if r.get("steps") else None
day = time.strftime("%Y%m%d", time.gmtime(t0 or time.time()))
metrics, sources = [], [{"name": "report", "kind": "job_output", "job_id": job, "path": f"{root}/report.json"}]
MOTION = (("run_px_median", "run_px_med", "px"), ("running_frame_ratio", "running_frames", "ratio"),
          ("changing_frame_ratio", "changing_frames", "ratio"), ("speed_px_median", "speed_med", "px"),
          ("moving_pixel_ratio_median", "moving_frac_med", "ratio"), ("center_diff_lag30_median", "center_lag30_med", "gray"))
for i, k, tier, d, u in units:
    tag = d.replace("/", "_")
    sources.append({"name": f"checks_{tag}", "kind": "job_output", "job_id": job, "path": f"{root}/{d}/checks.csv"})
    base = f"/tiers/{i}" + (f"/segments/{k}" if k is not None else "")
    p = u["per_100_frames_s"]
    seq = u.get("seq") or {}
    metrics += [
        {"name": "frames", "condition": tag, "value": u["convert"]["frames"], "unit": "frame", "scope": "image", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/convert/frames"}},
        {"name": "complete_frames", "condition": tag, "value": u["convert"]["complete"], "unit": "frame", "scope": "image", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/convert/complete"}},
        {"name": "missed_presents", "condition": tag, "value": seq.get("frames_missed"), "unit": "frame", "scope": "game_loop", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/seq/frames_missed"}},
        {"name": "capture_s_per_100_frames", "condition": tag, "value": p["capture"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/per_100_frames_s/capture"}},
        {"name": "pull_s_per_100_frames", "condition": tag, "value": p["pull"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/per_100_frames_s/pull"}},
        {"name": "convert_s_per_100_frames", "condition": tag, "value": p["convert"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"{base}/per_100_frames_s/convert"}},
    ]
    mo = u["convert"].get("motion")
    if mo:
        for name, key, unit in MOTION:
            metrics.append({"name": name, "condition": tag, "value": mo.get(key), "unit": unit, "scope": "image", "method": "measured",
                            "source": {"name": "report", "pointer": f"{base}/convert/motion/{key}"}})
def res(tag):   # "1920x1080_2" -> "1920x1080" (同一档重复开的会话算同一分辨率)
    return tag.split("_")[0]


tiers_done = sorted({res(u[2]) for u in units}, key=lambda x: -int(x.split("x")[0]) if "x" in x else 0)
per_tier = {tt: sum(u[4]["convert"]["frames"] for u in units if res(u[2]) == tt) for tt in tiers_done}
first = {}
for u in units:
    first.setdefault(res(u[2]), u[4]["present"])
present = ", ".join(f"{tt}→送显 {first[tt]['w']}x{first[tt]['h']}" for tt in tiers_done)
atts = units[0][4]["convert"]["atts"] if units else {}
FMT = {37: "RGBA8", 129: "D24S8", 58: "A2R10G10B10"}
att_desc = "、".join(f"{k} {FMT.get(v['vk_format'], v['vk_format'])}" for k, v in atts.items()) + "，每档都和送显同尺寸"
multi = any(u[1] is not None for u in units)
claims = [f"{a.desc}。" + (f"每档在同一次会话里沿路线连采几段、每段 {r['frames']} 帧，收录的段：" + "、".join(u[3] for u in units)
                          + "；各档帧数：" + "、".join(f"{tt} {n}" for tt, n in per_tier.items()) + "。" if multi
                          else f"每档一段连续 {r['frames']} 帧。") + f"{present}。附件：{att_desc}。",
          "目录：<档>/" + ("s<段>/" if multi else "") + "ui/（带 UI，RGB8 PNG）、noui/（不带 UI）、depth16/（深度高 16 位）、depth24/（24 位无损，R 高 G 中 B 低）、"
          "mv/（速度图，原始 32 位小端存成 RGBA8 PNG）、index.csv、checks.csv（含逐帧运动量）、summary.json、session.json。",
          "附件已上下翻正。深度是 reverse-Z（近处值大）。速度图 r=(u>>20)&1023 水平、g=(u>>10)&1023 竖直、b=u&1023 角色掩码；v=sign(c−509)·((c−509)/511.5)²，单位画面宽/高；翻正后 y 取反；上一帧位置=这一帧位置−v。"]
if multi:
    claims.append("运动标准（experiments dataset/fd_motion.py）：近处地面与远处竖直位移差 ≥4 像素（1080p、每 1/60 秒）的帧占 70% 以上 = 角色在跑；"
                  "位移中位数 ≥2 像素且动着的像素 ≥60% 的帧占 70% 以上，且 0.5 秒中央差中位数 ≥3 = 画面在变。收录的段都达标。"
                  + ("不达标没收录的段：" + "、".join(dropped) + "。" if dropped else ""))
if a.supersedes:
    claims.append("本数据集取代 " + "、".join(a.supersedes) + "（" + (a.supersedes_why or "旧数据角色多半站着转镜头、顶着礁石或夜里慢慢游，达不到运动标准") + "；旧数据不删）。")
m = {
    "schema": "phonefarm/result@1",
    "result_id": f"{a.prefix}-{r['scene'].replace('_', '-')}-{day}" + (f"-{a.tag}" if a.tag else ""),
    "title": f"原神连续序列帧：{a.name}（{len(tiers_done)} 档分辨率，每帧带/不带 UI、深度、速度图）",
    "job_id": job,
    "labels": {"device": "REDMAGIC NX809J", "game_id": "genshin", "game_version": r.get("game_version"), "research_id": "gi-capture-req-20261113",
               "direction_id": "gray-att-wmsize"},
    "origin": {"producer": "experiments dataset/gi_capture.py", "producer_version": r.get("experiment_commit"), "evidence_origin": "device",
               "observed_at": t0},
    "outcome": "unknown",
    "sources": sources,
    "metrics": metrics,
    "claims": claims,
    "limitations": ["每段第 1 帧没有附件（拷贝挂在开段后的下一次 present 上）。",
                    "1 级新号，只到得了开场的坠星山谷一带；城里、室内、秘境要有进度的号。2K 拿不到：游戏按约 207 万像素（1920×1080）封顶送显尺寸。",
                    ] + (["每档单独冷启动，各档的画面不是同一时刻、同一地点。"] if len(tiers_done) > 1 else []) + a.extra,
    "dataset": {"name": f"原神序列帧 {a.name}", "version": day, "pairing": "real",
                "purpose": f"红魔上录的原神连续画面，每帧有带 UI、不带 UI、深度和速度图，{len(tiers_done)} 档分辨率，用来训练和评估超分、插帧",
                "capture_method": "灰层 framedump 送显读回 + framedump_att 拷 tonemap 输出、G-buffer 深度、速度 pass；wm size 定分辨率，冷启动"
                                  + ("；多指注入导航（摇杆 + 冲刺 + 边跑边转镜头，按小地图修方向）" if multi else ""),
                "frames": frames, "scenes": 1, "splits": None, "size_bytes": size, "location": f"job:{job}:{root}/"},
}
if len(a.supersedes) == 1:
    m["supersedes"] = a.supersedes[0]      # 平台的 supersedes 字段只收一个 id; 多个就只写在 claims 里
print(json.dumps(m, ensure_ascii=False, indent=1))
print(f"frames={frames} complete={complete} size={size} dropped={dropped}", file=sys.stderr)
