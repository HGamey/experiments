#!/usr/bin/env python3
"""make_pairs.py — 方法 C 模拟对 (DATASET-COLLECTION §3.3/§3.8–§3.10): capture 输出目录 → 数据集版本目录 (本机跑, 不占设备)。

    python3 -I dataset/make_pairs.py <capture 输出目录> --out <数据集根> [--name gi-sim] [--version 0] [--batch gi-YYYYMMDD-01]
        [--hr-size 2141x969] [--lr-size 1505x681] [--tile 256] [--psnr-min 28] [--breaker 0.05] [--test-segments N] [--write-lr]
        [--no-preview]

输入: capture_native.sh 的产物目录 (含 capture-*/frames/manifest.jsonl, 可选 batch.json)。
构造 (与 selfsr/baseline_quality.py --truth-only 同口径):
  HR = 原生截图 (屏幕分辨率) LANCZOS 缩到游戏送显尺寸 --hr-size;
  LR = HR 经 BOX 缩到 --lr-size (Game Mode 0.56 时游戏实际渲染尺寸); source.kind=hr_downsampled。
  LR 只存配方 (§2.2), --write-lr 才落盘。
QA (§3.8, 规则沿用 SPEC_SR_LOOP §4.1, 倍率换成约 1.42):
  1 完整性: 文件字节数 / sha256 / 尺寸与 manifest 一致, 否则整段作废;
  2 有效画面: state=explore、横屏、非 ready_only、非冻结 (diff_prev >= 1.0), 再丢近乎纯色帧 (加载/黑屏);
  3 对齐: bicubic(LR)→HR 尺寸, 中央区 (0.20,0.15)-(0.80,0.80) 相位相关 + 三点抛物线亚像素, |dx|,|dy| > 0.25 px 整帧丢;
  4 切块: HR 上 --tile 网格, 与 HUD 排除区相交的块记为 hud (单独标注, 不进训练块);
  5 场景块逐块 bicubic 重建 PSNR (RGB 联合 MSE) > --psnr-min 才保留;
  6 熔断: (PSNR 丢弃块 + 对齐丢弃帧的场景块) / 可用场景块 > --breaker → 不发布 (只写 qa_report.json), 退出码 3。
划分 (§3.9, 按 segment, 同段不跨集): 路线末尾连续 --test-segments 段为 test, 其前 1 段为 guard (丢弃, 隔开相邻帧),
  其余 segment % 5 == 4 为 val, 剩下 train。基线锁定: val/test 场景块上双线性、bicubic 的 PSNR / SSIM 均值。
输出 (§3.10): <out>/<name>-v<version>/{manifest.json, split.json, qa_report.json,
  batches/<batch>/seg<NNN>/{hr/f<10 位帧号>.png, lr/..., meta.json}, preview/ (展示用接触表与裁剪对比)}。
  冻结判定: manifest 的 diff_prev 为 null 时 (cruise 采集) 离线按 64x36 灰度缩略图平均差与上一保留帧比。
  退出码 0 发布, 2 用法/输入错, 3 熔断。
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image

# HUD 排除区 (相对坐标 x0,y0,x1,y1), SPEC_SR_LOOP §4.1 原神实测框
HUD_BOXES = {"top_bar": (0, 0, 1, 0.13), "minimap_quest": (0, 0, 0.17, 0.40), "team": (0.80, 0.13, 1, 0.50),
             "skills": (0.60, 0.58, 1, 1), "joystick_chat": (0, 0.62, 0.25, 1), "hp_bar": (0.35, 0.85, 0.65, 1),
             "bottom_bar": (0, 0.93, 1, 1)}
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


def tiles(w, h, t):
    """HR 上 t×t 网格 (从左上起, 不重叠), 返回 (x, y, hud 命中的排除区名列表)。"""
    out = []
    for y in range(0, h - t + 1, t):
        for x in range(0, w - t + 1, t):
            hit = [n for n, (x0, y0, x1, y1) in HUD_BOXES.items()
                   if x < x1 * w and x + t > x0 * w and y < y1 * h and y + t > y0 * h]
            out.append((x, y, hit))
    return out


def derive(src, a):
    """HR = 截图 LANCZOS 到 --hr-size, LR = HR BOX 到 --lr-size (确定性, 发布时按原件重算, 不在内存里囤整批)。"""
    hr = src.resize(a.hr_size, Image.LANCZOS)
    return hr, hr.resize(a.lr_size, Image.BOX)


def label(img, text):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 8 + 7 * len(text), 16), fill=(0, 0, 0))
    d.text((4, 2), text, fill=(255, 255, 255))
    return img


def previews(root, items, a, n_sheet=24, n_crop=4, crop=(240, 160), zoom=2):
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
        best = max(((x, y) for x, y, hit in tiles(*a.hr_size, a.tile) if not hit),
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


def find_capture(src):
    ms = sorted(glob.glob(os.path.join(src, "**", "manifest.jsonl"), recursive=True))
    if len(ms) != 1:
        sys.exit(f"{src} 下应恰有一个 manifest.jsonl, 找到 {len(ms)} 个")
    bp = glob.glob(os.path.join(src, "**", "batch.json"), recursive=True)
    batch = json.load(open(bp[0])) if bp else {}
    rows = [json.loads(line) for line in open(ms[0]) if line.strip()]
    return os.path.dirname(ms[0]), rows, batch


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="gi-sim")
    ap.add_argument("--version", default="0")
    ap.add_argument("--batch", default=None, help="batch-id, 缺省 gi-<首帧日期>-01")
    ap.add_argument("--hr-size", type=size_arg, default=(2141, 969))
    ap.add_argument("--lr-size", type=size_arg, default=(1505, 681))
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--psnr-min", type=float, default=28.0)
    ap.add_argument("--breaker", type=float, default=0.05)
    ap.add_argument("--test-segments", type=int, default=None, help="路线末尾留作 test 的段数, 缺省 max(1, 段数的 10%%)")
    ap.add_argument("--write-lr", action="store_true", help="模拟 LR 也落盘 (缺省只存配方)")
    ap.add_argument("--no-preview", action="store_true", help="不出 preview/ (接触表与 HR/LR/bicubic 裁剪对比)")
    a = ap.parse_args()
    frames_dir, rows, batch = find_capture(a.src)
    if not rows:
        sys.exit("manifest 为空")
    (hw, hh), (lw, lh), t = a.hr_size, a.lr_size, a.tile
    day = datetime.datetime.fromtimestamp(rows[0]["ts_ms"] / 1000, datetime.timezone.utc).strftime("%Y%m%d")
    batch_id = a.batch or f"gi-{day}-01"
    root = os.path.join(a.out, f"{a.name}-v{a.version}")
    if os.path.exists(os.path.join(root, "qa_report.json")):
        sys.exit(f"{root} 已存在; 版本发布后不改, 换 --version")
    derivation = {"hr": {"from": "screencap", "kernel": "PIL.LANCZOS", "size": [hw, hh]},
                  "lr": {"from": "hr", "kernel": "PIL.BOX", "size": [lw, lh]},
                  "scale": [hw / lw, hh / lh], "pixel_center": "PIL 像素中心 (x+0.5)/w 映射", "colorspace": "sRGB8"}

    # 1 完整性: 任一帧不符 → 整段作废
    bad_seg, integrity = set(), []
    for r in rows:
        p = os.path.join(frames_dir, r["file"])
        err = None
        if not os.path.isfile(p):
            err = "missing"
        elif os.path.getsize(p) != r["bytes"]:
            err = "bytes"
        elif sha256_file(p) != r["sha256"]:
            err = "sha256"
        else:
            try:
                with Image.open(p) as im:
                    if im.format != "PNG" or im.size != (r["w"], r["h"]):
                        err = "format_or_size"
            except OSError:
                err = "decode"
        if err:
            bad_seg.add(r["segment"])
            integrity.append({"file": r["file"], "segment": r["segment"], "error": err})

    # 划分 (按 segment)
    segs = sorted({r["segment"] for r in rows if r["segment"] not in bad_seg})
    n_test = a.test_segments if a.test_segments is not None else max(1, round(len(segs) * 0.1))
    test_segs = segs[-n_test:] if n_test else []
    guard_segs = segs[-n_test - 1:-n_test] if n_test and len(segs) > n_test else []
    split_of = {}
    for s in segs:
        split_of[s] = ("test" if s in test_segs else "guard" if s in guard_segs
                       else "val" if s % VAL_MOD == VAL_REM else "train")

    drops = {"integrity_segment": 0, "state": 0, "portrait": 0, "ready_only": 0, "frozen": 0, "flat": 0, "align": 0}
    tile_grid = tiles(hw, hh, t)
    scene_tiles = [(x, y) for x, y, hit in tile_grid if not hit]
    counts = {"scene_tiles_available": 0, "scene_tiles_psnr_drop": 0, "scene_tiles_align_drop": 0,
              "scene_tiles_kept": 0, "hud_tiles": 0}
    frames, base = [], {s: {"bilinear": {"psnr": [], "ssim": []}, "bicubic": {"psnr": [], "ssim": []}} for s in ("val", "test")}
    tile_psnrs = []
    prev_thumb = None
    for r in rows:
        if r["segment"] in bad_seg:
            drops["integrity_segment"] += 1
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
        src = Image.open(os.path.join(frames_dir, r["file"])).convert("RGB")
        if r.get("diff_prev") is None:   # 采集端没算 (cruise): 离线按同一定义算, 与上一保留帧比
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
    qa = {"schema": "hgamey/dataset-qa@1", "dataset": f"{a.name}-v{a.version}", "batch": batch_id, "published": published,
          "rows": len(rows), "frames_kept": len(frames), "frame_drops": drops, "integrity_failures": integrity,
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

    src_meta = {k: batch.get(k) for k in ("node_id", "job_id", "experiment_commit", "research", "package", "version_name",
                                          "version_code", "fingerprint", "android", "platform", "gles", "screen", "render_size",
                                          "graphics_prefs")}
    index, shown = [], []
    frames = [f for f in frames if f["split"] != "guard"]   # guard 段只隔开 test, 不落盘
    for f in frames:
        r, seg = f["r"], f["r"]["segment"]
        f["hr"], f["lr"] = derive(Image.open(os.path.join(frames_dir, r["file"])).convert("RGB"), a)
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
            "source": dict(src_meta, batch=batch_id, scene=scene, frame=r["i"], kind="hr_downsampled",
                           capture_method="screencap", ts_ms=r["ts_ms"], step=r["step"]),
            "derivation": derivation,
            "size": {"hr": [hw, hh], "lr": [lw, lh], "scale": derivation["scale"], "colorspace": "sRGB8", "pre_transform": None},
            "tags": {"scene_type": None, "motion": None, "lighting": None, "hud": "standard", "time_weather": None,
                     "auto_luma_mean": f["luma_mean"]},
            "checks": {"files": files, "align_dx_dy": f["align"], "psnr_full_bicubic": f["psnr_full_bicubic"],
                       "tiles_kept": len(f["tiles"]), "tiles_dropped": len(f["tiles_dropped"]), "mask_coverage": None},
            "split": f["split"], "path": os.path.join("batches", batch_id, scene, files["hr"]["file"])})
    for scene in sorted({e["source"]["scene"] for e in index}):
        json.dump([e for e in index if e["source"]["scene"] == scene],
                  open(os.path.join(root, "batches", batch_id, scene, "meta.json"), "w"), ensure_ascii=False, indent=1)
    json.dump({"schema": "hgamey/dataset-manifest@1", "dataset": f"{a.name}-v{a.version}", "version": a.version,
               "batches": [{"id": batch_id, "capture": batch.get("capture"), "meta": src_meta}],
               "source_kind": "hr_downsampled", "derivation": derivation, "frames": index},
              open(os.path.join(root, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    json.dump({"schema": "hgamey/dataset-split@1", "dataset": f"{a.name}-v{a.version}", "fingerprint": fingerprint,
               "rule": {"test": f"路线末尾连续 {n_test} 段", "guard": "test 前 1 段, 丢弃", "val": f"segment % {VAL_MOD} == {VAL_REM}",
                        "train": "其余"},
               "thresholds": {"psnr_min": a.psnr_min, "align_max_px": ALIGN_MAX_PX, "breaker": a.breaker, "dup_thresh": DUP_THRESH,
                              "flat_std": FLAT_STD},
               "tile": {"size": a.tile, "space": "hr", "lr_box": "x*lw/hw, y*lh/hh (非整数, 训练端按比例取)", "hud_boxes": HUD_BOXES},
               "baseline_locked": baseline, "segments": {str(s): split_of[s] for s in segs},
               "frames": [{"path": e["path"], "split": e["split"], "segment": f["r"]["segment"],
                           "tiles": [[tt["x"], tt["y"]] for tt in f["tiles"]]} for e, f in zip(index, frames)]},
              open(os.path.join(root, "split.json"), "w"), ensure_ascii=False, indent=1)
    if not a.no_preview:
        previews(root, shown, a)
    print(json.dumps({"dataset": root, "frames": len(frames), "splits": {s: (v["frames"], v["tiles"]) for s, v in split_n.items()},
                      "drop_ratio": qa["drop_ratio"], "baseline": baseline}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
