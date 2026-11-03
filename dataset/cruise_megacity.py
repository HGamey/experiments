#!/usr/bin/env python3
"""cruise_megacity.py — Megacity Metro 飞行巡航截图 (capture_native.sh PKG=com.unity.megacity.metro 调用, 须已在设备锁下且已进游戏)。

    python3 -I dataset/cruise_megacity.py <serial> <输出目录> [--frames 40] [--settle-ms 800] [--timeout-s 1800] [--seed 0]

每段 10 帧一种运动, 按 static → turn_slow → turn_fast → flight 轮换, 写进 manifest 的 motion (来自输入记录, 不是看图判断):
  static     油门 0, 小转视角后停稳再截图
  turn_slow  油门 0, 瞄准摇杆小幅偏移, 按住时截图
  turn_fast  油门 0, 瞄准摇杆大幅偏移, 按住时截图
  flight     油门约一半, 边飞边截图; 每帧都转向, 免得撞墙后一直贴着墙
段首大转向, 让相邻段看不同方向。只发 input swipe, 不碰左上角设置。
输出与 cruise.py 同格式: frames/frame_NNNNN.png (screencap 原字节) + frames/manifest.jsonl, 另写 cruise.json 汇总。
退出码: 0 收满, 1 超时或步数用尽, 2 设备错 (连续截图失败 / 游戏不在前台)。
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # -I 不带脚本目录
from cruise import SEGMENT_FRAMES, adb, lock_fds, png_size

PKG = "com.unity.megacity.metro"
AIM = (0.20, 0.60)                  # 左半屏瞄准摇杆按下点: 偏移越大转得越快, 松手即停
THROTTLE_X = 0.94                   # 右侧油门滑块: 要按在滑块这一列才算数, 松手保持
THROTTLE_ZERO, THROTTLE_FLY = 0.735, 0.66   # y=0.735 油门 0, y≈0.585 约 93 (全速约 10 秒撞墙)
MOTIONS = ("static", "turn_slow", "turn_fast", "flight")
PITCH_LIMIT = 0.06                  # 俯仰累计偏移上限, 免得一直朝天或朝地


def swipe(serial, w, h, x, y, dx, dy, ms, wait=True):
    cmd = f"input swipe {int(x * w)} {int(y * h)} {int((x + dx) * w)} {int((y + dy) * h)} {ms}"
    if wait:
        adb(serial, "shell", cmd, timeout=ms / 1000 + 30)
        return None
    return subprocess.Popen(["adb", "-s", serial, "shell", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            pass_fds=lock_fds())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--settle-ms", type=int, default=800)
    ap.add_argument("--timeout-s", type=int, default=1800)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    fdir = os.path.join(a.out, "frames")
    os.makedirs(fdir, exist_ok=True)
    man = open(os.path.join(fdir, "manifest.jsonl"), "a")
    t0 = time.time()
    kept = steps = grab_fail = portrait = dups = not_fg = 0
    counts = dict.fromkeys(MOTIONS, 0)
    prev_sha, stop = None, "max_steps"
    throttle, pitch = THROTTLE_ZERO, 0.0
    w, h = 0, 0
    while steps < a.frames * 3:
        if time.time() - t0 > a.timeout_s:
            stop = "timeout"
            break
        steps += 1
        motion = MOTIONS[(kept // SEGMENT_FRAMES + a.seed) % len(MOTIONS)]
        hold = None
        if w:
            first = kept % SEGMENT_FRAMES == 0
            if first:
                want = THROTTLE_FLY if motion == "flight" else THROTTLE_ZERO
                if want != throttle:
                    swipe(a.serial, w, h, THROTTLE_X, throttle, 0, want - throttle, 300)
                    throttle = want
            side = rng.choice((-1, 1))
            dy = rng.uniform(-0.02, 0.02)
            if abs(pitch + dy) > PITCH_LIMIT:
                dy = -dy
            if first:   # 段首大转, 让相邻段看不同方向
                swipe(a.serial, w, h, *AIM, side * rng.uniform(0.06, 0.12), dy, 500)
                pitch += dy
            if motion == "turn_slow":
                hold = swipe(a.serial, w, h, *AIM, side * 0.03, 0, 1800, wait=False)
                time.sleep(0.7)
            elif motion == "turn_fast":
                hold = swipe(a.serial, w, h, *AIM, side * 0.14, 0, 1200, wait=False)
                time.sleep(0.5)
            elif motion == "flight":
                swipe(a.serial, w, h, *AIM, side * rng.uniform(0.02, 0.08), dy, 400)
                pitch += dy
                time.sleep(1.5)
            else:
                swipe(a.serial, w, h, *AIM, side * rng.uniform(0.01, 0.04), dy, 400)
                pitch += dy
                time.sleep(a.settle_ms / 1000)
        png = adb(a.serial, "exec-out", "screencap -p", binary=True, timeout=60)
        if hold:
            hold.wait(timeout=30)
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
        if steps % 5 == 1:   # 前台核对: 不是这款游戏就停 (不自己点任何东西去找回)
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
                              "segment": kept // SEGMENT_FRAMES, "diff_prev": None, "motion": motion, "throttle_y": throttle}) + "\n")
        man.flush()
        prev_sha = sha
        counts[motion] += 1
        kept += 1
        print(f"第{steps}步: 保留 {name} ({w}x{h}, {len(png) // 1024} KB) {motion} 进度 {kept}/{a.frames}", file=sys.stderr, flush=True)
        if kept >= a.frames:
            stop = "target"
            break
    rep = {"v": 1, "ok": kept >= a.frames, "capturer": "cruise", "script": "cruise_megacity.py", "frames": kept, "target": a.frames,
           "steps": steps, "grab_failed": grab_fail, "portrait": portrait, "duplicates": dups, "not_foreground": not_fg, "motion_frames": counts,
           "segment_frames": SEGMENT_FRAMES, "settle_ms": a.settle_ms, "seed": a.seed, "wall_s": round(time.time() - t0, 1),
           "stop_reason": stop, "manifest": os.path.join(fdir, "manifest.jsonl")}
    json.dump(rep, open(os.path.join(a.out, "cruise.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if rep["ok"] else 2 if stop.startswith(("grab_failed", "not_foreground")) else 1


if __name__ == "__main__":
    sys.exit(main())
