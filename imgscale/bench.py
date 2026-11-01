#!/usr/bin/env python3
"""imgscale bench: 游戏截图缩到一半再放大回原尺寸，比较 CPU 放大方法。

用法（平台按提交号运行；本地 python3 bench.py 直接跑）：
  python3 bench.py material.json xonotic.png stk.png ... [KEY=VALUE ...]

参数（KEY=VALUE）：
  METHODS=nearest,bilinear,bicubic,lanczos2,lanczos3,lanczos3_sharp   要跑的方法子集
  REPS=3            每方法每图计时重复次数（首图另有一次不计时的预热）
  CROP_W=960        工作图宽（对素材中心裁剪，不缩放原图）
  CROP_H=540        工作图高
  SCENES=2          输出多少个画面的盲评裁剪（每画面两套区域：方差最大 + 高频能量最高，
                    后者偏细线/小字；各含原生+各方法，文件名 v1..vN 不含来源）
  CROP_REGION_W=320 盲评裁剪宽
  CROP_REGION_H=180 盲评裁剪高
  SSIM_STRIDE=2     SSIM 窗口步长
  SSIM_WIN=8        SSIM 窗口边长（盒窗）

只用标准库。缩小统一 2x2 面积平均；放大用可分离核（边界按下标夹取的边缘延伸）；
lanczos3_sharp = lanczos3 放大后接 3×3 unsharp（amount 0.5）。
PSNR 全图 RGB；SSIM 用 Rec.601 亮度、盒窗（Wang 2004 的均匀窗变体，非高斯窗），
C1=(0.01*255)^2、C2=(0.03*255)^2。计时只含放大本身（perf_counter，单线程纯 Python）。

产物写到 $PF_JOB_OUT（本地缺省 ./out）：
  report.json table.md summary.csv half/ upscaled/ crops/
  crops/<sceneN>/v1..vK.png（v1=原生，其后按 METHODS 顺序）与 region.json（对应关系）
"""
import hashlib
import json
import math
import os
import struct
import sys
import time
import zlib

PF_JOB_OUT = os.environ.get("PF_JOB_OUT", "out")

# ---------------------------------------------------------------- PNG I/O


def png_read(path):
    """读 PNG（非隔行，8 位或调色板），返回 (w, h, rows)；rows 为每行 RGB 字节（w*3）。"""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: 不是 PNG")
    pos, w, h, depth, ctype, interlace = 8, 0, 0, 0, 0, 0
    palette, idat = [], []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            interlace = body[12]
        elif tag == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    if not idat:
        raise ValueError(f"{path}: 无图像数据")
    if interlace:
        raise ValueError(f"{path}: 不支持隔行 PNG")
    if ctype not in (0, 2, 3, 4, 6):
        raise ValueError(f"{path}: 不支持颜色类型 {ctype}")
    if ctype != 3 and depth != 8:
        raise ValueError(f"{path}: 只支持 8 位深（调色板除外），遇到 {depth}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    row_bytes = (w * depth + 7) // 8 if ctype == 3 else w * channels
    bpp = max(1, channels)
    raw = zlib.decompress(b"".join(idat))
    rows, prev = [], bytearray(row_bytes)
    p = 0
    for _ in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + row_bytes])
        p += 1 + row_bytes
        if f == 1:
            for i in range(bpp, row_bytes):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(row_bytes):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(row_bytes):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(row_bytes):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif f != 0:
            raise ValueError(f"{path}: 未知滤波类型 {f}")
        rows.append(line)
        prev = line
    out = []
    for line in rows:
        rgb = bytearray(w * 3)
        if ctype in (2, 6):
            step = 3 if ctype == 2 else 4
            for x in range(w):
                o = x * step
                rgb[x * 3], rgb[x * 3 + 1], rgb[x * 3 + 2] = line[o], line[o + 1], line[o + 2]
        elif ctype in (0, 4):
            step = 1 if ctype == 0 else 2
            for x in range(w):
                v = line[x * step]
                rgb[x * 3] = rgb[x * 3 + 1] = rgb[x * 3 + 2] = v
        else:  # 调色板
            if depth == 8:
                for x in range(w):
                    r, g, b = palette[line[x]]
                    rgb[x * 3], rgb[x * 3 + 1], rgb[x * 3 + 2] = r, g, b
            else:
                per_byte = 8 // depth
                mask = (1 << depth) - 1
                for x in range(w):
                    v = (line[x // per_byte] >> (8 - depth * (x % per_byte + 1))) & mask
                    r, g, b = palette[v]
                    rgb[x * 3], rgb[x * 3 + 1], rgb[x * 3 + 2] = r, g, b
        out.append(bytes(rgb))
    return w, h, out


def png_write(path, w, h, rows):
    """写 RGB8 PNG，filter 0。"""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    raw = bytearray()
    for line in rows:
        raw.append(0)
        raw += line
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))

