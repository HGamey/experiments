#!/usr/bin/env python3
"""framedump_compare.py — 离线比 framedump 抽帧和同时刻的截图 (framedump_probe.py 的产物目录, §3.5 第 3 步)。

    python3 -I dataset/framedump_compare.py <fd_ 目录> [比对图.jpg]

截图 LANCZOS 缩到帧的尺寸, 对截图前后时刻之内的每张抽帧算 PSNR (整帧、中央区), 取最像的一张再算误差不超过 4、16 级的像素比例。
帧是 RGBA, alpha 不全是 255 (实测最小 103), 屏幕上不起作用, 比较时丢掉。抽帧不一定正好是截图那一瞬间, 风吹的草和人物会拉低分数。
"""
import csv
import json
import os
import sys

import numpy as np
from PIL import Image

d = sys.argv[1]
s = json.load(open(f"{d}/session.json"))
r = json.load(open(f"{d}/report.json"))
w, h = s["w"], s["h"]
rows = {x["file"]: x for x in csv.DictReader(open(f"{d}/index.csv")) if x["file"].endswith(".raw")}


def load(n):
    return np.frombuffer(open(f"{d}/sample/{n}", "rb").read(), np.uint8).reshape(h, w, 4)[..., :3]


def psnr(a, b):
    m = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if m == 0 else 10 * np.log10(255 ** 2 / m)


x0, y0, x1, y1 = int(0.20 * w), int(0.15 * h), int(0.80 * w), int(0.80 * h)
result = []
for sh in r["shots"]:
    down = np.asarray(Image.open(f"{d}/{sh['file']}").convert("RGB").resize((w, h), Image.LANCZOS))
    res = []
    for n in sorted(os.listdir(f"{d}/sample")):
        t = int(rows[n]["t_ns"])
        if sh["t0_ns"] <= t <= sh["t1_ns"]:
            a = load(n)
            res.append({"file": n, "after_ms": round((t - sh["t0_ns"]) / 1e6), "full_db": round(psnr(a, down), 2), "centre_db": round(psnr(a[y0:y1, x0:x1], down[y0:y1, x0:x1]), 2)})
    if not res:
        continue
    best = max(res, key=lambda v: v["full_db"])
    a = load(best["file"])
    e = np.abs(a.astype(np.int16) - down.astype(np.int16)).max(axis=2)
    best.update(within_4=round(float((e <= 4).mean()), 4), within_16=round(float((e <= 16).mean()), 4))
    result.append({"shot": sh["file"], "frames_compared": len(res), "best": best})
    if len(sys.argv) > 2 and len(result) == 1:
        diff = np.clip(e[..., None].repeat(3, 2) * 8, 0, 255).astype(np.uint8)
        W, H = 900, round(900 * h / w)
        sheet = Image.new("RGB", (W * 3, H))
        for i, im in enumerate((a, down, diff)):
            sheet.paste(Image.fromarray(im).resize((W, H), Image.LANCZOS), (i * W, 0))
        sheet.save(sys.argv[2], quality=90)
print(json.dumps({"size": [w, h], "shots": result}, ensure_ascii=False, indent=1))
