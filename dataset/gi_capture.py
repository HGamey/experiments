#!/usr/bin/env python3
"""gi_capture.py — 原神连续序列采集: 每档分辨率一段, 同一帧的带 UI 画面 + 灰层拷出的附件 (不带 UI、深度等), 转成数据集文件。

    python3 -I dataset/gi_capture.py <serial> --scene <场景标签> --so <层> --att <规则> \\
        [--tiers 1920x1080,1280x720,960x540] [--frames 300] [--motion run|turn|static] [--walk-s 0] [--angle 0] [--pull all|meta]
平台上 (--so 是本机文件, pf-node 自动上传):
    pf-node --experiment <提交> --research <研究ID> --resource device:<serial> --detach \\
      experiment dataset/gi_capture.py <serial> --scene beach_day --so <层> --att noui=ac24d328:0,depth=0ed8a53d:3

每档: wm size <短边>x<长边> (native 不设) → framedump=1 + framedump_att → gray-layer session --so 启动游戏 →
进世界 (先试 phonefarm onboard --no-cold, 不行再用 gi_enter 看血条) → 第一档可先走 --walk-s 秒换地方 →
按 --motion 给输入并开一段 --frames 帧 → 停会话、停游戏 → 整段 tar 流到节点 → fd_convert 转 PNG、逐帧检查 → 删手机上的段。
附件规则来自 frame-audit (dataset/fa_probe.py) 报的 pass sig; 原神 7.2: noui=ac24d328:0 (tonemap 输出, 不带 UI),
depth=0ed8a53d:3 (G-buffer 深度 D24S8)。
产物 ($PF_JOB_OUT/gi_<场景>_<时间>/<档>/): session.json summary.json index.csv checks.csv ui/ noui/ depth16/ depth24/ shot.png
  report.json: 每档尺寸、帧数、完整帧数、各附件格式和尺寸、耗时 (采集 / 读回 / 转换, 各自折合每百帧)、温度、restore。
收尾 (含出错): 停会话摘层、清 debug.knobs.*、wm size reset、强停游戏、删手机输出目录、按 HOME, 逐项回读。
--pull meta 用来测长序列 (需求第 7 项): 只拉 session/summary/index 和首、中、尾三帧, 其余在手机上删掉;
  report.json 的 seq 记丢帧次数、最长连续段、开段前后手机剩余空间。
--motion nav --segments K: 每档一次会话里连采 K 段, 段前用 gi_nav.Navigator 跑 --gap-s 秒 (多指: 摇杆前推 + 间歇冲刺 + 镜头左右摆,
  按小地图箭头修方向、认出攀爬就落下掉头、卡住就跳、按大地图边界挑方向; 太暗先等天亮), 段内照跑不截图;
  每段读回到 <档>/s<k>/, 转换时用 fd_motion 量「在跑、在变」, 结论在 report.json 的 tiers[i].segments[k].convert.motion。
  下一档接着上一档最后的方向跑 (游戏重启后角色还在原地)。
退出码: 0 每档都采完, 1 有档没采完, 2 收尾没通过。
"""
import argparse
import hashlib
import signal
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    label = os.environ.get("DEVLOCK_LABEL", "")
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"gicapture@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
import selfsr_ab as ab  # noqa: E402
import gi_enter  # noqa: E402
import fd_convert  # noqa: E402
from cruise import lock_fds  # noqa: E402
from fd_att_probe import Mover  # noqa: E402
import gi_nav  # noqa: E402
from framedump_probe import temps  # noqa: E402


def pull_tar(serial, remote_dir, local_dir):
    os.makedirs(local_dir, exist_ok=True)
    p = subprocess.Popen(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote(f'tar -C {remote_dir} -cf - .')}"],
                         stdout=subprocess.PIPE, pass_fds=lock_fds())
    t = subprocess.run(["tar", "-C", local_dir, "-xf", "-"], stdin=p.stdout, timeout=7200)
    p.wait(timeout=120)
    return t.returncode == 0 and p.returncode == 0


def pull_some(serial, remote_dir, local_dir, names):
    """只拉列出的文件 (相对段目录)。"""
    os.makedirs(local_dir, exist_ok=True)
    lst = " ".join(shlex.quote(n) for n in names)   # 不存在的 (如没有 refused.json) 先滤掉, 否则 tar 返回非零
    p = subprocess.Popen(["adb", "-s", serial, "exec-out",
                          f"su -c {shlex.quote(f'cd {remote_dir} && tar -cf - $(ls -d {lst} 2>/dev/null)')}"],
                         stdout=subprocess.PIPE, pass_fds=lock_fds())
    t = subprocess.run(["tar", "-C", local_dir, "-xf", "-"], stdin=p.stdout, timeout=1800)
    p.wait(timeout=120)
    return t.returncode == 0


