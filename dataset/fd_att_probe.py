#!/usr/bin/env python3
"""fd_att_probe.py — 灰层「framedump 拷附件」原型的上机验证: 拷出来的是什么、对帧时间和掉帧的影响。

    python3 -I dataset/fd_att_probe.py <serial> --so <新层 libVkLayer_refknobs.so> --att noui=ac24d328:0,depth=0ed8a53d:3,mv=8b4e9a51:0
平台上 (--so 是本机文件, pf-node 自动上传):
    pf-node --experiment <提交> --research <研究ID> --resource device:<serial> --detach \\
      experiment dataset/fd_att_probe.py <serial> --so <层> --att <规则>

三组, 每组都冷启动游戏 (framedump_att 在 CreateInstance 时读, 换组必须重启):
  A platform  平台节点上的层 (不传 --so), 只开 framedump
  B new_off   新层, 只开 framedump (不设 framedump_att) —— 和 A 比, 看「不开时和现在一样」
  C new_att   新层, 开 framedump_att
每组: 进世界 → 走动时量一次上屏帧率 (不采) → 边走边采 --frames 帧, 同时量上屏帧率 → 读 summary.json / index.csv。
C 组再按 static (不动、每 2 秒点一下空白处) / turn (只转镜头) / run (推摇杆 + 转镜头) 各采 --motion-frames 帧,
每段拉回中间连续 4 帧的送显图和全部附件 (原始字节), 用来看附件是什么、速度图是否跟着运动变。
产物 ($PF_JOB_OUT/fd_att_<时间>/<组>/): session.json summary.json index.csv fps.json shot.png; C 组另有 <段>/f*.raw。
收尾: 停会话摘层、清 debug.knobs.*、强停游戏、删输出目录、按 HOME, 逐项回读写进 report.json 的 restore。
退出码: 0 三组都采完, 1 有组没采完, 2 收尾没通过。
"""
import argparse
import csv
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    label = os.environ.get("DEVLOCK_LABEL", "")
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"fdatt@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
import selfsr_ab as ab  # noqa: E402
import gi_enter  # noqa: E402
from cruise import CAM_X, CAM_Y, STICK, lock_fds  # noqa: E402
from framedump_probe import game_layer, layer_fps  # noqa: E402

W, H = 2688, 1216


def pull_bytes(serial, path, timeout=300):
    q = shlex.quote(path)
    return subprocess.run(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote(f'[ -f {q} ] && cat {q}')}"],
                          capture_output=True, timeout=timeout, pass_fds=lock_fds()).stdout


