#!/usr/bin/env python3
"""baseline_quality.py — 超分基线对比的离线画质管线 (本机跑, 不占设备)。

输入: 采集任务的截图 (2688x1216 屏幕分辨率) —— native 臂 (真值来源)、hwc:0.56 臂 (低分+显示路径双线性)、
sr:0.56 臂 (低分+我们的超分)。流程:
  1) 真值 = native 截图缩回游戏送显尺寸 (--native-size, 2141x969, LANCZOS);
  2) 低分输入 = hwc 截图 BOX 缩到 game mode 0.56 的实际低分尺寸 (--low-size, 1505x681);
  3) 同一低分帧上离线放大到送显尺寸: bilinear / SGSR1 (sgsr1.py 移植) / selfsr (Catmull-Rom+RCAS,
     sr_quality.py 与层着色器逐项对应), 与真值比 SSIM/PSNR/锐度 (只作参考);
  4) 交叉核对: 离线 bilinear vs 设备 hwc 截图、离线 selfsr vs 设备 sr 截图 (验证低分重建与算子复刻);
  5) 盲评图组: 全帧总览 + 4 处局部 2x 放大 (逐方法横排)。

    python3 -I selfsr/baseline_quality.py --native <A1.png> --hwc <B1.png> --sr <B1.png> \
        [--srip <B1.png>] [--native-size 2141x969] [--low-size 1505x681] --out <目录>
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sgsr1 import sgsr1  # noqa: E402
from sr_quality import ssim, up_catmull_rom, rcas, to_img, lap_var  # noqa: E402

METHODS = ("bilinear", "sgsr1", "selfsr")


def psnr(a, b):
    x = np.asarray(Image.open(a).convert("L"), dtype=np.float64)
    y = np.asarray(Image.open(b).convert("L"), dtype=np.float64)
    mse = float(((x - y) ** 2).mean())
    return 10 * np.log10(255 ** 2 / mse) if mse > 0 else float("inf")


def load(path):
    return Image.open(path).convert("RGB")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--native", required=True, help="native 臂截图 (2688x1216)")
    ap.add_argument("--hwc", required=True, help="hwc:0.56 臂截图 (低分+双线性)")
    ap.add_argument("--sr", required=True, help="sr:0.56 臂截图 (低分+我们的超分)")
    ap.add_argument("--srip", default=None, help="srip 第 7 组臂截图 (可选, 只进总览/交叉核对)")
    ap.add_argument("--native-size", default="2141x969")
    ap.add_argument("--low-size", default="1505x681")
    ap.add_argument("--sharp", type=float, default=0.8, help="selfsr RCAS 锐化 (层参数)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    nw, nh = (int(v) for v in a.native_size.split("x"))
    lw, lh = (int(v) for v in a.low_size.split("x"))
    os.makedirs(a.out, exist_ok=True)
    p = lambda *x: os.path.join(a.out, *x)  # noqa: E731

    gt = load(a.native).resize((nw, nh), Image.LANCZOS)
    gt.save(p("gt.png"))
    low = load(a.hwc).resize((lw, lh), Image.BOX)
    low.save(p("low_input.png"))

    arr = np.asarray(low, dtype=np.float64) / 255.0
    outs = {"bilinear": low.resize((nw, nh), Image.BILINEAR)}
    outs["sgsr1"] = Image.fromarray((np.clip(sgsr1(np.asarray(low, np.float32) / 255.0, nw, nh), 0, 1) * 255 + 0.5).astype(np.uint8))
    outs["selfsr"] = to_img(rcas(up_catmull_rom(low, nw, nh), a.sharp))   # up_catmull_rom 收 PIL Image

    rows = {"gt": {"sharp": lap_var(gt)}}
    for name, img in outs.items():
        img.save(p(f"{name}.png"))
        rows[name] = {"ssim": ssim(p("gt.png"), p(f"{name}.png")), "psnr": psnr(p("gt.png"), p(f"{name}.png")),
                      "sharp": lap_var(img)}

    cross = {"offline_bilinear_vs_device_hwc": ssim(p("bilinear.png"), a.hwc),
             "offline_selfsr_vs_device_sr": ssim(p("selfsr.png"), a.sr)}
    load(a.hwc).save(p("device_hwc.png"))
    load(a.sr).save(p("device_sr.png"))
    if a.srip:
        load(a.srip).save(p("device_srip.png"))
        cross["gt_vs_device_srip_scene_diff"] = ssim(p("gt.png"), a.srip)

    # 同帧模拟口径 (消除跨臂场景漂移): 真值 BOX 缩到低分尺寸再放大回来, 与真值逐像素可比
    low_sim = gt.resize((lw, lh), Image.BOX)
    low_sim.save(p("low_sim.png"))
    sim = {}
    sim_outs = {"bilinear": low_sim.resize((nw, nh), Image.BILINEAR),
                "selfsr": to_img(rcas(up_catmull_rom(low_sim, nw, nh), a.sharp))}
    sim_outs["sgsr1"] = Image.fromarray((np.clip(sgsr1(np.asarray(low_sim, np.float32) / 255.0, nw, nh), 0, 1) * 255 + 0.5).astype(np.uint8))
    for name, img in sim_outs.items():
        img.save(p(f"sim_{name}.png"))
        sim[name] = {"ssim": ssim(p("gt.png"), p(f"sim_{name}.png")), "psnr": psnr(p("gt.png"), p(f"sim_{name}.png")),
                     "sharp": lap_var(img)}

    # 总览: 每方法一行, 缩到宽 1280
    seq = [("gt 真值", p("gt.png")), ("bilinear", p("bilinear.png")), ("sgsr1 (移植)", p("sgsr1.png")),
           ("selfsr (离线)", p("selfsr.png")), ("设备 hwc", p("device_hwc.png")), ("设备 sr", p("device_sr.png"))]
    if a.srip:
        seq.append(("设备 srip g7", p("device_srip.png")))
    tw = 1280
    th = round(nh * tw / nw)
    sheet = Image.new("RGB", (tw, (th + 18) * len(seq)), (24, 24, 24))
    from PIL import ImageDraw
    dr = ImageDraw.Draw(sheet)
    for i, (label, path) in enumerate(seq):
        im = Image.open(path).resize((tw, th), Image.LANCZOS)
        y = i * (th + 18)
        dr.text((6, y + 3), label, fill=(255, 255, 255))
        sheet.paste(im, (0, y + 18))
    sheet.save(p("sheet_full.png"))

    # 局部 2x: 送显尺寸坐标 (比例), 输出 320x180 -> 640x360
    regions = {"center": (0.40, 0.35, 0.55, 0.53), "hud": (0.66, 0.01, 0.97, 0.11),
               "foliage": (0.05, 0.30, 0.22, 0.52), "water": (0.30, 0.60, 0.50, 0.80)}
    crops = {}
    for rname, (fx0, fy0, fx1, fy1) in regions.items():
        x0, y0 = int(nw * fx0), int(nh * fy0)
        x1, y1 = int(nw * fx1), int(nh * fy1)
        panel_w = 320 * len(seq)
        panel = Image.new("RGB", (panel_w, 180 + 18 * len(seq) + 6), (24, 24, 24))
        dr = ImageDraw.Draw(panel)
        for i, (label, path) in enumerate(seq):
            cx, cy = i * 320, i * 18
            dr.text((cx + 4, cy + 2), label, fill=(255, 255, 255))
            im = Image.open(path).crop((x0, y0, x1, y1)).resize((320, 180), Image.NEAREST)
            panel.paste(im, (cx, cy + 18))
        fp = p(f"crop_{rname}.png")
        panel.save(fp)
        crops[rname] = fp

    res = {"native_size": [nw, nh], "low_size": [lw, lh], "sharp": a.sharp,
           "metrics": rows, "same_frame_sim": sim, "crosscheck": cross, "crops": crops,
           "inputs": {"native": a.native, "hwc": a.hwc, "sr": a.sr, "srip": a.srip}}
    json.dump(res, open(p("baseline_quality.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({"metrics": rows, "same_frame_sim": sim, "crosscheck": cross}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
