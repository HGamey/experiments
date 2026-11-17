#!/usr/bin/env python3
"""gi_nav.py — 原神大世界里「边跑边转镜头」的多指输入, 以及按小地图修正方向的导航。

Android 的 `input` 只有一根手指: 按住摇杆的同时拖镜头, 拖镜头那一下会把摇杆抬起来, 角色就停了。
这里直接往触摸屏的 evdev 节点 (/dev/input/eventN) 写多点触控协议 B 的事件: adb shell 的 shell 用户在 input 组,
shell 用户虽在 input 组, SELinux 不让它写输入设备, 所以经一个常驻的 root shell (adb shell su -c sh) 发出;
每条命令是一句 printf (八进制转义写出 input_event 结构)。不改游戏、不改系统设置。
坐标一律用「横屏比例坐标」(u, v) ∈ [0,1]², 按 dumpsys input 的显示视口 (方向、wm size 下的物理框) 换成面板坐标。

    python3 -I dataset/gi_nav.py <serial> --selftest [--pkg com.miHoYo.Yuanshen] [--tier 1920x1080]
自检: wm size → 进世界 → 打开「指针位置」叠加 → 两指同时 (摇杆前推 + 横拖镜头) → 截图 → 关叠加
      → 截小地图 / 点开大地图截图 → 收尾 (还原指针位置、wm size、强停游戏、HOME)。截图和 nav.json 写到 $PF_JOB_OUT/nav_selftest_<时间>/。
"""
import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gi_enter  # noqa: E402

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_TOUCH, BTN_TOOL_FINGER = 0x14A, 0x145
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x30, 0x35, 0x36, 0x39

# 原神 7.2 手机 HUD (横屏比例坐标, 16:9 和全屏都按画面比例)
STICK = (0.15, 0.75)        # 摇杆: 左下任意处按下就是摇杆中心 (浮动摇杆)
STICK_R = 0.13              # 推杆半径, 按画面高度的比例
CAM = (0.56, 0.40)          # 拖镜头起点: 队伍栏左侧、技能区上方的空白
SPRINT = (0.922, 0.861)     # 冲刺键 (按住持续冲刺, 耗体力)
JUMP = (0.922, 0.653)       # 跳跃键
MINIMAP = (0.109, 0.144)    # 小地图圆心; 点它打开大地图
SLOT_STICK, SLOT_CAM, SLOT_BTN = 7, 8, 9   # 用高位槽, 不和真实触摸 (0 起) 撞


def _lock_fds():
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


def sh(serial, cmd, timeout=60):
    r = subprocess.run(["adb", "-s", serial, "shell", cmd], capture_output=True, timeout=timeout, pass_fds=_lock_fds())
    return r.stdout.decode("utf-8", "replace").replace("\r", "")


def find_touch(serial):
    """getevent -lp 里带 ABS_MT_POSITION_X 且 INPUT_PROP_DIRECT 的设备: {dev, xmax, ymax, slots}。"""
    o = sh(serial, "getevent -lp")
    for blk in re.split(r"(?=^add device)", o, flags=re.M):
        m = re.match(r"add device \d+: (\S+)", blk)
        mx = re.search(r"ABS_MT_POSITION_X\s*:.*?max (\d+)", blk)
        my = re.search(r"ABS_MT_POSITION_Y\s*:.*?max (\d+)", blk)
        if m and mx and my and "INPUT_PROP_DIRECT" in blk:
            ms = re.search(r"ABS_MT_SLOT\s*:.*?max (\d+)", blk)
            return {"dev": m.group(1), "xmax": int(mx.group(1)), "ymax": int(my.group(1)),
                    "slots": int(ms.group(1)) + 1 if ms else 10, "name": (re.search(r'name:\s+"([^"]*)"', blk) or [0, ""])[1]}
    raise RuntimeError("没找到触摸屏设备 (getevent -lp)")


def viewport(serial):
    """dumpsys input 的内屏视口: 方向 0–3、物理框 (当前方向下的显示坐标)、自然方向的设备尺寸。"""
    o = sh(serial, "dumpsys input")
    m = re.search(r"Viewport INTERNAL:.*?orientation=(\d).*?physicalFrame=\[(-?\d+), (-?\d+), (-?\d+), (-?\d+)\]"
                  r".*?deviceSize=\[(\d+), (\d+)\]", o)
    if not m:
        raise RuntimeError("dumpsys input 里没有 Viewport INTERNAL")
    ori = int(m.group(1))
    pf = [int(m.group(i)) for i in range(2, 6)]
    dw, dh = int(m.group(6)), int(m.group(7))
    nat = (min(dw, dh), max(dw, dh))      # 自然方向 (竖屏) 宽高
    return {"orientation": ori, "physical_frame": pf, "natural": nat, "line": m.group(0)[:300]}


