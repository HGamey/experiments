#!/usr/bin/env python3
"""gi_nav.py — 原神大世界里「边跑边转镜头」的多指输入, 以及按小地图修正方向的导航。

Android 的 `input` 只有一根手指: 按住摇杆的同时拖镜头, 拖镜头那一下会把摇杆抬起来, 角色就停了。
这里直接往触摸屏的 evdev 节点 (/dev/input/eventN) 写多点触控协议 B 的事件: adb shell 的 shell 用户在 input 组,
shell 用户虽在 input 组, SELinux 不让它写输入设备, 所以经一个常驻的 root shell (adb shell su -c sh) 发出;
每条命令是一句 printf (八进制转义写出 input_event 结构)。不改游戏、不改系统设置。
坐标一律用「横屏比例坐标」(u, v) ∈ [0,1]², 按 dumpsys input 的显示视口 (方向、wm size 下的物理框) 换成面板坐标。

    python3 -I dataset/gi_nav.py <serial> --selftest [--pkg com.miHoYo.Yuanshen] [--tier 1920x1080]
    python3 -I dataset/gi_nav.py <serial> --trial 150 [--heading 160] [--gap-s 8] [--seg-s 6] [--min-luma 0] [--inland 180]
    python3 -I dataset/gi_nav.py <serial> --story 600     # 推进开场剧情 (朝任务标记跑、点对话、打怪), 截图留证
自检: wm size → 进世界 → 打开「指针位置」叠加 → 两指同时 (摇杆前推 + 横拖镜头) → 截图 → 关叠加
      → 截小地图 / 点开大地图截图 → 收尾 (还原指针位置、wm size、强停游戏、HOME)。截图和 nav.json 写到 $PF_JOB_OUT/nav_selftest_<时间>/。
"""
import argparse
import collections
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
SLOT_TAP = 6


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
        # 上一个注入进程被杀时可能留下没抬起的手指: 先把自己用的槽位都抬起来
        ev = []
        for sl in (SLOT_TAP, SLOT_STICK, SLOT_CAM, SLOT_BTN):
            ev += [(EV_ABS, ABS_MT_SLOT, sl), (EV_ABS, ABS_MT_TRACKING_ID, -1)]
        self._emit(ev + [(EV_KEY, BTN_TOUCH, 0), (EV_KEY, BTN_TOOL_FINGER, 0)])

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
            if r < 80 and g > 180 and b > 225:          # 箭头是饱和的纯青 (0,230,255); 白天背后的海和天 r 都 > 80
                pts.append((x, y))
    pts = _component(pts, (int(cx), int(cy)))
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


def _component(pts, seed):
    """pts 里离 seed 最近那一点所在的 8 连通块 (只要箭头本身, 不要旁边零星的青色)。"""
    if not pts:
        return pts
    ps = set(pts)
    start = min(pts, key=lambda p: (p[0] - seed[0]) ** 2 + (p[1] - seed[1]) ** 2)
    seen, todo = {start}, [start]
    while todo:
        x, y = todo.pop()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                q = (x + dx, y + dy)
                if q in ps and q not in seen:
                    seen.add(q)
                    todo.append(q)
    return list(seen)


# 按钮图标的 24x24 网格模板 (格子里 ≥30% 的像素接近纯白、低饱和就记 1), 原神 7.2 手机 HUD
ATT_BOX = (0.79, 0.69, 0.875, 0.845)        # 攻击键: 站在地上是剑; 攀爬时变「落下」, 游泳时没有
SPR_BOX = (0.885, 0.79, 0.96, 0.935)        # 冲刺键: 游泳时变成波浪图标
TPL_SWORD = int("0000000000000000000000200000e00001c00007c0000f80001f00003e00007c0000f80001f0001be0001fc00007c0000780000d800010c000700000600000000000000000000000", 16)
TPL_SWIM = int("00000000000000000000000000000000000000078001f7c0003fe0001f00001f00003c00100fe01fffe00ff80003e300001c00000000000000000000000000000000000000000000", 16)


def icon_bits(shot, box, n=24):
    w, h, _ = shot
    W, H = max(w, h), min(w, h)
    x0, y0, x1, y1 = box[0] * W, box[1] * H, box[2] * W, box[3] * H
    st = max(1, int(H / 540))
    bits = 0
    for j in range(n):
        for i in range(n):
            cnt = tot = 0
            for yy in range(int(y0 + (y1 - y0) * j / n), int(y0 + (y1 - y0) * (j + 1) / n), st):
                for xx in range(int(x0 + (x1 - x0) * i / n), int(x0 + (x1 - x0) * (i + 1) / n), st):
                    r, g, b = gi_enter.pixel(shot, xx / (W - 1), yy / (H - 1))
                    tot += 1
                    cnt += min(r, g, b) > 215 and max(r, g, b) - min(r, g, b) < 40
            bits = (bits << 1) | (1 if tot and cnt / tot >= 0.3 else 0)
    return bits


def _iou(a, b):
    u = bin(a | b).count("1")
    return round(bin(a & b).count("1") / u, 2) if u else 0.0


def hud(shot):
    """角色状态: normal (攻击键是剑) / swim (冲刺键是波浪) / other (攀爬、滑翔、菜单等, 剑不见了)。"""
    sw = _iou(icon_bits(shot, ATT_BOX), TPL_SWORD)
    sm = _iou(icon_bits(shot, SPR_BOX), TPL_SWIM)
    state = "normal" if sw >= 0.6 else "swim" if sm >= 0.7 else "other"
    return {"state": state, "sword": sw, "swim": sm}