# ---------------------------------------------------------------- 重采样核


def _sinc(x):
    if x == 0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


def make_kernel(method):
    if method == "nearest":
        return 0.5 + 1e-9, lambda x: 1.0 if abs(x) < 0.5 else (0.5 if abs(x) == 0.5 else 0.0)
    if method == "bilinear":
        return 1.0, lambda x: max(0.0, 1.0 - abs(x))
    if method == "bicubic":  # Catmull-Rom (B=0, C=0.5)
        def cubic(x):
            ax = abs(x)
            if ax < 1:
                return 1.5 * ax ** 3 - 2.5 * ax ** 2 + 1.0
            if ax < 2:
                return -0.5 * ax ** 3 + 2.5 * ax ** 2 - 4.0 * ax + 2.0
            return 0.0
        return 2.0, cubic
    if method in ("lanczos2", "lanczos3"):
        a = 2.0 if method == "lanczos2" else 3.0

        def lz(x, a=a):
            ax = abs(x)
            return 0.0 if ax >= a else _sinc(x) * _sinc(x / a)
        return a, lz
    raise ValueError(f"未知方法 {method}")


def axis_weights(method, in_n, out_n):
    """每个输出下标 → (采样下标列表, 权重列表)；零权重裁掉，边界采样按下标夹取。"""
    support, f = make_kernel(method)
    scale = out_n / in_n
    filter_scale = 1.0 if scale >= 1 else 1.0 / scale
    eff = support * filter_scale
    result = []
    for i in range(out_n):
        center = (i + 0.5) / scale - 0.5
        lo = int(math.ceil(center - eff - 1e-9))
        hi = int(math.floor(center + eff + 1e-9))
        idxs, ws = [], []
        for j in range(lo, hi + 1):
            w = f((j - center) / filter_scale)
            if w != 0.0:
                idxs.append(min(in_n - 1, max(0, j)))
                ws.append(w)
        total = sum(ws)
        result.append((idxs, [w / total for w in ws]))
    return result


def resize(method, rows, in_w, in_h, out_w, out_h):
    """可分离重采样：先水平后垂直；输入输出都是每行 RGB 字节。
    名字以 _sharp 结尾 = 先按基础核放大，再做 3×3 unsharp（amount 0.5）。"""
    if method.endswith("_sharp"):
        base, sharpen = method[:-len("_sharp")], 0.5
    else:
        base, sharpen = method, None
    out = _resize_base(base, rows, in_w, in_h, out_w, out_h)
    if sharpen:
        out = unsharp(out, out_w, out_h, sharpen)
    return out


def _resize_base(method, rows, in_w, in_h, out_w, out_h):
    wx = axis_weights(method, in_w, out_w)
    wy = axis_weights(method, in_h, out_h)
    tmp = []
    for line in rows:
        acc = [0.0] * (out_w * 3)
        for ox in range(out_w):
            idxs, ws = wx[ox]
            a0 = a1 = a2 = 0.0
            for k in range(len(ws)):
                w = ws[k]
                p = idxs[k] * 3
                a0 += line[p] * w
                a1 += line[p + 1] * w
                a2 += line[p + 2] * w
            o = ox * 3
            acc[o] = a0
            acc[o + 1] = a1
            acc[o + 2] = a2
        tmp.append(acc)
    out = []
    width3 = out_w * 3
    for oy in range(out_h):
        idxs, ws = wy[oy]
        line = [0.0] * width3
        for k in range(len(ws)):
            w = ws[k]
            src = tmp[idxs[k]]
            if k == 0:
                for i in range(width3):
                    line[i] = src[i] * w
            else:
                for i in range(width3):
                    line[i] += src[i] * w
        out.append(bytes(min(255, max(0, int(v + 0.5))) for v in line))
    return out


