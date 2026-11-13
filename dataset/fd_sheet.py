#!/usr/bin/env python3
"""fd_sheet.py — 一档采集结果 (fd_convert 转好的 PNG) 的接触表和速度图对齐检验。要 numpy 和 Pillow。

    python3 -I fd_sheet.py <档目录> [--out sheet.png] [--frames 首,中,尾 | 帧号,...] [--mv-check 3]

接触表: 每帧一行, 带 UI | 不带 UI | 深度 (按本帧 1–99 分位拉伸, 近处亮) | 速度 (色相=方向, 亮度=大小)。
--mv-check N: 取中间连续 N+1 帧, 用速度图把上一帧逐像素对齐到这一帧, 打印对齐前后有纹理处的平均绝对差。
速度图解码 (原神 7.2 实测, 换游戏要重新确认): mv/*.png 的 RGBA 四个字节就是原始 A2R10G10B10 的小端 32 位,
  u = R | G<<8 | B<<16 | A<<24;  r = (u>>20)&1023 水平, g = (u>>10)&1023 竖直, b = u&1023 运动物体掩码 (0/1023)
  v = sign(c-509) * ((c-509)/511.5)^2, 单位是画面宽/高; 上一帧位置 = 这一帧位置 - v。PNG 已翻正, 所以 y 要取反。
"""
import argparse
import csv
import json
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MV_ZERO, MV_SCALE = 509.0, 511.5


def frame_ids(d):
    rows = list(csv.DictReader(open(os.path.join(d, "checks.csv"))))
    return [int(r["frame"]) for r in rows if r["ui"] == "ok"]


def png(d, sub, f):
    p = os.path.join(d, sub, f"f{f:010d}.png")
    return np.array(Image.open(p)) if os.path.exists(p) else None


def mv_decode(rgba):
    u = rgba[..., 0].astype(np.uint32) | (rgba[..., 1].astype(np.uint32) << 8) | (rgba[..., 2].astype(np.uint32) << 16) | \
        (rgba[..., 3].astype(np.uint32) << 24)
    r, g, b = ((u >> 20) & 1023).astype(np.float32), ((u >> 10) & 1023).astype(np.float32), (u & 1023)
    h, w = r.shape
    vx = np.sign(r - MV_ZERO) * ((r - MV_ZERO) / MV_SCALE) ** 2 * w
    vy = -np.sign(g - MV_ZERO) * ((g - MV_ZERO) / MV_SCALE) ** 2 * h   # 已翻正: y 取反
    return vx, vy, b == 1023


def flow_rgb(vx, vy):
    mag = np.hypot(vx, vy)
    top = max(float(np.percentile(mag, 99)), 1.0)
    ang = (np.arctan2(vy, vx) + np.pi) / (2 * np.pi)
    hsv = np.stack([ang * 255, np.full_like(ang, 255), np.clip(mag / top, 0, 1) * 255], -1).astype(np.uint8)
    return np.array(Image.fromarray(hsv, "HSV").convert("RGB")), top


def gray(a):
    return a[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def warp_err(prev, cur, vx, vy, mask):
    h, w = cur.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    x, y = xx - vx, yy - vy
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    fx, fy = x - x0, y - y0
    ok = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)
    x0, y0 = np.clip(x0, 0, w - 2), np.clip(y0, 0, h - 2)
    s = (prev[y0, x0] * (1 - fx) * (1 - fy) + prev[y0, x0 + 1] * fx * (1 - fy) + prev[y0 + 1, x0] * (1 - fx) * fy
         + prev[y0 + 1, x0 + 1] * fx * fy)
    gy, gx = np.gradient(cur)
    m = ok & mask & (np.hypot(gx, gy) > 6)
    return float(np.abs(prev - cur)[m].mean()), float(np.abs(s - cur)[m].mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--out")
    ap.add_argument("--frames")
    ap.add_argument("--mv-check", type=int, default=0)
    ap.add_argument("--width", type=int, default=480)
    a = ap.parse_args()
    ids = frame_ids(a.dir)
    if a.frames:
        pick = [int(x) for x in a.frames.split(",")]
    else:
        pick = [ids[1], ids[len(ids) // 2], ids[-1]] if len(ids) > 2 else ids   # 第 1 帧没有附件, 从第 2 帧起
    rows = []
    for f in pick:
        row = []
        ui = png(a.dir, "ui", f)
        if ui is not None:
            row.append(("带 UI", ui[..., :3]))
        nu = png(a.dir, "noui", f)
        if nu is not None:
            row.append(("不带 UI", nu[..., :3]))
        dp = png(a.dir, "depth16", f)
        if dp is not None:
            z = dp.astype(np.float32)
            lo, hi = np.percentile(z, 1), np.percentile(z, 99)
            v = (np.clip((z - lo) / max(hi - lo, 1), 0, 1) * 255).astype(np.uint8)
            row.append(("深度", np.repeat(v[..., None], 3, 2)))
        mv = png(a.dir, "mv", f)
        if mv is not None:
            vx, vy, _ = mv_decode(mv)
            img, top = flow_rgb(vx, vy)
            row.append((f"速度 (p99 {top:.1f} px)", img))
        rows.append((f, row))
    res = {"dir": a.dir, "frames": pick}
    if a.mv_check and os.path.isdir(os.path.join(a.dir, "mv")):
        mid = len(ids) // 2
        seq = ids[mid:mid + a.mv_check + 1]
        pairs = []
        for p, c in zip(seq, seq[1:]):
            if c != p + 1:
                continue
            a0, a1, mv = png(a.dir, "noui", p), png(a.dir, "noui", c), png(a.dir, "mv", c)
            if a0 is None or a1 is None or mv is None:
                continue
            vx, vy, obj = mv_decode(mv)
            e0, e1 = warp_err(gray(a0), gray(a1), vx, vy, ~obj)
            pairs.append({"prev": p, "cur": c, "err_no_warp": round(e0, 2), "err_mv_warp": round(e1, 2),
                          "disp_px_p50": round(float(np.median(np.hypot(vx, vy))), 2)})
        res["mv_check"] = pairs
    if rows and a.out:
        tw = a.width
        h0, w0 = rows[0][1][0][1].shape[:2]
        th = int(tw * h0 / w0)
        try:
            font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 18)
        except OSError:
            font = ImageFont.load_default()
        ncol = max(len(r) for _, r in rows)
        sheet = Image.new("RGB", (ncol * tw, len(rows) * (th + 26)), "white")
        dr = ImageDraw.Draw(sheet)
        for i, (f, row) in enumerate(rows):
            for j, (lab, img) in enumerate(row):
                sheet.paste(Image.fromarray(np.ascontiguousarray(img)).resize((tw, th)), (j * tw, i * (th + 26) + 26))
                dr.text((j * tw + 4, i * (th + 26) + 3), f"f{f} {lab} {img.shape[1]}x{img.shape[0]}", fill="black", font=font)
        sheet.save(a.out)
        res["sheet"] = a.out
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
