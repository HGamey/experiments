"""fd_convert.py — framedump 原始帧 (送显图 + framedump_att 附件) 转成数据集文件, 并逐帧检查。只用标准库。

输入: 一段的目录 (session.json, summary.json, index.csv, f<帧号>.raw, f<帧号>_<附件>.raw)。
附件默认上下翻转后再存: 原神 (Unity, Vulkan) 渲染到中间目标时是倒的, 送显合成时才翻回来;
翻得对不对看 checks.csv 的 noui_vs_ui_med (翻对了只有零点几, 翻错了是几十)。别的游戏用 --no-flip。
输出 (同目录下):
  ui/f<帧号>.png        送显图, 带 UI, RGB8 (原始 RGBA8 去掉 alpha: 原神的 alpha 不是 255)
  <附件>/f<帧号>.png    颜色附件 (RGBA8/BGRA8) 去 alpha 存 RGB8
  depth16/f<帧号>.png   深度 (D24S8 的 24 位深度取高 16 位), 16 位灰度 PNG, 0 = 近/远看游戏约定 (见报告)
  depth24/f<帧号>.png   同一深度的 24 位无损: R=高 8 位, G=中 8 位, B=低 8 位, d = (R<<16|G<<8|B) / 16777215
  <附件>/f<帧号>.png    其他 32 位附件 (如 A2R10G10B10) 原样 4 字节存 RGBA8 PNG (无损, 小端: R 通道=第 0 字节)
  checks.csv            逐帧: 帧号、gap、附件齐不齐、中央区帧间差中位数、不带 UI 与带 UI 中央区差中位数、深度范围
python3 -I fd_convert.py <段目录> [--workers 4] [--delete-raw]
"""
import argparse
import csv
import json
import os
import struct
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor

COLOR8 = {37: "rgba", 43: "rgba", 44: "bgra", 50: "bgra"}
DEPTH24 = {129, 125}
PACKED32 = {58, 64, 122, 100, 83}


def png_bytes(w, h, color_type, bit_depth, pixels, channels, level=6):
    """pixels: 行紧密排布的字节 (高位在前的 16 位样本已经排好)。"""
    stride = w * channels * (bit_depth // 8)
    raw = bytearray(h * (stride + 1))
    for y in range(h):
        o = y * (stride + 1)
        raw[o + 1:o + 1 + stride] = pixels[y * stride:(y + 1) * stride]
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level)) + chunk(b"IEND", b""))


def flip_rows(b, w, h, bpp):
    """按行倒序 (上下翻转)。"""
    stride = w * bpp
    return b"".join(b[(h - 1 - y) * stride:(h - y) * stride] for y in range(h))


def rgb_from_rgba(raw, order="rgba"):
    n = len(raw) // 4
    rgb = bytearray(n * 3)
    if order == "rgba":
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[0::4], raw[1::4], raw[2::4]
    else:
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    return rgb


def depth16_from_d24(raw):
    n = len(raw) // 4
    o = bytearray(n * 2)
    o[0::2], o[1::2] = raw[2::4], raw[1::4]   # 大端 16 位 = 24 位深度的高 16 位
    return o


def depth24_from_d24(raw):
    n = len(raw) // 4
    o = bytearray(n * 3)
    o[0::3], o[1::3], o[2::3] = raw[2::4], raw[1::4], raw[0::4]
    return o


def convert_one(job):
    """(段目录, 帧号字符串, 附件表) -> 写好的文件和几个统计量; 在子进程里跑。"""
    d, fr, ui_wh, atts, flip = job
    res = {"frame": fr}
    raw = open(os.path.join(d, f"f{fr}.raw"), "rb").read()
    w, h = ui_wh
    rgb = rgb_from_rgba(raw)
    os.makedirs(os.path.join(d, "ui"), exist_ok=True)
    open(os.path.join(d, "ui", f"f{fr}.png"), "wb").write(png_bytes(w, h, 2, 8, rgb, 3))
    res["ui_center"] = sample_center(rgb, w, h, 3)
    for name, a in atts.items():
        p = os.path.join(d, f"f{fr}_{name}.raw")
        if not os.path.exists(p):
            res[name] = "MISSING"
            continue
        b = open(p, "rb").read()
        aw, ah, f = a["w"], a["h"], a["vk_format"]
        if len(b) != aw * ah * a["bytes_per_pixel"]:
            res[name] = "BAD_SIZE"
            continue
        if flip:
            b = flip_rows(b, aw, ah, a["bytes_per_pixel"])
        if f in COLOR8:
            c = rgb_from_rgba(b, COLOR8[f])
            os.makedirs(os.path.join(d, name), exist_ok=True)
            open(os.path.join(d, name, f"f{fr}.png"), "wb").write(png_bytes(aw, ah, 2, 8, c, 3))
            res[name + "_center"] = sample_center(c, aw, ah, 3)
        elif f in DEPTH24:
            for sub, fn, ct, ch, bd in (("depth16", depth16_from_d24, 0, 1, 16), ("depth24", depth24_from_d24, 2, 3, 8)):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
                open(os.path.join(d, sub, f"f{fr}.png"), "wb").write(png_bytes(aw, ah, ct, bd, fn(b), ch))
            hi = b[2::4 * 97]   # 抽样看深度高字节的范围
            res[name + "_range"] = (min(hi), max(hi))
        elif a["bytes_per_pixel"] == 4:
            os.makedirs(os.path.join(d, name), exist_ok=True)
            open(os.path.join(d, name, f"f{fr}.png"), "wb").write(png_bytes(aw, ah, 6, 8, b, 4))
        res[name] = "ok"
    return res


