#!/usr/bin/env python3
"""make_pairs.py — 方法 C 模拟对 (DATASET-COLLECTION §3.3/§3.8–§3.10): capture 输出目录 → 数据集版本目录 (本机跑, 不占设备)。

    python3 -I dataset/make_pairs.py <capture 输出目录> [<capture 输出目录> ...] --out <数据集根> [--name gi-sim] [--version 0]
        [--batch gi-YYYYMMDD-01 ...] [--exclude 排除清单 ...] [--labels 场景标签 ...] [--test-seg <batch>:<段号> ...]
        [--hr-size 2141x969] [--lr-size 1505x681] [--tile 256] [--psnr-min 28] [--breaker 0.05] [--test-segments N] [--write-lr] [--no-preview]

输入: capture_native.sh 的产物目录 (含 capture-*/frames/manifest.jsonl, 可选 batch.json), capture_framedump.py 的产物目录或它的 key/ (含 clips.json),
或 framedump 的原始段目录 (每段 session.json、index.csv、summary.json、f*.raw, 如 framedump_probe.py 的产物)。多批按命令行顺序拼接;
--batch、--exclude、--labels 重复时与输入一一对应。每批帧号和段号各自从 0 起, 内部把段号加上偏移 (保持 % 5 不变) 变成
全数据集唯一, 清单里仍记原批帧号和 batch_segment; 新批首帧不与上一批比冻结。
--labels 是人看图写的场景标签, 写进清单 tags: 每行 <帧号或 a-b> <scene_type> <pose> <lighting> [inferred] (inferred: 光照没看到天空, 由时间线推断)。
tags.motion 优先取采集 manifest 的 motion (来自输入记录), 没有这一列时有标签的帧是 static (停稳后截图)。HUD 排除框按 batch.json 的包名选, 没标过的游戏拒绝;
--hr-size 必须等于批次记录的实际渲染尺寸 (记录为空时不查)。
framedump 输入 (§3.5): 帧就是送显图, HR 直接用, 不再缩放, LR 仍由 HR 模拟, 所以还是模拟对 (hr_downsampled); 帧可以是 .raw (按 session.json 的宽高和 vk_format 读, 丢掉 alpha) 或已转好的 PNG。每段是一个连续片段, 相邻帧不算独立样本:
  只有关键帧 (clips.json 标了 key, 原始段按 --stride 取, 缺省每 0.5 秒一帧) 做 QA、切块、发布, 其余帧不要求在本机, 哈希和帧号留在发布目录的 clips.json 里。片段内不判冻结。
构造 (与 selfsr/baseline_quality.py --truth-only 同口径):
  HR = 原生截图 (屏幕分辨率) LANCZOS 缩到游戏送显尺寸 --hr-size;
  LR = HR 经 BOX 缩到 --lr-size (Game Mode 0.56 时游戏实际渲染尺寸); source.kind=hr_downsampled。
  LR 只存配方 (§2.2), --write-lr 才落盘。
QA (§3.8, 规则沿用 SPEC_SR_LOOP §4.1, 倍率换成约 1.42):
  1 完整性: 文件字节数 / sha256 / 尺寸与 manifest 一致, 否则整段作废;
  2 有效画面: --exclude 人工排除的帧 (菜单/对话/加载页), state=explore、横屏、非 ready_only、非冻结 (diff_prev >= 1.0),
    再丢近乎纯色帧 (加载/黑屏);
  3 对齐: bicubic(LR)→HR 尺寸, 中央区 (0.20,0.15)-(0.80,0.80) 相位相关 + 三点抛物线亚像素, |dx|,|dy| > 0.25 px 整帧丢;
  4 切块: HR 上 --tile 网格, 与 HUD 排除区相交的块记为 hud (单独标注, 不进训练块);
  5 场景块逐块 bicubic 重建 PSNR (RGB 联合 MSE) > --psnr-min 才保留;
  6 熔断: (PSNR 丢弃块 + 对齐丢弃帧的场景块) / 可用场景块 > --breaker → 不发布 (只写 qa_report.json), 退出码 3。
划分 (§3.9, 按 segment, 同段不跨集): 路线末尾连续 --test-segments 段为 test, 其前 1 段为 guard (丢弃, 隔开相邻帧);
  给了 --test-seg 就改用人选的段 (每种场景类型至少一个), 它们同批的前后相邻段为 guard;
  其余 segment % 5 == 4 为 val, 剩下 train。基线锁定: val/test 场景块上双线性、bicubic 的 PSNR / SSIM 均值。
输出 (§3.10): <out>/<name>-v<version>/{manifest.json, split.json, qa_report.json,
  batches/<batch>/seg<NNN>/{hr/f<10 位帧号>.png, lr/..., meta.json}, preview/ (展示用接触表与裁剪对比)}。
  冻结判定: manifest 的 diff_prev 为 null 时 (cruise 采集) 离线按 64x36 灰度缩略图平均差与上一保留帧比。
  退出码 0 发布, 2 用法/输入错, 3 熔断。
"""
import argparse
import csv
import datetime
import glob
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image