def halve_area(rows, w, h):
    """2x2 面积平均缩小（宽高须为偶数），返回 (w//2, h//2, 行)。"""
    ow, oh = w // 2, h // 2
    out = []
    for y in range(oh):
        r0, r1 = rows[y * 2], rows[y * 2 + 1]
        line = bytearray(ow * 3)
        for x in range(ow):
            p = x * 6
            line[x * 3] = (r0[p] + r0[p + 3] + r1[p] + r1[p + 3] + 2) >> 2
            line[x * 3 + 1] = (r0[p + 1] + r0[p + 4] + r1[p + 1] + r1[p + 4] + 2) >> 2
            line[x * 3 + 2] = (r0[p + 2] + r0[p + 5] + r1[p + 2] + r1[p + 5] + 2) >> 2
        out.append(bytes(line))
    return ow, oh, out


def unsharp(rows, w, h, amount=0.5):
    """3×3 unsharp：可分离 [1,2,1]/4 模糊后 out = v + amount*(v - blur)，边界下标夹取。"""
    tmp = []
    for line in rows:
        acc = [0.0] * (w * 3)
        for x in range(w):
            p0 = (x - 1 if x else 0) * 3
            p1 = x * 3
            p2 = (x + 1 if x + 1 < w else w - 1) * 3
            for c in range(3):
                acc[x * 3 + c] = (line[p0 + c] + 2.0 * line[p1 + c] + line[p2 + c]) / 4.0
        tmp.append(acc)
    out = []
    for y in range(h):
        r0, r1, r2 = tmp[max(0, y - 1)], tmp[y], tmp[min(h - 1, y + 1)]
        src = rows[y]
        line = bytearray(w * 3)
        for x in range(w):
            for c in range(3):
                blur = (r0[x * 3 + c] + 2.0 * r1[x * 3 + c] + r2[x * 3 + c]) / 4.0
                v = src[x * 3 + c] + amount * (src[x * 3 + c] - blur)
                line[x * 3 + c] = min(255, max(0, int(v + 0.5))) & 0xFF
        out.append(bytes(line))
    return out

# ---------------------------------------------------------------- 指标


def psnr_rgb(a, b, w, h):
    se = 0
    for ra, rb in zip(a, b):
        for ba, bb in zip(ra, rb):
            d = ba - bb
            se += d * d
    mse = se / (w * h * 3)
    if mse <= 0:
        return float("inf")
    return 10.0 * math.log10(255.0 * 255.0 / mse)


def luma_plane(rows, w, h):
    return [0.299 * r[3 * x] + 0.587 * r[3 * x + 1] + 0.114 * r[3 * x + 2]
            for r in rows for x in range(w)]


def integral(plane, w, h):
    """返回 (积分表, 行宽)；积分表大小 (w+1)*(h+1)，含零行零列。"""
    iw = w + 1
    table = [0.0] * (iw * (h + 1))
    for y in range(h):
        base, prev_base = (y + 1) * iw, y * iw
        run = 0.0
        row = plane[y * w:(y + 1) * w]
        prev_row = table[prev_base + 1:prev_base + iw]
        for x in range(w):
            run += row[x]
            table[base + x + 1] = prev_row[x] + run
    return table, iw


C1 = (0.01 * 255) ** 2
C2 = (0.03 * 255) ** 2


def ssim_box(la, lb, w, h, win=8, stride=2, orig_ints=None):
    """盒窗 SSIM。orig_ints=(ta,taa,iw) 可传入原图的积分以复用。"""
    if w < win or h < win:
        raise ValueError("图小于 SSIM 窗")
    if orig_ints is None:
        ta, iw = integral(la, w, h)
        taa, _ = integral([v * v for v in la], w, h)
    else:
        ta, taa, iw = orig_ints
    tb, _ = integral(lb, w, h)
    tbb, _ = integral([v * v for v in lb], w, h)
    tab, _ = integral([x * y for x, y in zip(la, lb)], w, h)
    area = float(win * win)
    total, count = 0.0, 0
    for y0 in range(0, h - win + 1, stride):
        r1, r2 = y0 * iw, (y0 + win) * iw
        for x0 in range(0, w - win + 1, stride):
            a, b = x0, x0 + win
            sa = ta[r2 + b] - ta[r2 + a] - ta[r1 + b] + ta[r1 + a]
            sb = tb[r2 + b] - tb[r2 + a] - tb[r1 + b] + tb[r1 + a]
            saa = taa[r2 + b] - taa[r2 + a] - taa[r1 + b] + taa[r1 + a]
            sbb = tbb[r2 + b] - tbb[r2 + a] - tbb[r1 + b] + tbb[r1 + a]
            sab = tab[r2 + b] - tab[r2 + a] - tab[r1 + b] + tab[r1 + a]
            ma, mb = sa / area, sb / area
            va = saa / area - ma * ma
            vb = sbb / area - mb * mb
            cov = sab / area - ma * mb
            total += ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma * ma + mb * mb + C1) * (va + vb + C2))
            count += 1
    return total / count

