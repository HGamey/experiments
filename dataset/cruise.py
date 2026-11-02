#!/usr/bin/env python3
"""cruise.py — 原神大世界触屏巡航截图 (capture_native.sh CAPTURER=cruise 调用, 须已在设备锁下且游戏已进大世界)。

    python3 -I dataset/cruise.py <serial> <输出目录> [--frames 300] [--settle-ms 800] [--walk-ms 4000] [--timeout-s 5400] [--seed 0]

每步: 右侧空白区横划转视角 (段首大转 90–180°, 段内小转) → 左下摇杆按住前推 --walk-ms 走位 → 等 --settle-ms → screencap。
只发 input swipe, 不点技能/菜单/对话。输出与 phonefarm capture 同格式: frames/frame_NNNNN.png (screencap 原字节)
+ frames/manifest.jsonl (i/file/ts_ms/step/state/w/h/bytes/sha256/segment/diff_prev, 每 10 张保留帧一个 segment),
另写 cruise.json 汇总。state 只按「游戏在前台且横屏」记 explore (不认菜单/对话, 留给离线 QA 和人工看图);
与上一保留帧字节完全相同记冻结跳过, 缩略图差 diff_prev 留 null 由 make_pairs.py 离线算。
退出码: 0 收满, 1 超时或步数用尽, 2 设备错 (连续截图失败 / 游戏不在前台)。
"""
import argparse
import hashlib
import json
import math
import os
import random
import struct
import subprocess
import sys
import time

PKG = "com.miHoYo.Yuanshen"
SEGMENT_FRAMES = 10                 # 与 phonefarm capture 一致
STICK = (0.14, 0.74)                # 左下摇杆按下点 (横屏比例坐标), 在 HUD 排除区 joystick_chat 里
CAM_Y = (0.30, 0.48)                # 转视角的横划高度: 队伍栏左侧、技能区上方的空白
CAM_X = (0.40, 0.72)


def lock_fds():
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


def adb(serial, *args, timeout=60, binary=False):
    r = subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=timeout, pass_fds=lock_fds())
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").replace("\r", "")


def png_size(b):
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--settle-ms", type=int, default=800)
    ap.add_argument("--walk-ms", type=int, default=4000)
    ap.add_argument("--timeout-s", type=int, default=5400)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    fdir = os.path.join(a.out, "frames")
    os.makedirs(fdir, exist_ok=True)
    man = open(os.path.join(fdir, "manifest.jsonl"), "a")
    t0 = time.time()
    kept = steps = grab_fail = portrait = dups = not_fg = 0
    prev_sha, w, h, stop = None, 0, 0, "max_steps"
    while steps < a.frames * 3:
        if time.time() - t0 > a.timeout_s:
            stop = "timeout"
            break
        steps += 1
        if w:   # 已知横屏尺寸后才动: 先转视角, 再推摇杆走
            if kept % SEGMENT_FRAMES == 0:
                turn = rng.choice((-1, 1)) * rng.uniform(0.30, 0.55)   # 段首大转, 让相邻段看不同方向
            else:
                turn = rng.uniform(-0.10, 0.10)
            cy = int(rng.uniform(*CAM_Y) * h)
            cx = int(rng.uniform(*CAM_X) * w)
            x1 = max(int(0.05 * w), min(int(0.95 * w), cx + int(turn * w)))
            if abs(x1 - cx) > 40:
                adb(a.serial, "shell", f"input swipe {cx} {cy} {x1} {cy} 450")
            sx, sy = int(STICK[0] * w), int(STICK[1] * h)
            ang = math.radians(rng.uniform(-20, 20))       # 以正前方为主, 略偏
            r = 0.13 * h
            adb(a.serial, "shell", f"input swipe {sx} {sy} {int(sx + r * math.sin(ang))} {int(sy - r * math.cos(ang))} {a.walk_ms}",
                timeout=a.walk_ms / 1000 + 30)
        time.sleep(a.settle_ms / 1000)
        png = adb(a.serial, "exec-out", "screencap -p", binary=True, timeout=60)
        size = png_size(png)
        if not size:
            grab_fail += 1
            if grab_fail >= 10:
                stop = "grab_failed"
                break
            continue
        if size[0] < size[1]:
            portrait += 1
            continue
        w, h = size
        if steps % 5 == 1:   # 前台核对: 不是原神就停 (不自己点任何东西去找回)
            fg = adb(a.serial, "shell", "dumpsys window | grep -m1 mCurrentFocus")
            if PKG not in fg:
                not_fg += 1
                if not_fg >= 3:
                    stop = "not_foreground: " + fg.strip()[:160]
                    break
                continue
            not_fg = 0
        sha = hashlib.sha256(png).hexdigest()
        if sha == prev_sha:
            dups += 1
            continue
        name = f"frame_{kept:05d}.png"
        with open(os.path.join(fdir, name), "wb") as f:
            f.write(png)
        man.write(json.dumps({"i": kept, "file": name, "ts_ms": int(time.time() * 1000), "step": steps, "state": "explore",
                              "state_source": "foreground_landscape", "w": w, "h": h, "bytes": len(png), "sha256": sha,
                              "segment": kept // SEGMENT_FRAMES, "diff_prev": None}) + "\n")
        man.flush()
        prev_sha = sha
        kept += 1
        print(f"第{steps}步: 保留 {name} ({w}x{h}, {len(png) // 1024} KB) 进度 {kept}/{a.frames}", file=sys.stderr, flush=True)
        if kept >= a.frames:
            stop = "target"
            break
    rep = {"v": 1, "ok": kept >= a.frames, "capturer": "cruise", "frames": kept, "target": a.frames, "steps": steps,
           "grab_failed": grab_fail, "portrait": portrait, "duplicates": dups, "not_foreground": not_fg,
           "segment_frames": SEGMENT_FRAMES, "walk_ms": a.walk_ms, "settle_ms": a.settle_ms, "seed": a.seed,
           "wall_s": round(time.time() - t0, 1), "stop_reason": stop, "manifest": os.path.join(fdir, "manifest.jsonl")}
    json.dump(rep, open(os.path.join(a.out, "cruise.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if rep["ok"] else 2 if stop.startswith(("grab_failed", "not_foreground")) else 1


if __name__ == "__main__":
    sys.exit(main())