# HUD 排除区 (相对坐标 x0,y0,x1,y1)。原神: SPEC_SR_LOOP §4.1 实测框; Megacity: 看探路和试采截图标的 (2026-11-03),
# 设置按钮、油门滑块整条、按住时才出现的瞄准摇杆; 飞行时浮动的距离圈位置不固定, 框不住
GI_HUD_BOXES = {"top_bar": (0, 0, 1, 0.13), "minimap_quest": (0, 0, 0.17, 0.40), "team": (0.80, 0.13, 1, 0.50),
                "skills": (0.60, 0.58, 1, 1), "joystick_chat": (0, 0.62, 0.25, 1), "hp_bar": (0.35, 0.85, 0.65, 1),
                "bottom_bar": (0, 0.93, 1, 1)}
HUD_BOXES = {"com.miHoYo.Yuanshen": GI_HUD_BOXES,
             "com.unity.megacity.metro": {"settings": (0, 0, 0.07, 0.13), "throttle": (0.88, 0.48, 1, 0.95),
                                          "aim_stick": (0.11, 0.33, 0.29, 0.60)}}
ALIGN_REGION = (0.20, 0.15, 0.80, 0.80)
ALIGN_MAX_PX = 0.25
DUP_THRESH = 1.0       # 与 phonefarm capture 冻结判定一致
FLAT_STD = 6.0         # 灰度标准差低于此值视为纯色帧 (黑屏/白屏/加载底图)
VAL_MOD, VAL_REM = 5, 4


