"""gi_enter.py — 原神冷启动进世界, 不靠 OCR (中心节点是 Linux, phonefarm onboard 的 OCR 只能在 macOS 上编)。

判据只有一个: 角色血条左段是绿色 (原神 7.2, 横屏比例坐标 x=0.43..0.46, y=0.918, 实测 RGB 150,215,34)。
没进世界就每 --tap-every 秒点一下屏幕中央: 门口「点击进入」点哪里都进; 资源下载页、白色加载页、世界里点中央都无害。
截图用 screencap 的原始 RGBA (不解码 PNG), 一张 13 MB, 取几个像素。
"""
import os
import struct
import subprocess
import time

HP_POINTS = [(0.43, 0.918), (0.44, 0.918), (0.45, 0.918), (0.46, 0.918)]


def _lock_fds():
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


def raw_shot(serial, timeout=60):
    """(w, h, rgba bytes); 头部长度按 总长 - w*h*4 算 (安卓版本不同是 12 或 16 字节)。"""
    b = subprocess.run(["adb", "-s", serial, "exec-out", "screencap"], capture_output=True, timeout=timeout,
                       pass_fds=_lock_fds()).stdout
    if len(b) < 16:
        return None
    w, h = struct.unpack("<II", b[:8])
    head = len(b) - w * h * 4
    if w == 0 or h == 0 or head not in (12, 16):
        return None
    return w, h, memoryview(b)[head:]


def pixel(shot, fx, fy):
    """横屏比例坐标取像素; 截图是竖屏时按顺时针转 90 度后的横屏坐标换算 (红魔横屏游戏时 screencap 是横的)。"""
    w, h, px = shot
    if w >= h:
        x, y = int(fx * (w - 1)), int(fy * (h - 1))
    else:
        x, y = int((1 - fy) * (w - 1)), int(fx * (h - 1))
    o = (y * w + x) * 4
    return px[o], px[o + 1], px[o + 2]


def in_world(shot):
    if not shot:
        return False
    good = 0
    for fx, fy in HP_POINTS:
        r, g, b = pixel(shot, fx, fy)
        good += g > 180 and 100 < r < 200 and b < 100
    return good >= 3


def save_png(serial, path):
    png = subprocess.run(["adb", "-s", serial, "exec-out", "screencap -p"], capture_output=True, timeout=60,
                         pass_fds=_lock_fds()).stdout
    with open(path, "wb") as f:
        f.write(png)
    return path


def enter_world(serial, pkg, cold=True, timeout=900, tap_every=6.0, first_tap_after=20.0, log=print, fail_png=None):
    """冷启动 (cold) 或接着当前画面进世界。返回 (ok, 秒数, 点了几下)。连续两次看到血条才算进了。"""
    sh = lambda c: subprocess.run(["adb", "-s", serial, "shell", c], capture_output=True, text=True, timeout=60,
                                  pass_fds=_lock_fds()).stdout
    t0 = time.time()
    sh("input keyevent KEYCODE_WAKEUP")
    if cold:
        sh(f"am force-stop {pkg}")
        time.sleep(1)
        sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    elif not sh(f"pidof {pkg}").strip():
        sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    seen, taps, last_tap = 0, 0, 0.0
    while time.time() - t0 < timeout:
        time.sleep(2)
        shot = raw_shot(serial)
        if in_world(shot):
            seen += 1
            if seen >= 2:
                log(f"进世界: {time.time() - t0:.0f} 秒, 点了 {taps} 下")
                return True, round(time.time() - t0, 1), taps
            continue
        seen = 0
        if time.time() - t0 >= first_tap_after and time.time() - last_tap >= tap_every and shot:
            w, h, _ = shot
            cx, cy = (w // 2, h // 2)
            sh(f"input tap {cx} {cy}")
            taps += 1
            last_tap = time.time()
    if fail_png:
        save_png(serial, fail_png)
    log(f"{timeout} 秒内没进世界, 点了 {taps} 下")
    return False, round(time.time() - t0, 1), taps
