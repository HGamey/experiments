#!/usr/bin/env python3
"""fd_res_probe.py — 每档分辨率各抓一小段 framedump, 核对游戏实际送显尺寸 (采集需求第 2 项)。

    python3 -I dataset/fd_res_probe.py <serial> [--sizes native,1920x1080,1280x720,960x540,2560x1440] [--frames 60]
平台上: pf-node --experiment <提交> --research <研究ID> --resource device:<serial> --detach \\
          experiment dataset/fd_res_probe.py <serial>

每档: wm size 设成 <短边>x<长边> (native 不设) → gray-layer session (平台层, framedump=1) 启动游戏 → gi_enter 进世界 →
framedump_go 开一段 --frames 帧, 边走边抓 → 读 session.json 的 w/h、summary.json、index.csv, 拉回首帧和中间帧的原始字节 →
停会话、停游戏。所有档做完 wm size reset, 逐项回读写进 report.json 的 restore。
产物 ($PF_JOB_OUT/fd_res_<时间>/<档>/): session.json summary.json index.csv f*.raw shot.png sf.txt;  report.json 汇总每档尺寸。
退出码: 0 每档都抓到, 1 有档没抓到, 2 收尾没通过。
"""
import argparse
import json
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
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"fdres@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
import selfsr_ab as ab  # noqa: E402
import gi_enter  # noqa: E402
from cruise import CAM_X, CAM_Y, STICK, lock_fds  # noqa: E402


def pull_bytes(serial, path, timeout=300):
    return subprocess.run(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote('cat ' + shlex.quote(path))}"],
                          capture_output=True, timeout=timeout, pass_fds=lock_fds()).stdout