def in_world(shot, mm=None):
    """在大世界里 (不是对话 / 过场 / 复苏页): 血条是绿的, 或者血少时是红的 (255,90,90), 或者小地图箭头认得出来。
    只认绿血条会把低血量当成不在世界里 (11-17 实测, 整段站着没动)。"""
    if gi_enter.in_world(shot):
        return True
    red = sum(1 for fx, fy in gi_enter.HP_POINTS if (lambda c: c[0] > 220 and c[1] < 130 and c[2] < 130)(gi_enter.pixel(shot, fx, fy)))
    if red >= 3:
        return True
    w, h, _ = shot
    mm = mm or minimap(shot)
    return mm["arrow_px"] >= 0.1 * (0.03 * min(w, h)) ** 2


def ang_diff(a, b):
    """a - b, 折到 (-180, 180]。"""
    return (a - b + 180) % 360 - 180


class Runner:
    """后台线程: 按住摇杆按 angle 推 (度, 0 正前、90 向右, 相对镜头); 拖镜头排队执行 (turn(du): 横拖 du 个画面宽, 正值向右拖,
    角色跟着镜头转); 每 sweep_every 秒自动左右交替摆一次 (幅度 sweep_amp, 0 关); 冲刺键每 sprint_period 秒按住 sprint_on 秒。
    pause() 抬起所有手指 (开大地图、点按钮前用), resume() 接着跑。"""

    def __init__(self, touch, aspect, angle=0.0, sweep_every=3.0, sweep_s=0.8, sweep_amp=0.10, sprint_on=0.0, sprint_period=6.0,
                 hz=20):
        self.t, self.aspect, self.angle = touch, aspect, angle
        self.sweep_every, self.sweep_s, self.sweep_amp = sweep_every, sweep_s, sweep_amp
        self.sprint_on, self.sprint_period, self.hz = sprint_on, sprint_period, hz
        self.stop_ev, self.paused, self.idle, self.th, self.sweeps = threading.Event(), threading.Event(), threading.Event(), None, 0
        self.queue, self.log, self.t0 = collections.deque(), [], time.time()
        self.sweeping = sweep_every > 0     # 自动摆镜头开关; 摆法 +a, -2a, +2a, ... 让朝向在均值两侧对称摆
        self.offset, self.swiping, self.last_end = 0.0, False, 0.0
        self.force_sprint = False          # 采集段内一直按住冲刺 (段短, 体力够)

    def stick_point(self, a):
        r = STICK_R
        return STICK[0] + r * math.sin(math.radians(a)) / self.aspect, STICK[1] - r * math.cos(math.radians(a))

    def turn(self, du, secs=None):
        """横拖 du 个画面宽 (单次最多 0.33); secs 给了就用这么长时间拖完 (慢速长拖 = 匀速转镜头)。"""
        while abs(du) > 1e-4:
            d = max(-0.33, min(0.33, du))
            self.queue.append((d, secs or max(0.3, min(1.0, abs(d) * 4))))
            du -= d

    def start_sweeps(self):
        self.sweeping = True

    def stop_sweeps(self):
        """停摆, 并把摆出去的偏移拖回来 (朝向回到均值)。"""
        self.sweeping = False
        if abs(self.offset) > 1e-4:
            self.turn(-self.offset)
            self.offset = 0.0

    def busy(self):
        return self.swiping or bool(self.queue)

    def pause(self):
        self.idle.clear()
        self.paused.set()
        self.idle.wait(3)

    def resume(self):
        self.paused.clear()

    def _loop(self):
        t = self.t
        held, cur, swipe, btn = False, None, None, False
        next_sweep = time.time() + max(self.sweep_every, 0.5)
        while not self.stop_ev.is_set():
            now = time.time()
            if self.paused.is_set():
                if held or swipe or btn:
                    for sl in (SLOT_CAM, SLOT_BTN, SLOT_STICK):
                        t.up(sl)
                    held, swipe, btn, cur, self.swiping = False, None, False, None, False
                self.idle.set()
                time.sleep(0.05)
                continue
            if not held:
                t.down(SLOT_STICK, *STICK)
                held, cur = True, None
                time.sleep(0.05)
            if cur != self.angle:
                cur = self.angle
                t.move(SLOT_STICK, *self.stick_point(cur))
            if swipe is None:
                if self.queue:
                    du, secs = self.queue.popleft()
                    swipe = [now, du, secs, "turn"]
                elif self.sweeping and self.sweep_amp > 0 and now >= next_sweep:
                    tgt = -self.sweep_amp if self.offset > 0 else self.sweep_amp
                    swipe = [now, tgt - self.offset, self.sweep_s, "sweep"]
                    self.offset = tgt
                if swipe:
                    self.swiping = True
                    t.down(SLOT_CAM, *CAM)
            if swipe:
                k = min(1.0, (now - swipe[0]) / swipe[2])
                t.move(SLOT_CAM, CAM[0] + swipe[1] * k, CAM[1])
                if k >= 1.0:
                    t.up(SLOT_CAM)
                    self.log.append({"t": round(now - self.t0, 2), swipe[3]: round(swipe[1], 3)})
                    if swipe[3] == "sweep":
                        self.sweeps += 1
                        next_sweep = now + self.sweep_every
                    swipe, self.swiping, self.last_end = None, False, time.time()
            if self.sprint_on > 0 or self.force_sprint:
                want = self.force_sprint or ((now - self.t0) % self.sprint_period) < self.sprint_on
                if want and not btn:
                    t.down(SLOT_BTN, *SPRINT)
                elif btn and not want:
                    t.up(SLOT_BTN)
                btn = want
            time.sleep(1.0 / self.hz)
        for sl in (SLOT_CAM, SLOT_BTN, SLOT_STICK):
            t.up(sl)

    def __enter__(self):
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()
        self.th.join(timeout=10)
        self.t.sync(5)


