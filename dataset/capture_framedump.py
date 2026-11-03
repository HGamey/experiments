#!/usr/bin/env python3
"""capture_framedump.py — 方法 D 采集 (DATASET-COLLECTION §3.5、§3.7): 一次进游戏, 按运动类型逐段 framedump, 段与段之间走动换地方, 收完再读回转 PNG。

    python3 -I dataset/capture_framedump.py <serial> [--clips 16] [--frames 420] [--walk-s 60] [--stride 30] [--seed 0]
平台上: pf-node --experiment <提交> --research <研究ID> --device-access write --resource device:<serial> --detach \\
          experiment dataset/capture_framedump.py <serial> --clips 16

每段一种运动, 按 static → turn_slow → turn_fast → run 轮换, 写进 clips.json 的 motion (来自输入记录):
  static     不走不转; 每 2 秒点一下空白处, 否则游戏约 30 帧, 一段会超过 10 秒
  turn_slow  按住慢慢拖视角
  turn_fast  来回快速划视角
  run        摇杆一直推到底; 开段前先确认画面在动, 否则是撞墙或爬墙
段间走动和 cruise.py 一样: 先大转向, 再边小转边走, 碰到溺水/倒下复苏页就点复苏。推着摇杆撞到岩壁会爬上去 (gi-sim v0 约四成帧如此), 所以每走一步截图看一次:
HUD 上没有冲刺键就点松手键掉下来, 再转个大方向走开。
每段采完趁游戏还开着抽 5 帧核对: 在地上、run 的画面确实在动、static 的画面确实不动; 不过就删掉, 走一段再采, 同一段最多重来 --retries 次。结果写进 clips.json 的 motion_check。
读回: 手机上的原始帧经 tar 流到节点, 边流边核对 SHA-256、丢掉 alpha、存 PNG, 原始帧不落节点的盘 (一段 480 帧 4 GB, 节点到不了那么多);
读完这段就删手机上的目录。输出 ($PF_JOB_OUT/fd_capture_<时间>/):
  batch.json restore.json report.json clips.json     clips.json 记每段每帧的帧号、时刻、gap、PNG 和原始帧的哈希
  clips/<段标签>/{session.json,index.csv,summary.json,f<帧号>.png}
  key/                                               关键帧的硬链接和 clips.json、batch.json, 自成一个 make_pairs 输入, 用来拉回
  key/consecutive/<段标签>/                          段中间的连续 6 帧, 用来看运动
收尾 (含出错): 停会话摘层、清 debug.knobs.*、还原 Game Mode 和音量、强停游戏、删手机输出目录, 逐项回读写进 restore.json。
退出码: 0 全部完成且收尾通过, 1 有段没完成, 2 收尾没通过。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):   # 整个脚本在同一把设备锁下; session 和 onboard 继承这把锁
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    label = os.environ.get("DEVLOCK_LABEL", "")
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"framedump@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]   # -I 不带脚本目录
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
import framedump_probe as fp  # noqa: E402
from cruise import CAM_X, CAM_Y, REVIVE_TAP, STICK, lock_fds, revive_dialog  # noqa: E402

ab = fp.ab
MOTIONS = ("static", "turn_slow", "turn_fast", "run")
W, H = 2688, 1216                # 屏幕横屏尺寸, 输入坐标用
LIMIT_BYTES = 18e9               # 节点上这次最多留这么多 PNG (任务上限 20 GB, 含试采)
RGB_ORDER = {37: (0, 1, 2), 44: (2, 1, 0)}   # vk_format → R, G, B 在每像素 4 字节里的位置
DASH_ICON = (0.845, 0.815, 0.895, 0.905)     # 冲刺键, 爬墙时消失
DROP_TAP = (0.80, 0.77)                      # 爬墙时是松手键, 平时是普攻, 点了无害
SCENE = (0.25, 0.15, 0.75, 0.80)             # 比运动只看这块, 避开 HUD
RUN_MIN_DIFF = 1.5                           # 跑着时相隔 1 秒以上的两帧, 中央区灰度差的中位数不小于这个; 试采卡住时是 0, 原地挨打放特效时是 1
STATIC_MAX_DIFF = 1.0                        # 静止段任意两帧的中位差不大于这个; 试采是 0


def adb_input(serial, cmd):
    subprocess.run(["adb", "-s", serial, "shell", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, pass_fds=lock_fds())


def hold_stick(serial, dx=0.0):
    """把摇杆一下推到底并按住, 直到 release_stick (input swipe 是匀速滑过去的, 前几秒只算走路)。"""
    x0, y0, x1, y1 = int(STICK[0] * W), int(STICK[1] * H), int((STICK[0] + dx) * W), int((STICK[1] - 0.13) * H)
    adb_input(serial, f"input motionevent DOWN {x0} {y0}; input motionevent MOVE {x1} {y1}")
    return x1, y1


def release_stick(serial, at):
    adb_input(serial, f"input motionevent UP {at[0]} {at[1]}")


def look(serial, out):
    """截一张屏, 返回 (路径, RGB 图)。"""
    shot = os.path.join(out, "_check.png")
    with open(shot, "wb") as f:
        subprocess.run(["adb", "-s", serial, "exec-out", "screencap -p"], stdout=f, timeout=60, pass_fds=lock_fds())
    return shot, Image.open(shot).convert("RGB")


def on_foot(im):
    """冲刺键在 = 不在爬墙。试采在地上时冲刺键区近白像素占 25%, 爬墙时 0。"""
    w, h = im.size
    a = np.asarray(im.crop((int(DASH_ICON[0] * w), int(DASH_ICON[1] * h), int(DASH_ICON[2] * w), int(DASH_ICON[3] * h))))
    return bool((a.min(axis=2) > 200).mean() > 0.04)


def scene_diff(a, b):
    """两张图中央区灰度差的中位数, 尺寸不同也能比。用中位数: 人没走时角色身上的特效把平均差抬到 13, 背景不动中位数还是 1, 而转视角最少也有 4。"""
    def g(im):
        w, h = im.size
        return np.asarray(im.crop((int(SCENE[0] * w), int(SCENE[1] * h), int(SCENE[2] * w), int(SCENE[3] * h))).convert("L").resize((240, 130)), float)
    return round(float(np.median(np.abs(g(a) - g(b)))), 2)


def swipe_cmd(x0, y0, x1, y1, ms):
    return f"input swipe {int(x0 * W)} {int(y0 * H)} {int(x1 * W)} {int(y1 * H)} {ms}"


def act(serial, motion, stop, rng):
    """static、turn_slow、turn_fast 段内的输入, 直到 stop。"""
    cy = sum(CAM_Y) / 2
    side = rng.choice((-1, 1))
    while not stop.is_set():
        if motion == "static":
            adb_input(serial, f"input tap {int(0.5 * W)} {int(0.2 * H)}")
            stop.wait(2)
        elif motion == "turn_slow":
            a, b = (CAM_X[0], CAM_X[1]) if side > 0 else (CAM_X[1], CAM_X[0])
            adb_input(serial, swipe_cmd(a, cy, b, cy, 9000))
            side = -side
        else:
            a, b = (CAM_X[0], CAM_X[1]) if side > 0 else (CAM_X[1], CAM_X[0])
            adb_input(serial, swipe_cmd(a, cy, b, cy, 300))
            side = -side


def settle(serial, out, rec):
    """处理复苏页和爬墙。返回 (现在能动, 这回爬墙了, 截图)。"""
    climbed = False
    for _ in range(20):
        shot, im = look(serial, out)
        if revive_dialog(shot):
            rec["revives"] = rec.get("revives", 0) + 1
            adb_input(serial, f"input tap {int(REVIVE_TAP[0] * W)} {int(REVIVE_TAP[1] * H)}")
            time.sleep(2)
        elif not on_foot(im):
            climbed = True
            rec["climbs"] = rec.get("climbs", 0) + 1
            adb_input(serial, f"input tap {int(DROP_TAP[0] * W)} {int(DROP_TAP[1] * H)}")
            time.sleep(1.5)
        else:
            return True, climbed, im
    return False, climbed, im


def big_turn(serial, rng):
    """转开大约半圈, 离开挡住的方向。"""
    cy = rng.uniform(*CAM_Y)
    a, b = (0.15, 0.85) if rng.random() < 0.5 else (0.85, 0.15)
    adb_input(serial, swipe_cmd(a, cy, b, cy, 450))


def walk(serial, seconds, rng, out, rec):
    """段间换地方: 先大转向, 再边小转边走。爬墙、卡住就转开。"""
    t0, prev = time.time(), None
    big_turn(serial, rng)
    while time.time() - t0 < seconds:
        at = hold_stick(serial, rng.uniform(-0.03, 0.03))
        time.sleep(3.5)
        release_stick(serial, at)
        ok, climbed, im = settle(serial, out, rec)
        if not ok:
            return False
        if climbed or (prev is not None and scene_diff(prev, im) < RUN_MIN_DIFF):
            rec["stuck"] = rec.get("stuck", 0) + 1
            big_turn(serial, rng)
        else:
            cy, cx = rng.uniform(*CAM_Y), rng.uniform(*CAM_X)
            adb_input(serial, swipe_cmd(cx, cy, max(0.05, min(0.95, cx + rng.uniform(-0.10, 0.10))), cy, 450))
        prev = im
    return True


def start_run(serial, rng, out, rec):
    """按住摇杆直到确认在地上跑起来; 返回按住的坐标, 不让开段就返回 None 并已松手。"""
    for _ in range(6):
        at = hold_stick(serial)
        time.sleep(1.0)
        a = look(serial, out)[1]
        time.sleep(0.3)
        b = look(serial, out)[1]
        if on_foot(b) and scene_diff(a, b) >= RUN_MIN_DIFF:
            return at
        release_stick(serial, at)
        if not settle(serial, out, rec)[0]:
            return None
        rec["stuck"] = rec.get("stuck", 0) + 1
        big_turn(serial, rng)
    return None


def raw_rgb(data, session):
    w, h = session["w"], session["h"]
    return np.frombuffer(data, np.uint8).reshape(h, w, 4)[..., RGB_ORDER[session["vk_format"]]]


def check_clip(dev, serial, seg, motion):
    """趁游戏还开着, 从手机抽 5 帧核对这段名副其实。返回 (通过, 依据)。"""
    session = json.loads(dev.su(f"cat {seg}/session.json"))
    names = sorted(x for x in dev.su(f"ls {seg}").split() if x.endswith(".raw"))
    ims = []
    for name in names[:: max(1, (len(names) - 1) // 4)][:5]:
        cmd = shlex.quote(f"cat {seg}/{name}")
        data = subprocess.run(["adb", "-s", serial, "exec-out", f"su -c {cmd}"], stdout=subprocess.PIPE, timeout=60, pass_fds=lock_fds()).stdout
        ims.append(Image.fromarray(raw_rgb(data, session)))
    diffs = [scene_diff(a, b) for a, b in zip(ims, ims[1:])]
    feet = [on_foot(x) for x in ims]
    ok = all(feet)
    if motion == "run":
        ok = ok and min(diffs) >= RUN_MIN_DIFF
    elif motion == "static":
        ok = ok and max(diffs) <= STATIC_MAX_DIFF
    return ok, {"on_foot": feet, "diffs": diffs, "passed": ok}


def convert(data, name, dst, w, h, order, want, check):
    raw_sha = hashlib.sha256(data).hexdigest()
    if raw_sha != want:
        return {"name": name, "error": "raw sha256 mismatch"}
    if len(data) != w * h * 4:
        return {"name": name, "error": f"raw size {len(data)}"}
    rgba = np.frombuffer(data, np.uint8).reshape(h, w, 4)
    rgb = rgba[..., order]   # alpha 不全是 255 (最低约 103), 屏幕上不起作用, 丢掉
    png = os.path.join(dst, name[:-4] + ".png")
    Image.fromarray(rgb).save(png, compress_level=6)
    if check and not np.array_equal(np.asarray(Image.open(png).convert("RGB")), rgb):
        return {"name": name, "error": "png decode differs"}
    with open(png, "rb") as f:
        b = f.read()
    return {"name": name, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(), "raw_sha256": raw_sha, "alpha_min": int(rgba[..., 3].min())}


def pack_clip(dev, serial, seg, dst, pool):
    """把手机上一段流回来转 PNG, 不落原始帧。返回 (session, index 行, 每帧结果)。"""
    os.makedirs(dst, exist_ok=True)
    want = {ln.split()[1]: ln.split()[0] for ln in dev.su(f"cd {seg} && sha256sum f*.raw", timeout=900).splitlines() if len(ln.split()) == 2}
    session = json.loads(dev.su(f"cat {seg}/session.json"))
    w, h, order = session["w"], session["h"], RGB_ORDER[session["vk_format"]]
    p = subprocess.Popen(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote(f'tar -C {seg} -cf - .')}"], stdout=subprocess.PIPE, pass_fds=lock_fds())
    slots, futures, n = threading.Semaphore(16), [], 0
    def job(data, name, check):
        try:
            return convert(data, name, dst, w, h, order, want.get(name), check)
        finally:
            slots.release()
    with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
        for m in tf:
            name = os.path.basename(m.name)
            if not m.isfile():
                continue
            data = tf.extractfile(m).read()
            if name.endswith(".raw"):
                slots.acquire()
                futures.append(pool.submit(job, data, name, n % 50 == 0))
                n += 1
            else:
                with open(os.path.join(dst, name), "wb") as f:
                    f.write(data)
    p.wait(timeout=60)
    res = {r["name"]: r for r in (f.result() for f in futures)}
    rows = list(csv.DictReader(io.StringIO(open(os.path.join(dst, "index.csv")).read())))
    return session, rows, res, len(want)


def onboard_failure(out, err):
    """onboard 失败时 stderr 常是空的, 原因在 enter.json; 维护弹窗的时间也在那里。"""
    try:
        d = json.load(open(os.path.join(out, "onboard", "enter", "enter.json")))
    except (OSError, ValueError):
        return err
    notice = next((" ".join(s["ocr"]) for s in d.get("steps", []) if "维护" in " ".join(s.get("ocr") or [])), None)
    return f"{d.get('reason')}; {notice or err}"


def meta(dev, pkg):
    m = lambda text, pat: (re.search(pat, text) or [None, None])[1]
    pkgs = dev.shell(f"dumpsys package {pkg}")
    wm = dev.shell("wm size")
    x = dev.su(f"cat /data/data/{pkg}/shared_prefs/{pkg}.v2.playerprefs.xml")
    g = re.search(r'name="GENERAL_DATA">([^<]*)<', x)
    t = urllib.parse.unquote(g.group(1)) if g else ""
    return {"version_name": m(pkgs, r"versionName=(\S+)"), "version_code": m(pkgs, r"versionCode=(\d+)"), "screen": m(wm, r"Physical size: *(\S+)"), "override_size": m(wm, r"Override size: *(\S+)"),
            "density": m(dev.shell("wm density"), r"Physical density: *(\S+)"), "fingerprint": dev.prop("ro.build.fingerprint"), "android": dev.prop("ro.build.version.release"),
            "platform": dev.prop("ro.board.platform"), "gles": m(dev.shell("dumpsys SurfaceFlinger | grep -m1 '^GLES:'"), r"GLES: *(.+)"), "root": dev.su("id").count("uid=0") > 0,
            "graphics_prefs": ",".join(a + "=" + b for a, b in re.findall(r'entryType\\":(\d+),\\"index\\":(\d+)', t)) or None,
            "game_mode_configs": dev.shell(f"cmd game list-configs {pkg}").strip()[-200:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--clips", type=int, default=16)
    ap.add_argument("--motions", default=",".join(MOTIONS), help="逗号分隔, 按段轮换; 补采某种运动时用")
    ap.add_argument("--frames", type=int, default=420)
    ap.add_argument("--walk-s", type=float, default=60)
    ap.add_argument("--stride", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--session-seconds", type=int, default=3600)
    ap.add_argument("--cool-max", type=float, default=42.0)
    ap.add_argument("--battery-max", type=float, default=48.0, help="电池超过这个温度就不再开新段")
    ap.add_argument("--retries", type=int, default=2, help="一段没核对过时最多重采几次")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    motions = a.motions.split(",")
    if set(motions) - set(MOTIONS):
        ap.error(f"--motions 只能取 {MOTIONS}")
    rng = random.Random(a.seed)
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"fd_capture_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(out, "clips"))
    dev = ab.Dev(a.serial)
    base = f"/data/data/{a.pkg}/files/i3dump"
    rec = {"plan": vars(a), "temps": [], "clips": [], "steps": []}
    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)
    subprocess.run(["bash", os.path.join(os.path.dirname(HERE), "selfsr", "gray", "build", "build_layer.sh")], check=True, stdout=subprocess.DEVNULL, pass_fds=lock_fds())
    so = os.path.join(out, "libVkLayer_refknobs.so")
    shutil.copyfile(os.path.join(os.path.dirname(HERE), "selfsr", "gray", "build", "out", "libVkLayer_refknobs.so"), so)
    rec["before"] = {"game_mode": ab.current_mode(dev, a.pkg), "volume": (re.search(r"volume is (\d+)", dev.shell("cmd media_session volume --stream 3 --get")) or [None, None])[1],
                     "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1], "free": dev.shell("df -k /data | tail -1").strip()}
    orig_mode = rec["before"]["game_mode"]
    info = meta(dev, a.pkg)
    rec["temps"].append(fp.temps(dev, "before"))
    rec["cool_wait_c"] = ab.wait_batt_temp(dev, a.cool_max, rising=False, max_s=600)
    proc, sdir, st, t_start = None, os.path.join(out, "session"), None, time.time()
    clips, held = [], None
    try:
        dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
        dev.shell(f"am force-stop {a.pkg}")
        ab.game_mode(dev, a.pkg, None, orig_mode)
        dev.shell(f"setprop debug.knobs.framedump 1; setprop debug.knobs.framedump_n {a.frames}")
        step("属性已设, 挂层")
        proc = subprocess.Popen([PF, "gray-layer", "session", "--mode", "probe", "--package", a.pkg, "--serial", a.serial, "--out", sdir,
                                 "--max-seconds", str(a.session_seconds), "--so", so],
                                stdout=open(sdir + ".log", "w"), stderr=subprocess.STDOUT, env=ab.SESSION_ENV, pass_fds=lock_fds())
        phase = None
        for _ in range(450):
            time.sleep(2)
            if proc.poll() is not None:
                break
            code, s = ab.pf_json(PF, "gray-layer", "session-status", "--out", sdir, "--json")
            phase = s.get("phase") if code == 0 else None
            if phase == "ready":
                break
        if phase != "ready":
            raise RuntimeError(f"session not ready (phase={phase}, exit={proc.poll()})")
        code, err = ab.onboard(SimpleNamespace(phonefarm=PF, pkg=a.pkg, serial=a.serial), out, "enter", False)
        if code != 0:
            raise RuntimeError(f"onboard --no-cold: {onboard_failure(out, err)}")
        time.sleep(20)   # 等加载动画和公告淡出
        global W, H
        W, H = look(a.serial, out)[1].size
        pid = dev.shell(f"pidof {a.pkg}").strip()
        rec["temps"].append(fp.temps(dev, "in_world"))
        step("已进世界")
        for i in range(a.clips):
            motion = motions[i % len(motions)]
            label = f"c{i:02d}_{motion}"
            t = fp.temps(dev, label)
            rec["temps"].append(t)
            if (t.get("battery") or 0) >= a.battery_max:
                rec["stopped_hot"] = {"clip": i, "battery": t["battery"]}
                step(f"电池 {t['battery']}°C, 不再开新段")
                break
            dead = False
            for attempt in range(a.retries + 1):
                try_label = label if not attempt else f"{label}_r{attempt}"
                if (i or attempt) and not walk(a.serial, a.walk_s if not attempt else a.walk_s / 2, rng, out, rec):
                    dead = True
                    break
                if not settle(a.serial, out, rec)[0]:
                    dead = True
                    break
                seg = f"{base}/{try_label}"
                # 先动起来再开段, 段的第一帧就在运动里; static 也先点一下保持约 60 帧
                stop, thr = threading.Event(), None
                if motion == "run":
                    held = start_run(a.serial, rng, out, rec)
                    if held is None:
                        rec.setdefault("rejected", []).append({"label": try_label, "reason": "跑不起来"})
                        continue
                else:
                    thr = threading.Thread(target=act, args=(a.serial, motion, stop, rng), daemon=True)
                    thr.start()
                    time.sleep(0.5)
                t0_ns, t0 = fp.now_ns(dev), time.time()
                dev.shell(f"setprop debug.knobs.framedump_go {try_label}")
                finished = False
                for _ in range(int((a.frames / 10 + 60) / 0.5)):
                    names = dev.su(f"ls {seg} 2>/dev/null | grep -v raw")
                    if "summary.json" in names or "refused.json" in names:
                        finished = "summary.json" in names
                        break
                    time.sleep(0.5)
                stop.set()
                if thr:
                    thr.join(timeout=30)
                if held:
                    release_stick(a.serial, held)
                    held = None
                alive = dev.shell(f"pidof {a.pkg}").strip() == pid
                passed, check = check_clip(dev, a.serial, seg, motion) if finished and alive else (False, None)
                step(f"{try_label} 结束 {finished}, 游戏活着 {alive}, 核对 {check}")
                if not finished or not alive:
                    dead = True
                    break
                if passed:
                    clips.append({"label": try_label, "segment": i, "motion": motion, "finished": True, "game_alive": alive, "motion_check": check, "wall_t0": t0, "t0_ns": t0_ns, "t_done": time.time()})
                    break
                rec.setdefault("rejected", []).append({"label": try_label, "reason": check})
                dev.su(f"rm -rf {seg}")
            else:
                rec.setdefault("gave_up", []).append(label)
            if dead:
                rec["stopped_clip"] = i
                step("采集停在这段")
                break
        rec["temps"].append(fp.temps(dev, "after_capture"))
        rec["capture_wall_s"] = round(time.time() - t_start, 1)
        rec["layer_marker"] = ab.session_action(PF, sdir, "marker")
        st = ab.session_action(PF, sdir, "stop")
        proc.wait(timeout=300)
        dev.shell(f"am force-stop {a.pkg}")
        step("采集结束, 读回")
        pool = ThreadPoolExecutor(a.workers)
        kept_bytes, frames_total, bad = 0, 0, []
        for c in clips:
            if not c["finished"]:
                continue
            seg, dst = f"{base}/{c['label']}", os.path.join(out, "clips", c["label"])
            if kept_bytes >= LIMIT_BYTES:
                c["dropped"] = "节点存储上限"
                rec.setdefault("dropped", []).append(c["label"])
                dev.su(f"rm -rf {seg}")
                continue
            t = time.time()
            session, rows, res, n_raw = pack_clip(dev, a.serial, seg, dst, pool)
            frames = []
            for r in rows:
                if not r["file"].endswith(".raw"):
                    c.setdefault("skipped", []).append(r["file"])
                    continue
                got = res.get(r["file"]) or {"error": "not received"}
                if got.get("error"):
                    bad.append({"clip": c["label"], "file": r["file"], "error": got["error"]})
                    continue
                frames.append({"seq": int(r["seq"]), "frame": int(r["frame"]), "t_ns": int(r["t_ns"]), "dt_ns": int(r["dt_ns"]), "gap_before": int(r["gap_before"]), "file": f"clips/{c['label']}/{r['file'][:-4]}.png",
                               "bytes": got["bytes"], "sha256": got["sha256"], "raw_sha256": got["raw_sha256"], "key": False})
            for f in frames[::a.stride]:
                f["key"] = True
            ts = [f["t_ns"] for f in frames]
            c.update(w=session["w"], h=session["h"], vk_format=session["vk_format"], pre_transform=session["pre_transform"], frames_raw=n_raw, frames=frames, frames_png=len(frames),
                     gaps=sum(f["gap_before"] for f in frames), span_s=round((ts[-1] - ts[0]) / 1e9, 3) if len(ts) > 1 else None, png_bytes=sum(f["bytes"] for f in frames),
                     raw_bytes=session["w"] * session["h"] * 4 * len(frames), alpha_min=min((r.get("alpha_min", 255) for r in res.values()), default=None), summary=json.load(open(os.path.join(dst, "summary.json"))), pull_s=round(time.time() - t, 1))
            kept_bytes += c["png_bytes"]
            frames_total += len(frames)
            dev.su(f"rm -rf {seg}")
            step(f"{c['label']}: {len(frames)} 帧 PNG {c['png_bytes'] / 1e9:.2f} GB, {c['span_s']} 秒, 读回转换 {c['pull_s']} 秒, 错 {len(bad)}")
        pool.shutdown()
        rec["bad_frames"] = bad[:50]
        rec["bad_count"] = len(bad)
        rec["png_bytes"] = kept_bytes
        rec["frames_png"] = frames_total
        done = [c for c in clips if c.get("frames")]
        clips_json = {"schema": "hgamey/framedump-clips@1", "stride": a.stride, "clips": done}
        json.dump(clips_json, open(os.path.join(out, "clips.json"), "w"), ensure_ascii=False)
        batch = {"schema": "hgamey/dataset-capture-batch@1", "method": "D", "capture_method": "framedump", "capturer": "capture_framedump", "node_id": os.environ.get("PF_NODE_ID"), "job_id": os.environ.get("PF_JOB_ID"),
                 "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"), "research": os.environ.get("PF_JOB_RESEARCH"), "package": a.pkg, **info,
                 "render_size": f"{done[0]['w']}x{done[0]['h']}" if done else None, "settings_changed_by_script": False,
                 "capture": {"ok": len(done) == a.clips and not bad, "frames": frames_total, "target": a.clips * a.frames, "segment_frames": a.frames, "seed": a.seed, "wall_s": rec["capture_wall_s"],
                             "stop_reason": "target" if len(done) == a.clips else "see report.json"}}
        json.dump(batch, open(os.path.join(out, "batch.json"), "w"), ensure_ascii=False, indent=1)
        key = os.path.join(out, "key")
        os.makedirs(key)
        for n in ("clips.json", "batch.json"):
            shutil.copyfile(os.path.join(out, n), os.path.join(key, n))
        for c in done:
            os.makedirs(os.path.join(key, "clips", c["label"]))
            for n in ("session.json", "summary.json"):
                shutil.copyfile(os.path.join(out, "clips", c["label"], n), os.path.join(key, "clips", c["label"], n))
            for f in c["frames"]:
                if f["key"]:
                    os.link(os.path.join(out, f["file"]), os.path.join(key, f["file"]))
            mid = len(c["frames"]) // 2
            os.makedirs(os.path.join(key, "consecutive", c["label"]))
            for f in c["frames"][mid:mid + 6]:
                os.link(os.path.join(out, f["file"]), os.path.join(key, "consecutive", c["label"], os.path.basename(f["file"])))
        step("读回完成")
    except Exception as e:   # 走不通也要收尾, 写清卡在哪
        rec["error"] = f"{type(e).__name__}: {e}"
        print(rec["error"], file=sys.stderr, flush=True)
    finally:
        if held:
            release_stick(a.serial, held)
        if proc and proc.poll() is None:
            st = ab.session_action(PF, sdir, "stop")
        if proc:
            try:
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=60)
        dev.shell(f"am force-stop {a.pkg}")
        dev.su(f"rm -rf {base}")
        for k in re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M):
            dev.shell(f"setprop {k} ''")
        ab.game_mode(dev, a.pkg, None, orig_mode)
        if rec["before"]["volume"] is not None:
            dev.shell(f"cmd audio set-volume 3 {rec['before']['volume']}")
        if rec["before"]["wakefulness"] != "Awake":
            dev.shell("input keyevent KEYCODE_SLEEP")
        time.sleep(2)
        layer_ok, layer_info = ab.layer_absent(dev, a.pkg)
        left = re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M)
        dir_left = dev.su(f"ls {base} 2>/dev/null").strip()
        restore = {"session_exit": proc.returncode if proc else None, "session_stop": (st or {}).get("status"), "layer_removed": layer_ok, "layer_info": layer_info, "knobs_left": left, "output_dir_left": dir_left,
                   "game_running": bool(dev.shell(f"pidof {a.pkg}").strip()), "game_mode": ab.current_mode(dev, a.pkg), "free": dev.shell("df -k /data | tail -1").strip()}
        restore["ok"] = bool(layer_ok and not left and not dir_left and not restore["game_running"] and restore["game_mode"] == orig_mode)
        rec["restore"] = restore
        rec["temps"].append(fp.temps(dev, "after_restore"))
        rec["clips"] = [{k: v for k, v in c.items() if k != "frames"} for c in clips]
        rec["node_free_gb"] = round(shutil.disk_usage(out).free / 1e9, 1)
        json.dump(restore, open(os.path.join(out, "restore.json"), "w"), ensure_ascii=False, indent=1)
        step("收尾完成")
    print(json.dumps({k: rec.get(k) for k in ("frames_png", "png_bytes", "bad_count", "capture_wall_s", "error", "node_free_gb")}, ensure_ascii=False))
    print(json.dumps(rec["restore"], ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if not rec.get("error") and rec.get("frames_png") == a.clips * a.frames and not rec.get("bad_count") else 1


if __name__ == "__main__":
    sys.exit(main())
