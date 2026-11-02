#!/usr/bin/env python3
"""fsr1.py — AMD FidelityFX FSR 1.0 (EASU 放大 + RCAS 锐化) 的 numpy 移植, 供离线画质对比 (业界基线)。

源: GPUOpen FidelityFX-FSR  ffx-fsr/ffx_fsr1.h  (MIT, Copyright (c) 2021 Advanced Micro Devices, Inc.)。
逐式移植 FsrEasuCon/FsrEasuF (32 位 float 路径) 与 FsrRcasCon/FsrRcasF, 非官方实现。

EASU: pp = (ip+0.5)*in/out - 0.5 (con0), fp = floor(pp), 取 f 周围 12 tap (4x4 去四角):
        b c
      e f g h
      i j k l
        n o
  亮度 L = 0.5R + G + 0.5B (FSR 的「亮度×2」); 4 个双线性象限 (f/g/j/k 为中心的 '+' 形) 按双线性权重累加
  梯度方向 dir 与边缘度 len (FsrEasuSetF: 梯度反转→0, 平方整形); dir 归一化 (|dir|²<1/32768 则取 (1,0)),
  len=(len/2)²; stretch=|dir|²/max(|dx|,|dy|); len2=(1+(stretch-1)len, 1-0.5len) 各向异性;
  lob=0.5-0.29len (负瓣), clp=1/lob。每 tap: 偏移按 dir 旋转、乘 len2, d2=min(|v|², clp),
  w = (25/16·(0.4·d2-1)² - 9/16) · (lob·d2-1)² (无 sin/sqrt 的 Lanczos2 近似); 加权平均后
  clamp 到 f/g/j/k 四点的 min/max (去振铃)。
RCAS: 十字邻域 (b d e f h), lobe = clamp(max over RGB of max(-hitMin, hitMax), -0.1875, 0) · con,
  con = 2^-sharpness (sharpness 单位为 stops, 0=最强; FSR 缺省 0.2); out = (lobe·(b+d+f+h)+e)/(4lobe+1)。
  FSR_RCAS_DENOISE 为可选宏 (缺省不开), 这里 denoise=False 对应缺省; 开启时 lobe *= 1-0.5·nz。

差异: float64 代替 fp32/fp16; APrxLoRcp/APrxLoRsq/APrxMedRcp 等位运算近似用精确倒数/开方代替 (rcp(0) 时
FSR 得到一个大有限数再乘 0, 这里直接按 0 处理); 边界用 CLAMP_TO_EDGE (FSR 用 SamplerState clamp)。
输入假定为感知空间 (sRGB 8 位截图, 与 FSR 要求一致)。

与 selfsr 的锐化对应: sr_quality.rcas(s, sharp) 的 sharp 即 FSR 的 con (线性倍数, 无 denoise), 所以
selfsr 层参数 sharp=0.8 ≡ FSR sharpness = -log2(0.8) ≈ 0.32 stops; FSR 缺省 0.2 stops ≡ con 0.871, 比 selfsr 略强。
两者唯一公式差异: FSR 的 hitMin/hitMax 分母只用十字环 (mx4/mn4), sr_quality.rcas 分母含中心像素且加 1e-5。

验证 (`--check` + 本机实测):
  - RCAS: 真实帧 (盲评真值 BOX 缩到 1505x681 再 EASU) 上与 sr_quality.rcas(con=0.8) 比, 0-255 刻度
    平均 |diff| 0.0005、p99 0、max 23.5 (只在中心为局部极值且环接近 0/1 的少数像素); 随机噪声上 max 0.12-0.16 (同因)。
  - 几何: 低分 64x48 → 160x120 (2.5x 非整数倍) 水平/竖直/斜向线性梯度, 去 4 像素边后 EASU 与
    sr_quality.up_lanczos / up_catmull_rom / PIL bilinear max |diff| 1.2e-3..5.1e-3 (≈ 1/255 量化级, 无系统偏移);
    阶跃边 50% 交叉点 EASU 79.500 = Lanczos2 79.500 = 理论 79.500 输出像素 → 无半像素偏移。
  - 恒定图: 输出恒等 (max |diff| 0)。
  - 速度: 1505x681 → 2141x969 EASU 约 1.8 s + RCAS 0.2 s (numpy float64, 单线程)。
  - 未与官方 FSR 输出逐像素对齐 (本机无 GPU 参考图); 盲评时标注「FSR 1.0 EASU+RCAS numpy 移植」。

    python3 -I selfsr/fsr1.py --out up.png [--sharp 0.2] <低分图.png> <输出宽x高>
    python3 -I selfsr/fsr1.py --check     # RCAS 对照 + 梯度几何自检
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

FSR_RCAS_LIMIT = 0.25 - 1.0 / 16.0           # 0.1875, 与 sr_quality.rcas 的负瓣上限一致

# 12 个 tap 相对 fp 的偏移 (x, y), 名字同 ffx_fsr1.h 注释图
_TAPS = {"b": (0, -1), "c": (1, -1),
         "e": (-1, 0), "f": (0, 0), "g": (1, 0), "h": (2, 0),
         "i": (-1, 1), "j": (0, 1), "k": (1, 1), "l": (2, 1),
         "n": (0, 2), "o": (1, 2)}


def _set(ppx, ppy, quad, la, lb, lc, ld, le):
    """FsrEasuSetF: 一个双线性象限的方向/边缘度贡献。'+' 形: a 上, b c d 中行, e 下 (c 为中心)。"""
    if quad == "s":
        w = (1 - ppx) * (1 - ppy)
    elif quad == "t":
        w = ppx * (1 - ppy)
    elif quad == "u":
        w = (1 - ppx) * ppy
    else:
        w = ppx * ppy

    def axis(lo, mid, hi):
        m = np.maximum(np.abs(hi - mid), np.abs(mid - lo))
        d = hi - lo
        # lenX = sat(|d| * rcp(m)); m=0 时 d 也必为 0, FSR 的近似 rcp 给大有限数 → 0
        ln = np.clip(np.abs(d) / np.where(m > 0, m, 1.0), 0.0, 1.0)
        return d, ln * ln

    dx, lx = axis(lb, lc, ld)
    dy, ly = axis(la, lc, le)
    return dx * w, dy * w, (lx + ly) * w


def easu(src, w_out, h_out):
    """src: (Hs,Ws,3) float [0,1] (感知空间) → (h_out,w_out,3) float [0,1]。"""
    src = np.asarray(src, dtype=np.float64)
    h, w = src.shape[:2]
    # con0: 输入/输出比例与 0.5*比例-0.5 偏移 (输入视口 = 整张输入)
    ox = (np.arange(w_out) + 0.5) * (w / w_out) - 0.5
    oy = (np.arange(h_out) + 0.5) * (h / h_out) - 0.5
    fx = np.floor(ox).astype(np.int64)
    fy = np.floor(oy).astype(np.int64)
    ppx = (ox - fx)[None, :]
    ppy = (oy - fy)[:, None]

    pad = 2                                           # tap 范围 -1..+2, pad 2 足够; CLAMP_TO_EDGE
    sp = np.pad(src, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    lum = sp[..., 0] * 0.5 + sp[..., 1] + sp[..., 2] * 0.5   # 亮度×2 (FSR 原式)

    rgb, L = {}, {}
    for nm, (tx, ty) in _TAPS.items():
        yy = (fy + ty + pad)[:, None]
        xx = (fx + tx + pad)[None, :]
        rgb[nm] = sp[yy, xx]
        L[nm] = lum[yy, xx]

    dirx = np.zeros((h_out, w_out))
    diry = np.zeros((h_out, w_out))
    ln = np.zeros((h_out, w_out))
    for quad, (a, b, c, d, e) in (("s", "befgj"), ("t", "cfghk"), ("u", "fijkn"), ("v", "gjklo")):
        ddx, ddy, dl = _set(ppx, ppy, quad, L[a], L[b], L[c], L[d], L[e])
        dirx += ddx
        diry += ddy
        ln += dl

    # 归一化方向; 接近 0 时取 (1,0)
    dir_r = dirx * dirx + diry * diry
    zro = dir_r < 1.0 / 32768.0
    rs = np.where(zro, 1.0, 1.0 / np.sqrt(np.where(zro, 1.0, dir_r)))
    dirx = np.where(zro, 1.0, dirx) * rs
    diry = diry * rs                                   # zro 时 dir.y 保持原值 (≈0) 乘 1, 与原式一致
    ln = ln * 0.5
    ln = ln * ln
    stretch = (dirx * dirx + diry * diry) / np.maximum(np.abs(dirx), np.abs(diry))
    len2x = 1.0 + (stretch - 1.0) * ln
    len2y = 1.0 - 0.5 * ln
    lob = 0.5 + ((1.0 / 4.0 - 0.04) - 0.5) * ln
    clp = 1.0 / lob

    acc = np.zeros((h_out, w_out, 3))
    aw = np.zeros((h_out, w_out))
    for nm, (tx, ty) in _TAPS.items():
        offx = tx - ppx
        offy = ty - ppy
        vx = (offx * dirx + offy * diry) * len2x
        vy = (offx * -diry + offy * dirx) * len2y
        d2 = np.minimum(vx * vx + vy * vy, clp)
        wb = 2.0 / 5.0 * d2 - 1.0
        wa = lob * d2 - 1.0
        wb = wb * wb
        wa = wa * wa
        wb = 25.0 / 16.0 * wb - (25.0 / 16.0 - 1.0)
        wt = wb * wa
        acc += rgb[nm] * wt[..., None]
        aw += wt

    q = (rgb["f"], rgb["g"], rgb["j"], rgb["k"])
    mn4 = np.minimum.reduce(q)
    mx4 = np.maximum.reduce(q)
    return np.minimum(mx4, np.maximum(mn4, acc / aw[..., None]))


def rcas(s, sharpness=0.2, denoise=False):
    """FsrRcasF: s (H,W,3) float [0,1]; sharpness 单位 stops (0=最强, 每 +1 减半)。"""
    con = 2.0 ** (-sharpness)
    p = np.pad(np.asarray(s, dtype=np.float64), ((1, 1), (1, 1), (0, 0)), mode="edge")
    e = p[1:-1, 1:-1]
    b = p[:-2, 1:-1]
    hh = p[2:, 1:-1]
    d = p[1:-1, :-2]
    f = p[1:-1, 2:]
    mn4 = np.minimum.reduce([b, d, f, hh])
    mx4 = np.maximum.reduce([b, d, f, hh])
    # 分母只用十字环 (mx4/mn4, 不含中心 e), 分子含 e —— 与 ffx_fsr1.h 一致 (sr_quality.rcas 分母含中心并加 1e-5)
    with np.errstate(divide="ignore", invalid="ignore"):
        hit_min = np.minimum(mn4, e) / (4.0 * mx4)
        hit_max = (1.0 - np.maximum(mx4, e)) / (4.0 * mn4 - 4.0)
    lobe_c = np.nan_to_num(np.maximum(-hit_min, hit_max), nan=0.0, posinf=0.0, neginf=-1.0)
    lobe = np.clip(lobe_c.max(axis=2), -FSR_RCAS_LIMIT, 0.0) * con
    if denoise:
        def luma(x):
            return x[..., 2] * 0.5 + (x[..., 0] * 0.5 + x[..., 1])
        bl, dl, el, fl, hl = luma(b), luma(d), luma(e), luma(f), luma(hh)
        nz = 0.25 * (bl + dl + fl + hl) - el
        rng = np.maximum.reduce([bl, dl, el, fl, hl]) - np.minimum.reduce([bl, dl, el, fl, hl])
        nz = np.clip(np.abs(nz) / np.where(rng > 0, rng, 1.0), 0.0, 1.0)
        lobe = lobe * (1.0 - 0.5 * nz)
    lobe = lobe[..., None]
    return np.clip((lobe * (b + d + hh + f) + e) / (4.0 * lobe + 1.0), 0.0, 1.0)


def fsr1(src, w_out, h_out, sharpness=0.2, denoise=False):
    """EASU + RCAS 全流程。src (Hs,Ws,3) float [0,1] → (h_out,w_out,3) float [0,1]。"""
    return rcas(easu(src, w_out, h_out), sharpness, denoise)


def _check():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sr_quality import rcas as rcas_ref, up_lanczos, up_catmull_rom  # noqa: E402
    rng = np.random.default_rng(0)
    s = rng.random((60, 80, 3))
    for st in (0.0, 0.2, -np.log2(0.8), 1.0):
        d = np.abs(rcas(s, st) - rcas_ref(s, 2.0 ** -st)).max()
        print(f"RCAS {st:.3f} stops vs sr_quality.rcas(con={2.0 ** -st:.3f}): max|diff| = {d:.2e}")
    lw, lh, W, H = 64, 48, 160, 120
    yy, xx = np.mgrid[0:lh, 0:lw]
    grads = {"水平": xx / (lw - 1), "竖直": yy / (lh - 1), "斜向": (xx + yy) / (lw + lh - 2)}
    for nm, g in grads.items():
        lo = np.repeat((0.1 + 0.8 * g)[..., None], 3, axis=2)
        u8 = Image.fromarray((lo * 255).round().astype(np.uint8))
        lo = np.asarray(u8, dtype=np.float64) / 255
        ea = easu(lo, W, H)
        lz = up_lanczos(u8, W, H)
        cr = up_catmull_rom(u8, W, H)
        bl = np.asarray(u8.resize((W, H), Image.BILINEAR), dtype=np.float64) / 255
        m = (slice(4, -4), slice(4, -4))
        print(f"{nm}梯度 EASU vs lanczos2 {np.abs(ea - lz)[m].max():.2e}  vs catmull-rom {np.abs(ea - cr)[m].max():.2e}"
              f"  vs PIL bilinear {np.abs(ea - bl)[m].max():.2e}  (1/255={1 / 255:.2e})")
    # 阶跃边缘: EASU 50% 交叉点应与 Lanczos2 一致 (半像素偏移会差 ~1.25 输出像素)
    st = np.zeros((lh, lw, 3)); st[:, lw // 2:] = 1.0
    u8 = Image.fromarray((st * 255).astype(np.uint8))
    row_e = easu(st, W, H)[lh, :, 1]
    row_l = up_lanczos(u8, W, H)[lh, :, 1]

    def cross(r):
        i = int(np.argmax(r >= 0.5))
        return i - 1 + (0.5 - r[i - 1]) / (r[i] - r[i - 1])
    print(f"阶跃 50% 交叉: EASU {cross(row_e):.3f}  lanczos2 {cross(row_l):.3f}  理论 {W / 2 - 0.5:.3f} (输出像素坐标)")
    c = np.full((20, 30, 3), 0.37)
    print(f"恒定图: max|EASU-0.37| = {np.abs(easu(c, 47, 31) - 0.37).max():.2e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lo", nargs="?", help="低分输入 png")
    ap.add_argument("hi", nargs="?", help="输出尺寸 WxH (缺省 2x)")
    ap.add_argument("--out", default="fsr1_out.png", help="输出 png")
    ap.add_argument("--sharp", type=float, default=0.2, help="RCAS sharpness (stops, FSR 缺省 0.2)")
    ap.add_argument("--denoise", action="store_true", help="FSR_RCAS_DENOISE")
    ap.add_argument("--check", action="store_true", help="RCAS 对照 + 梯度几何自检")
    a = ap.parse_args()
    if a.check:
        _check()
        return 0
    if not a.lo:
        ap.error("需要低分输入 png")
    im = Image.open(a.lo).convert("RGB")
    if a.hi:
        wo, ho = (int(v) for v in a.hi.lower().split("x"))
    else:
        wo, ho = im.width * 2, im.height * 2
    up = fsr1(np.asarray(im, dtype=np.float64) / 255.0, wo, ho, a.sharp, a.denoise)
    Image.fromarray((np.clip(up, 0, 1) * 255 + 0.5).astype(np.uint8)).save(a.out)
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