# ---------------------------------------------------------------- 主流程

ALL_METHODS = ["nearest", "bilinear", "bicubic", "lanczos2", "lanczos3", "lanczos3_sharp"]


def center_crop(rows, w, h, cw, ch):
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return [rows[y][x0 * 3:(x0 + cw) * 3] for y in range(y0, y0 + ch)]


def pick_crop_region(ta, taa, iw, w, h, rw, rh, step=16):
    """用亮度积分图找方差最大的 rw×rh 窗口（粗网格步进）。"""
    best = None
    for y0 in range(0, h - rh + 1, step):
        r1, r2 = y0 * iw, (y0 + rh) * iw
        for x0 in range(0, w - rw + 1, step):
            a, b = x0, x0 + rw
            s = ta[r2 + b] - ta[r2 + a] - ta[r1 + b] + ta[r1 + a]
            ss = taa[r2 + b] - taa[r2 + a] - taa[r1 + b] + taa[r1 + a]
            area = float(rw * rh)
            var = ss / area - (s / area) ** 2
            if best is None or var > best[0]:
                best = (var, x0, y0)
    return best[1], best[2]


def fine_energy_plane(luma, w, h):
    """|luma - 3×3 盒模糊| 高频能量图（细线/小字处高）。"""
    blur = []
    for y in range(h):
        r0 = luma[max(0, y - 1) * w:(max(0, y - 1) + 1) * w]
        r1 = luma[y * w:(y + 1) * w]
        r2 = luma[min(h - 1, y + 1) * w:(min(h - 1, y + 1) + 1) * w]
        row = []
        for x in range(w):
            a, b, c = r0[max(0, x - 1)], r0[x], r0[min(w - 1, x + 1)]
            d, e, f = r1[max(0, x - 1)], r1[x], r1[min(w - 1, x + 1)]
            g, i, j = r2[max(0, x - 1)], r2[x], r2[min(w - 1, x + 1)]
            row.append(abs(d - (a + b + c + d + e + f + g + i + j) / 9.0))
        blur.append(row)
    return [v for row in blur for v in row]


def pick_fine_region(hp, w, h, rw, rh, avoid=None, step=16):
    """找高频能量均值最大的 rw×rh 窗口；avoid=(x,y,rw,rh) 时避开与它的大面积重叠。"""
    thp, iw = integral(hp, w, h)
    area = float(rw * rh)
    ranked = []
    for y0 in range(0, h - rh + 1, step):
        r1, r2 = y0 * iw, (y0 + rh) * iw
        for x0 in range(0, w - rw + 1, step):
            a, b = x0, x0 + rw
            mean = (thp[r2 + b] - thp[r2 + a] - thp[r1 + b] + thp[r1 + a]) / area
            if avoid:
                ax, ay, aw, ah = avoid
                ox = max(0, min(x0 + rw, ax + aw) - max(x0, ax))
                oy = max(0, min(y0 + rh, ay + ah) - max(y0, ay))
                if ox * oy > 0.2 * rw * rh:
                    continue
            ranked.append((mean, x0, y0))
    ranked.sort(reverse=True)
    return ranked[0][1], ranked[0][2]


def crop_png(rows, x0, y0, cw, ch):
    return [rows[y][x0 * 3:(x0 + cw) * 3] for y in range(y0, y0 + ch)]


