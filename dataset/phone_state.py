#!/usr/bin/env python3
"""phone_state.py — 查一台手机上采集留下的现场, 可选还原 (任务被取消、收尾没跑完时用)。

    python3 -I dataset/phone_state.py <serial> [--pkg com.miHoYo.Yuanshen] [--restore] [--recover-job <被取消的任务ID>]
平台上: pf-node --experiment <提交> --resource device:<serial> --detach experiment dataset/phone_state.py <serial> --restore --recover-job <ID>

报告: wm size、非空的 debug.knobs.* 和 debug.vulkan.layers、应用 lib 目录里的层 .so、灰层状态文件、游戏进程、framedump 输出目录。
--restore: 先对 --recover-job 那个任务里留下的灰层会话目录跑 phonefarm gray-layer session-recover (灰层按自己的登记收尾),
  再 wm size reset、清 debug.knobs.*、停游戏、删 framedump 输出目录、按 HOME, 然后再报告一次。
产物: $PF_JOB_OUT/phone_state.json
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", f"phonestate@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
import selfsr_ab as ab  # noqa: E402
from cruise import lock_fds  # noqa: E402


def state(dev, pkg):
    _, layer = ab.layer_absent(dev, pkg)
    return {"wm_size": dev.shell("wm size").strip(),
            "knobs": re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[(.+)\]$", dev.shell("getprop"), re.M),
            "layer": layer,
            "layer_state_file": dev.su("cat /data/local/tmp/knobs_layer_state 2>/dev/null").strip()[:400],
            "game_pid": dev.shell(f"pidof {pkg}").strip(),
            "framedump_dir": dev.su(f"ls /data/data/{pkg}/files/i3dump 2>/dev/null").strip()[:400],
            "focus": dev.shell("dumpsys window | grep -m1 mCurrentFocus").strip()[-120:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--recover-job")
    a = ap.parse_args()
    dev = ab.Dev(a.serial)
    out = os.environ.get("PF_JOB_OUT") or "."
    rec = {"before": state(dev, a.pkg)}
    print(json.dumps(rec["before"], ensure_ascii=False), flush=True)
    if a.restore:
        steps = []
        if a.recover_job:
            # 任务输出目录是 <jobs>/<任务ID>/out; 本任务的 PF_JOB_OUT 同在 <jobs> 下
            jobs = os.path.dirname(os.path.dirname(os.path.abspath(out)))
            for sdir in sorted(glob.glob(os.path.join(jobs, a.recover_job, "out", "**", "session"), recursive=True)):
                r = subprocess.run([PF, "gray-layer", "session-recover", "--session", sdir, "--serial", a.serial, "--json"],
                                   capture_output=True, text=True, timeout=600, pass_fds=lock_fds())
                steps.append({"session_recover": sdir, "exit": r.returncode, "out": (r.stdout + r.stderr)[-800:]})
                print(steps[-1], flush=True)
            if not steps:
                steps.append({"session_recover": "没找到会话目录", "searched": os.path.join(jobs, a.recover_job, "out")})
        dev.shell("wm size reset")
        for k, _ in re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[(.+)\]$", dev.shell("getprop"), re.M):
            dev.shell(f"setprop {k} ''")
        dev.shell(f"am force-stop {a.pkg}")
        dev.su(f"rm -rf /data/data/{a.pkg}/files/i3dump")
        dev.shell("input keyevent KEYCODE_HOME")
        rec["steps"] = steps
        rec["after"] = state(dev, a.pkg)
        print(json.dumps(rec["after"], ensure_ascii=False), flush=True)
    json.dump(rec, open(os.path.join(out, "phone_state.json"), "w"), ensure_ascii=False, indent=1)
    s = rec.get("after") or rec["before"]
    clean = ("Override" not in s["wm_size"] and not s["knobs"] and not s["layer"]["so_present"]
             and not s["layer"]["debug.vulkan.layers"] and not s["game_pid"] and not s["framedump_dir"])
    print("干净" if clean else "还有残留")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
