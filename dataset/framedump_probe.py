#!/usr/bin/env python3
"""framedump_probe.py — 方法 D 上机验证 (DATASET-COLLECTION §3.5): 挂层 → 进世界 → 开一段 → 核对并读回 → 收尾。这一步验证工具, 不是采数据集。

    python3 -I dataset/framedump_probe.py <serial> [--arm native|gamemode|selfsr] [--frames 600] [--keep-all] [--pkg 包名]
平台上: pf-node --experiment <提交> --research <研究ID> --device-access write --resource device:<serial> --detach \\
          experiment dataset/framedump_probe.py <serial> --frames 60

--arm native 只开 framedump; gamemode 加 Game Mode 降到 --scale; selfsr 再加 debug.knobs.selfsr=--sr。
开段前量两种基线帧率 (不动、走动); 开段后先不动截 2 张图 (带 CLOCK_MONOTONIC 前后时刻, 用来和同时刻的帧比), 再转视角加走动到段结束。
产物 ($PF_JOB_OUT/fd_<arm>_<时间>/): report.json, session.json, index.csv, summary.json, raw.sha256, logcat.txt,
  shot_*.png, sample/f*.raw (抽帧), segment.tar (--keep-all 才有, 整段原始帧)。手机上的输出目录读回后删除。
收尾 (含出错): 停会话摘层、清 debug.knobs.*、还原 Game Mode 和音量、强停游戏、删输出目录, 逐项回读写进 report.json 的 restore。
退出码: 0 段完成且收尾通过, 1 段没完成, 2 收尾没通过。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import threading
import time
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
import selfsr_ab as ab  # noqa: E402
from cruise import CAM_X, CAM_Y, STICK, lock_fds  # noqa: E402


def now_ns(dev):
    m = re.search(r"now at (\d+) nsecs", dev.su("grep -m1 'now at' /proc/timer_list"))
    return int(m.group(1)) if m else None


THERMAL_TYPES = {"0": "cpu", "1": "gpu", "2": "battery", "3": "skin"}


def temps(dev, tag):
    """thermalservice 前面的是缓存值, 不更新; 取 HAL 那一节, 每类取最高。"""
    by = {}
    for v, ty in re.findall(r"mValue=([0-9.]+), mType=(\d+), mName=", dev.shell("dumpsys thermalservice").split("from HAL")[-1]):
        if ty in THERMAL_TYPES:
            by[THERMAL_TYPES[ty]] = max(by.get(THERMAL_TYPES[ty], 0), round(float(v), 1))
    return {"at": tag, "t": round(time.time(), 1), "battery_dumpsys_c": ab.batt_temp_c(dev), **by}


def game_layer(dev, pkg):
    m = re.findall(r"RequestedLayerState\{([0-9a-f]+ SurfaceView\[" + re.escape(pkg) + r"[^ ]*\]\(BLAST\)#\d+) ", dev.shell("dumpsys SurfaceFlinger --list"))
    return m[-1] if m else None


def layer_fps(dev, name, windows=6):
    """游戏图层的上屏帧率: SF --latency 的 actual-present 列, 每段 1 s (缓冲只存 128 帧)。"""
    if not name:
        return None
    fps = []
    for _ in range(windows):
        dev.shell("dumpsys SurfaceFlinger --latency-clear >/dev/null; sleep 1")
        ts = sorted(int(r[1]) for r in (ln.split() for ln in dev.shell(f"dumpsys SurfaceFlinger --latency {shlex.quote(name)}").splitlines())
                    if len(r) == 3 and 0 < int(r[1]) < 9e18)
        if len(ts) > 2:
            fps.append(round((len(ts) - 1) / ((ts[-1] - ts[0]) / 1e9), 2))
    return {"windows": fps, "mean": round(sum(fps) / len(fps), 2) if fps else None}


def move(dev, serial, side):
    """走 1.8 s, 同时转一下视角。"""
    w, h = 2688, 1216
    sx, sy = int(STICK[0] * w), int(STICK[1] * h)
    walk = subprocess.Popen(["adb", "-s", serial, "shell", f"input swipe {sx} {sy} {sx} {sy - int(0.13 * h)} 1800"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=lock_fds())
    cy, cx = int(sum(CAM_Y) / 2 * h), int(sum(CAM_X) / 2 * w)
    dev.shell(f"input swipe {cx} {cy} {cx + side * int(0.12 * w)} {cy} 700")
    walk.wait(timeout=30)


def pull_bytes(serial, path, timeout=120):
    r = subprocess.run(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote('cat ' + shlex.quote(path))}"], capture_output=True, timeout=timeout, pass_fds=lock_fds())
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--arm", choices=("native", "gamemode", "selfsr"), default="native")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--scale", type=float, default=0.56)
    ap.add_argument("--sr", default="2141x969:0.8")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--keep-all", action="store_true")
    ap.add_argument("--session-seconds", type=int, default=900)
    ap.add_argument("--cool-max", type=float, default=42.0, help="开跑前等电池降到这个温度, 最多等 10 分钟")
    a = ap.parse_args()
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"fd_{a.arm}_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(out, "sample"))
    dev = ab.Dev(a.serial)
    label = f"fd_{a.arm}_{int(time.time())}"
    base = f"/data/data/{a.pkg}/files/i3dump"
    seg = f"{base}/{label}"
    rec = {"arm": a.arm, "label": label, "frames_planned": a.frames, "pkg": a.pkg, "temps": [], "shots": [], "steps": []}
    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)
    subprocess.run(["bash", os.path.join(os.path.dirname(HERE), "selfsr", "gray", "build", "build_layer.sh")], check=True, stdout=subprocess.DEVNULL, pass_fds=lock_fds())
    so = os.path.join(out, "libVkLayer_refknobs.so")
    with open(os.path.join(os.path.dirname(HERE), "selfsr", "gray", "build", "out", "libVkLayer_refknobs.so"), "rb") as f, open(so, "wb") as g:
        g.write(f.read())
    rec["layer_sha256"] = hashlib.sha256(open(so, "rb").read()).hexdigest()
    rec["experiment_commit"] = os.environ.get("PF_EXPERIMENT_COMMIT")
    rec["before"] = {"game_mode": ab.current_mode(dev, a.pkg), "game_configs": dev.shell(f"cmd game list-configs {a.pkg}").strip()[-300:],
                     "knobs": dev.shell("getprop | grep '^\\[debug.knobs'").strip(), "free": dev.shell("df -k /data | tail -1").strip(),
                     "volume": (re.search(r"volume is (\d+)", dev.shell("cmd media_session volume --stream 3 --get")) or [None, None])[1],
                     "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1]}
    orig_mode = rec["before"]["game_mode"]
    rec["temps"].append(temps(dev, "before"))
    rec["cool_wait_c"] = ab.wait_batt_temp(dev, a.cool_max, rising=False, max_s=600)
    rec["temps"].append(temps(dev, "after_cool_wait"))
    proc, sdir, st = None, os.path.join(out, "session"), None
    try:
        dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
        dev.shell(f"am force-stop {a.pkg}")
        rec["game_mode_set"] = ab.game_mode(dev, a.pkg, a.scale if a.arm != "native" else None, orig_mode)
        props = {"debug.knobs.framedump": "1", "debug.knobs.framedump_n": str(a.frames)}
        if a.slots:
            props["debug.knobs.framedump_slots"] = str(a.slots)
        if a.arm == "selfsr":
            props["debug.knobs.selfsr"] = a.sr
        dev.shell("; ".join(f"setprop {k} {shlex.quote(v)}" for k, v in props.items()))
        rec["props_set"] = {k: dev.prop(k) for k in props}
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
        step("层就绪, 进世界")
        code, err = ab.onboard(SimpleNamespace(phonefarm=PF, pkg=a.pkg, serial=a.serial), out, "enter", False)
        if code != 0:
            raise RuntimeError(f"onboard --no-cold: {err}")
        time.sleep(20)   # 等加载动画和公告淡出
        pid = dev.shell(f"pidof {a.pkg}").strip()
        layer = game_layer(dev, a.pkg)
        rec.update(game_pid=pid, game_layer=layer)
        rec["temps"].append(temps(dev, "in_world"))
        step(f"已进世界, 图层 {layer}, 量基线帧率")
        rec["fps_no_capture"] = layer_fps(dev, layer)
        got = {}   # 不动时游戏只有约 30 帧, 走动时才到 60, 所以两种状态都量, 和抓帧时同样的走动比
        thr = threading.Thread(target=lambda: got.update(layer_fps(ab.Dev(a.serial), layer, 8) or {}), daemon=True)
        thr.start()
        for i in range(5):
            move(dev, a.serial, 1 if i % 2 else -1)
        thr.join(timeout=30)
        rec["fps_no_capture_moving"] = got
        rec["temps"].append(temps(dev, "before_go"))
        dev.shell(f"logcat -c; setprop debug.knobs.framedump_go {label}")
        rec["t_go"] = time.time()
        for _ in range(60):
            names = dev.su(f"ls {seg} 2>/dev/null | grep -v raw")
            if "session.json" in names or "refused.json" in names:
                break
            time.sleep(0.5)
        step(f"开段: {names.split()}")
        if "refused.json" in names:
            rec["refused"] = json.loads(dev.su(f"cat {seg}/refused.json"))
            raise RuntimeError("framedump 拒抓")
        fps_during = {}
        thr = threading.Thread(target=lambda: fps_during.update(layer_fps(ab.Dev(a.serial), layer, 6) or {}), daemon=True)
        thr.start()
        for i in range(2):   # 不动时截图, 带前后时刻, 离线和同时刻的帧比
            t0 = now_ns(dev)
            png = subprocess.run(["adb", "-s", a.serial, "exec-out", "screencap -p"], capture_output=True, timeout=60, pass_fds=lock_fds()).stdout
            t1 = now_ns(dev)
            name = f"shot_{i}.png"
            open(os.path.join(out, name), "wb").write(png)
            rec["shots"].append({"file": name, "t0_ns": t0, "t1_ns": t1, "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest()})
        step("不动时的截图已存, 开始走动")
        deadline, done = time.time() + a.frames / 5 + 120, False
        side = 1
        while time.time() < deadline:
            if "summary.json" in dev.su(f"ls {seg} 2>/dev/null | grep -v raw"):
                done = True
                break
            move(dev, a.serial, side)
            side = -side
        rec["t_end"] = time.time()
        thr.join(timeout=30)
        rec["fps_during_sf"] = fps_during
        rec["temps"].append(temps(dev, "after_capture"))
        rec["game_alive_after"] = dev.shell(f"pidof {a.pkg}").strip() == pid
        rec["segment_finished"] = done
        open(os.path.join(out, "logcat.txt"), "w").write(dev.shell("logcat -d | grep -E 'refknobs|framedump' | tail -200"))
        step(f"段结束 {done}, 游戏活着 {rec['game_alive_after']}")
        rec["layer_marker"] = ab.session_action(PF, sdir, "marker")
        st = ab.session_action(PF, sdir, "stop")   # 读回时游戏不用开着, 手机少发热
        proc.wait(timeout=300)
        dev.shell(f"am force-stop {a.pkg}")
        for n in ("session.json", "index.csv", "summary.json"):
            open(os.path.join(out, n), "wb").write(pull_bytes(a.serial, f"{seg}/{n}"))
        ls = dev.su(f"ls -l {seg}")
        sizes = [int(p[4]) for p in (ln.split() for ln in ls.splitlines()) if len(p) >= 8 and p[-1].endswith(".raw")]
        t = time.time()
        sums = dev.su(f"cd {seg} && sha256sum f*.raw", timeout=900).replace("\r", "")
        open(os.path.join(out, "raw.sha256"), "w").write(sums)
        rec["raw_files"] = {"count": len(sizes), "bytes": sum(sizes), "sizes": sorted(set(sizes)), "sha256_s": round(time.time() - t, 1)}
        want = {ln.split()[1]: ln.split()[0] for ln in sums.splitlines() if len(ln.split()) == 2}
        rows = list(csv.DictReader(io.StringIO(open(os.path.join(out, "index.csv")).read())))
        files = [r["file"] for r in rows if r["file"].endswith(".raw")]
        pick = {files[round(i * (len(files) - 1) / max(1, a.samples - 1))] for i in range(a.samples)} if files else set()
        for sh in rec["shots"]:   # 截图前后时刻之内的帧: 取最前面的 12 张 (实测截图发生在前 100 ms 左右), 离线挑最像的比
            inside = [r["file"] for r in rows if r["file"].endswith(".raw") and sh["t0_ns"] and sh["t0_ns"] <= int(r["t_ns"]) <= sh["t1_ns"]]
            sh["frames_in_window"] = len(inside)
            pick |= set(inside[:12])
        rec["consecutive"] = files[round(len(files) * 0.75):][:6]   # 走动时的连续几帧
        pick |= set(rec["consecutive"])
        t, bad = time.time(), []
        for n in sorted(pick):
            b = pull_bytes(a.serial, f"{seg}/{n}")
            open(os.path.join(out, "sample", n), "wb").write(b)
            if hashlib.sha256(b).hexdigest() != want.get(n):
                bad.append(n)
        rec["sample"] = {"count": len(pick), "seconds": round(time.time() - t, 1), "sha_mismatch": bad}
        if a.keep_all:
            step("整段拉回")
            t, tar = time.time(), os.path.join(out, "segment.tar")
            with open(tar, "wb") as f:
                subprocess.run(["adb", "-s", a.serial, "exec-out", f"su -c {shlex.quote(f'tar -C {seg} -cf - .')}"], stdout=f, timeout=3600, pass_fds=lock_fds())
            took = time.time() - t
            ok = bad = 0
            with tarfile.open(tar) as tf:
                for m in tf:
                    if m.name.endswith(".raw"):
                        d = hashlib.sha256(tf.extractfile(m).read()).hexdigest()
                        if d == want.get(os.path.basename(m.name)):
                            ok += 1
                        else:
                            bad += 1
            rec["pulled_all"] = {"bytes": os.path.getsize(tar), "seconds": round(took, 1), "raw_sha_ok": ok, "raw_sha_bad": bad}
        step("读回完成")
    except Exception as e:   # 走不通也要收尾, 写清卡在哪
        rec["error"] = f"{type(e).__name__}: {e}"
        print(rec["error"], file=sys.stderr, flush=True)
    finally:
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
        layer_ok, info = ab.layer_absent(dev, a.pkg)
        left = re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M)
        dir_left = dev.su(f"ls {base} 2>/dev/null").strip()
        cfg = dev.shell(f"cmd game list-configs {a.pkg}").strip()
        rec["restore"] = {"session_exit": proc.returncode if proc else None, "session_stop": (st or {}).get("status"), "layer_removed": layer_ok, "layer_info": info,
                          "knobs_left": left, "output_dir_left": dir_left, "game_running": bool(dev.shell(f"pidof {a.pkg}").strip()),
                          "game_mode": ab.current_mode(dev, a.pkg), "game_configs": cfg[-200:], "free": dev.shell("df -k /data | tail -1").strip()}
        rec["temps"].append(temps(dev, "after_restore"))
        rec["restore"]["ok"] = bool(layer_ok and not left and not dir_left and not rec["restore"]["game_running"] and rec["restore"]["game_mode"] == orig_mode)
        step("收尾完成")
    print(json.dumps({k: rec.get(k) for k in ("arm", "segment_finished", "game_alive_after", "raw_files", "fps_no_capture", "fps_during_sf", "error")}, ensure_ascii=False))
    print(json.dumps(rec["restore"], ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if rec.get("segment_finished") else 1


if __name__ == "__main__":
    sys.exit(main())