class Mover:
    """后台按运动类型给输入, stop() 结束并松手。
    size: 当前逻辑横屏尺寸 (宽, 高)。input 注入用的是 wm size 覆盖后的逻辑坐标, 不传就按物理 2688x1216 算。"""
    def __init__(self, serial, motion, size=None, angle=0.0):
        self.serial, self.motion, self.stop_ev = serial, motion, threading.Event()
        self.w, self.h = size or (W, H)
        self.angle = math.radians(angle)   # run 的摇杆方向: 0 正前, 180 正后, 90 向右
        self.t = threading.Thread(target=self.run, daemon=True)

    def sh(self, cmd):
        subprocess.run(["adb", "-s", self.serial, "shell", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, pass_fds=lock_fds())

    def run(self):
        """run: 只按住摇杆往前跑, 每 3 秒在正前、左前 30°、右前 30° 之间换方向 (别一直顶着墙);
        turn: 只左右拖镜头; static: 不动, 每 2 秒点一下空白处保持 60 帧。
        input 只有一个指针: 跑的同时再 swipe 拖镜头, 会把按住的摇杆顶掉, 所以两种不混在一起。"""
        W_, H_ = self.w, self.h
        x0, y0 = int(STICK[0] * W_), int(STICK[1] * H_)
        cy, cx = int(sum(CAM_Y) / 2 * H_), int(sum(CAM_X) / 2 * W_)
        r = int(0.13 * H_)
        if self.motion == "run":
            self.sh(f"input motionevent DOWN {x0} {y0}")
            k = 0
            while not self.stop_ev.is_set():
                ang = self.angle + (0.0, -0.52, 0.0, 0.52)[k % 4]   # 弧度, 0 = 正前
                self.sh(f"input motionevent MOVE {x0 + int(r * math.sin(ang))} {y0 - int(r * math.cos(ang))}")
                k += 1
                self.stop_ev.wait(3)
            self.sh(f"input motionevent UP {x0} {y0 - r}")
            return
        side = 1
        while not self.stop_ev.is_set():
            if self.motion == "turn":
                self.sh(f"input swipe {cx} {cy} {cx + side * int(0.10 * W_)} {cy} 900")
                side = -side
            else:   # static: 不点游戏 30 帧左右; 点空白处 (摇杆和按键之外) 保持 60 帧
                self.sh(f"input tap {int(0.55 * W_)} {int(0.25 * H_)}")
                self.stop_ev.wait(2)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop_ev.set()
        self.t.join(timeout=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--so", required=True, help="新层 (本机文件)")
    ap.add_argument("--att", required=True)
    ap.add_argument("--frames", type=int, default=240, help="每组量帧时用的一段帧数")
    ap.add_argument("--motion-frames", type=int, default=60)
    ap.add_argument("--arms", default="platform,new_off,new_att")
    a = ap.parse_args()
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"fd_att_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    dev = ab.Dev(a.serial)
    rec = {"pkg": a.pkg, "att": a.att, "frames": a.frames, "arms": [], "steps": [], "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT")}
    rec["so_sha256"] = __import__("hashlib").sha256(open(a.so, "rb").read()).hexdigest()

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)

    m = re.search(r"versionName=(\S+)", dev.shell(f"dumpsys package {a.pkg} | grep versionName"))
    rec["game_version"] = m.group(1) if m else None
    rec["before"] = {"wm_size": dev.shell("wm size").strip(), "debug.vulkan.layers": dev.prop("debug.vulkan.layers"),
                     "knobs": dev.shell("getprop | grep '^\\[debug.knobs' | grep -v ': \\[\\]'").strip(),
                     "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1]}
    if rec["before"]["debug.vulkan.layers"] or rec["before"]["knobs"] or "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有层/knobs/wm size 覆盖, 不动: {rec['before']}")
        return 1
    base = f"/data/data/{a.pkg}/files/i3dump"

    def segment(arm_dir, label, n, motion, pull_frames):
        """开一段 n 帧 (motion 输入下), 同时量上屏帧率; 读回 session/index/summary 和指定的连续帧。"""
        seg = f"{base}/{label}"
        dev.shell(f"setprop debug.knobs.framedump_n {n}")
        layer = game_layer(dev, a.pkg)
        fps = {}
        with Mover(a.serial, motion):
            time.sleep(2.0)
            thr = threading.Thread(target=lambda: fps.update(layer_fps(ab.Dev(a.serial), layer, max(3, n // 60)) or {}), daemon=True)
            thr.start()
            dev.shell(f"setprop debug.knobs.framedump_go {label}")
            done = False
            for _ in range(n // 20 + 120):
                names = dev.su(f"ls {seg} 2>/dev/null | grep -v raw$")
                if "summary.json" in names or "refused.json" in names:
                    done = "summary.json" in names
                    break
                time.sleep(1)
            thr.join(timeout=60)
        sdir = os.path.join(arm_dir, label)
        os.makedirs(sdir, exist_ok=True)
        for f in ("session.json", "index.csv", "summary.json", "refused.json"):
            b = pull_bytes(a.serial, f"{seg}/{f}")
            if b:
                open(os.path.join(sdir, f), "wb").write(b)
        json.dump(fps, open(os.path.join(sdir, "fps.json"), "w"))
        res = {"label": label, "motion": motion, "finished": done, "sf_fps": fps}
        try:
            res["summary"] = json.load(open(os.path.join(sdir, "summary.json")))
            rows = list(csv.DictReader(io.StringIO(open(os.path.join(sdir, "index.csv")).read())))
            dts = sorted(int(r["dt_ns"]) / 1e6 for r in rows if r["dt_ns"] not in ("", "0"))
            res["frames_ok"] = sum(1 for r in rows if r["file"].endswith(".raw"))
            res["gaps"] = sum(int(r["gap_before"] or 0) for r in rows)
            res["dropped_rows"] = sum(1 for r in rows if not r["file"].endswith(".raw"))
            if dts:
                res["present_dt_ms"] = {"p50": round(dts[len(dts) // 2], 2), "p95": round(dts[int(len(dts) * 0.95)], 2),
                                        "max": round(dts[-1], 2), "mean": round(sum(dts) / len(dts), 2)}
            att_cols = [c for c in (rows[0].keys() if rows else []) if c not in ("seq", "frame", "t_ns", "dt_ns", "gap_before", "file", "write_us")]
            res["att_present"] = {c: sum(1 for r in rows if r[c].endswith(".raw")) for c in att_cols}
            if pull_frames and rows:
                good = [r for r in rows if r["file"].endswith(".raw")]   # 送显图在就拉, 附件有几个拉几个
                mid = len(good) // 2
                pick = good[mid:mid + pull_frames]
                for r in pick:
                    for f in [r["file"]] + [r[c] for c in att_cols if r[c].endswith(".raw")]:
                        open(os.path.join(sdir, f), "wb").write(pull_bytes(a.serial, f"{seg}/{f}"))
                res["pulled"] = [r["frame"] for r in pick]
        except Exception as e:
            res["parse_error"] = f"{type(e).__name__}: {e}"
        dev.su(f"rm -rf {seg}")
        return res

    for arm in a.arms.split(","):
        arm_dir = os.path.join(out, arm)
        os.makedirs(arm_dir)
        r = {"arm": arm, "segments": []}
        rec["arms"].append(r)
        proc, sdir = None, os.path.join(arm_dir, "session")
        try:
            dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
            dev.shell(f"am force-stop {a.pkg}")
            props = {"debug.knobs.framedump": "1", "debug.knobs.framedump_n": str(a.frames)}
            if arm == "new_att":
                props["debug.knobs.framedump_att"] = a.att
            dev.shell("; ".join(f"setprop {k} {shlex.quote(v)}" for k, v in props.items()))
            cmd = [PF, "gray-layer", "session", "--mode", "probe", "--package", a.pkg, "--serial", a.serial, "--out", sdir,
                   "--max-seconds", "1800"]
            if arm != "platform":
                cmd += ["--so", a.so]
            proc = subprocess.Popen(cmd, stdout=open(sdir + ".log", "w"), stderr=subprocess.STDOUT, env=ab.SESSION_ENV, pass_fds=lock_fds())
            phase = None
            for _ in range(300):
                time.sleep(2)
                if proc.poll() is not None:
                    break
                code, s = ab.pf_json(PF, "gray-layer", "session-status", "--out", sdir, "--json")
                phase = s.get("phase") if code == 0 else None
                if phase == "ready":
                    break
            if phase != "ready":
                raise RuntimeError(f"session not ready (phase={phase}, exit={proc.poll()})")
            ok, secs, taps = gi_enter.enter_world(a.serial, a.pkg, cold=False, timeout=600, log=step,
                                                  fail_png=os.path.join(arm_dir, "enter_fail.png"))
            r["enter"] = {"ok": ok, "seconds": secs, "taps": taps}
            if not ok:
                raise RuntimeError("没进世界")
            time.sleep(10)
            gi_enter.save_png(a.serial, os.path.join(arm_dir, "shot.png"))
            layer = game_layer(dev, a.pkg)
            with Mover(a.serial, "run"):
                time.sleep(2)
                r["sf_fps_no_capture_run"] = layer_fps(dev, layer, 6)
            step(f"{arm}: 不采时走动上屏 {r['sf_fps_no_capture_run']}")
            seg = segment(arm_dir, f"{arm}_run{a.frames}", a.frames, "run", 0)
            r["segments"].append(seg)
            step(f"{arm}: 采 {a.frames} 帧 run: {json.dumps({k: seg.get(k) for k in ('finished', 'frames_ok', 'gaps', 'present_dt_ms', 'att_present', 'sf_fps')}, ensure_ascii=False)}")
            if arm == "new_att":
                for motion in ("static", "turn", "run"):
                    seg = segment(arm_dir, f"att_{motion}", a.motion_frames, motion, 4)
                    r["segments"].append(seg)
                    step(f"{arm}: {motion} 段 {json.dumps({k: seg.get(k) for k in ('finished', 'frames_ok', 'att_present', 'pulled')}, ensure_ascii=False)}")
            r["logcat"] = dev.shell("logcat -d | grep -E 'framedump|refknobs' | tail -60")
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
            step(f"{arm}: {r['error']}")
        finally:
            if proc and proc.poll() is None:
                ab.session_action(PF, sdir, "stop")
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
    dev.shell("input keyevent KEYCODE_HOME")
    if rec["before"]["wakefulness"] != "Awake":
        dev.shell("input keyevent KEYCODE_SLEEP")
    time.sleep(2)
    layer_ok, info = ab.layer_absent(dev, a.pkg)
    rec["restore"] = {"focus": gi_enter.focus(a.serial)[-160:], "wm_size": dev.shell("wm size").strip(), "layer_removed": layer_ok,
                      "layer_info": info, "knobs_left": re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M),
                      "output_dir_left": dev.su(f"ls {base} 2>/dev/null").strip(), "game_running": bool(dev.shell(f"pidof {a.pkg}").strip())}
    rec["restore"]["ok"] = bool(layer_ok and not rec["restore"]["knobs_left"] and not rec["restore"]["output_dir_left"]
                                and "Override" not in rec["restore"]["wm_size"] and not rec["restore"]["game_running"])
    step("收尾完成")
    print(json.dumps(rec["restore"], ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if all(s.get("finished") for r in rec["arms"] for s in r["segments"]) and all(r["segments"] for r in rec["arms"]) else 1


if __name__ == "__main__":
    sys.exit(main())
