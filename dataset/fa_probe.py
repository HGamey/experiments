#!/usr/bin/env python3
"""fa_probe.py — 平台灰层 frame-audit 抓原神一帧的渲染步骤 (每个 pass 的附件格式、尺寸、loadOp/storeOp、提交顺序)。

    python3 -I dataset/fa_probe.py <serial> [--pkg 包名] [--wm-size 1080x1920]
平台上: pf-node --experiment <提交> --research <研究ID> --resource device:<serial> --detach \\
          experiment dataset/fa_probe.py <serial>

gray-layer session --mode frameaudit (平台层) 启动游戏 → gi_enter 进世界 → 走动中 setprop debug.knobs.frameaudit_go <编号> →
等层写出 knobs_frameaudit.json (武装后第 6 帧起连抓 3 帧, 第 90 帧落盘) → 拉回 → 停会话、停游戏、清属性。
--wm-size 给了就先 wm size (竖屏宽x高, 如 1080x1920 得到横屏 1920x1080), 收尾 wm size reset。
产物 ($PF_JOB_OUT/fa_<时间>/): knobs_frameaudit.json, shot.png, report.json (含 restore)。退出码: 0 拿到, 1 没拿到, 2 收尾没通过。
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
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"frameaudit@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
import selfsr_ab as ab  # noqa: E402
import gi_enter  # noqa: E402
from cruise import lock_fds  # noqa: E402
from fd_res_probe import walk  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--wm-size", default=None)
    ap.add_argument("--ts", action="store_true", help="同时测每个 pass 的 GPU 时间 (frameaudit-ts)")
    a = ap.parse_args()
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"fa_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    dev = ab.Dev(a.serial)
    rec = {"pkg": a.pkg, "wm_size": a.wm_size, "steps": [], "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT")}

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
    cands = [f"/storage/emulated/0/Android/data/{a.pkg}/files/knobs_frameaudit.json", f"/data/data/{a.pkg}/knobs_frameaudit.json",
             f"/data/local/tmp/knobs_frameaudit.{a.pkg}.json"]
    proc, sdir, got = None, os.path.join(out, "session"), False
    try:
        dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
        dev.shell(f"am force-stop {a.pkg}")
        dev.su("rm -f " + " ".join(cands))
        if a.wm_size:
            dev.shell(f"wm size {a.wm_size}")
        proc = subprocess.Popen([PF, "gray-layer", "session", "--mode", "frameaudit-ts" if a.ts else "frameaudit", "--package", a.pkg,
                                 "--serial", a.serial, "--out", sdir, "--max-seconds", "1200"],
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
        ok, secs, taps = gi_enter.enter_world(a.serial, a.pkg, cold=False, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
        rec["enter"] = {"ok": ok, "seconds": secs, "taps": taps}
        if not ok:
            raise RuntimeError("没进世界")
        time.sleep(8)
        gi_enter.save_png(a.serial, os.path.join(out, "shot.png"))
        mover = threading.Thread(target=walk, args=(ab.Dev(a.serial), a.serial, 12), daemon=True)
        mover.start()
        time.sleep(1.5)
        dev.shell(f"setprop debug.knobs.frameaudit_go {int(time.time()) % 1000000}")
        for _ in range(90):
            time.sleep(1)
            for c in cands:
                if dev.su(f"ls {c} 2>/dev/null").strip():
                    b = subprocess.run(["adb", "-s", a.serial, "exec-out", f"su -c {shlex.quote('cat ' + c)}"], capture_output=True,
                                       timeout=300, pass_fds=lock_fds()).stdout
                    open(os.path.join(out, "knobs_frameaudit.json"), "wb").write(b)
                    rec["fa_path"], rec["fa_bytes"], got = c, len(b), True
                    break
            if got:
                break
        mover.join(timeout=60)
        step(f"frame-audit {'拿到' if got else '没拿到'} {rec.get('fa_bytes')} 字节")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        step(rec["error"])
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
        dev.su("rm -f " + " ".join(cands))
        for k in re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M):
            dev.shell(f"setprop {k} ''")
        if a.wm_size:
            dev.shell("wm size reset")
        dev.shell("input keyevent KEYCODE_HOME")   # 清掉游戏空间、弹窗等残留界面
        if rec["before"]["wakefulness"] != "Awake":
            dev.shell("input keyevent KEYCODE_SLEEP")
        time.sleep(2)
        layer_ok, info = ab.layer_absent(dev, a.pkg)
        rec["restore"] = {"focus": gi_enter.focus(a.serial)[-160:], "wm_size": dev.shell("wm size").strip(), "layer_removed": layer_ok, "layer_info": info,
                          "knobs_left": re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M),
                          "game_running": bool(dev.shell(f"pidof {a.pkg}").strip())}
        rec["restore"]["ok"] = bool(layer_ok and not rec["restore"]["knobs_left"] and "Override" not in rec["restore"]["wm_size"]
                                    and not rec["restore"]["game_running"])
        step("收尾完成")
    print(json.dumps({"got": got, "error": rec.get("error"), "restore": rec["restore"]}, ensure_ascii=False))
    return 2 if not rec["restore"]["ok"] else (0 if got else 1)


if __name__ == "__main__":
    sys.exit(main())
