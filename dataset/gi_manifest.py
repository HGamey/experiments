#!/usr/bin/env python3
"""gi_manifest.py — 把 gi_capture.py 采的一个场景写成 pf-node results register 的清单 (phonefarm/result@1, 带 dataset)。

    pf-node out <任务> <gi_场景_时间>/report.json
    python3 -I gi_manifest.py <任务ID> <report.json> "<场景名>" "<场景说明>" ["<本场景另外的局限>" ...] > manifest.json
    pf-node results register manifest.json

result_id 是 gi-seq-<场景>-<采集日期>，dataset.version 是采集日期；研究 ID、方向 ID、设备型号按需改下面的 labels。
每档的帧数、完整帧数、漏掉的 present、每百帧采集/读回/转换秒数都作为 metrics 指向 report.json 里的值，登记时中心会逐项核对。
"""
import time
import json
import os
import sys

job, rp, name, desc = sys.argv[1:5]
extra = sys.argv[5:]   # 本场景另外的局限
r = json.load(open(rp))
root = os.path.basename(os.path.dirname(rp))   # gi_<场景>_<时间>
tiers = [t for t in r["tiers"] if t.get("convert")]
frames = sum(t["convert"]["frames"] for t in tiers)
complete = sum(t["convert"]["complete"] for t in tiers)
size = sum(t.get("disk_bytes") or 0 for t in tiers)
t0 = r["steps"][0]["t"] if r.get("steps") else None
day = time.strftime("%Y%m%d", time.gmtime(t0 or time.time()))
metrics, sources = [], [{"name": "report", "kind": "job_output", "job_id": job, "path": f"{root}/report.json"}]
for i, t in enumerate(r["tiers"]):
    tag = t["tier"]
    sources.append({"name": f"checks_{tag}", "kind": "job_output", "job_id": job, "path": f"{root}/{tag}/checks.csv"})
    if not t.get("convert"):
        continue
    p = t["per_100_frames_s"]
    seq = t.get("seq") or {}
    metrics += [
        {"name": "frames", "condition": tag, "value": t["convert"]["frames"], "unit": "frame", "scope": "image", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/convert/frames"}},
        {"name": "complete_frames", "condition": tag, "value": t["convert"]["complete"], "unit": "frame", "scope": "image", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/convert/complete"}},
        {"name": "missed_presents", "condition": tag, "value": seq.get("frames_missed"), "unit": "frame", "scope": "game_loop", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/seq/frames_missed"}},
        {"name": "capture_s_per_100_frames", "condition": tag, "value": p["capture"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/per_100_frames_s/capture"}},
        {"name": "pull_s_per_100_frames", "condition": tag, "value": p["pull"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/per_100_frames_s/pull"}},
        {"name": "convert_s_per_100_frames", "condition": tag, "value": p["convert"], "unit": "s", "scope": "unknown", "method": "measured",
         "source": {"name": "report", "pointer": f"/tiers/{i}/per_100_frames_s/convert"}},
    ]
present = ", ".join(f"{t['tier']}→送显 {t['present']['w']}x{t['present']['h']}" for t in tiers)
atts = tiers[0]["convert"]["atts"] if tiers else {}
FMT = {37: "RGBA8", 129: "D24S8", 58: "A2R10G10B10"}
att_desc = "、".join(f"{k} {FMT.get(v['vk_format'], v['vk_format'])}" for k, v in atts.items()) + "，每档都和送显同尺寸"
m = {
    "schema": "phonefarm/result@1",
    "result_id": f"gi-seq-{r['scene'].replace('_', '-')}-{day}",
    "title": f"原神连续序列帧：{name}（{len(tiers)} 档分辨率，每帧带/不带 UI、深度、速度图）",
    "job_id": job,
    "labels": {"device": "REDMAGIC NX809J", "game_id": "genshin", "game_version": r.get("game_version"), "research_id": "gi-capture-req-20261113",
               "direction_id": "gray-att-wmsize"},
    "origin": {"producer": "experiments dataset/gi_capture.py", "producer_version": r.get("experiment_commit"), "evidence_origin": "device",
               "observed_at": t0},
    "outcome": "unknown",
    "sources": sources,
    "metrics": metrics,
    "claims": [f"{desc}。每档一段连续 {r['frames']} 帧，{present}。附件：{att_desc}。",
               "目录：<档>/ui/（带 UI，RGB8 PNG）、noui/（不带 UI）、depth16/（深度高 16 位）、depth24/（24 位无损，R 高 G 中 B 低）、mv/（速度图，原始 32 位小端存成 RGBA8 PNG）、index.csv、checks.csv、summary.json、session.json。",
               "附件已上下翻正。深度是 reverse-Z（近处值大）。速度图 r=(u>>20)&1023 水平、g=(u>>10)&1023 竖直、b=u&1023 角色掩码；v=sign(c−509)·((c−509)/511.5)²，单位画面宽/高；翻正后 y 取反；上一帧位置=这一帧位置−v。"],
    "limitations": ["每档第 1 帧没有附件（拷贝挂在开段后的下一次 present 上）。",
                    "1 级新号，只到得了开场一带的野外；城里、室内、秘境要有进度的号。2K 拿不到：游戏把送显封顶在约 1080p。",
                    "每档单独冷启动，同一场景三档的画面不是同一时刻。"] + extra,
    "dataset": {"name": f"原神序列帧 {name}", "version": day, "pairing": "real",
                "purpose": "红魔上录的原神连续画面，每帧有带 UI、不带 UI、深度和速度图，三档分辨率，用来训练和评估超分、插帧",
                "capture_method": "灰层 framedump 送显读回 + framedump_att 拷 tonemap 输出、G-buffer 深度、速度 pass；wm size 定分辨率，冷启动",
                "frames": frames, "scenes": 1, "splits": None, "size_bytes": size, "location": f"job:{job}:{root}/"},
}
print(json.dumps(m, ensure_ascii=False, indent=1))
print(f"frames={frames} complete={complete} size={size}", file=sys.stderr)