def size_arg(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def psnr(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def ssim(a, b):
    """灰度 SSIM, 8x8 不重叠块均值 (与 selfsr_ab.ssim 同定义, 这里收 RGB uint8 数组)。"""
    def gray(x):
        return x.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    x, y = gray(a), gray(b)
    h, w = (x.shape[0] // 8) * 8, (x.shape[1] // 8) * 8
    x = x[:h, :w].reshape(h // 8, 8, w // 8, 8)
    y = y[:h, :w].reshape(h // 8, 8, w // 8, 8)
    mx, my = x.mean(axis=(1, 3)), y.mean(axis=(1, 3))
    vx, vy = x.var(axis=(1, 3)), y.var(axis=(1, 3))
    cxy = ((x - mx[:, None, :, None]) * (y - my[:, None, :, None])).mean(axis=(1, 3))
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float((((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx ** 2 + my ** 2 + c1) * (vx + vy + c2))).mean())


def phase_shift(a, b):
    """a 相对 b 的平移 (dx, dy), 相位相关峰 + 三点抛物线亚像素。输入灰度 float 数组。"""
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2((a - a.mean()) * win), np.fft.fft2((b - b.mean()) * win)
    r = fa * np.conj(fb)
    c = np.real(np.fft.ifft2(r / (np.abs(r) + 1e-12)))
    py, px = np.unravel_index(int(np.argmax(c)), c.shape)

    def sub(cm, c0, cp):
        d = cm - 2 * c0 + cp
        return 0.0 if d == 0 else 0.5 * (cm - cp) / d
    h, w = c.shape
    dy = py + sub(c[(py - 1) % h, px], c[py, px], c[(py + 1) % h, px])
    dx = px + sub(c[py, (px - 1) % w], c[py, px], c[py, (px + 1) % w])
    dy = dy - h if dy > h / 2 else dy
    dx = dx - w if dx > w / 2 else dx
    return float(dx), float(dy)


def tiles(w, h, t, boxes):
    """HR 上 t×t 网格 (从左上起, 不重叠), 返回 (x, y, hud 命中的排除区名列表)。"""
    out = []
    for y in range(0, h - t + 1, t):
        for x in range(0, w - t + 1, t):
            hit = [n for n, (x0, y0, x1, y1) in boxes.items()
                   if x < x1 * w and x + t > x0 * w and y < y1 * h and y + t > y0 * h]
            out.append((x, y, hit))
    return out


def derive(src, a):
    """HR = 截图 LANCZOS 到 --hr-size (framedump 帧已是这个尺寸, 不动), LR = HR BOX 到 --lr-size (确定性, 发布时按原件重算, 不在内存里囤整批)。"""
    hr = src if src.size == a.hr_size else src.resize(a.hr_size, Image.LANCZOS)
    return hr, hr.resize(a.lr_size, Image.BOX)


RGB_ORDER = {37: (0, 1, 2), 44: (2, 1, 0)}   # framedump 的 vk_format → R, G, B 在每像素 4 字节里的位置


def open_frame(r):
    """读一帧成 RGB; .raw 按 RGBA 读, alpha 不全是 255 (屏幕上不起作用), 丢掉。"""
    p = os.path.join(r["frames_dir"], r["file"])
    if p.endswith(".raw"):
        a = np.frombuffer(open(p, "rb").read(), np.uint8).reshape(r["h"], r["w"], 4)
        return Image.fromarray(a[..., RGB_ORDER[r["vk_format"]]])
    return Image.open(p).convert("RGB")


def label(img, text):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 8 + 7 * len(text), 16), fill=(0, 0, 0))
    d.text((4, 2), text, fill=(255, 255, 255))
    return img


def previews(root, items, a, hud_boxes, n_sheet=24, n_crop=4, crop=(240, 160), zoom=2):
    """展示用 (不进清单): contact_sheet.jpg (均匀取 n_sheet 张 HR 缩略图) 与 crop_NN.png (HR | LR 最近邻放大 | bicubic)。
    items: [(HR 路径, 标签)], LR 由 HR (无损 PNG) 按同一 BOX 核重算。裁剪框取该帧场景块里梯度能量最高的块中心。"""
    pdir = os.path.join(root, "preview")
    os.makedirs(pdir, exist_ok=True)
    if not items:
        return {}
    pick = [items[round(i * (len(items) - 1) / max(1, n_sheet - 1))] for i in range(min(n_sheet, len(items)))]
    tw, th = 356, round(356 * a.hr_size[1] / a.hr_size[0])
    cols = 4
    sheet = Image.new("RGB", (cols * tw, -(-len(pick) // cols) * th), (0, 0, 0))
    for k, (hp, lab) in enumerate(pick):
        im = Image.open(hp).convert("RGB").resize((tw, th), Image.BILINEAR)
        sheet.paste(label(im, lab), ((k % cols) * tw, (k // cols) * th))
    sheet.save(os.path.join(pdir, "contact_sheet.jpg"), quality=88)
    crops = []
    cw, ch = crop
    sx, sy = a.lr_size[0] / a.hr_size[0], a.lr_size[1] / a.hr_size[1]
    for k in range(min(n_crop, len(items))):
        hp, lab = items[round((k + 0.5) * len(items) / n_crop - 0.5)]
        hr = Image.open(hp).convert("RGB")
        lr = hr.resize(a.lr_size, Image.BOX)
        g = np.asarray(hr.convert("L"), dtype=np.float64)
        e = np.abs(np.diff(g, axis=0))[:, :-1] + np.abs(np.diff(g, axis=1))[:-1, :]
        best = max(((x, y) for x, y, hit in tiles(*a.hr_size, a.tile, hud_boxes) if not hit),
                   key=lambda p: e[p[1]:p[1] + a.tile, p[0]:p[0] + a.tile].mean(), default=(0, 0))
        x0 = max(0, min(a.hr_size[0] - cw, best[0] + a.tile // 2 - cw // 2))
        y0 = max(0, min(a.hr_size[1] - ch, best[1] + a.tile // 2 - ch // 2))
        box = (x0, y0, x0 + cw, y0 + ch)
        lbox = tuple(round(v * s) for v, s in zip(box, (sx, sy, sx, sy)))
        bic = lr.resize(a.hr_size, Image.BICUBIC).crop(box)
        panels = [("HR " + lab, hr.crop(box)), ("LR %dx%d (nearest)" % a.lr_size, lr.crop(lbox).resize((cw, ch), Image.NEAREST)),
                  ("bicubic %.2f dB" % psnr(np.asarray(bic), np.asarray(hr.crop(box))), bic)]
        out = Image.new("RGB", (3 * cw * zoom + 2 * 6, ch * zoom), (255, 255, 255))
        for j, (t, im) in enumerate(panels):
            out.paste(label(im.resize((cw * zoom, ch * zoom), Image.NEAREST), t), (j * (cw * zoom + 6), 0))
        name = f"crop_{k + 1:02d}.png"
        out.save(os.path.join(pdir, name))
        crops.append({"file": name, "frame": lab, "hr_box": list(box), "lr_box": list(lbox)})
    meta = {"contact_sheet": "contact_sheet.jpg", "sheet_frames": [p[1] for p in pick], "crops": crops,
            "note": "展示用, 不属于数据集; LR 面板为最近邻放大, 只看像素量"}
    json.dump(meta, open(os.path.join(pdir, "preview.json"), "w"), ensure_ascii=False, indent=1)
    return meta


def raw_clips(src, stride):
    """原始段目录 → 和 clips.json 一样的结构; 只给关键帧算哈希 (一段几 GB)。没有 summary.json 的段不完整, 整段不要 (§3.8)。"""
    clips = []
    for p in sorted(glob.glob(os.path.join(src, "**", "session.json"), recursive=True)):
        d = os.path.dirname(p)
        if not os.path.exists(os.path.join(d, "index.csv")):
            continue
        if not os.path.exists(os.path.join(d, "summary.json")):
            print(f"{d} 没有 summary.json, 整段作废", file=sys.stderr)
            continue
        s = json.load(open(p))
        rows = [r for r in csv.DictReader(open(os.path.join(d, "index.csv"))) if r["file"].endswith(".raw")]
        frames = []
        for n, r in enumerate(rows):
            fp = os.path.join(d, r["file"])
            key = n % stride == 0
            frames.append({"seq": int(r["seq"]), "frame": int(r["frame"]), "t_ns": int(r["t_ns"]), "dt_ns": int(r["dt_ns"]), "gap_before": int(r["gap_before"]), "file": os.path.relpath(fp, src),
                           "bytes": s["w"] * s["h"] * 4, "sha256": sha256_file(fp) if key else None, "raw_sha256": None, "key": key})
        clips.append({"label": os.path.basename(d), "motion": None, "w": s["w"], "h": s["h"], "vk_format": s["vk_format"], "pre_transform": s.get("pre_transform"), "wall_t0": 0, "t0_ns": 0, "frames": frames})
    return clips


def framedump_rows(clips):
    """段列表 → 关键帧的行, 字段和 manifest.jsonl 一样; 每段是一个 segment。"""
    rows = []
    for seg, c in enumerate(clips):
        for f in c["frames"]:
            if f["key"]:
                rows.append({"i": f["frame"], "file": f["file"], "ts_ms": round(c["wall_t0"] * 1000 + (f["t_ns"] - c["t0_ns"]) / 1e6), "step": f["seq"], "state": "explore",
                             "w": c["w"], "h": c["h"], "vk_format": c["vk_format"], "pre_transform": c.get("pre_transform"), "bytes": f["bytes"], "sha256": f["sha256"], "segment": seg, "motion": c["motion"], "clip": c["label"], "continuous": True})
    return rows


def find_framedump(src, stride):
    """framedump 输入 → (帧目录, 段列表, batch.json 内容); 不是就返回 None。"""
    cj = [p for p in sorted(glob.glob(os.path.join(src, "**", "clips.json"), recursive=True))]
    cj = [p for p in cj if os.path.basename(os.path.dirname(p)) != "key"] or cj   # 产物目录里 key/ 也有一份
    if len(cj) > 1:
        sys.exit(f"{src} 下应恰有一个 clips.json, 找到 {len(cj)} 个")
    if cj:
        return os.path.dirname(cj[0]), json.load(open(cj[0]))["clips"], json.load(open(os.path.join(os.path.dirname(cj[0]), "batch.json")))
    clips = raw_clips(src, stride)
    if not clips:
        return None
    bs = glob.glob(os.path.join(src, "**", "batch.json"), recursive=True)
    return src, clips, dict(json.load(open(bs[0])) if bs else {}, capture_method="framedump")


def find_capture(src, stride=30):
    fd = find_framedump(src, stride)
    if fd:
        return fd[0], framedump_rows(fd[1]), fd[2]
    ms = sorted(glob.glob(os.path.join(src, "**", "manifest.jsonl"), recursive=True))
    if len(ms) != 1:
        sys.exit(f"{src} 下应恰有一个 manifest.jsonl, 找到 {len(ms)} 个")
    bp = glob.glob(os.path.join(src, "**", "batch.json"), recursive=True)
    batch = json.load(open(bp[0])) if bp else {}
    rows = [json.loads(line) for line in open(ms[0]) if line.strip()]
    return os.path.dirname(ms[0]), rows, batch


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="gi-sim")
    ap.add_argument("--version", default="0")
    ap.add_argument("--batch", action="append", default=[], help="batch-id, 缺省 gi-<首帧日期>-01; 多批时逐批给")
    ap.add_argument("--hr-size", type=size_arg, default=(2141, 969))
    ap.add_argument("--lr-size", type=size_arg, default=(1505, 681))
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--psnr-min", type=float, default=28.0)
    ap.add_argument("--breaker", type=float, default=0.05)
    ap.add_argument("--test-segments", type=int, default=None, help="路线末尾留作 test 的段数, 缺省 max(1, 段数的 10%%)")
    ap.add_argument("--stride", type=int, default=30, help="原始段的关键帧间隔 (帧), 相邻帧不算独立样本")
    ap.add_argument("--no-guard", action="store_true", help="--test-seg 不丢相邻段 (framedump: 段与段之间走动换了地方, 不是相邻帧)")
    ap.add_argument("--write-lr", action="store_true", help="模拟 LR 也落盘 (缺省只存配方)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="人工看图排除的帧: 每行 <帧号或 a-b> <原因>, # 开头为注释 (菜单/对话/加载页等, §3.8 有效画面); 多批时逐批给")
    ap.add_argument("--labels", action="append", default=[], help="人看图写的场景标签, 格式见文件头; 多批时逐批给")
    ap.add_argument("--test-seg", action="append", default=[], metavar="BATCH:SEG", help="人选的 test 段 (批内段号), 可重复")
    ap.add_argument("--no-preview", action="store_true", help="不出 preview/ (接触表与 HR/LR/bicubic 裁剪对比)")
    a = ap.parse_args()
    for name, given in (("--batch", a.batch), ("--exclude", a.exclude), ("--labels", a.labels)):
        if given and len(given) != len(a.src):
            sys.exit(f"{name} 给了 {len(given)} 个, 输入有 {len(a.src)} 批")

    def ranged(path):
        for line in open(path, encoding="utf-8") if path else []:
            line = line.split("#", 1)[0].strip()
            if line:
                rng, _, rest = line.partition(" ")
                lo, _, hi = rng.partition("-")
                for i in range(int(lo), int(hi or lo) + 1):
                    yield i, rest.strip()
    rows, batches, seg_base, excluded, labels, methods = [], [], 0, {}, {}, set()
    for k, src in enumerate(a.src):
        frames_dir, batch_rows, batch = find_capture(src, a.stride)
        if not batch_rows:
            sys.exit(f"{src} 的 manifest 为空")
        day = datetime.datetime.fromtimestamp(batch_rows[0]["ts_ms"] / 1000, datetime.timezone.utc).strftime("%Y%m%d")
        batch_id = a.batch[k] if a.batch else f"gi-{day}-01"
        for i, why in ranged(a.exclude[k] if a.exclude else None):
            excluded[batch_id, i] = why or "manual"
        for i, words in ranged(a.labels[k] if a.labels else None):
            scene_type, pose, lighting, *basis = words.split()
            labels[batch_id, i] = {"scene_type": scene_type, "motion": "static", "pose": pose, "lighting": lighting,
                                   "lighting_basis": basis[0] if basis else "seen"}
        for r in batch_rows:   # 段号全数据集唯一, 偏移取 VAL_MOD 的倍数使 val 规则不变
            r.update(batch_id=batch_id, frames_dir=frames_dir, batch_segment=r["segment"], segment=seg_base + r["segment"])
        rows += batch_rows
        methods.add(batch.get("capture_method", "screencap"))
        batches.append({"id": batch_id, "capture": batch.get("capture"), "meta": {k2: batch.get(k2) for k2 in (
            "node_id", "job_id", "experiment_commit", "research", "package", "version_name", "version_code", "fingerprint",
            "android", "platform", "gles", "screen", "render_size", "graphics_prefs")}})
        seg_base += (max(r["batch_segment"] for r in batch_rows) // VAL_MOD + 1) * VAL_MOD
    meta_of = {b["id"]: b["meta"] for b in batches}
    (hw, hh), (lw, lh), t = a.hr_size, a.lr_size, a.tile
    packages = {b["meta"]["package"] or "com.miHoYo.Yuanshen" for b in batches}   # 老批次可能没有 batch.json
    if len(packages) != 1 or not packages <= set(HUD_BOXES):
        sys.exit(f"一个版本只放一款有 HUD 排除框的游戏, 现在是 {sorted(packages)}")
    hud_boxes = HUD_BOXES[packages.pop()]
    for b in batches:
        if b["meta"]["render_size"] not in (None, f"{hw}x{hh}"):
            sys.exit(f"--hr-size {hw}x{hh} 和 {b['id']} 的实际渲染尺寸 {b['meta']['render_size']} 不一致")
    root = os.path.join(a.out, f"{a.name}-v{a.version}")
    if os.path.exists(os.path.join(root, "qa_report.json")):
        sys.exit(f"{root} 已存在; 版本发布后不改, 换 --version")
    if len(methods) != 1:
        sys.exit(f"一个版本只放一种采集方法, 现在是 {sorted(methods)}")
    capture_method = methods.pop()
    derivation = {"hr": {"from": capture_method, "kernel": "PIL.LANCZOS" if capture_method == "screencap" else None, "size": [hw, hh]},
                  "lr": {"from": "hr", "kernel": "PIL.BOX", "size": [lw, lh]},
                  "scale": [hw / lw, hh / lh], "pixel_center": "PIL 像素中心 (x+0.5)/w 映射", "colorspace": "sRGB8"}

    # 1 完整性: 任一帧不符 → 整段作废
    bad_seg, integrity = set(), []
    for r in rows:
        p = os.path.join(r["frames_dir"], r["file"])
        err = None
        if not os.path.isfile(p):
            err = "missing"
        elif os.path.getsize(p) != r["bytes"]:
            err = "bytes"
        elif sha256_file(p) != r["sha256"]:
            err = "sha256"
        else:
            try:
                if not p.endswith(".raw"):
                    with Image.open(p) as im:
                        if im.format != "PNG" or im.size != (r["w"], r["h"]):
                            err = "format_or_size"
            except OSError:
                err = "decode"
        if err:
            bad_seg.add(r["segment"])
            integrity.append({"batch": r["batch_id"], "file": r["file"], "segment": r["segment"], "error": err})

    # 划分 (按 segment)
    segs = sorted({r["segment"] for r in rows if r["segment"] not in bad_seg})
    if a.test_seg:
        where = {(r["batch_id"], r["batch_segment"]): r["segment"] for r in rows}
        try:
            test_segs = sorted({where[spec.rpartition(":")[0], int(spec.rpartition(":")[2])] for spec in a.test_seg})
        except KeyError as e:
            sys.exit(f"--test-seg 没有这一段: {e}")
        batch_of = {r["segment"]: r["batch_id"] for r in rows}
        guard_segs = [] if a.no_guard else sorted({n for s in test_segs for n in (s - 1, s + 1)
                                                   if n in batch_of and batch_of[n] == batch_of[s] and n not in test_segs})
        test_rule = f"人选 {len(test_segs)} 段 (每种场景类型至少一个)"
        guard_rule = "不设: 段与段之间走动换了地方" if a.no_guard else "test 同批前后相邻段, 丢弃"
    else:
        n_test = a.test_segments if a.test_segments is not None else max(1, round(len(segs) * 0.1))
        test_segs = segs[-n_test:] if n_test else []
        guard_segs = segs[-n_test - 1:-n_test] if n_test and len(segs) > n_test else []
        test_rule = f"路线末尾连续 {n_test} 段"
        guard_rule = "test 前 1 段, 丢弃"
    split_of = {}
    for s in segs:
        split_of[s] = ("test" if s in test_segs else "guard" if s in guard_segs
                       else "val" if s % VAL_MOD == VAL_REM else "train")

    drops = {"integrity_segment": 0, "excluded": 0, "state": 0, "portrait": 0, "ready_only": 0, "frozen": 0, "flat": 0, "align": 0}
    tile_grid = tiles(hw, hh, t, hud_boxes)
    scene_tiles = [(x, y) for x, y, hit in tile_grid if not hit]
    counts = {"scene_tiles_available": 0, "scene_tiles_psnr_drop": 0, "scene_tiles_align_drop": 0,
              "scene_tiles_kept": 0, "hud_tiles": 0}
    frames, base = [], {s: {"bilinear": {"psnr": [], "ssim": []}, "bicubic": {"psnr": [], "ssim": []}} for s in ("val", "test")}
    tile_psnrs = []
    prev_thumb, prev_batch = None, None
    for r in rows:
        if r["batch_id"] != prev_batch:
            prev_thumb, prev_batch = None, r["batch_id"]
        if r["segment"] in bad_seg:
            drops["integrity_segment"] += 1
            continue
        if (r["batch_id"], r["i"]) in excluded:
            drops["excluded"] += 1
            continue
        if r.get("ready_only"):
            drops["ready_only"] += 1
            continue
        if r["w"] <= r["h"]:
            drops["portrait"] += 1
            continue
        if r.get("state") != "explore":
            drops["state"] += 1
            continue
        if r.get("diff_prev") is not None and r["diff_prev"] < DUP_THRESH:
            drops["frozen"] += 1
            continue
        src = open_frame(r)
        if r.get("diff_prev") is None and not r.get("continuous"):   # 采集端没算 (cruise): 离线按同一定义算, 与上一保留帧比
            th = np.asarray(src.convert("L").resize((64, 36), Image.BILINEAR), dtype=np.float64)
            if prev_thumb is not None and np.abs(th - prev_thumb).mean() < DUP_THRESH:
                drops["frozen"] += 1
                continue
            prev_thumb = th
        hr_img, lr_img = derive(src, a)
        hr = np.asarray(hr_img)
        if hr.astype(np.float64).mean(axis=2).std() < FLAT_STD:
            drops["flat"] += 1
            continue
        bic = np.asarray(lr_img.resize((hw, hh), Image.BICUBIC))
        x0, y0, x1, y1 = (round(ALIGN_REGION[0] * hw), round(ALIGN_REGION[1] * hh),
                          round(ALIGN_REGION[2] * hw), round(ALIGN_REGION[3] * hh))
        g = np.array([0.299, 0.587, 0.114])
        dx, dy = phase_shift(bic[y0:y1, x0:x1].astype(np.float64) @ g, hr[y0:y1, x0:x1].astype(np.float64) @ g)
        split = split_of[r["segment"]]
        counts["hud_tiles"] += len(tile_grid) - len(scene_tiles)
        if split != "guard":
            counts["scene_tiles_available"] += len(scene_tiles)
        if abs(dx) > ALIGN_MAX_PX or abs(dy) > ALIGN_MAX_PX:
            drops["align"] += 1
            if split != "guard":
                counts["scene_tiles_align_drop"] += len(scene_tiles)
            continue
        kept, dropped = [], []
        bil = np.asarray(lr_img.resize((hw, hh), Image.BILINEAR)) if split in base else None
        for x, y in scene_tiles:
            hp, bp = hr[y:y + t, x:x + t], bic[y:y + t, x:x + t]
            v = psnr(bp, hp)
            tile_psnrs.append(v)
            (kept if v > a.psnr_min else dropped).append({"x": x, "y": y, "psnr_bicubic": round(v, 3)})
            if v > a.psnr_min and split in base:
                lp = bil[y:y + t, x:x + t]
                base[split]["bicubic"]["psnr"].append(v)
                base[split]["bicubic"]["ssim"].append(ssim(bp, hp))
                base[split]["bilinear"]["psnr"].append(psnr(lp, hp))
                base[split]["bilinear"]["ssim"].append(ssim(lp, hp))
        if split != "guard":
            counts["scene_tiles_psnr_drop"] += len(dropped)
            counts["scene_tiles_kept"] += len(kept)
        frames.append({"r": r, "split": split, "align": [round(dx, 4), round(dy, 4)],
                       "tiles": kept, "tiles_dropped": dropped, "luma_mean": round(float(hr.mean()), 2),
                       "psnr_full_bicubic": round(psnr(bic, hr), 3)})
        print(f"{r['file']} seg{r['segment']:03d} {split:5s} 对齐 ({dx:+.3f},{dy:+.3f}) 块 {len(kept)}/{len(scene_tiles)}",
              file=sys.stderr)

    avail = counts["scene_tiles_available"]
    drop_ratio = (counts["scene_tiles_psnr_drop"] + counts["scene_tiles_align_drop"]) / avail if avail else 1.0
    published = avail > 0 and drop_ratio <= a.breaker
    split_n = {s: {"frames": sum(1 for f in frames if f["split"] == s), "tiles": sum(len(f["tiles"]) for f in frames if f["split"] == s),
                   "segments": sorted({f["r"]["segment"] for f in frames if f["split"] == s})} for s in ("train", "val", "test", "guard")}
    fingerprint = hashlib.sha256(json.dumps([[f["r"]["sha256"], f["split"]] for f in frames] + [derivation, a.tile, a.psnr_min],
                                            sort_keys=True).encode()).hexdigest()

    def mean(v):
        return round(float(np.mean(v)), 4) if v else None
    baseline = {s: {m: {k: mean(v) for k, v in d.items()} for m, d in base[s].items()} for s in base}
    batch_ids = [b["id"] for b in batches]
    qa = {"schema": "hgamey/dataset-qa@1", "dataset": f"{a.name}-v{a.version}", "batch": batch_ids[0] if len(batch_ids) == 1 else batch_ids, "published": published,
          "rows": len(rows), "frames_kept": len(frames), "frame_drops": drops,
          "excluded": [{"batch": b, "frame": i, "reason": w} for (b, i), w in sorted(excluded.items())
                       if any(r["batch_id"] == b and r["i"] == i for r in rows)], "integrity_failures": integrity,
          "tiles": counts, "drop_ratio": round(drop_ratio, 5), "breaker": a.breaker, "psnr_min": a.psnr_min,
          "align_max_px": ALIGN_MAX_PX, "tile": a.tile, "tiles_per_frame": {"scene": len(scene_tiles), "hud": len(tile_grid) - len(scene_tiles)},
          "tile_psnr_bicubic": {"min": round(min(tile_psnrs), 3) if tile_psnrs else None, "mean": mean(tile_psnrs),
                                "p05": round(float(np.percentile(tile_psnrs, 5)), 3) if tile_psnrs else None},
          "align_abs_max": [max((abs(f["align"][0]) for f in frames), default=None), max((abs(f["align"][1]) for f in frames), default=None)],
          "splits": split_n, "baseline": baseline}
    os.makedirs(root, exist_ok=True)
    json.dump(qa, open(os.path.join(root, "qa_report.json"), "w"), ensure_ascii=False, indent=1)
    if not published:
        print(f"熔断: 丢弃块占比 {drop_ratio:.2%} > {a.breaker:.0%}, 不发布 (见 qa_report.json)", file=sys.stderr)
        return 3

    index, shown = [], []
    frames = [f for f in frames if f["split"] != "guard"]   # guard 段只隔开 test, 不落盘
    clip_info, clip_all = [], {"schema": "hgamey/framedump-clips@1", "clips": []}   # framedump: 每段的全部帧 (含没发布的), 帧号和哈希留在 clips.json
    for b_i, src in enumerate(a.src):
        fd = find_framedump(src, a.stride)
        if not fd:
            continue
        batch_id = batches[b_i]["id"]
        for c in fd[1]:
            seg = next(r["segment"] for r in rows if r["batch_id"] == batch_id and r.get("clip") == c["label"])
            if split_of.get(seg, "guard") == "guard":
                continue
            kept = [x for x in c["frames"] if (batch_id, x["frame"]) not in excluded]
            ts = [x["t_ns"] for x in kept]
            clip_info.append({"batch": batch_id, "label": c["label"], "segment": seg, "split": split_of[seg], "motion": c["motion"], "frames_total": len(kept), "frames_excluded": len(c["frames"]) - len(kept),
                              "first_frame": kept[0]["frame"], "last_frame": kept[-1]["frame"], "span_s": round((ts[-1] - ts[0]) / 1e9, 3), "gaps": sum(x["gap_before"] for x in kept), "png_bytes": sum(x["bytes"] for x in kept)})
            clip_all["clips"].append({"batch": batch_id, "segment": seg, "label": c["label"], "motion": c["motion"], "frames": [{k: x[k] for k in ("frame", "t_ns", "dt_ns", "gap_before", "file", "bytes", "sha256", "raw_sha256")} for x in kept]})
    for f in frames:
        r, seg = f["r"], f["r"]["segment"]
        batch_id = r["batch_id"]
        f["hr"], f["lr"] = derive(open_frame(r), a)
        scene = f"seg{seg:03d}"
        d = os.path.join(root, "batches", batch_id, scene)
        os.makedirs(os.path.join(d, "hr"), exist_ok=True)
        name = f"f{r['i']:010d}.png"
        hp = os.path.join(d, "hr", name)
        f["hr"].save(hp)
        files = {"src": {"file": r["file"], "bytes": r["bytes"], "sha256": r["sha256"], "w": r["w"], "h": r["h"]},
                 "hr": {"file": f"hr/{name}", "bytes": os.path.getsize(hp), "sha256": sha256_file(hp)}}
        if a.write_lr:
            os.makedirs(os.path.join(d, "lr"), exist_ok=True)
            lp = os.path.join(d, "lr", name)
            f["lr"].save(lp)
            files["lr"] = {"file": f"lr/{name}", "bytes": os.path.getsize(lp), "sha256": sha256_file(lp)}
        else:
            files["lr"] = None
        shown.append((hp, f"seg{seg:03d} f{r['i']} {f['split']}"))
        f.pop("hr"), f.pop("lr")
        index.append({
            "source": dict(meta_of[batch_id], batch=batch_id, scene=scene, batch_segment=r["batch_segment"], frame=r["i"], kind="hr_downsampled",
                           capture_method=capture_method, ts_ms=r["ts_ms"], step=r["step"], **({"clip": r["clip"]} if r.get("clip") else {})),
            "derivation": derivation,
            "size": {"hr": [hw, hh], "lr": [lw, lh], "scale": derivation["scale"], "colorspace": "sRGB8", "pre_transform": r.get("pre_transform")},
            "tags": {"scene_type": None, "motion": None, "lighting": None, **labels.get((batch_id, r["i"]), {}),
                     **({"motion": r["motion"]} if r.get("motion") else {}), "hud": "standard",
                     "time_weather": None, "auto_luma_mean": f["luma_mean"]},
            "checks": {"files": files, "align_dx_dy": f["align"], "psnr_full_bicubic": f["psnr_full_bicubic"],
                       "tiles_kept": len(f["tiles"]), "tiles_dropped": len(f["tiles_dropped"]), "mask_coverage": None},
            "split": f["split"], "path": os.path.join("batches", batch_id, scene, files["hr"]["file"])})
    for batch_id, scene in sorted({(e["source"]["batch"], e["source"]["scene"]) for e in index}):
        json.dump([e for e in index if e["source"]["batch"] == batch_id and e["source"]["scene"] == scene],
                  open(os.path.join(root, "batches", batch_id, scene, "meta.json"), "w"), ensure_ascii=False, indent=1)
    json.dump({"schema": "hgamey/dataset-manifest@1", "dataset": f"{a.name}-v{a.version}", "version": a.version,
               "batches": batches,
               "source_kind": "hr_downsampled", "derivation": derivation, "frames": index, **({"clips": clip_info} if clip_info else {})},
              open(os.path.join(root, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    json.dump({"schema": "hgamey/dataset-split@1", "dataset": f"{a.name}-v{a.version}", "fingerprint": fingerprint,
               "rule": {"test": test_rule, "guard": guard_rule, "val": f"segment % {VAL_MOD} == {VAL_REM}",
                        "train": "其余"},
               "thresholds": {"psnr_min": a.psnr_min, "align_max_px": ALIGN_MAX_PX, "breaker": a.breaker, "dup_thresh": DUP_THRESH,
                              "flat_std": FLAT_STD},
               "tile": {"size": a.tile, "space": "hr", "lr_box": "x*lw/hw, y*lh/hh (非整数, 训练端按比例取)", "hud_boxes": hud_boxes},
               "baseline_locked": baseline, "segments": {str(s): split_of[s] for s in segs},
               "segment_source": {str(s): sorted({(r["batch_id"], r["batch_segment"]) for r in rows if r["segment"] == s}) for s in segs},
               "frames": [{"path": e["path"], "split": e["split"], "segment": f["r"]["segment"],
                           "tiles": [[tt["x"], tt["y"]] for tt in f["tiles"]]} for e, f in zip(index, frames)]},
              open(os.path.join(root, "split.json"), "w"), ensure_ascii=False, indent=1)
    if clip_info:
        json.dump(clip_all, open(os.path.join(root, "clips.json"), "w"), ensure_ascii=False)
    if not a.no_preview:
        previews(root, shown, a, hud_boxes)
    print(json.dumps({"dataset": root, "frames": len(frames), "splits": {s: (v["frames"], v["tiles"]) for s, v in split_n.items()},
                      "drop_ratio": qa["drop_ratio"], "baseline": baseline}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
