#!/usr/bin/env python3
"""fd_motion.py — 用速度图 (mv 附件) 量一段序列的运动, 判断「角色在跑、画面在变」是否达标。只用标准库。

    python3 -I fd_motion.py <档目录> ... [--every 2] [--json-out 目录]   # 读 fd_convert 转好的 mv/ ui/ PNG, 写 motion.json
fd_convert.py 转换时逐帧调 frame_stats() 和 center_samples(), 结论 (verdict) 进它的返回值, gi_capture.py 写进 report.json。

速度图解码 (原神 7.2, 翻正后): u = 小端 32 位; r=(u>>20)&1023 水平, g=(u>>10)&1023 竖直, b=u&1023 (1023 = 角色等运动物体);
v = sign(c-509)·((c-509)/511.5)², 单位是画面宽/高。位移一律折成「1080p 像素/帧」(水平 ×1920, 竖直 ×1080), 和档位无关。

逐帧四个量 (角色掩码以外, 每隔几个像素取一个点):
  speed_med    位移中位数                                  —— 画面整体动得多快
  moving_frac  位移 > 1 像素的点占比                        —— 动的是整个画面, 不只是角色四肢或一片水面
  run_px       近处地面 (y 0.80–0.95) 和远处 (y 0.10–0.30) 竖直位移中位数之差的绝对值 (都取 x 0.25–0.75)
               角色往前 (或朝镜头) 跑, 镜头跟着平移, 脚下的地面上下流过、远处几乎不动, 差值大;
               左右转镜头 (原神的镜头绕角色转) 只有水平分量, 竖直差为 0; 上下转镜头整屏一起移, 差也接近 0;
               原地卡住 (顶着石头) 差为 0 —— 所以它只对「角色真的在位移」敏感
  center_lag30 画面中央 (x 0.3–0.7, y 0.2–0.75) 和 30 帧 (0.5 秒) 前的灰度差中位数, 两帧任一帧是角色的点不算
               —— 内容在换, 而不是同一个画面上角色在动

一段达标 (阈值见 THRESH, 按段内帧数比例判, 偶尔停一下、转个弯不算失败):
  在跑  run_px >= 4 的帧 >= 70%
  在变  speed_med >= 2 且 moving_frac >= 0.6 的帧 >= 70%, 且 center_lag30 的段中位数 >= 3
帧率不是 60 时, 位移阈值按实测帧间隔等比折算 (阈值是「每 1/60 秒」的量)。
"""
import argparse
import csv
import json
import os
import struct
import zlib

ZERO, SCALE = 509.0, 511.5
THRESH = {
    # 实测 (gi-seq-*-20261113, 60 fps): 正常跑步 run_px 10–11; 游泳 2.2–2.6; 慢走/后退 2.4–3.2;
    # 只转镜头 0.0; 顶着礁石原地跑 0.0。取 4: 跑步 (含上坡、边跑边转) 留一倍余量, 游泳和慢走排除在外。
    "run_px": 4.0, "run_frames": 0.70,
    # 画面在动: 跑步段 speed_med 3–5.4、moving_frac 0.73–1.0; 夜里游泳 0.9–1.1、0.46–0.54。
    # 2 像素/帧 = 每秒 120 像素, 再慢对超分/插帧就近似静止; 60% 的点在动, 保证动的是整个场景。
    "speed_px": 2.0, "moving_frac": 0.60, "change_frames": 0.70,
    # 内容在换 (兜底, 防「速度图说在动、画面却没变」): 0–255 灰度, 0.5 秒前后中央区差中位数。实测受亮度和纹理影响大
    # (夜里、沙滩、水面都平): 跑步段 4–10、快速转镜头 5–9; 原地卡住 2、夜里游泳 1。取 3, 只拦画面冻住的段。
    "center_lag30": 3,
}
NEAR, FAR, XS = (0.80, 0.95), (0.10, 0.30), (0.25, 0.75)
CX, CY, NX, NY = (0.3, 0.7), (0.2, 0.75), 48, 24


