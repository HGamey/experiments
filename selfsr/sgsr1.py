#!/usr/bin/env python3
"""sgsr1.py — Qualcomm SGSR1 (mobile, GLSL sgsr1_shader_mobile.frag) 的 numpy 移植, 供离线画质对比。

源: https://github.com/SnapdragonGameStudios/snapdragon-gsr  sgsr/v1/include/glsl/sgsr1_shader_mobile.frag
(BSD-3-Clause, Copyright (c) 2023 Qualcomm Innovation Center)。逐式移植, 非官方实现。

算法: 基底=低分图双线性; 绿通道当亮度 (OperationMode=1)。imgCoord = uv*srcWH + (-0.5,+0.5),
取 4x4 邻域的 12 个 tap (去四角), |亮度差| 和过阈值判边缘; 过阈值的像素用 fastLanczos2(距²·0.55 +
clamp(|c|·std)) 的自适应权重在亮度域重建, clamp(min4,max4) 后把亮度修正量 (±23/255 上限) 加回 RGB。
tap 坐标由 shader 的 weightY(dx,dy) 与 mean 四点 (=双线性单元 {px,px+1}x{py-1,py}, gather 在 texel 角锚点) 自洽解出:
  col = px-dx, row = py+dy  (left 块 {px-1,px}, right 块 {px+1,px+2}; dx 正=向左)。

差异: float32 代替 mediump; 边界用 CLAMP_TO_EDGE。验证状态: 官方 discovery 素材 (1080p→4K) 对比,
本移植 SSIM 0.93-0.94 (双线性 0.95); 官方素材疑似用 UseEdgeDirection 变体生成 (仅 HLSL 头文件有该分支,
GL frag 无), 未逐像素对齐; 用于盲评时标注「SGSR1 mobile (无边缘方向) 移植」。几何映射已自洽验证:
mean 四点=双线性单元, tap col=px-dx, row=py+dy (gather 角锚点)。

    python3 -I selfsr/sgsr1.py --out up.png <低分图.png> <输出宽x高>
    python3 -I selfsr/sgsr1.py --validate <原始图.png> <官方SGSR输出.png>   # 试 3x/4x 降分重建, 报 SSIM
"""
import argparse
import sys

import numpy as np
from PIL import Image


