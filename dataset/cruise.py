#!/usr/bin/env python3
"""cruise.py — 原神大世界触屏巡航截图 (capture_native.sh CAPTURER=cruise 调用, 须已在设备锁下且游戏已进大世界)。

    python3 -I dataset/cruise.py <serial> <输出目录> [--frames 300] [--settle-ms 800] [--walk-ms 4000] [--timeout-s 5400] [--seed 0]

每步: 右侧空白区横划转视角 (段首大转 90–180°, 段内小转) → 左下摇杆按住前推 --walk-ms 走位 → 等 --settle-ms → screencap。
只发 input swipe; 识别到溺水/全队倒下复苏页时停止走位并点底部中央「复苏」, 恢复后继续。
输出与 phonefarm capture 同格式: frames/frame_NNNNN.png (screencap 原字节)
+ frames/manifest.jsonl (i/file/ts_ms/step/state/w/h/bytes/sha256/segment/diff_prev, 每 10 张保留帧一个 segment),
另写 cruise.json 汇总。普通帧记 explore; 复苏页记 revive_dialog, 点击后的首张非复苏页记 revive_transition,
make_pairs.py 会按 state != explore 自动丢弃。识别失败记 unknown, 不把不确定画面发布进数据集。
与上一保留帧字节完全相同记冻结跳过, 缩略图差 diff_prev 留 null 由 make_pairs.py 离线算。
退出码: 0 收满, 1 超时或步数用尽, 2 设备错 (连续截图失败 / 游戏不在前台)。
"""
import argparse
import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import time
import zlib

PKG = "com.miHoYo.Yuanshen"
SEGMENT_FRAMES = 10                 # 与 phonefarm capture 一致
STICK = (0.14, 0.74)                # 左下摇杆按下点 (横屏比例坐标), 在 HUD 排除区 joystick_chat 里
CAM_Y = (0.30, 0.48)                # 转视角的横划高度: 队伍栏左侧、技能区上方的空白
CAM_X = (0.40, 0.72)
REVIVE_TAP = (0.50, 0.89)             # 溺水/全队倒下共用的底部中央「复苏」按钮
BUTTON_POINTS = [(x, y) for y in (0.872, 0.895, 0.916) for x in (0.43, 0.45, 0.55, 0.57)]
RED_POINTS = [(x, y) for y in (0.30, 0.40, 0.55, 0.65) for x in (0.32, 0.38, 0.62, 0.68)]
SIPS = shutil.which("sips")            # macOS 节点先缩略图; 其他宿主用下面的标准库 PNG 解码


def lock_fds():
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


def adb(serial, *args, timeout=60, binary=False):
    r = subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=timeout, pass_fds=lock_fds())
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").replace("\r", "")