def logical_size(dev):
    m = re.search(r" cur=(\d+)x(\d+)", dev.shell("dumpsys window displays | grep -m1 ' cur='"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def walk(dev, serial, secs):
    """推着摇杆走 secs 秒, 中间来回转视角 (坐标按当前逻辑屏幕尺寸)。"""
    w, h = logical_size(dev) or (2688, 1216)
    w, h = max(w, h), min(w, h)
    x0, y0 = int(STICK[0] * w), int(STICK[1] * h)
    dev.shell(f"input motionevent DOWN {x0} {y0}; input motionevent MOVE {x0} {y0 - int(0.13 * h)}")
    t0, side = time.time(), 1
    cy, cx = int(sum(CAM_Y) / 2 * h), int(sum(CAM_X) / 2 * w)
    while time.time() - t0 < secs:
        dev.shell(f"input swipe {cx} {cy} {cx + side * int(0.10 * w)} {cy} 800")
        side = -side
    dev.shell(f"input motionevent UP {x0} {y0 - int(0.13 * h)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--sizes", default="native,1920x1080,1280x720,960x540,2560x1440",
                    help="逗号分隔; WxH 是横屏宽x高, 内部按 wm size HxW 设")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--session-seconds", type=int, default=1200)
    a = ap.parse_args()
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"fd_res_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    dev = ab.Dev(a.serial)
    rec = {"pkg": a.pkg, "frames": a.frames, "tiers": [], "steps": [], "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT")}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)

    m = re.search(r"versionName=(\S+)", dev.shell(f"dumpsys package {a.pkg} | grep versionName"))
    rec["game_version"] = m.group(1) if m else None
    rec["before"] = {"wm_size": dev.shell("wm size").strip(), "wm_density": dev.shell("wm density").strip(),
                     "debug.vulkan.layers": dev.prop("debug.vulkan.layers"),
                     "knobs": dev.shell("getprop | grep '^\\[debug.knobs' | grep -v ': \\[\\]'").strip(),
                     "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1]}
    if rec["before"]["debug.vulkan.layers"] or rec["before"]["knobs"] or "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有层/knobs/wm size 覆盖, 不动: {rec['before']}")
        return 1
    base = f"/data/data/{a.pkg}/files/i3dump"
    for tier in a.sizes.split(","):
        tdir = os.path.join(out, tier)
        os.makedirs(tdir)
        t = {"tier": tier}
        rec["tiers"].append(t)
        proc, sdir = None, os.path.join(tdir, "session")
        try:
            dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
            dev.shell(f"am force-stop {a.pkg}")
            if tier == "native":
                dev.shell("wm size reset")
            else:
                w, h = (int(v) for v in tier.split("x"))
                dev.shell(f"wm size {h}x{w}")
            time.sleep(2)
            t["wm_size"] = dev.shell("wm size").strip()
            t["logical"] = logical_size(dev)
            dev.shell(f"setprop debug.knobs.framedump 1; setprop debug.knobs.framedump_n {a.frames}")
            proc = subprocess.Popen([PF, "gray-layer", "session", "--mode", "probe", "--package", a.pkg, "--serial", a.serial,
                                     "--out", sdir, "--max-seconds", str(a.session_seconds)],
                                    stdout=open(sdir + ".log", "w"), stderr=subprocess.STDOUT, env=ab.SESSION_ENV, pass_fds=lock_fds())
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
                                                  fail_png=os.path.join(tdir, "enter_fail.png"))
            t["enter"] = {"ok": ok, "seconds": secs, "taps": taps}
            if not ok:
                raise RuntimeError("没进世界")
            time.sleep(8)
            gi_enter.save_png(a.serial, os.path.join(tdir, "shot.png"))
            open(os.path.join(tdir, "sf.txt"), "w").write(
                dev.shell(f"dumpsys SurfaceFlinger | grep -A3 'SurfaceView\\[{a.pkg}' | head -12"))
            label = f"res_{tier}_{int(time.time())}"
            seg = f"{base}/{label}"
            mover = threading.Thread(target=walk, args=(ab.Dev(a.serial), a.serial, max(6, a.frames // 30 + 4)), daemon=True)
            mover.start()
            time.sleep(1.5)
            dev.shell(f"setprop debug.knobs.framedump_go {label}")
            done = False
            for _ in range(240):
                names = dev.su(f"ls {seg} 2>/dev/null | grep -v raw")
                if "summary.json" in names or "refused.json" in names:
                    done = "summary.json" in names
                    break
                time.sleep(1)
            mover.join(timeout=120)
            t["segment_finished"] = done
            for n in ("session.json", "index.csv", "summary.json", "refused.json"):
                b = pull_bytes(a.serial, f"{seg}/{n}")
                if b:
                    open(os.path.join(tdir, n), "wb").write(b)
            if os.path.exists(os.path.join(tdir, "session.json")):
                s = json.load(open(os.path.join(tdir, "session.json")))
                t.update(w=s.get("w"), h=s.get("h"), vk_format=s.get("vk_format"))
            if os.path.exists(os.path.join(tdir, "summary.json")):
                t["summary"] = json.load(open(os.path.join(tdir, "summary.json")))
            files = sorted(dev.su(f"ls {seg} | grep raw$").split())
            for n in files[:1] + files[len(files) // 2:len(files) // 2 + 1]:
                open(os.path.join(tdir, n), "wb").write(pull_bytes(a.serial, f"{seg}/{n}"))
            t["raw_files"] = len(files)
            step(f"{tier}: 送显 {t.get('w')}x{t.get('h')}, 段完成 {done}, {len(files)} 帧")
        except Exception as e:
            t["error"] = f"{type(e).__name__}: {e}"
            step(f"{tier}: {t['error']}")
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
    dev.shell("wm size reset")
    if rec["before"]["wakefulness"] != "Awake":
        dev.shell("input keyevent KEYCODE_SLEEP")
    time.sleep(2)
    layer_ok, info = ab.layer_absent(dev, a.pkg)
    rec["restore"] = {"wm_size": dev.shell("wm size").strip(), "layer_removed": layer_ok, "layer_info": info,
                      "knobs_left": re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M),
                      "output_dir_left": dev.su(f"ls {base} 2>/dev/null").strip(),
                      "game_running": bool(dev.shell(f"pidof {a.pkg}").strip())}
    rec["restore"]["ok"] = bool(layer_ok and not rec["restore"]["knobs_left"] and not rec["restore"]["output_dir_left"]
                                and "Override" not in rec["restore"]["wm_size"] and not rec["restore"]["game_running"])
    step("收尾完成")
    print(json.dumps([{k: t.get(k) for k in ("tier", "w", "h", "segment_finished", "error")} for t in rec["tiers"]], ensure_ascii=False))
    print(json.dumps(rec["restore"], ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if all(t.get("segment_finished") for t in rec["tiers"]) else 1


if __name__ == "__main__":
    sys.exit(main())