def _bilinear(src, u, v):
    """src (H,W,3) float; u,v 像素连续坐标 (texel 中心 = 整数+0.5); CLAMP_TO_EDGE。"""
    h, w = src.shape[:2]
    x = np.clip(u - 0.5, 0, w - 1)
    y = np.clip(v - 0.5, 0, h - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    a = src[y0, x0] * (1 - fx) + src[y0, x1] * fx
    b = src[y1, x0] * (1 - fx) + src[y1, x1] * fx
    return a * (1 - fy) + b * fy


def _fast_lanczos2(x):
    w_a = x - 4.0
    w_b = x * w_a - w_a
    w_a = w_a * w_a
    return w_b * w_a


# 12 个 tap: (dx, dy, 取样名) — dx,dy 与 shader weightY 的参数一致; col=px-dx, row=py+dy
_TAPS = ((0, 1, "updown_x"), (-1, 1, "updown_y"), (-1, -2, "updown_z"), (0, -2, "updown_w"),
         (1, -1, "left_x"), (0, -1, "left_y"), (0, 0, "left_z"), (1, 0, "left_w"),
         (-1, -1, "right_x"), (-2, -1, "right_y"), (-2, 0, "right_z"), (-1, 0, "right_w"))
# mean 的四个点 (双线性单元): left_y, left_z, right_x, right_w
_CELL = ("left_y", "left_z", "right_x", "right_w")


def sgsr1(src, w_out, h_out, edge_threshold=8.0 / 255.0, edge_sharpness=2.0, y_off=0.5, flip=False):
    """src: (Hs,Ws,3) float [0,1] → (h_out,w_out,3) float [0,1]。
    y_off/flip: 官方参考图对齐实验参数 (frag 里 imgCoord.y = uv*srcH + 0.5; 行序约定可能差半像素/上下翻转)。"""
    if flip:
        src = src[::-1]
        out = sgsr1(src, w_out, h_out, edge_threshold, edge_sharpness, y_off, False)
        return out[::-1]
    h, w = src.shape[:2]
    oy, ox = np.mgrid[0:h_out, 0:w_out]
    u = (ox + 0.5) * w / w_out
    v = (oy + 0.5) * h / h_out
    base = _bilinear(src, u, v)                       # 基底 = 双线性
    g_base = base[..., 1]                             # OperationMode=1: 绿通道当亮度

    img_x = u - 0.5
    img_y = v + y_off                                 # frag: uv*srcWH + (-0.5, +0.5)
    px = np.floor(img_x).astype(np.int64)
    py = np.floor(img_y).astype(np.int64)
    plx = img_x - px
    ply = img_y - py

    lum = src[..., 1]
    lum = np.pad(lum, 2, mode="edge")                 # CLAMP_TO_EDGE; tap 范围 -2..+2, pad 2 覆盖

    def tap(name):
        dx, dy, nm = next(t for t in _TAPS if t[2] == name)
        return lum[py + dy + 2, px - dx + 2]        # +2: pad; 推导: col = px-dx, row = py+dy (gather 角锚点+单元自洽)

    t = {nm: tap(nm) for _, _, nm in _TAPS}
    edge_vote = np.abs(t["left_z"] - t["left_y"]) + np.abs(g_base - t["left_y"]) + np.abs(g_base - t["left_z"])
    m = edge_vote > edge_threshold

    mean = sum(t[n] for n in _CELL) * 0.25
    c = {k: val - mean for k, val in t.items()}       # 中心化
    g_c = g_base - mean
    s = sum(np.abs(val) for val in c.values())
    std = 2.181818 / np.maximum(s, 1e-6)              # frag 版: sumMean 直接用 (HLSL 版为平方, 未采用)

    acc_w = np.zeros((h_out, w_out))
    acc_y = np.zeros((h_out, w_out))
    for dx, dy, nm in _TAPS:
        x = (plx + dx) ** 2 + (ply + dy) ** 2
        x = x * 0.55 + np.clip(np.abs(c[nm]) * std, 0.0, 1.0)
        wgt = _fast_lanczos2(x)
        acc_w += wgt
        acc_y += wgt * c[nm]
    final_y = acc_y / np.maximum(acc_w, 1e-6)
    max4 = np.maximum(np.maximum(c["left_y"], c["left_z"]), np.maximum(c["right_x"], c["right_w"]))
    min4 = np.minimum(np.minimum(c["left_y"], c["left_z"]), np.minimum(c["right_x"], c["right_w"]))
    final_y = np.clip(edge_sharpness * final_y, min4, max4)
    delta = np.clip(final_y - g_c, -23.0 / 255.0, 23.0 / 255.0)

    out = base + delta[..., None]
    return np.where(m[..., None], np.clip(out, 0, 1), base)


def _ssim_gray(a, b):
    x = np.asarray(a.convert("L"), dtype=np.float64)
    y = np.asarray(b.convert("L"), dtype=np.float64)
    h, w = (x.shape[0] // 8) * 8, (x.shape[1] // 8) * 8
    x = x[:h, :w].reshape(h // 8, 8, w // 8, 8)
    y = y[:h, :w].reshape(h // 8, 8, w // 8, 8)
    mx, my = x.mean(axis=(1, 3)), y.mean(axis=(1, 3))
    vx, vy = x.var(axis=(1, 3)), y.var(axis=(1, 3))
    cxy = ((x - mx[:, None, :, None]) * (y - my[:, None, :, None])).mean(axis=(1, 3))
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float((((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx ** 2 + my ** 2 + c1) * (vx + vy + c2))).mean())


def _validate(orig_path, sgsr_path):
    orig = Image.open(orig_path).convert("RGB")
    ref = Image.open(sgsr_path).convert("RGB")
    wo, ho = orig.size
    for factor in (3, 4):
        for ds in ("box", "bilinear"):
            low = orig.resize((wo // factor, ho // factor), getattr(Image, "BOX" if ds == "box" else "BILINEAR"))
            arr = np.asarray(low, dtype=np.float32) / 255.0
            up = sgsr1(arr, wo, ho)
            img = Image.fromarray((np.clip(up, 0, 1) * 255 + 0.5).astype(np.uint8))
            print(f"降分 {factor}x/{ds}: SSIM(移植 vs 官方输出) = {_ssim_gray(img, ref):.4f}")
            img.save(f".sgsr1_validate_{factor}x_{ds}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lo", help="低分输入 png (--validate 时为原始图)")
    ap.add_argument("hi", nargs="?", help="输出尺寸 WxH (缺省 2x); --validate 时为官方 SGSR 输出图")
    ap.add_argument("--out", default="sgsr1_out.png", help="输出 png")
    ap.add_argument("--validate", action="store_true", help="试 3x/4x 降分重建, 与官方输出比 SSIM")
    a = ap.parse_args()
    if a.validate:
        _validate(a.lo, a.hi)
        return 0
    im = Image.open(a.lo).convert("RGB")
    if a.hi:
        wo, ho = (int(v) for v in a.hi.lower().split("x"))
    else:
        wo, ho = im.width * 2, im.height * 2
    up = sgsr1(np.asarray(im, dtype=np.float32) / 255.0, wo, ho)
    Image.fromarray((np.clip(up, 0, 1) * 255 + 0.5).astype(np.uint8)).save(a.out)
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