def seq_stats(index_csv):
    """index.csv -> 抓到的帧数、丢帧次数 (gap_before>0 的行)、丢掉的帧总数、最长连续段 (帧)。"""
    import csv
    rows = list(csv.DictReader(open(index_csv)))
    got = [r for r in rows if r["file"].endswith(".raw")]
    gaps = [int(r["gap_before"] or 0) for r in got[1:]]
    run, best = 1 if got else 0, 1 if got else 0
    for g in gaps:
        run = 1 if g > 0 else run + 1
        best = max(best, run)
    att_cols = [c for c in (rows[0].keys() if rows else []) if c not in ("seq", "frame", "t_ns", "dt_ns", "gap_before", "file", "write_us")]
    return {"frames": len(got), "gap_events": sum(1 for g in gaps if g > 0), "frames_missed": sum(gaps), "longest_run": best,
            "att_missing": {c: sum(1 for r in got if not r[c].endswith(".raw")) for c in att_cols}}


def data_free(dev):
    m = re.search(r"\s(\d+)\s+\d+%", dev.su("df -k /data | tail -1"))
    return int(m.group(1)) * 1024 if m else None


def enter(a, out, step):
    """先用平台 onboard (Linux 中心上 OCR 修好以后能用), 失败再看血条。"""
    code, err = ab.onboard(SimpleNamespace(phonefarm=PF, pkg=a.pkg, serial=a.serial), out, "enter", False)
    if code == 0 and gi_enter.in_world(gi_enter.raw_shot(a.serial)):
        return {"how": "onboard", "ok": True}
    step(f"onboard 没进去 (exit {code}: {err[-160:]}), 改用血条判据")
    ok, secs, taps = gi_enter.enter_world(a.serial, a.pkg, cold=False, timeout=600, log=step, fail_png=os.path.join(out, "enter_fail.png"))
    return {"how": "health_bar", "ok": ok, "seconds": secs, "taps": taps, "onboard_exit": code}


def logical_landscape(dev):
    """当前逻辑横屏尺寸: 有 wm size 覆盖就用覆盖值, 否则物理尺寸; 长边当宽。"""
    o = dev.shell("wm size")
    m = re.search(r"Override size: (\d+)x(\d+)", o) or re.search(r"Physical size: (\d+)x(\d+)", o)
    a, b = int(m.group(1)), int(m.group(2))
    return max(a, b), min(a, b)