def png_size(b):
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def png_samples(b, points):
    """标准库读取 8-bit RGB/RGBA PNG 的少量归一化坐标; 无第三方依赖。"""
    if len(b) < 33 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", b[16:29])
    channels = {2: 3, 6: 4}.get(color)
    if depth != 8 or channels is None or interlace or not w or not h:
        return None
    ids, pos = [], 8
    while pos + 12 <= len(b):
        n = struct.unpack(">I", b[pos:pos + 4])[0]
        if pos + 12 + n > len(b):
            return None
        if b[pos + 4:pos + 8] == b"IDAT":
            ids.append(b[pos + 8:pos + 8 + n])
        pos += 12 + n
    xy = [(min(h - 1, int(y * h)), min(w - 1, int(x * w)), i) for i, (x, y) in enumerate(points)]
    wanted = {}
    for y, x, i in xy:
        wanted.setdefault(y, []).append((x, i))
    raw = zlib.decompress(b"".join(ids))
    stride = w * channels
    if len(raw) != h * (stride + 1):
        return None
    out, prev = [None] * len(points), bytearray(stride)
    for y in range(h):
        start = y * (stride + 1)
        f, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if f == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 255
        elif f == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                row[i] = (row[i] + ((row[i - channels] if i >= channels else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                bb, c = prev[i], prev[i - channels] if i >= channels else 0
                p = a + bb - c
                pa, pb, pc = abs(p - a), abs(p - bb), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else bb if pb <= pc else c)) & 255
        elif f != 0:
            return None
        if y in wanted:
            for x, i in wanted[y]:
                p = x * channels
                out[i] = tuple(row[p:p + 3])
        prev = row
    return out


def revive_dialog(path):
    """共同特征: 中央暗红遮罩 + 底部浅奶油色复苏按钮; 不依赖标题文字。"""
    thumb = path + ".revive-thumb.png"
    data = None
    try:
        if SIPS:
            r = subprocess.run([SIPS, "-s", "format", "png", "-z", "36", "64", path, "--out", thumb],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                with open(thumb, "rb") as f:
                    data = f.read()
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        samples = png_samples(data, BUTTON_POINTS + RED_POINTS)
        if samples is None or any(p is None for p in samples):
            return None
        button = sum(min(p) >= 175 and max(p) - min(p) <= 55 for p in samples[:len(BUTTON_POINTS)])
        red = sum(r > 30 and r > 1.25 * g and r > 1.15 * b and g < 120
                  for r, g, b in samples[len(BUTTON_POINTS):])
        return button >= 9 and red >= 8
    except (OSError, ValueError, zlib.error, subprocess.TimeoutExpired) as e:
        print(f"复苏识别失败: {path}: {e}", file=sys.stderr, flush=True)
        return None
    finally:
        try:
            os.unlink(thumb)
        except FileNotFoundError:
            pass


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
    revive_dialogs = revive_taps = revived = 0
    recovering, last_revive_tap, revive_events = False, 0.0, []
    prev_sha, w, h, stop = None, 0, 0, "max_steps"
    while steps < a.frames * 3:
        if time.time() - t0 > a.timeout_s:
            stop = "timeout"
            break
        steps += 1
        if w and not recovering:   # 已知横屏尺寸且不在复苏页时才动: 先转视角, 再推摇杆走
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
        path = os.path.join(fdir, name)
        with open(path, "wb") as f:
            f.write(png)
        ts_ms = int(time.time() * 1000)
        dialog = revive_dialog(path)
        state, state_source = ("revive_dialog", "revive_button_pixels_v1") if dialog else (
            "unknown", "revive_detector_error") if dialog is None else ("explore", "foreground_landscape")
        if dialog:
            if not recovering:
                revive_dialogs += 1
                revive_events.append({"ts_ms": ts_ms, "frame": kept, "event": "detected"})
            recovering = True
            now = time.time()
            if now - last_revive_tap >= 2.0:
                adb(a.serial, "shell", f"input tap {int(REVIVE_TAP[0] * w)} {int(REVIVE_TAP[1] * h)}")
                revive_taps += 1
                last_revive_tap = now
                revive_events.append({"ts_ms": int(now * 1000), "frame": kept, "event": "tap"})
        elif dialog is False and recovering:
            state, state_source = "revive_transition", "revive_button_pixels_v1"
            recovering = False
            revived += 1
            revive_events.append({"ts_ms": ts_ms, "frame": kept, "event": "recovered"})
        man.write(json.dumps({"i": kept, "file": name, "ts_ms": ts_ms, "step": steps, "state": state,
                              "state_source": state_source, "w": w, "h": h, "bytes": len(png), "sha256": sha,
                              "segment": kept // SEGMENT_FRAMES, "diff_prev": None}) + "\n")
        man.flush()
        prev_sha = sha
        kept += 1
        extra = f" state={state} 复苏={revived}/{revive_dialogs}" if state != "explore" else ""
        print(f"第{steps}步: 保留 {name} ({w}x{h}, {len(png) // 1024} KB) 进度 {kept}/{a.frames}{extra}",
              file=sys.stderr, flush=True)
        if kept >= a.frames:
            stop = "target"
            break
    rep = {"v": 1, "ok": kept >= a.frames, "capturer": "cruise", "frames": kept, "target": a.frames, "steps": steps,
           "grab_failed": grab_fail, "portrait": portrait, "duplicates": dups, "not_foreground": not_fg,
           "revive_dialogs": revive_dialogs, "revive_taps": revive_taps, "revived": revived,
           "revive_events": revive_events, "revive_detector": "sips_thumbnail" if SIPS else "stdlib_png",
           "segment_frames": SEGMENT_FRAMES, "walk_ms": a.walk_ms, "settle_ms": a.settle_ms, "seed": a.seed,
           "wall_s": round(time.time() - t0, 1), "stop_reason": stop, "manifest": os.path.join(fdir, "manifest.jsonl")}
    json.dump(rep, open(os.path.join(a.out, "cruise.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if rep["ok"] else 2 if stop.startswith(("grab_failed", "not_foreground")) else 1


if __name__ == "__main__":
    sys.exit(main())
