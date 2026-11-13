#!/usr/bin/env python3
"""rd_capture.py — 在商业包游戏里用 RenderDoc 1.46 抓帧, 不用 qrenderdoc: 层手动放进游戏库目录, 手机上的 rdtool 触发抓帧并回放导出。

    python3 -I dataset/rd_capture.py <serial> --rdtool <rdtool> --rdlib <libVkLayer_GLES_RenderDoc.so> [--frames 2] [--pkg 包名]
平台上 (两个文件是本机路径, pf-node 自动上传):
    pf-node --experiment <提交> --research <研究ID> --resource device:<serial> --detach \\
      experiment dataset/rd_capture.py <serial> --rdtool rd/rdtool --rdlib rd/libVkLayer_GLES_RenderDoc.so

rdtool 和库的来历见 dataset/rdtool/README.md (rdtool.cpp 用 NDK r28c 编译, 库从 RenderDoc 1.46 的
plugins/android/org.renderdoc.renderdoccmd.arm64.apk 里取)。手机要 root。

步骤: 查 debug.vulkan.layers 为空 → 停游戏, 把层放进游戏的 lib/arm64 (system:system 755, apk_data_file) →
setprop debug.vulkan.layers VK_LAYER_RENDERDOC_Capture → gi_enter 冷启动进世界 (看血条, 不靠 OCR) → 核对 /proc/<pid>/maps 里有层,
马上清空属性 → 推着摇杆走、转视角, rdtool cap 连续抓 --frames 帧 → 停游戏、摘层 →
手机上 rdtool info (纹理、动作、render pass、创建参数) 和 rdtool passes (每个 pass 的输出与深度预览) → 拉回节点。
.rdc 拉回节点一份 (--no-pull-rdc 不拉), 手机上留在 /data/local/tmp/rd/ 给后续 rdtool export 用 (--clean 删掉)。
产物 ($PF_JOB_OUT/rd_<时间>/): report.json, shot_*.png, cap<k>.rdc, cap<k>/info.json, cap<k>/passes/{passes.json,*.jpg}
收尾 (含出错): 强停游戏、删库目录里的层、清 debug.vulkan.layers 和 debug.rdoc.*, 逐项回读写进 report.json 的 restore。
退出码: 0 抓到并导出, 1 没抓到或导出失败, 2 收尾没通过。
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):   # 整个脚本在同一把设备锁下; onboard 继承这把锁
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    label = os.environ.get("DEVLOCK_LABEL", "")
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"renderdoc@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]   # -I 不带脚本目录
import selfsr_ab as ab  # noqa: E402
from cruise import CAM_X, CAM_Y, STICK, lock_fds  # noqa: E402
import gi_enter  # noqa: E402

LAYER = "libVkLayer_GLES_RenderDoc.so"
RD = "/data/local/tmp/rd"
W, H = 2688, 1216   # 横屏输入坐标 (红魔); 别的手机用 --screen


def adb(serial, *args, timeout=600, **kw):
    return subprocess.run(["adb", "-s", serial, *args], timeout=timeout, pass_fds=lock_fds(), **kw)


def pull_tar(serial, remote_dir, local_dir):
    """手机目录经 tar 流到节点 (root 读), 返回字节数。"""
    os.makedirs(local_dir, exist_ok=True)
    p = subprocess.Popen(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote(f'tar -C {remote_dir} -cf - .')}"],
                         stdout=subprocess.PIPE, pass_fds=lock_fds())
    t = subprocess.run(["tar", "-C", local_dir, "-xf", "-"], stdin=p.stdout, timeout=3600)
    p.wait(timeout=60)
    return t.returncode == 0 and p.returncode == 0


def pull_file(serial, remote, local):
    h = hashlib.sha256()
    with open(local, "wb") as f:
        p = subprocess.Popen(["adb", "-s", serial, "exec-out", f"su -c {shlex.quote('cat ' + shlex.quote(remote))}"],
                             stdout=subprocess.PIPE, pass_fds=lock_fds())
        while True:
            b = p.stdout.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
        p.wait(timeout=60)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--rdtool", required=True)
    ap.add_argument("--rdlib", required=True)
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--frames", type=int, default=2)
    ap.add_argument("--minw", type=int, default=256, help="passes 只给宽度不小于这个的目标出预览")
    ap.add_argument("--screen", default=f"{W}x{H}")
    ap.add_argument("--no-pull-rdc", action="store_true")
    ap.add_argument("--clean", action="store_true", help="结束时删掉手机上的 /data/local/tmp/rd")
    ap.add_argument("--settle", type=int, default=20, help="进世界后等多少秒再抓")
    ap.add_argument("--enter-timeout", type=int, default=900, help="冷启动到进世界最多等多少秒 (含资源下载)")
    a = ap.parse_args()
    sw, sh = (int(x) for x in a.screen.split("x"))
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"rd_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    dev = ab.Dev(a.serial)
    rec = {"pkg": a.pkg, "frames": a.frames, "steps": [], "captures": [], "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT")}

    def step(msg):
        rec["steps"].append({"t": round(time.time(), 1), "msg": msg})
        print(msg, flush=True)
        json.dump(rec, open(os.path.join(out, "report.json"), "w"), ensure_ascii=False, indent=1)

    def shot(name):
        png = adb(a.serial, "exec-out", "screencap -p", capture_output=True, timeout=60).stdout
        open(os.path.join(out, name), "wb").write(png)
        return name

    for f in (a.rdtool, a.rdlib):
        rec.setdefault("inputs", {})[os.path.basename(f)] = hashlib.sha256(open(f, "rb").read()).hexdigest()
    m = re.search(r"versionName=(\S+)", dev.shell(f"dumpsys package {a.pkg} | grep versionName"))
    rec["game_version"] = m.group(1) if m else None
    rec["device"] = {"model": dev.prop("ro.product.model"), "gpu": dev.su("cat /sys/class/kgsl/kgsl-3d0/gpu_model 2>/dev/null").strip()}
    pre = dev.prop("debug.vulkan.layers")
    knobs = dev.shell("getprop | grep '^\\[debug.knobs' | grep -v ': \\[\\]'").strip()
    rec["before"] = {"debug.vulkan.layers": pre, "knobs": knobs, "wakefulness": (re.search(r"mWakefulness=(\w+)", dev.shell("dumpsys power")) or [None, None])[1]}
    if pre or knobs:
        step(f"手机上已有别的层或 knobs 属性, 不动: layers=[{pre}] knobs=[{knobs}]")
        return 1
    libdir = dev.shell(f"dumpsys package {a.pkg} | sed -n 's/.*legacyNativeLibraryDir=//p'").strip() + "/arm64"
    rec["libdir"] = libdir
    ok = False
    try:
        dev.shell("input keyevent KEYCODE_WAKEUP; am kill-all")
        dev.shell(f"am force-stop {a.pkg}")
        dev.su(f"mkdir -p {RD}/out && rm -rf {RD}/out/*")
        for f, name in ((a.rdtool, "rdtool"), (a.rdlib, LAYER)):
            adb(a.serial, "push", f, f"/data/local/tmp/{name}", capture_output=True)
            dev.su(f"mv /data/local/tmp/{name} {RD}/{name} && chmod 755 {RD}/{name}")
        dev.su(f"cp {RD}/{LAYER} {libdir}/ && chmod 755 {libdir}/{LAYER} && chown system:system {libdir}/{LAYER} && "
               f"chcon u:object_r:apk_data_file:s0 {libdir}/{LAYER}")
        rec["layer_ls"] = dev.su(f"ls -lZ {libdir}/{LAYER}").strip()
        dev.su("setprop debug.vulkan.layers VK_LAYER_RENDERDOC_Capture; setprop debug.rdoc.IGNORE_LAYERS 0")
        step(f"层已放好: {rec['layer_ls']}")
        okw, secs, taps = gi_enter.enter_world(a.serial, a.pkg, cold=True, timeout=a.enter_timeout, log=step,
                                               fail_png=os.path.join(out, "shot_enter_fail.png"))
        rec["enter"] = {"ok": okw, "seconds": secs, "taps": taps}
        pid = dev.shell(f"pidof {a.pkg}").strip().split()
        pid = pid[0] if pid else ""
        maps = dev.su(f"grep -c {LAYER} /proc/{pid}/maps").strip() if pid else "0"
        dev.su("setprop debug.vulkan.layers ''")   # 层已经在游戏进程里, 关掉全局属性免得别的应用也加载
        rec.update(game_pid=pid, layer_maps=maps)
        shot("shot_enter.png")
        if not okw:
            raise RuntimeError(f"{secs} 秒内没进世界")
        if not pid or maps in ("", "0"):
            raise RuntimeError(f"层没加载上 (pid={pid}, maps={maps})")
        step(f"已进世界, pid {pid}, 层映射 {maps} 段; 等 {a.settle} 秒")
        time.sleep(a.settle)
        shot("shot_before.png")
        # 推着摇杆走 + 慢慢转视角, 抓的帧里相机和角色都在动 (运动矢量才看得出来)
        x0, y0 = int(STICK[0] * sw), int(STICK[1] * sh)
        dev.shell(f"input motionevent DOWN {x0} {y0}; input motionevent MOVE {x0} {y0 - int(0.13 * sh)}")
        cy, cx = int(sum(CAM_Y) / 2 * sh), int(CAM_X[0] * sw)
        turn = subprocess.Popen(["adb", "-s", a.serial, "shell", f"input swipe {cx} {cy} {cx + int(0.25 * sw)} {cy} 4000"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=lock_fds())
        time.sleep(1.5)
        t0 = time.time()
        r = subprocess.run(["adb", "-s", a.serial, "shell", f"su -c {shlex.quote(f'cd {RD} && ./rdtool cap {a.frames} 900 {a.pkg}')}"],
                           capture_output=True, text=True, timeout=1200, pass_fds=lock_fds())
        rec["cap_seconds"] = round(time.time() - t0, 1)
        dev.shell(f"input motionevent UP {x0} {y0 - int(0.13 * sh)}")
        turn.wait(timeout=60)
        rec["cap_stdout"], rec["cap_stderr"], rec["cap_exit"] = r.stdout[-4000:], r.stderr[-2000:], r.returncode
        caps = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{\"capture_id\"")]
        shot("shot_after.png")
        step(f"rdtool cap 退出 {r.returncode}, 抓到 {len(caps)} 帧, 用时 {rec['cap_seconds']} 秒")
        for k, c in enumerate(caps):
            dst = f"{RD}/cap{k}.rdc"
            dev.su(f"cp {shlex.quote(c['path'])} {dst}")
            c["phone_path"] = dst
            c["sha256"] = dev.su(f"sha256sum {dst}", timeout=600).split()[0]
            c["size"] = int(dev.su(f"stat -c %s {dst}").strip() or 0)
            rec["captures"].append(c)
        if not caps:
            raise RuntimeError("没抓到帧")
    except Exception as e:   # 走不通也要收尾, 写清卡在哪
        rec["error"] = f"{type(e).__name__}: {e}"
        print(rec["error"], file=sys.stderr, flush=True)
    finally:
        dev.shell(f"am force-stop {a.pkg}")
        dev.su(f"rm -f {libdir}/{LAYER}")
        dev.su("setprop debug.vulkan.layers ''; setprop debug.rdoc.IGNORE_LAYERS ''")
        step("游戏已停, 层已摘")

    # 回放导出: 游戏已停, 手机内存够 (回放一帧约 1.2 GB)
    for k, c in enumerate(rec["captures"]):
        d = f"{RD}/out/cap{k}"
        dev.su(f"mkdir -p {d}/passes")
        t = time.time()
        r = dev.su(f"cd {RD} && ./rdtool info cap{k}.rdc {d}/info.json 2>&1 | tail -5", timeout=1800)
        c["info"] = {"seconds": round(time.time() - t, 1), "out": r.strip()[-500:]}
        if k == 0:
            t = time.time()
            r = dev.su(f"cd {RD} && ./rdtool passes cap{k}.rdc {d}/passes {a.minw} 2>&1 | tail -3", timeout=3600)
            c["passes"] = {"seconds": round(time.time() - t, 1), "out": r.strip()[-500:]}
        c["pulled"] = pull_tar(a.serial, d, os.path.join(out, f"cap{k}"))
        if not a.no_pull_rdc:
            t = time.time()
            got = pull_file(a.serial, c["phone_path"], os.path.join(out, f"cap{k}.rdc"))
            c["rdc_pulled"] = {"sha_ok": got == c["sha256"], "seconds": round(time.time() - t, 1)}
        step(f"cap{k} 导出完成: {c.get('info')} {c.get('passes', '')}")
        ok = bool(c["pulled"])
    if a.clean:
        dev.su(f"rm -rf {RD}")
    else:
        dev.su(f"rm -rf {RD}/out")
    for c in rec["captures"]:   # 游戏自己目录里的原件
        dev.su(f"rm -f {shlex.quote(c['path'])}")
    if rec["before"]["wakefulness"] != "Awake":
        dev.shell("input keyevent KEYCODE_SLEEP")
    left = dev.su(f"ls {libdir} | grep -c {LAYER}").strip()
    rec["restore"] = {"layer_in_libdir": left, "debug.vulkan.layers": dev.prop("debug.vulkan.layers"),
                      "game_running": bool(dev.shell(f"pidof {a.pkg}").strip()),
                      "rd_dir_left": dev.su(f"ls {RD} 2>/dev/null").split()}
    rec["restore"]["ok"] = left in ("0", "") and rec["restore"]["debug.vulkan.layers"] == "" and not rec["restore"]["game_running"]
    step("收尾完成")
    print(json.dumps({"captures": [{k: c.get(k) for k in ("frame", "size", "sha256")} for c in rec["captures"]],
                      "error": rec.get("error"), "restore": rec["restore"]}, ensure_ascii=False))
    if not rec["restore"]["ok"]:
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