def trigger(dev, base, lab, frames):
    """开一段 (改 framedump_go), 等它的 summary.json / refused.json。"""
    seg = f"{base}/{lab}"
    t0 = time.time()
    dev.shell(f"setprop debug.knobs.framedump_go {lab}")
    done = False
    for _ in range(frames // 20 + 180):
        names = dev.su(f"ls {seg}/summary.json {seg}/refused.json 2>/dev/null")
        if "summary.json" in names or "refused.json" in names:
            done = "summary.json" in names
            break
        time.sleep(1)
    return {"finished": done, "capture_wall_s": round(time.time() - t0, 2)}


def finish(a, dev, seg, sdir, sg, step, tag):
    """一段: 读回 (all: 整段 tar; meta: 只要元数据和首中尾三帧) → 转换 + 逐帧检查 + 运动量 → 删手机上的段。结果写进 sg。"""
    t0 = time.time()
    if a.pull == "all":
        ok = pull_tar(a.serial, seg, sdir)
    else:
        ok = pull_some(a.serial, seg, sdir, ["session.json", "summary.json", "index.csv", "refused.json"])
        import csv
        got = [r["frame"].zfill(10) for r in csv.DictReader(open(os.path.join(sdir, "index.csv"))) if r["file"].endswith(".raw")]
        pick = sorted({got[0], got[len(got) // 2], got[-1]}) if got else []
        names = dev.su(f"ls {seg}").split()
        ok = pull_some(a.serial, seg, sdir, [n for n in names if any(n.startswith(f"f{fr}") for fr in pick)]) and ok
    sg["pull_s"] = round(time.time() - t0, 1)
    sg["pull_bytes"] = sum(os.path.getsize(os.path.join(sdir, f)) for f in os.listdir(sdir) if f.endswith(".raw"))
    sg["pull_ok"] = ok
    dev.su(f"rm -rf {seg}")
    summ = json.load(open(os.path.join(sdir, "summary.json")))
    sess = json.load(open(os.path.join(sdir, "session.json")))
    sg["present"] = {"w": sess["w"], "h": sess["h"], "vk_format": sess["vk_format"]}
    sg["summary"] = {k: summ.get(k) for k in ("captured", "dropped_slot_busy", "skipped_other", "span_s", "bytes", "write_us_max")}
    sg["att"] = summ.get("att")
    sg["seq"] = seq_stats(os.path.join(sdir, "index.csv"))
    t0 = time.time()
    sg["convert"] = fd_convert.convert_segment(sdir, a.workers, delete_raw=not a.keep_raw)
    sg["convert_s"] = round(time.time() - t0, 1)
    k = max(1, sg["convert"]["frames"]) / 100
    kc = max(1, summ.get("captured") or 0) / 100
    sg["per_100_frames_s"] = {"capture": round(summ.get("span_s", 0) / kc, 2),
                              "pull": round(sg["pull_s"] / k, 1) if a.pull == "all" else None,
                              "convert": round(sg["convert_s"] / k, 1) if a.pull == "all" else None}
    sg["disk_bytes"] = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(sdir) for f in fs)
    mo = sg["convert"].get("motion") or {}
    step(f"{tag}: 送显 {sess['w']}x{sess['h']}, 采 {summ.get('captured')} 帧, 完整 {sg['convert']['complete']}, "
         f"读回 {sg['pull_s']} 秒 / {sg['pull_bytes'] / 1e9:.2f} GB, 转换 {sg['convert_s']} 秒; "
         f"运动 {'达标' if mo.get('ok') else '不达标 ' + str(mo.get('why'))} (run_px {mo.get('run_px_med')}, 中央差 {mo.get('center_lag30_med')})")


def main():
    # 平台取消任务先发 SIGINT 再升级: 第一次收到就忽略后续信号并转成 SystemExit, 让每档的 finally 和最后的收尾跑完
    def stop(sig, _frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + sig)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--scene", required=True)
    ap.add_argument("--so", help="自己编的层 (本机文件, pf-node 自动上传); 不给就用平台自带的层")
    ap.add_argument("--att", required=True)
    ap.add_argument("--tiers", default="1920x1080,1280x720,960x540")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--motion", default="run", choices=("run", "turn", "static", "nav"),
                    help="nav: gi_nav 多指导航 (摇杆 + 冲刺 + 边跑边转镜头, 按小地图修方向), 段间修正")
    ap.add_argument("--segments", type=int, default=1, help="每档连采几段 (同一会话, 每段 --frames 帧; nav 时段间跑 --gap-s 秒)")
    ap.add_argument("--gap-s", type=float, default=8, help="nav: 每段之前跑多少秒 (修方向、换地方)")
    ap.add_argument("--heading", type=float, default=160, help="nav: 起始目标方向, 度 (0 北 90 东)")
    ap.add_argument("--min-luma", type=float, default=80, help="nav: 画面中央平均灰度低于它就停下等天亮, 最多 12 分钟 (0 不等)")
    ap.add_argument("--walk-s", type=float, default=0, help="第一档开采前推着摇杆走这么多秒 (换地方)")
    ap.add_argument("--angle", type=float, default=0, help="run 和 --walk-s 的摇杆方向, 度: 0 正前, 180 正后, 90 向右")
    ap.add_argument("--slots", type=int, default=16)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--pull", default="all", choices=("all", "meta"))
    a = ap.parse_args()
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"gi_{a.scene}_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    dev = ab.Dev(a.serial)
    rec = {"scene": a.scene, "pkg": a.pkg, "att": a.att, "frames": a.frames, "motion": a.motion, "tiers": [], "steps": [],
           "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"), "so_sha256": hashlib.sha256(open(a.so, "rb").read()).hexdigest() if a.so else None}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)

    m = re.search(r"versionName=(\S+)", dev.shell(f"dumpsys package {a.pkg} | grep versionName"))
    rec["game_version"] = m.group(1) if m else None
    rec["device"] = {"model": dev.prop("ro.product.model"), "brand": dev.prop("ro.product.brand"), "android": dev.prop("ro.build.version.release")}
    rec["before"] = {"wm_size": dev.shell("wm size").strip(), "debug.vulkan.layers": dev.prop("debug.vulkan.layers"),
                     "knobs": dev.shell("getprop | grep '^\\[debug.knobs' | grep -v ': \\[\\]'").strip(),
                     "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1]}
    if rec["before"]["debug.vulkan.layers"] or rec["before"]["knobs"] or "Override" in rec["before"]["wm_size"]:
        step(f"手机上已有层/knobs/wm size 覆盖, 不动: {rec['before']}")
        return 1
    base = f"/data/data/{a.pkg}/files/i3dump"
    try:
        for n, tier in enumerate(a.tiers.split(",")):
            tdir = os.path.join(out, tier)
            os.makedirs(tdir)
            t = {"tier": tier}
            rec["tiers"].append(t)
            proc, sdir = None, os.path.join(tdir, "session")
            label = f"{a.scene}_{tier}_{int(time.time())}"
            seg = f"{base}/{label}"
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
                size = logical_landscape(dev)
                t["input_size"] = list(size)
                props = {"debug.knobs.framedump": "1", "debug.knobs.framedump_n": str(a.frames), "debug.knobs.framedump_att": a.att,
                         "debug.knobs.framedump_slots": str(a.slots)}
                dev.shell("; ".join(f"setprop {k} {shlex.quote(v)}" for k, v in props.items()))
                proc = subprocess.Popen([PF, "gray-layer", "session", "--mode", "probe", "--package", a.pkg, "--serial", a.serial,
                                         "--out", sdir, "--max-seconds", str(int(max(1800, a.frames / 20 + 1200)))]
                                    + (["--so", a.so] if a.so else []),
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
                t["enter"] = enter(a, tdir, step)
                if not t["enter"]["ok"]:
                    raise RuntimeError("没进世界")
                time.sleep(10)
                if n == 0 and a.walk_s > 0 and a.motion != "nav":
                    with Mover(a.serial, "run", size, a.angle):
                        time.sleep(a.walk_s)
                    time.sleep(2)
                gi_enter.save_png(a.serial, os.path.join(tdir, "shot.png"))
                t["temps_before"] = temps(dev, "before")
                t["data_free_before"] = data_free(dev)
                labels = [label] if a.segments == 1 else [f"{label}_s{k + 1}" for k in range(a.segments)]
                t["segments"] = []
                if a.motion == "nav":
                    touch = gi_nav.Touch(a.serial)
                    nav = gi_nav.Navigator(a.serial, touch, size[0] / size[1], log=step, heading=a.heading, min_luma=a.min_luma)
                    try:
                        with nav:
                            for lab in labels:
                                sg = {"label": lab, "gap": nav.gap(a.gap_s)}
                                nav.segment_start()          # 段内边跑边左右摆镜头
                                sg.update(trigger(dev, base, lab, a.frames))
                                nav.segment_end()
                                t["segments"].append(sg)
                                step(f"{tier} {lab}: 采完={sg['finished']}, 目标方向 {nav.target:.0f}")
                        a.heading = nav.target          # 下一档接着这个方向跑
                    finally:
                        t["nav"] = {"hist": nav.hist, "plans": nav.plans, "runner": nav.runner.log}
                        touch.close()
                else:
                    with Mover(a.serial, a.motion, size, a.angle):
                        time.sleep(2.0)
                        for lab in labels:
                            t["segments"].append({"label": lab, **trigger(dev, base, lab, a.frames)})
                t["temps_after"] = temps(dev, "after")
                t["data_free_after"] = data_free(dev)
                gi_enter.save_png(a.serial, os.path.join(tdir, "shot_after.png"))
                t["segment_finished"] = bool(t["segments"]) and all(sg["finished"] for sg in t["segments"])
                ab.session_action(PF, sdir, "stop")   # 读回时游戏不用开着, 手机少发热
                proc.wait(timeout=300)
                dev.shell(f"am force-stop {a.pkg}")
                for k, sg in enumerate(t["segments"]):
                    sd = tdir if len(labels) == 1 else os.path.join(tdir, f"s{k + 1}")
                    try:
                        finish(a, dev, f"{base}/{sg['label']}", sd, sg, step, tier if len(labels) == 1 else f"{tier}/s{k + 1}")
                    except Exception as e:
                        sg["error"] = f"{type(e).__name__}: {e}"
                        step(f"{tier}/s{k + 1}: {sg['error']}")
                if len(labels) == 1:   # 单段: 和以前一样把段的结果放在档这一层
                    t.update({k2: v for k2, v in t["segments"][0].items() if k2 not in ("label", "finished")})
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
                for k2 in re.findall(r"^\[(debug\.knobs\.[^\]]+)\]: \[.+\]$", dev.shell("getprop"), re.M):
                    dev.shell(f"setprop {k2} ''")
    finally:
        dev.shell("wm size reset")
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
    print(json.dumps([{k: t.get(k) for k in ("tier", "present", "segment_finished", "per_100_frames_s", "error")} for t in rec["tiers"]], ensure_ascii=False))
    print(json.dumps(rec["restore"], ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if all(t.get("segment_finished") for t in rec["tiers"]) else 1


if __name__ == "__main__":
    sys.exit(main())