class Touch:
    """多指触控。down/move/up 的坐标是横屏比例坐标 (u, v)。"""

    def __init__(self, serial):
        self.serial = serial
        self.info = find_touch(serial)
        self.vp = viewport(serial)
        self.p = subprocess.Popen(["adb", "-s", serial, "shell", "su -c sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, pass_fds=_lock_fds())
        self.active, self.tid, self.lock, self.nsync = set(), 30000, threading.Lock(), 0

    def refresh(self):
        self.vp = viewport(self.serial)

    def panel(self, u, v):
        """横屏比例坐标 → 面板原始坐标。方向 1: 设备逆时针转 90° (自然方向的顶在左); 方向 3: 顺时针。"""
        l, t, r, b = self.vp["physical_frame"]
        x, y = l + u * (r - l), t + v * (b - t)
        wn, hn = self.vp["natural"]
        ori = self.vp["orientation"]
        xn, yn = {0: (x, y), 1: (wn - y, x), 2: (wn - x, hn - y), 3: (y, hn - x)}[ori]
        px = int(round(xn * (self.info["xmax"] + 1) / wn))
        py = int(round(yn * (self.info["ymax"] + 1) / hn))
        return min(max(px, 0), self.info["xmax"]), min(max(py, 0), self.info["ymax"])

    def _emit(self, evs):
        b = b"".join(struct.pack("<qqHHi", 0, 0, t, c, v) for t, c, v in evs + [(EV_SYN, 0, 0)])
        line = "printf '" + "".join("\\%03o" % x for x in b) + "' > " + self.info["dev"] + "\n"
        self.p.stdin.write(line.encode())
        self.p.stdin.flush()

    def down(self, slot, u, v):
        with self.lock:
            px, py = self.panel(u, v)
            self.tid += 1
            ev = [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, self.tid), (EV_ABS, ABS_MT_TOUCH_MAJOR, 6),
                  (EV_ABS, ABS_MT_POSITION_X, px), (EV_ABS, ABS_MT_POSITION_Y, py)]
            if not self.active:
                ev += [(EV_KEY, BTN_TOUCH, 1), (EV_KEY, BTN_TOOL_FINGER, 1)]
            self.active.add(slot)
            self._emit(ev)

    def move(self, slot, u, v):
        with self.lock:
            if slot not in self.active:
                return
            px, py = self.panel(u, v)
            self._emit([(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_POSITION_X, px), (EV_ABS, ABS_MT_POSITION_Y, py)])

    def up(self, slot):
        with self.lock:
            if slot not in self.active:
                return
            self.active.discard(slot)
            ev = [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]
            if not self.active:
                ev += [(EV_KEY, BTN_TOUCH, 0), (EV_KEY, BTN_TOOL_FINGER, 0)]
            self._emit(ev)

    def tap(self, slot, u, v, hold=0.08):
        self.down(slot, u, v)
        time.sleep(hold)
        self.up(slot)

    def sync(self, timeout=10):
        """等手机上的 shell 把之前的命令都执行完; 返回期间的输出 (正常为空)。"""
        with self.lock:
            self.nsync += 1
            tag = f"__sync_{self.nsync}"
            self.p.stdin.write(f"echo {tag}\n".encode())
            self.p.stdin.flush()
        out, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            line = self.p.stdout.readline()
            if not line or tag.encode() in line:
                break
            out += line
        return out.decode("utf-8", "replace")

    def close(self):
        for s in list(self.active):
            self.up(s)
        try:
            self.sync(5)
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def minimap(shot):
    """读小地图 (北朝上): 角色箭头朝向 heading (度, 0 北 90 东) 和任务标记 (黄色菱形) 的方位 / 距离 (按小地图半径归一)。
    shot = gi_enter.raw_shot()。箭头是带后缘缺口的青色三角, 离重心最远的几个点是箭尖。读不到的字段为 None。"""
    if not shot:
        return {"heading": None, "quest_bearing": None, "quest_dist": None}
    w, h, _ = shot
    W, H = max(w, h), min(w, h)
    cx, cy = MINIMAP[0] * W, MINIMAP[1] * H
    out = {"heading": None, "arrow_px": 0, "quest_bearing": None, "quest_dist": None, "quest_px": 0}
    ra = 0.03 * H
    pts = []
    for y in range(int(cy - ra), int(cy + ra)):
        for x in range(int(cx - ra), int(cx + ra)):
            r, g, b = gi_enter.pixel(shot, x / (W - 1), y / (H - 1))
            if b > 200 and g > 140 and r < 150 and b - r > 80:
                pts.append((x, y))
    out["arrow_px"] = len(pts)
    if len(pts) >= 0.1 * ra * ra:
        mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        far = sorted(pts, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)[-5:]
        tx, ty = sum(p[0] for p in far) / 5, sum(p[1] for p in far) / 5
        out["heading"] = round(math.degrees(math.atan2(tx - mx, -(ty - my))) % 360, 1)
    rq, step = 0.125 * H, max(1, int(H / 540))
    ye = []
    for y in range(int(cy - rq), int(cy + rq), step):
        for x in range(int(cx - rq), int(cx + rq), step):
            if (x - cx) ** 2 + (y - cy) ** 2 > rq * rq:
                continue
            r, g, b = gi_enter.pixel(shot, x / (W - 1), y / (H - 1))
            if r > 200 and g > 150 and b < 100:
                ye.append((x, y))
    out["quest_px"] = len(ye)
    if len(ye) * step * step >= 0.004 * rq * rq:
        qx, qy = sum(p[0] for p in ye) / len(ye) - cx, sum(p[1] for p in ye) / len(ye) - cy
        out["quest_bearing"] = round(math.degrees(math.atan2(qx, -qy)) % 360, 1)
        out["quest_dist"] = round(math.hypot(qx, qy) / rq, 3)
    return out


def ang_diff(a, b):
    """a - b, 折到 (-180, 180]。"""
    return (a - b + 180) % 360 - 180


class Runner:
    """后台线程: 按住摇杆按 angle (度, 0 正前、90 向右, 相对镜头) 推; 每 sweep_every 秒拖一次镜头 (左右交替);
    冲刺键每 sprint_period 秒按住 sprint_on 秒 (sprint_on=0 不冲刺)。angle 可随时改。"""

    def __init__(self, touch, aspect, angle=0.0, sweep_every=3.0, sweep_s=0.8, sweep_amp=0.10, sprint_on=0.0, sprint_period=6.0,
                 hz=20):
        self.t, self.aspect, self.angle = touch, aspect, angle
        self.sweep_every, self.sweep_s, self.sweep_amp = sweep_every, sweep_s, sweep_amp
        self.sprint_on, self.sprint_period, self.hz = sprint_on, sprint_period, hz
        self.stop_ev, self.th, self.sweeps = threading.Event(), None, 0
        self.log = []

    def stick_point(self, a):
        r = STICK_R
        return STICK[0] + r * math.sin(math.radians(a)) / self.aspect, STICK[1] - r * math.cos(math.radians(a))

    def _loop(self):
        t = self.t
        t.down(SLOT_STICK, *STICK)
        time.sleep(0.05)
        t0, cur, sweep_t0, sweep_dir, btn = time.time(), None, None, 1, False
        next_sweep = t0 + self.sweep_every if self.sweep_every > 0 else float("inf")
        while not self.stop_ev.is_set():
            now = time.time()
            if cur != self.angle:
                cur = self.angle
                t.move(SLOT_STICK, *self.stick_point(cur))
            if sweep_t0 is None and now >= next_sweep:
                sweep_t0 = now
                t.down(SLOT_CAM, *CAM)
            if sweep_t0 is not None:
                k = min(1.0, (now - sweep_t0) / self.sweep_s)
                t.move(SLOT_CAM, CAM[0] + sweep_dir * self.sweep_amp * k, CAM[1])
                if k >= 1.0:
                    t.up(SLOT_CAM)
                    self.log.append({"t": round(now - t0, 2), "sweep": sweep_dir})
                    sweep_t0, sweep_dir, self.sweeps = None, -sweep_dir, self.sweeps + 1
                    next_sweep = now + self.sweep_every
            if self.sprint_on > 0:
                want = ((now - t0) % self.sprint_period) < self.sprint_on
                if want and not btn:
                    t.down(SLOT_BTN, *SPRINT)
                elif btn and not want:
                    t.up(SLOT_BTN)
                btn = want
            time.sleep(1.0 / self.hz)
        for s in (SLOT_CAM, SLOT_BTN, SLOT_STICK):
            t.up(s)

    def __enter__(self):
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()
        self.th.join(timeout=10)
        self.t.sync(5)


def selftest(a):
    out = os.path.join(os.environ.get("PF_JOB_OUT") or ".", f"nav_selftest_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    rec = {"steps": []}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "nav.json"), "w"), ensure_ascii=False, indent=1)

    s = a.serial
    rec["before"] = {"wm_size": sh(s, "wm size").strip(), "pointer_location": sh(s, "settings get system pointer_location").strip()}
    if "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有 wm size 覆盖, 不动: {rec['before']}")
        return 2
    touch = None
    try:
        sh(s, "input keyevent KEYCODE_WAKEUP")
        if a.tier != "native":
            w, h = (int(x) for x in a.tier.split("x"))
            sh(s, f"wm size {h}x{w}")
        ok, secs, taps = gi_enter.enter_world(s, a.pkg, cold=True, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
        rec["enter"] = {"ok": ok, "seconds": secs, "taps": taps}
        if not ok:
            raise RuntimeError("没进世界")
        time.sleep(5)
        touch = Touch(s)
        rec["touch"], rec["viewport"] = touch.info, touch.vp
        step(f"触摸屏 {touch.info}, 视口 {touch.vp}")
        rec["panel_examples"] = {f"{u},{v}": touch.panel(u, v) for u, v in ((0, 0), (1, 0), (0, 1), (0.5, 0.5), STICK, CAM)}
        gi_enter.save_png(s, os.path.join(out, "world.png"))
        sh(s, "settings put system pointer_location 1")
        time.sleep(1)
        touch.down(SLOT_STICK, *STICK)
        for k in range(1, 11):
            touch.move(SLOT_STICK, STICK[0], STICK[1] - STICK_R * k / 10)
            time.sleep(0.03)
        touch.down(SLOT_CAM, *CAM)
        for k in range(1, 21):
            touch.move(SLOT_CAM, CAM[0] + 0.15 * k / 20, CAM[1])
            time.sleep(0.03)
        step("两指按着: " + touch.sync())
        gi_enter.save_png(s, os.path.join(out, "mt_two_fingers.png"))
        touch.up(SLOT_CAM)
        time.sleep(1.5)
        gi_enter.save_png(s, os.path.join(out, "mt_stick_only.png"))
        touch.up(SLOT_STICK)
        touch.sync()
        sh(s, f"settings put system pointer_location {rec['before']['pointer_location'] if rec['before']['pointer_location'] in ('0', '1') else 0}")
        time.sleep(1)
        rec["minimap"] = {"world": minimap(gi_enter.raw_shot(s))}
        # 只拖镜头 (不推摇杆): 北朝上的话箭头和任务方位都不该变
        touch.down(SLOT_CAM, *CAM)
        for k in range(1, 16):
            touch.move(SLOT_CAM, CAM[0] + 0.12 * k / 15, CAM[1])
            time.sleep(0.03)
        touch.up(SLOT_CAM)
        touch.sync()
        time.sleep(0.8)
        gi_enter.save_png(s, os.path.join(out, "cam_only.png"))
        rec["minimap"]["cam_only"] = minimap(gi_enter.raw_shot(s))
        # 标定: 摇杆前推跑 2 秒读朝向; 边跑边拖 +0.10 再跑 2 秒读; 拖 -0.20 再跑 2 秒读
        aspect = 16 / 9
        cal = []
        with Runner(touch, aspect, angle=0, sweep_every=0) as r:
            time.sleep(2.0)
            cal.append({"after": "run2s", **minimap(gi_enter.raw_shot(s))})
            for amp in (0.10, -0.20, 0.10):
                touch.down(SLOT_CAM, *CAM)
                for k in range(1, 16):
                    touch.move(SLOT_CAM, CAM[0] + amp * k / 15, CAM[1])
                    time.sleep(0.03)
                touch.up(SLOT_CAM)
                time.sleep(2.0)
                cal.append({"after": f"swipe{amp:+.2f}", **minimap(gi_enter.raw_shot(s))})
        rec["calibration"] = cal
        step(f"标定: {cal}")
        # 冲刺 + 边跑边转: 4 秒
        with Runner(touch, aspect, angle=0, sweep_every=1.2, sweep_s=0.6, sprint_on=2.0, sprint_period=4.0) as r:
            time.sleep(2.0)
            gi_enter.save_png(s, os.path.join(out, "run_sprint.png"))
            time.sleep(2.0)
        rec["runner_log"] = r.log
        time.sleep(1)
        gi_enter.save_png(s, os.path.join(out, "after_run.png"))
        rec["minimap"]["after_run"] = minimap(gi_enter.raw_shot(s))
        touch.tap(SLOT_BTN, *MINIMAP)
        time.sleep(3)
        gi_enter.save_png(s, os.path.join(out, "map.png"))
        sh(s, "input keyevent KEYCODE_BACK")
        time.sleep(2)
        gi_enter.save_png(s, os.path.join(out, "map_closed.png"))
        step("自检完成")
        return 0
    except Exception as e:
        step(f"出错: {type(e).__name__}: {e}")
        return 1
    finally:
        if touch:
            touch.close()
        pl = rec["before"]["pointer_location"]
        sh(s, f"settings put system pointer_location {pl if pl in ('0', '1') else 0}")
        sh(s, f"am force-stop {a.pkg}")
        sh(s, "wm size reset")
        sh(s, "input keyevent KEYCODE_HOME")
        rec["restore"] = {"wm_size": sh(s, "wm size").strip(), "pointer_location": sh(s, "settings get system pointer_location").strip(),
                          "game_running": bool(sh(s, f"pidof {a.pkg}").strip())}
        step(f"收尾: {rec['restore']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--tier", default="1920x1080")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest(a))
    ap.error("目前只有 --selftest; 采集里用 Touch / Runner / Navigator")