def main():
    args = sys.argv[1:]
    defaults = {"METHODS": ",".join(ALL_METHODS), "REPS": "3", "CROP_W": "960", "CROP_H": "540",
                "SCENES": "2", "CROP_REGION_W": "320", "CROP_REGION_H": "180",
                "SSIM_STRIDE": "2", "SSIM_WIN": "8"}
    opts, files = dict(defaults), []
    for a in args:
        key, sep, value = a.partition("=")
        if sep and key in opts:
            opts[key] = value
        else:
            files.append(a)
    if not files:
        raise SystemExit("用法: bench.py material.json 图1.png [图2.png ...] [KEY=VALUE ...]")
    manifest = json.load(open(files[0]))
    by_name = {os.path.basename(f): f for f in files[1:]}
    methods = opts["METHODS"].split(",")
    reps = int(opts["REPS"])
    cw, ch = int(opts["CROP_W"]), int(opts["CROP_H"])
    scenes = int(opts["SCENES"])
    rw, rh = int(opts["CROP_REGION_W"]), int(opts["CROP_REGION_H"])
    stride, win = int(opts["SSIM_STRIDE"]), int(opts["SSIM_WIN"])
    out_root = PF_JOB_OUT
    for sub in ("half", "upscaled", "crops"):
        os.makedirs(os.path.join(out_root, sub), exist_ok=True)

    t_start = time.time()
    images = []
    for entry in manifest["images"]:
        path = by_name[entry["file"]]
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if entry.get("sha256") and digest != entry["sha256"]:
            raise SystemExit(f"{entry['file']}: sha256 与 manifest 不符")
        w, h, rows = png_read(path)
        if w < cw or h < ch:
            raise SystemExit(f"{entry['file']}: {w}x{h} 小于工作区 {cw}x{ch}")
        images.append((entry, cw, ch, center_crop(rows, w, h, cw, ch)))
        print(f"[load] {entry['file']} {w}x{h} -> crop {cw}x{ch} sha256={digest[:12]}", flush=True)

    results = []
    for idx, (entry, w, h, work) in enumerate(images):
        name = entry["file"].rsplit(".", 1)[0]
        hw, hh, half = halve_area(work, w, h)
        png_write(os.path.join(out_root, "half", name + ".png"), hw, hh, half)
        la = luma_plane(work, w, h)
        ta, iw = integral(la, w, h)
        taa, _ = integral([v * v for v in la], w, h)
        outputs = {}
        for method in methods:
            if idx == 0:  # 首图预热一次，不计入计时
                t0 = time.perf_counter()
                resize(method, half, hw, hh, w, h)
                print(f"[warm] {method} {time.perf_counter() - t0:.1f}s", flush=True)
            ms, output = [], None
            for _ in range(reps):
                t0 = time.perf_counter()
                output = resize(method, half, hw, hh, w, h)
                ms.append((time.perf_counter() - t0) * 1000.0)
            outputs[method] = output
            up_dir = os.path.join(out_root, "upscaled", name)
            os.makedirs(up_dir, exist_ok=True)
            png_write(os.path.join(up_dir, method + ".png"), w, h, output)
            p = psnr_rgb(work, output, w, h)
            lb = luma_plane(output, w, h)
            s = ssim_box(la, lb, w, h, win=win, stride=stride, orig_ints=(ta, taa, iw))
            ms_sorted = sorted(ms)
            n = len(ms_sorted)
            median = ms_sorted[n // 2] if n % 2 else (ms_sorted[n // 2 - 1] + ms_sorted[n // 2]) / 2
            results.append({"image": name, "method": method, "psnr": p, "ssim": s,
                            "ms_reps": ms, "ms_median": median, "ms_min": min(ms)})
            print(f"[run] {name} {method} PSNR={p:.3f} SSIM={s:.5f} "
                  f"ms={['%.0f' % m for m in ms]} median={median:.0f}", flush=True)
        if idx < scenes:
            regions = {}
            x0, y0 = pick_crop_region(ta, taa, iw, w, h, rw, rh)
            regions["scene%d" % (idx + 1)] = (x0, y0, "variance")
            hp = fine_energy_plane(la, w, h)
            fx0, fy0 = pick_fine_region(hp, w, h, rw, rh, avoid=(x0, y0, rw, rh))
            regions["scene%dfine" % (idx + 1)] = (fx0, fy0, "fine")
            for scene_name, (rx, ry, kind) in regions.items():
                scene_dir = os.path.join(out_root, "crops", scene_name)
                os.makedirs(scene_dir, exist_ok=True)
                variants = [("v1", "native", crop_png(work, rx, ry, rw, rh))]
                for mi, method in enumerate(methods):
                    variants.append((f"v{mi + 2}", method, crop_png(outputs[method], rx, ry, rw, rh)))
                for vid, _method, crop in variants:
                    png_write(os.path.join(scene_dir, vid + ".png"), rw, rh, crop)
                with open(os.path.join(scene_dir, "region.json"), "w") as fh:
                    json.dump({"image": name, "x": rx, "y": ry, "w": rw, "h": rh, "kind": kind,
                               "variants": {vid: method for vid, method, _ in variants}}, fh)
                print(f"[crop] {scene_name} <- {name} region=({rx},{ry}) kind={kind}", flush=True)

    def agg(method):
        rows = [r for r in results if r["method"] == method]
        return {"psnr_mean": sum(r["psnr"] for r in rows) / len(rows),
                "ssim_mean": sum(r["ssim"] for r in rows) / len(rows),
                "ms_median_mean": sum(r["ms_median"] for r in rows) / len(rows),
                "ms_min_mean": sum(r["ms_min"] for r in rows) / len(rows)}

    summary = {m: agg(m) for m in methods}
    base = summary.get("bilinear")
    vs_base = ({m: {"d_psnr_db": s["psnr_mean"] - base["psnr_mean"],
                    "d_ssim": s["ssim_mean"] - base["ssim_mean"],
                    "time_ratio": s["ms_median_mean"] / base["ms_median_mean"]}
                for m, s in summary.items()} if base else {})
    report = {
        "meta": {
            "argv": sys.argv[1:],
            "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
            "python": sys.platform + " " + sys.version.split()[0],
            "work_size": [cw, ch],
            "half_size": [cw // 2, ch // 2],
            "methods": methods,
            "reps": reps,
            "ssim_variant": f"box{win}x{win} stride{stride} luma Rec.601 C1/C2 standard",
            "wall_seconds": round(time.time() - t_start, 1),
            "material": manifest,
        },
        "results": results,
        "summary": summary,
        "vs_bilinear": vs_base,
    }
    with open(os.path.join(out_root, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)

    lines = ["# 图像放大方法基准（缩到一半再放大回原尺寸）", "",
             f"- 工作图：{cw}×{ch}（素材中心裁剪，原生像素）；缩小 2×2 面积平均；输出 {cw}×{ch}",
             f"- 计时：每方法每图 {reps} 次重复的中位数（再对图取均值）；单线程纯 Python 标准库",
             f"- PSNR 全图 RGB；SSIM {report['meta']['ssim_variant']}", "",
             "| 方法 | PSNR (dB) | SSIM | 单次耗时 (ms) | 相对双线性耗时 | ΔPSNR vs 双线性 (dB) |",
             "|---|---|---|---|---|---|"]
    for m in methods:
        s = summary[m]
        if base:
            lines.append(f"| {m} | {s['psnr_mean']:.3f} | {s['ssim_mean']:.5f} | "
                         f"{s['ms_median_mean']:.0f} | {vs_base[m]['time_ratio']:.2f}× | "
                         f"{vs_base[m]['d_psnr_db']:+.2f} |")
        else:
            lines.append(f"| {m} | {s['psnr_mean']:.3f} | {s['ssim_mean']:.5f} | {s['ms_median_mean']:.0f} | - | - |")
    lines += ["", "## 每图明细", "", "| 图 | 方法 | PSNR (dB) | SSIM | ms(中位) |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['image']} | {r['method']} | {r['psnr']:.3f} | {r['ssim']:.5f} | {r['ms_median']:.0f} |")
    with open(os.path.join(out_root, "table.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(os.path.join(out_root, "summary.csv"), "w") as fh:
        fh.write("image,method,psnr_db,ssim,ms_median,ms_min\n")
        for r in results:
            fh.write(f"{r['image']},{r['method']},{r['psnr']:.4f},{r['ssim']:.6f},"
                     f"{r['ms_median']:.1f},{r['ms_min']:.1f}\n")
    print("[done] 汇总写入 report.json / table.md / summary.csv", flush=True)


if __name__ == "__main__":
    main()