def _u32(b, i):
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)


def _speed(c, scale):
    d = c - ZERO
    v = (d / SCALE) ** 2 * scale
    return v if d >= 0 else -v


def _med(a):
    return sorted(a)[len(a) // 2] if a else 0.0


def frame_stats(b, w, h, step=None):
    """b: 已翻正的速度图原始字节 (每像素 4 字节, 行优先)。返回逐帧指标, 没有有效点时返回 None。"""
    step = step or max(1, w // 240)
    speeds, near, far = [], [], []
    n_obj = 0
    for y in range(0, h, step):
        fy = y / h
        row = y * w * 4
        band = 1 if NEAR[0] <= fy <= NEAR[1] else 2 if FAR[0] <= fy <= FAR[1] else 0
        for x in range(0, w, step):
            u = _u32(b, row + x * 4)
            if (u & 1023) == 1023:
                n_obj += 1
                continue
            vx = _speed((u >> 20) & 1023, 1920)
            vy = -_speed((u >> 10) & 1023, 1080)   # 翻正后向下为正
            speeds.append((vx * vx + vy * vy) ** 0.5)
            if band and XS[0] <= x / w <= XS[1]:
                (near if band == 1 else far).append(vy)
    if not speeds:
        return None
    speeds.sort()
    return {"speed_med": round(speeds[len(speeds) // 2], 3),
            "moving_frac": round(sum(1 for s in speeds if s > 1.0) / len(speeds), 4),
            "run_px": round(abs(_med(near) - _med(far)), 3),
            "obj_frac": round(n_obj / (n_obj + len(speeds)), 4)}


def center_samples(img, w, h, ch, kind="rgb"):
    """中央 NX*NY 个点: kind=rgb 返回灰度, kind=mv 返回该点是不是角色 (速度图翻正后的原始字节)。"""
    out = []
    for j in range(NY):
        y = int((CY[0] + (CY[1] - CY[0]) * j / (NY - 1)) * (h - 1))
        for i in range(NX):
            x = int((CX[0] + (CX[1] - CX[0]) * i / (NX - 1)) * (w - 1))
            o = (y * w + x) * ch
            if kind == "mv":
                out.append((_u32(img, o) & 1023) == 1023)
            else:
                out.append((img[o] * 299 + img[o + 1] * 587 + img[o + 2] * 114) // 1000)
    return out


def center_lag(by_frame, lag=30):
    """by_frame: {帧号: (灰度采样, 角色掩码或 None)}; 返回 {帧号: 与 lag 帧前的差中位数}。"""
    out = {}
    for f, (g, m) in by_frame.items():
        p = by_frame.get(f - lag)
        if not p or not g or not p[0] or len(p[0]) != len(g):
            continue
        pm = p[1]
        v = sorted(abs(a - b) for k, (a, b) in enumerate(zip(g, p[0]))
                   if not ((m and m[k]) or (pm and pm[k])))
        if v:
            out[f] = v[len(v) // 2]
    return out


def verdict(per_frame, lags, dt_ms=1000 / 60):
    """per_frame: frame_stats 的列表 (可含 None); lags: center_lag30 的值列表; dt_ms: 实测帧间隔中位数。"""
    fr = [p for p in per_frame if p]
    if not fr:
        return {"ok": False, "why": "没有速度图"}
    k = dt_ms / (1000 / 60)          # 帧率低于 60 时每帧位移更大, 阈值等比放大
    n = len(fr)
    run = sum(1 for p in fr if p["run_px"] >= THRESH["run_px"] * k) / n
    chg = sum(1 for p in fr if p["speed_med"] >= THRESH["speed_px"] * k and p["moving_frac"] >= THRESH["moving_frac"]) / n
    lag = _med(lags) if lags else None
    out = {"frames": n, "fps": round(1000 / dt_ms, 1), "running_frames": round(run, 3), "changing_frames": round(chg, 3),
           "center_lag30_med": lag, "run_px_med": _med([p["run_px"] for p in fr]),
           "speed_med": _med([p["speed_med"] for p in fr]), "moving_frac_med": _med([p["moving_frac"] for p in fr]),
           "thresholds": THRESH}
    why = []
    if run < THRESH["run_frames"]:
        why.append(f"在跑的帧 {run:.0%} < {THRESH['run_frames']:.0%}")
    if chg < THRESH["change_frames"]:
        why.append(f"画面在动的帧 {chg:.0%} < {THRESH['change_frames']:.0%}")
    if lag is None or lag < THRESH["center_lag30"]:
        why.append(f"0.5 秒中央差中位数 {lag} < {THRESH['center_lag30']}")
    out["ok"] = not why
    out["why"] = "；".join(why)
    return out


def read_png(path):
    """读 fd_convert 写的 PNG (每行过滤类型 0)。返回 (w, h, 每像素字节, 像素字节)。"""
    data = open(path, "rb").read()
    pos, idat, w, h, bpp = 8, [], 0, 0, 0
    while pos < len(data):
        n, t = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if t == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", body[:10])
            bpp = {2: 3, 6: 4, 0: 1}[ct] * bd // 8
        elif t == b"IDAT":
            idat.append(body)
        pos += 12 + n
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    rows = bytearray()
    for y in range(h):
        o = y * (stride + 1)
        if raw[o] != 0:
            raise ValueError(f"{path}: 不是 fd_convert 写的 PNG (行过滤类型 {raw[o]})")
        rows += raw[o + 1:o + 1 + stride]
    return w, h, bpp, bytes(rows)


def audit_dir(d, every=1, mv="mv", json_out=None):
    """对 fd_convert 转好的档目录量 (读 PNG), 写 motion.json (默认写进档目录; 审计别的任务的数据时给 json_out, 不碰原目录),
    返回结论。every>1 时隔帧量, 中央差仍按 30 帧配对。"""
    rows = list(csv.DictReader(open(os.path.join(d, "checks.csv"))))
    ok = [int(r["frame"]) for r in rows if r.get(mv) == "ok" and r["ui"] == "ok"]
    pick = set(ok[::every]) | {f - 30 for f in ok[::every]}
    per, cent = [], {}
    for f in ok:
        if f not in pick:
            continue
        w, h, _, b = read_png(os.path.join(d, mv, f"f{f:010d}.png"))
        mask = center_samples(b, w, h, 4, "mv")
        if f in ok[::every]:
            s = frame_stats(b, w, h)
            if s:
                s["frame"] = f
            per.append(s)
        uw, uh, ub, u = read_png(os.path.join(d, "ui", f"f{f:010d}.png"))
        cent[f] = (center_samples(u, uw, uh, ub), mask)
    lags = center_lag(cent)
    t = [int(r["t_ns"]) for r in rows if r.get("t_ns")]
    dt = _med([(b - a) / 1e6 for a, b in zip(t, t[1:]) if b > a]) or 1000 / 60
    v = verdict(per, list(lags.values()), dt)
    for p in per:
        if p and p["frame"] in lags:
            p["center_lag30"] = lags[p["frame"]]
    dst = json_out or os.path.join(d, "motion.json")
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    json.dump({"dir": d, "verdict": v, "per_frame": per}, open(dst, "w"), ensure_ascii=False)
    return v


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--every", type=int, default=1, help="每隔几帧量一帧 (旧数据快速审计用)")
    ap.add_argument("--mv", default="mv", help="速度图附件名")
    ap.add_argument("--json-out", help="motion_<序号>.json 写到这个目录, 不写进被审计的档目录")
    a = ap.parse_args()
    for i, d in enumerate(a.dirs):
        jo = os.path.join(a.json_out, f"motion_{i}.json") if a.json_out else None
        print(json.dumps({"dir": d, **audit_dir(d, a.every, a.mv, jo)}, ensure_ascii=False), flush=True)