def map_free(shot, n=16):
    """大地图 (打开时以角色为中心) 上沿 n 个方位找红色的可活动区边界。返回 {方位度: 距离 (按画面高归一), None = 到画面边都没碰到}。"""
    w, h, _ = shot
    W, H = max(w, h), min(w, h)
    cx, cy, st = W / 2, H / 2, max(2, H // 360)
    out = {}
    for i in range(n):
        a = 360.0 * i / n
        dx, dy = math.sin(math.radians(a)), -math.cos(math.radians(a))
        r, hit = 0.03 * H, None
        while True:
            x, y = cx + dx * r, cy + dy * r
            if not (st + 1 <= x < W - st - 1 and st + 1 <= y < H - st - 1):
                break
            for ox, oy in ((0, 0), (-st, 0), (st, 0), (0, -st), (0, st)):
                cr, cg, cb = gi_enter.pixel(shot, (x + ox) / (W - 1), (y + oy) / (H - 1))
                if cr - max(cg, cb) > 25 and cg < 100 and cb > cg:   # 边界偏粉; 任务菱形描边偏橙 (b < g)
                    hit = r
                    break
            if hit is not None:
                break
            r += st
        out[a] = round(hit / H, 3) if hit is not None else None
    return out


def luma(shot):
    """画面中央 (x 0.3–0.7, y 0.2–0.75) 的平均灰度; 白天 90–150, 夜里 40–65。"""
    if not shot:
        return None
    v = [sum(gi_enter.pixel(shot, 0.3 + 0.4 * i / 23, 0.2 + 0.55 * j / 11)[k] * c for k, c in ((0, .299), (1, .587), (2, .114)))
         for i in range(24) for j in range(12)]
    return round(sum(v) / len(v), 1)


ATTACK = (0.826, 0.766)     # 攻击键; 攀爬时是「落下」
INTERACT = (0.63, 0.50)     # 交互提示 (对话 / 调查) 出现在角色右侧
DIALOG_TAP = (0.75, 0.63)   # 对话里点这里: 普通对话点哪都翻页, 有选项时正好落在选项列表上
REVIVE = (0.50, 0.89)       # 摔死 / 淹死后的「复苏」按钮 (对话里点这里也只是翻页)
RETURN_BTN = (0.33, 0.50)   # 离开任务区域时左侧出现的「返回任务点」, 右端是黄色圆形 ! (0.401, 0.499)


def return_button(shot):
    """有「返回任务点」按钮就返回 True: (0.38–0.42, 0.47–0.53) 里黄色 (r>200, g>150, b<80) 像素够多。"""
    w, h, _ = shot
    W, H = max(w, h), min(w, h)
    n = tot = 0
    st = max(1, int(H / 540))
    for y in range(int(0.47 * H), int(0.53 * H), st):
        for x in range(int(0.38 * W), int(0.42 * W), st):
            r, g, b = gi_enter.pixel(shot, x / (W - 1), y / (H - 1))
            tot += 1
            n += r > 200 and g > 150 and b < 80
    return tot > 0 and n / tot > 0.05      # 有按钮时约 0.10, 没有时接近 0


class Navigator:
    """沿一个方向跑出去: Runner 一直推摇杆、间歇冲刺、镜头小幅左右摆 (角色跟着镜头走 S 形)。
    开头 calibrate(): 原地跑着左右拖几下镜头, 用小地图箭头量「拖一个画面宽转多少度」(符号和大小每次会话现量)。
    不采集的时候 (gap) 每 ~1.2 秒截一次图:
      - 小地图箭头 = 角色朝向; 和目标方向差 > 12° 就拖镜头修正
      - 攻击键不是剑了: 冲刺键是波浪 = 在游泳, 掉头; 否则多半在攀爬, 点「落下」再掉头 (之后 4 秒不再判)
      - 任务标记在小地图里却 2 次截图都没动 = 卡住: 跳一下, 左右交替转 70°
      - 离任务标记 < 0.2 个小地图半径且正朝它跑: 转开 (碰到会触发剧情对话)
    每个 gap 开头打开一次大地图, 按红色边界挑方向: 当前方向离边界不到 1/4 屏才换, 离边界远的优先、不往回走。
    采集段内不截图 (截图会打断合成), Runner 照跑。"""

    def __init__(self, serial, touch, aspect, log=print, heading=180.0, gain=None, sweep_amp=0.05, sweep_every=1.5,
                 sprint_on=2.5, sprint_period=5.0, min_luma=80.0, max_wait_s=720, inland=None, use_return=False,
                 circle_dps=None, calm_s=8.0):
        self.s, self.touch, self.log = serial, touch, log
        self.runner = Runner(touch, aspect, 0.0, sweep_every, 0.7, sweep_amp, sprint_on, sprint_period)
        self.runner.sweeping = False        # gap 里只修方向; 采集段内才自动摆镜头 (segment_start / segment_end)
        self.target, self.gain, self.min_luma, self.max_wait_s = (heading if heading >= 0 else 0) % 360, gain, min_luma, max_wait_s
        self.pref = heading % 360 if heading >= 0 else None   # 总的偏好方向; None = 第一次看大地图时取最开阔的方向
        self.blocked = []                   # [(方位, 到期时间)]: 刚游泳 / 攀爬过的方向, 一阵子不往那边走
        self.inland = inland                # 沿海岸跑时「陆地那侧」的方位: 游泳就往这边转, 爬墙就往反方向转
        self.use_return = use_return        # 每个 gap 开头有「返回任务点」就传回去 (崖顶草地), 转身背对崖边再跑
        self.n_return, self.no_avoid_until = 0, 0.0
        self.circle_dps = circle_dps        # 段内绕圈的转速, 度/秒 (按标定增益换成拖动量); None = 每秒拖 0.055 个画面宽
        self.calm_s, self.last_bad = calm_s, 0.0   # gap 结束前要连续这么多秒没爬墙/游泳 (体力回满, 段内才冲得起来)
        self.hist, self.plans, self.flip, self.t0, self.quiet_until, self.cal = [], [], 1, time.time(), 0.0, None

    def turn_standing(self, deg):
        """站着转身: 拖镜头 deg 度 (按标定的增益), 再轻推摇杆 0.4 秒让角色转向镜头。"""
        r, t, gain = self.runner, self.touch, self.gain or 700.0
        r.pause()
        du = deg / gain
        while abs(du) > 1e-4:
            d = max(-0.33, min(0.33, du))
            t.down(SLOT_CAM, *CAM)
            for k in range(1, 13):
                t.move(SLOT_CAM, CAM[0] + d * k / 12, CAM[1])
                time.sleep(0.04)
            t.up(SLOT_CAM)
            time.sleep(0.2)
            du -= d
        t.down(SLOT_STICK, *STICK)
        t.move(SLOT_STICK, *r.stick_point(0))
        time.sleep(0.4)
        t.up(SLOT_STICK)
        time.sleep(0.5)

    def return_reset(self):
        """有「返回任务点」就点它, 传回任务点 (原神开场: 崖顶草地, 角色朝崖边约 205°), 站着转身 180° 背对崖边。"""
        shot = gi_enter.raw_shot(self.s)
        if not shot or not return_button(shot):
            return None
        self.runner.pause()
        self.tap(RETURN_BTN)
        time.sleep(6)
        st = self.read()
        for _ in range(6):                  # 传送途中摔了 (「坠落」/「复苏」页): 点复苏, 等回到世界
            if st and st["world"]:
                break
            self.tap(REVIVE)
            time.sleep(4)
            st = self.read()
        h = st["heading"] if st else None
        off = (0, 40, -40, 70, -70)[self.n_return % 5]   # 每段换个出发方向, 别每段都是同一条路
        self.n_return += 1
        self.turn_standing(180 + off)
        self.target = ((h if h is not None else 205.0) + 180 + off) % 360
        self.no_avoid_until = time.time() + 12             # 刚站在任务点上, 别让「躲任务点」把方向又掰回崖边
        self.pref = self.target
        self.runner.resume()
        return {"heading_after_return": h, "offset": off, "target": round(self.target, 1)}

    def segment_start(self, secs=None, watch=False):
        """采集段开始: 一直按住冲刺; secs 给了就绕大圈跑 —— 镜头匀速转约 0.33 个画面宽 (约 230°), 角色跟着镜头跑弧线,
        左右交替。watch=True 时段内每秒截一次图: 认出爬墙 (攻击键成了「落下」) 就点落下、转开 120°, 游泳也转开;
        这时绕圈拆成每秒一小段拖动, 好让转开的那一下插进来。不给 secs 就是左右对称摆镜头。"""
        self.runner.force_sprint = True
        self.seg_log = []
        if not secs:
            self.runner.start_sweeps()
            return
        self.circle_dir = -getattr(self, "circle_dir", -1)
        gain = self.gain or 700.0
        per_s = self.circle_dps / gain if self.circle_dps else 0.055
        if not watch:
            self.runner.turn(per_s * secs * self.circle_dir, secs)
            return
        self.seg_stop = threading.Event()

        def loop():
            t_end, gain = time.time() + secs, self.gain or 700.0
            while not self.seg_stop.is_set() and time.time() < t_end:
                t1 = time.time()
                self.runner.turn(per_s * self.circle_dir, 0.9)
                shot = gi_enter.raw_shot(self.s)
                st = hud(shot)["state"] if shot else None
                if st and st != "normal":
                    if st == "other":
                        self.tap(ATTACK)
                    self.runner.turn(120 * self.circle_dir / gain, 0.5)
                    self.seg_log.append({"t": round(t1 - self.t0, 1), "state": st})
                self.seg_stop.wait(max(0.0, 1.0 - (time.time() - t1)))
        self.seg_th = threading.Thread(target=loop, daemon=True)
        self.seg_th.start()

    def segment_end(self):
        if getattr(self, "seg_th", None):
            self.seg_stop.set()
            self.seg_th.join(timeout=5)
            self.seg_th = None
        self.runner.force_sprint = False
        self.runner.stop_sweeps()

    def __enter__(self):
        self.runner.__enter__()
        return self

    def __exit__(self, *exc):
        self.runner.__exit__(*exc)

    def _rec(self, **kw):
        r = {"t": round(time.time() - self.t0, 1), **kw}
        self.hist.append(r)
        return r

    def tap(self, pt, hold=0.08):
        self.touch.tap(SLOT_TAP, *pt, hold)

    def read(self):
        shot = gi_enter.raw_shot(self.s)
        if not shot:
            return None
        mm = minimap(shot)
        return {**mm, **hud(shot), "world": in_world(shot, mm)}

    def calibrate(self, du=0.10):
        """站着标定: 拖镜头 +du / -2du / +du (净转 0), 每次之后把摇杆轻推 0.5 秒让角色转向镜头正前方, 再读箭头。
        增益 = 朝向变化 / 拖动量, 取多数符号、绝对值中位数。每步只走一两米, 不会冲进任务点。"""
        r, t = self.runner, self.touch
        r.pause()
        time.sleep(0.8)
        prev, g = self.read(), []
        for d in (du, -2 * du, du):
            t.down(SLOT_CAM, *CAM)
            for k in range(1, 13):
                t.move(SLOT_CAM, CAM[0] + d * k / 12, CAM[1])
                time.sleep(0.04)
            t.up(SLOT_CAM)
            time.sleep(0.3)
            t.down(SLOT_STICK, *STICK)
            t.move(SLOT_STICK, *r.stick_point(0))
            time.sleep(0.5)
            t.up(SLOT_STICK)
            time.sleep(0.8)
            cur = self.read()
            if prev and cur and prev["heading"] is not None and cur["heading"] is not None \
                    and prev["state"] == cur["state"] == "normal":
                g.append(ang_diff(cur["heading"], prev["heading"]) / d)
            prev = cur
        r.resume()
        # 符号固定为正 (向右拖朝向变大, 多次会话一致); 爬坡、撞树时样本会乱, 只取正样本的中位数, 夹在 450–1100
        pos = sorted(x for x in g if x > 0)
        self.gain = min(1100.0, max(450.0, pos[len(pos) // 2])) if pos else 700.0
        self.cal = {"samples": [round(x) for x in g], "gain": self.gain}
        self.log(f"镜头增益标定: {self.cal}")
        return self.cal

    def check(self):
        st = self.read()
        if not st:
            return self._rec(err="截图失败")
        rec = self._rec(heading=st["heading"], quest=[st["quest_bearing"], st["quest_dist"]], state=st["state"],
                        sword=st["sword"], target=round(self.target, 1), world=st["world"])
        now = time.time()
        if not st["world"]:
            # 摔死 / 淹死的「复苏」页, 或者剧情对话: 松手, 交替点复苏键和对话区
            self.runner.pause()
            self.offworld = getattr(self, "offworld", 0) + 1
            self.tap(REVIVE if self.offworld % 2 else DIALOG_TAP)
            rec["action"] = "offworld"
            return rec
        self.runner.resume()
        prev = [h for h in self.hist[-3:-1] if h.get("quest") and h["quest"][1] is not None]
        gain = self.gain or 580.0
        if st["state"] != "normal" and now >= self.quiet_until:
            rec["action"] = "recover_" + st["state"]
            self.recover(st["state"], st["heading"])
        elif (st["quest_dist"] is not None and st["quest_dist"] < 0.95 and len(prev) == 2 and st["state"] == "normal"
              and all(abs(p["quest"][1] - st["quest_dist"]) < 0.012 and abs(ang_diff(p["quest"][0], st["quest_bearing"])) < 2 for p in prev)):
            rec["action"] = "unstick"
            self.tap(JUMP)
            self.target = (self.target + 70 * self.flip) % 360
            self.runner.turn(70 * self.flip / gain)
            self.flip = -self.flip
        elif st["heading"] is not None:
            if (st["quest_dist"] is not None and st["quest_dist"] < 0.2 and now >= self.no_avoid_until
                    and abs(ang_diff(st["quest_bearing"], self.target)) < 70):
                self.target = (st["quest_bearing"] + 180 + 30 * self.flip) % 360
                rec["action"] = "avoid_quest"
            e = ang_diff(self.target, st["heading"])
            rec["err"] = round(e, 1)
            # 上一次拖动做完、角色转稳 (0.9 秒) 之后才再修, 每次修 70%, 免得来回过冲
            if abs(e) > 15 and not self.runner.busy() and now - self.runner.last_end > 0.9:
                du = 0.7 * e / gain
                rec["action"] = (rec.get("action", "") + f" steer {du:+.3f}").strip()
                self.runner.turn(du)
        return rec

    def recover(self, state, heading):
        """游泳 / 攀爬: (攀爬先停下点「落下」) 往左或右转 100° (交替), 这个方向 40 秒内不再走; 之后 4 秒不再判状态。
        不掉头 180°: 从出生海滩往里走时, 掉头就是回海滩。"""
        gain = self.gain or 580.0
        h = heading if heading is not None else self.target
        self.last_bad = time.time()
        self.blocked.append((h, time.time() + 40))
        if state == "other":
            self.runner.pause()
            self.tap(ATTACK)
            time.sleep(1.2)
            self.runner.resume()
        if self.inland is not None:
            side = 1 if ang_diff(self.inland, h) > 0 else -1
            turn = side * 100 if state == "swim" else -side * 70
        else:
            turn = 100 * self.flip
            self.flip = -self.flip
        self.target = (h + turn) % 360
        self.runner.turn(turn / gain)
        self.quiet_until = time.time() + 4.0

    def replan(self):
        """开大地图看红色边界: 每个方位打分 = 离边界距离 (封顶 0.6 屏高) - 偏离偏好方向的角度/600, 刚游泳/攀爬过的方向不要。
        偏好方向离边界不到 0.12 屏高时, 偏好换成最开阔、又不朝来路的方向。"""
        self.runner.pause()
        self.tap(MINIMAP)
        time.sleep(2.5)
        shot = gi_enter.raw_shot(self.s)
        sh(self.s, "input keyevent KEYCODE_BACK")
        time.sleep(1.5)
        self.runner.resume()
        if not shot:
            return None
        free = map_free(shot)
        now = time.time()
        self.blocked = [(b_, u) for b_, u in self.blocked if u > now]
        ok = {a_: (0.6 if d is None else min(d, 0.6)) for a_, d in free.items()
              if all(abs(ang_diff(a_, b_)) > 45 for b_, _ in self.blocked)} or {a_: (0.6 if d is None else min(d, 0.6)) for a_, d in free.items()}
        if self.pref is None:
            self.pref = max(ok, key=lambda a_: ok[a_])
        near_pref = min(ok, key=lambda a_: abs(ang_diff(a_, self.pref)))
        old_pref = self.pref
        if ok[near_pref] < 0.12:
            # 偏好方向走到头了: 换最开阔、又不朝来路的方向; 都不行 (比如沙滩带的尽头) 才掉头
            cand = {a_: d for a_, d in ok.items() if abs(ang_diff(a_, self.pref + 180)) > 45 and d >= 0.15} or ok
            self.pref = max(cand, key=lambda a_: cand[a_])
        best = max(ok, key=lambda a_: ok[a_] - abs(ang_diff(a_, self.pref)) / 600.0)
        plan = {"t": round(now - self.t0, 1), "free": free, "old": round(self.target, 1), "new": best, "pref": [old_pref, self.pref],
                "blocked": [round(b_) for b_, _ in self.blocked]}
        self.plans.append(plan)
        self.target = best
        return plan

    def wait_daylight(self):
        t0 = time.time()
        while True:
            lu = luma(gi_enter.raw_shot(self.s))
            if lu is None or lu >= self.min_luma or time.time() - t0 > self.max_wait_s:
                return {"luma": lu, "waited_s": round(time.time() - t0)}
            if not self.runner.paused.is_set():
                self.runner.pause()
                self.log(f"画面太暗 ({lu} < {self.min_luma}), 停下等天亮")
            time.sleep(30)

    def gap(self, secs, replan=True):
        """不采集的一段: 等天亮 → (开大地图挑方向) → 跑 secs 秒并每 1.2 秒修正一次; 结束时要状态正常、方向差 < 25°, 否则最多再跑 8 秒。"""
        out = {"day": self.wait_daylight() if self.min_luma else None}
        self.runner.resume()
        if self.cal is None:
            out["calibration"] = self.calibrate()
        if self.use_return:
            out["return"] = self.return_reset()
            replan = replan and not out["return"]   # 刚传回任务点就不用看大地图挑方向了
        if replan:
            out["plan"] = self.replan()
        t_end, last = time.time() + secs, None
        while True:
            now = time.time()
            unsettled = last and (last.get("action") or abs(last.get("err") or 0) > 25) and now < t_end + 8
            uncalm = now - self.last_bad < self.calm_s and now < t_end + 25
            if not (now < t_end or unsettled or uncalm):
                break
            t1 = time.time()
            last = self.check()
            time.sleep(max(0.0, 1.2 - (time.time() - t1)))
        out["calm_s"] = round(time.time() - self.last_bad, 1) if self.last_bad else None
        # 开段前确认真的在动: 隔 0.4 秒两张图的画面中央差太小 (站住 / 卡住 / 被界面挡住) 就再跑, 最多 10 秒
        for _ in range(4):
            d = self.moving()
            out.setdefault("moving", []).append(d)
            if d is None or d >= 6:
                break
            self.runner.resume()
            self.runner.turn(60 * self.flip / (self.gain or 700.0))
            self.flip = -self.flip
            t2 = time.time() + 2.5
            while time.time() < t2:
                last = self.check()
                time.sleep(1.0)
        # 再确认开段时是正常状态 (攻击键是剑): 画面在动也可能是在爬墙, 爬墙就落下、转开、再跑几秒
        for _ in range(3):
            st = self.read()
            out.setdefault("pre_state", []).append(st["state"] if st else None)
            if not st or st["state"] == "normal":
                break
            self.recover(st["state"], st["heading"])
            t2 = time.time() + max(3.0, self.calm_s)
            while time.time() < t2:
                last = self.check()
                time.sleep(1.0)
        out["last"] = last
        return out

    def moving(self):
        """隔 0.4 秒两张截图, 画面中央 24x12 个点的平均灰度差 (跑起来 10 以上, 站着 2 以下)。"""
        a = gi_enter.raw_shot(self.s)
        time.sleep(0.4)
        b = gi_enter.raw_shot(self.s)
        if not a or not b:
            return None
        g = lambda sh, fx, fy: sum(c * v for c, v in zip((.299, .587, .114), gi_enter.pixel(sh, fx, fy)))
        pts = [(0.3 + 0.4 * i / 23, 0.2 + 0.55 * j / 11) for i in range(24) for j in range(12)]
        return round(sum(abs(g(a, x, y) - g(b, x, y)) for x, y in pts) / len(pts), 1)


def story(a):
    """推进开场剧情: 在世界里就朝小地图任务标记跑 (到了先点交互, 再连点攻击打怪), 不在世界里 (对话 / 过场) 就松手点对话区。
    每 3–6 秒存一张截图到 $PF_JOB_OUT/nav_story_<时间>/, story.json 记每一步。跑 --story 秒为止。"""
    out = os.path.join(os.environ.get("PF_JOB_OUT") or ".", f"nav_story_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    rec = {"steps": [], "log": [], "args": vars(a)}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "story.json"), "w"), ensure_ascii=False, indent=1)

    s = a.serial
    rec["before"] = {"wm_size": sh(s, "wm size").strip()}
    if "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有 wm size 覆盖, 不动: {rec['before']}")
        return 2
    touch = None
    try:
        sh(s, "input keyevent KEYCODE_WAKEUP")
        w, h = 16, 9
        if a.tier != "native":
            w, h = (int(x) for x in a.tier.split("x"))
            sh(s, f"wm size {h}x{w}")
        ok, secs, taps = gi_enter.enter_world(s, a.pkg, cold=True, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
        rec["enter"] = {"ok": ok, "seconds": secs}
        if not ok:
            raise RuntimeError("没进世界")
        time.sleep(3)
        touch = Touch(s)
        nav = Navigator(s, touch, w / h, log=step, heading=0, min_luma=0, sprint_on=a.sprint_on)
        with nav:
            nav.runner.pause()
            t_end, last_png, near_since, odd_since, k, last_jump = time.time() + a.story, 0.0, None, None, 0, 0.0
            while time.time() < t_end:
                t1 = time.time()
                shot = gi_enter.raw_shot(s)
                world = bool(shot) and gi_enter.in_world(shot)
                e = {"t": round(t1 - nav.t0, 1), "world": world}
                if world:
                    if nav.cal is None:
                        nav.runner.resume()
                        nav.calibrate()
                    nav.runner.resume()
                    mm, hd = minimap(shot), hud(shot)
                    e.update(heading=mm["heading"], quest=[mm["quest_bearing"], mm["quest_dist"]], state=hd["state"])
                    gain = nav.gain or 700.0
                    if return_button(shot):
                        nav.runner.pause()
                        nav.tap(RETURN_BTN)
                        e["act"] = "return_to_quest"
                        near_since = None
                        rec["log"].append(e)
                        time.sleep(4)
                        gi_enter.save_png(s, os.path.join(out, f"s{k:03d}_r.png"))
                        last_png, k = time.time(), k + 1
                        continue
                    if hd["state"] != "normal" and hd["state"] != "other_still":
                        odd_since = odd_since or t1
                        if t1 - odd_since > (15 if hd["state"] == "swim" else 8) and t1 >= nav.quiet_until:
                            nav.recover(hd["state"], mm["heading"])
                            e["act"] = "recover_" + hd["state"]
                            odd_since = None
                    else:
                        odd_since = None
                    if "act" not in e and mm["quest_bearing"] is not None and (mm["quest_dist"] or 0) > 0.06:
                        near_since = None
                        nav.runner.resume()
                        nav.target = mm["quest_bearing"]
                        if t1 - last_jump > 1.5:     # 跑向任务点时隔一会儿点跳跃: 地上就是跳一下, 空中 (跳崖) 会展开风之翼
                            nav.tap(JUMP)
                            last_jump = t1
                        if mm["heading"] is not None:
                            err = ang_diff(nav.target, mm["heading"])
                            if abs(err) > 15 and not nav.runner.busy() and t1 - nav.runner.last_end > 0.9:
                                nav.runner.turn(0.7 * err / gain)
                                e["act"] = f"steer {err:+.0f}"
                    elif "act" not in e:
                        # 到了任务点 (标记不见了或就在脚下): 站着别动 (任务点常在崖边), 先点交互 / 对话; 20 秒没进展再试攻击
                        nav.runner.pause()
                        near_since = near_since or t1
                        dt = t1 - near_since
                        if int(dt) % 2 == 0:
                            nav.tap(INTERACT)
                            e["act"] = "interact"
                        if dt > 20:
                            for _ in range(4):
                                nav.tap(ATTACK)
                                time.sleep(0.35)
                            e["act"] = "attack"
                        if dt > 40:
                            near_since = None
                    if t1 - last_png > 6:
                        gi_enter.save_png(s, os.path.join(out, f"s{k:03d}_w.png"))
                        last_png, k = time.time(), k + 1
                else:
                    nav.runner.pause()
                    near_since = None
                    if t1 - last_png > 3:
                        gi_enter.save_png(s, os.path.join(out, f"s{k:03d}_d.png"))
                        last_png, k = time.time(), k + 1
                    tap_pt = DIALOG_TAP if len(rec["log"]) % 2 == 0 else REVIVE
                    nav.tap(tap_pt)
                    e["act"] = "dialog_tap" if tap_pt is DIALOG_TAP else "revive_tap"
                rec["log"].append(e)
                if len(rec["log"]) % 10 == 0:
                    json.dump(rec, open(os.path.join(out, "story.json"), "w"), ensure_ascii=False, indent=1)
                time.sleep(max(0.0, 1.2 - (time.time() - t1)))
            rec["cal"] = nav.cal
        gi_enter.save_png(s, os.path.join(out, "end.png"))
        step("story 结束")
        return 0
    except Exception as e:
        step(f"出错: {type(e).__name__}: {e}")
        return 1
    finally:
        if touch:
            touch.close()
        sh(s, f"am force-stop {a.pkg}")
        sh(s, "wm size reset")
        sh(s, "input keyevent KEYCODE_HOME")
        rec["restore"] = {"wm_size": sh(s, "wm size").strip(), "game_running": bool(sh(s, f"pidof {a.pkg}").strip())}
        step(f"收尾: {rec['restore']}")


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


def trial(a):
    """只跑不采: 进世界后循环「gap (修正方向) → seg_s 秒安静 (模拟采集段)」直到 --trial 秒, 每轮截一张图, 记录导航过程。"""
    out = os.path.join(os.environ.get("PF_JOB_OUT") or ".", f"nav_trial_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    rec = {"steps": [], "args": vars(a)}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "nav.json"), "w"), ensure_ascii=False, indent=1)

    s = a.serial
    rec["before"] = {"wm_size": sh(s, "wm size").strip()}
    if "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有 wm size 覆盖, 不动: {rec['before']}")
        return 2
    touch = None
    try:
        sh(s, "input keyevent KEYCODE_WAKEUP")
        w, h = 16, 9
        if a.tier != "native":
            w, h = (int(x) for x in a.tier.split("x"))
            sh(s, f"wm size {h}x{w}")
        ok, secs, taps = gi_enter.enter_world(s, a.pkg, cold=True, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
        rec["enter"] = {"ok": ok, "seconds": secs}
        if not ok:
            raise RuntimeError("没进世界")
        time.sleep(5)
        touch = Touch(s)
        gi_enter.save_png(s, os.path.join(out, "start.png"))
        with Navigator(s, touch, w / h, log=step, heading=a.heading, min_luma=a.min_luma, inland=a.inland) as nav:
            t_end, k = time.time() + a.trial, 0
            while time.time() < t_end:
                g = nav.gap(a.gap_s)
                step(f"gap {k}: plan={g.get('plan') and g['plan'].get('new')} target={nav.target:.0f} last={g['last']}")
                gi_enter.save_png(s, os.path.join(out, f"seg{k:02d}.png"))
                nav.segment_start()
                time.sleep(a.seg_s)
                nav.segment_end()
                k += 1
            rec["hist"], rec["plans"], rec["runner_log"] = nav.hist, nav.plans, nav.runner.log
        time.sleep(1)
        gi_enter.save_png(s, os.path.join(out, "end.png"))
        step("trial 完成")
        return 0
    except Exception as e:
        step(f"出错: {type(e).__name__}: {e}")
        return 1
    finally:
        if touch:
            touch.close()
        sh(s, f"am force-stop {a.pkg}")
        sh(s, "wm size reset")
        sh(s, "input keyevent KEYCODE_HOME")
        rec["restore"] = {"wm_size": sh(s, "wm size").strip(), "game_running": bool(sh(s, f"pidof {a.pkg}").strip())}
        step(f"收尾: {rec['restore']}")


def probe_return(a):
    """看「返回任务点」传回去的地方: 传回后站着读 6 次朝向 (看读数抖不抖), 每次转 45° 存一张图 (8 张),
    开大地图存一张, 再背对原朝向直跑 6 秒不修方向、每 0.6 秒读一次朝向 (看是被地形带偏还是读错)。"""
    out = os.path.join(os.environ.get("PF_JOB_OUT") or ".", f"nav_return_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    rec = {"steps": [], "args": vars(a)}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "nav.json"), "w"), ensure_ascii=False, indent=1)

    s = a.serial
    rec["before"] = {"wm_size": sh(s, "wm size").strip()}
    if "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有 wm size 覆盖, 不动: {rec['before']}")
        return 2
    touch = None
    try:
        sh(s, "input keyevent KEYCODE_WAKEUP")
        w, h = 16, 9
        if a.tier != "native":
            w, h = (int(x) for x in a.tier.split("x"))
            sh(s, f"wm size {h}x{w}")
        ok, secs, taps = gi_enter.enter_world(s, a.pkg, cold=True, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
        rec["enter"] = {"ok": ok, "seconds": secs}
        if not ok:
            raise RuntimeError("没进世界")
        time.sleep(5)
        touch = Touch(s)
        with Navigator(s, touch, w / h, log=step, heading=a.heading, min_luma=0) as nav:
            nav.runner.pause()
            rec["calibration"] = nav.calibrate()
            step(f"标定 {rec['calibration']}")
            gi_enter.save_png(s, os.path.join(out, "before_return.png"))
            shot = gi_enter.raw_shot(s)
            rec["return_button"] = bool(shot and return_button(shot))
            nav.tap(RETURN_BTN)
            time.sleep(7)
            gi_enter.save_png(s, os.path.join(out, "ret.png"))
            rec["standing"] = [nav.read() for _ in range(6)]
            step(f"站着读朝向: {[r and r['heading'] for r in rec['standing']]}")
            h0 = next((r["heading"] for r in rec["standing"] if r and r["heading"] is not None), None)
            rec["turns"] = []
            for k in range(8):
                nav.turn_standing(45)
                time.sleep(0.8)
                gi_enter.save_png(s, os.path.join(out, f"turn{k}.png"))
                r = nav.read()
                rec["turns"].append(r)
                step(f"转 {45 * (k + 1)}°: 朝向 {r and r['heading']} 状态 {r and r['state']}")
            nav.tap(MINIMAP)
            time.sleep(2.5)
            gi_enter.save_png(s, os.path.join(out, "map.png"))
            sh(s, "input keyevent KEYCODE_BACK")
            time.sleep(1.5)
            nav.tap(RETURN_BTN)
            time.sleep(7)
            nav.turn_standing(180)
            nav.runner.force_sprint = True
            nav.runner.resume()
            t0, run = time.time(), []
            while time.time() - t0 < 6:
                r = nav.read()
                run.append({"t": round(time.time() - t0, 2), "heading": r and r["heading"], "state": r and r["state"],
                            "quest": r and [r["quest_bearing"], r["quest_dist"]]})
                if len(run) % 3 == 0:
                    gi_enter.save_png(s, os.path.join(out, f"run{len(run):02d}.png"))
            nav.runner.pause()
            nav.runner.force_sprint = False
            rec["run"], rec["h0"] = run, h0
            step(f"直跑: {[(x['t'], x['heading'], x['state']) for x in run]}")
        step("probe 完成")
        return 0
    except Exception as e:
        step(f"出错: {type(e).__name__}: {e}")
        return 1
    finally:
        if touch:
            touch.close()
        sh(s, f"am force-stop {a.pkg}")
        sh(s, "wm size reset")
        sh(s, "input keyevent KEYCODE_HOME")
        rec["restore"] = {"wm_size": sh(s, "wm size").strip(), "game_running": bool(sh(s, f"pidof {a.pkg}").strip())}
        step(f"收尾: {rec['restore']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--tier", default="1920x1080")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--trial", type=float, default=0, help="只跑不采这么多秒, 检验导航")
    ap.add_argument("--heading", type=float, default=160, help="起始目标方向, 度 (0 北 90 东)")
    ap.add_argument("--gap-s", type=float, default=8)
    ap.add_argument("--seg-s", type=float, default=6)
    ap.add_argument("--min-luma", type=float, default=0, help="画面中央平均灰度低于它就停下等天亮 (0 不等)")
    ap.add_argument("--inland", type=float, help="沿海岸跑: 陆地那侧的方位 (游泳往这边转, 爬墙往反方向转)")
    ap.add_argument("--story", type=float, default=0, help="推进开场剧情这么多秒 (朝任务标记跑、点对话、打怪)")
    ap.add_argument("--probe-return", action="store_true", help="看「返回任务点」传回去的地方: 转一圈截图、读朝向、直跑 6 秒")
    ap.add_argument("--sprint-on", type=float, default=0.0, help="story 时冲刺键每 5 秒按住几秒 (0 不冲刺, 省体力)")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest(a))
    if a.trial > 0:
        sys.exit(trial(a))
    if a.story > 0:
        sys.exit(story(a))
    if a.probe_return:
        sys.exit(probe_return(a))
    ap.error("给 --selftest 或 --trial; 采集里用 Touch / Runner / Navigator")