def sample_center(rgb, w, h, ch, nx=48, ny=24):
    """画面中央 (x 0.3–0.7, y 0.2–0.75, 避开 HUD) 的 nx*ny 个点的灰度。"""
    out = []
    for j in range(ny):
        y = int((0.2 + 0.55 * j / (ny - 1)) * (h - 1))
        for i in range(nx):
            x = int((0.3 + 0.4 * i / (nx - 1)) * (w - 1))
            o = (y * w + x) * ch
            out.append((rgb[o] * 299 + rgb[o + 1] * 587 + rgb[o + 2] * 114) // 1000)
    return out


def med_absdiff(a, b):
    if not a or not b or len(a) != len(b):
        return None
    v = sorted(abs(x - y) for x, y in zip(a, b))
    return v[len(v) // 2]


def convert_segment(d, workers=4, delete_raw=False, flip=True):
    s = json.load(open(os.path.join(d, "session.json")))
    summ = json.load(open(os.path.join(d, "summary.json")))
    atts = {r["name"]: r for r in ((summ.get("att") or {}).get("rules") or []) if r.get("w")}
    rows = list(csv.DictReader(open(os.path.join(d, "index.csv"))))
    frames = [r["frame"].zfill(10) for r in rows if r["file"].endswith(".raw")]
    jobs = [(d, fr, (s["w"], s["h"]), atts, flip) for fr in frames]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(convert_one, jobs, chunksize=4))
    by = {r["frame"]: r for r in res}
    noui = next((n for n, a in atts.items() if a["vk_format"] in COLOR8), None)
    depth = next((n for n, a in atts.items() if a["vk_format"] in DEPTH24), None)
    out_rows, prev = [], None
    for r in rows:
        fr = r["frame"].zfill(10)
        x = by.get(fr)
        row = {"frame": int(r["frame"]), "t_ns": r["t_ns"], "gap_before": r["gap_before"], "ui": "ok" if x else r["file"]}
        for n in atts:
            row[n] = (x or {}).get(n, "MISSING")
        row["motion_med"] = med_absdiff(prev, x["ui_center"]) if x and prev else ""
        row["noui_vs_ui_med"] = med_absdiff(x.get(noui + "_center"), x["ui_center"]) if x and noui and x.get(noui + "_center") else ""
        row["depth_hi_range"] = "%d-%d" % x[depth + "_range"] if x and depth and x.get(depth + "_range") else ""
        prev = x["ui_center"] if x else None
        out_rows.append(row)
    with open(os.path.join(d, "checks.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        wr.writeheader()
        wr.writerows(out_rows)
    if delete_raw:
        for fr in frames:
            for p in [f"f{fr}.raw"] + [f"f{fr}_{n}.raw" for n in atts]:
                q = os.path.join(d, p)
                if os.path.exists(q):
                    os.remove(q)
    complete = sum(1 for r in out_rows if r["ui"] == "ok" and all(r[n] == "ok" for n in atts))
    meds = sorted(r["noui_vs_ui_med"] for r in out_rows if isinstance(r["noui_vs_ui_med"], int))
    return {"frames": len(frames), "complete": complete, "flip": flip,
            "noui_vs_ui_med_p50": meds[len(meds) // 2] if meds else None, "atts": {n: {k: a[k] for k in ("vk_format", "w", "h", "bytes_per_pixel")} for n, a in atts.items()}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delete-raw", action="store_true")
    ap.add_argument("--no-flip", action="store_true")
    a = ap.parse_args()
    print(json.dumps(convert_segment(a.dir, a.workers, a.delete_raw, not a.no_flip), ensure_ascii=False))
