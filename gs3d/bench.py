#!/usr/bin/env python3
"""3DGS 手机浏览器基准：在一台手机的系统浏览器里跑开源 WebGL 3DGS 查看器，测帧率、加载时间、温度和功耗估算。

    python3 gs3d/bench.py <serial> [--scenes nike,train,truck@500000,...] [--dpr 1,native] [--caps-only]

做法 (整段在 phonefarm lease 设备锁下):
  1. 宿主准备网页: 按固定提交取 antimatter15/splat 的 index.html / main.js，打最小补丁 (固定渲染倍率、
     帧/排序钩子、相机轨迹从加载完成时刻起算)；场景取公开 .splat (cakewalk/splat-data)。
     `name@N` 取该场景文件的前 N 个高斯 (.splat 按 尺度×不透明度 降序存放，截前缀即粗 LOD)。
  2. 宿主起 HTTP 服务，adb reverse 到手机 127.0.0.1，浏览器用 VIEW intent 打开 (同一 application_id 复用一个标签)。
  3. 每个场景: 冷却 → 打开页面 → 页面报告加载完成 → 预热 → 统计窗口内 rAF 间隔；设备侧 sampler.sh 每秒读电流/电压/
     GPU 频率与温度。相机走查看器自带的 carousel 轨迹 (5 s 时间常数的正弦摆动)，时间从加载完成算起，次次相同。
  4. 结束还原: 关浏览器、撤 adb reverse、删推送文件、还原亮度设置；记录在 restore.json。
功耗: 本机没有可用的停充节点 (见 README 段落/报告)，按 整机≈USB 输入功率−电池充入功率 估算，另给相对空白页的增量。
"""
import argparse
import hashlib
import http.server
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("PF_ROOT") or ""
LOCK_PREFIX = os.environ["DEVLOCK_LABEL"] + "-" if os.environ.get("DEVLOCK_LABEL") else ""
VIEWER_COMMIT = "ba182b51b7c2ad5738cdd6741cd63336d27470fb"
VIEWER_RAW = f"https://raw.githubusercontent.com/antimatter15/splat/{VIEWER_COMMIT}/"
DEV_DIR = "/data/local/tmp/gs3d"
ROW = 32  # .splat 每个高斯 32 字节


def adb_bin():
    for c in (shutil.which("adb"), "/opt/homebrew/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")):
        if c and os.path.exists(c):
            return c
    sys.exit("找不到 adb")


ADB = adb_bin()


def lock_fds():
    """phonefarm lease 交给子进程的锁 fd；子进程带上它们，锁随整段任务保持。"""
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


class Dev:
    def __init__(self, serial, log):
        self.s, self.log = serial, log

    def run(self, *args, timeout=60, check=False):
        p = subprocess.run([ADB, "-s", self.s, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, pass_fds=lock_fds())
        if check and p.returncode:
            raise RuntimeError(f"adb {' '.join(args)[:120]} 失败: {p.stderr.strip()[-300:]}")
        return p.stdout

    def sh(self, cmd, timeout=60):
        return self.run("shell", cmd, timeout=timeout)

    def su(self, cmd, timeout=60):
        # su -c 只接一个参数: 先写成设备侧脚本再执行，避免引号被 adb shell 拆散
        return self.run("shell", f"su -c 'sh -c \"{cmd}\"' </dev/null", timeout=timeout)


VIEWER_SHA256 = {"main.js": "b63e01a96e28d88039c2df5bc7ea33cacee13dea298cc2a357d284f4e7ca81a2",
                 "index.html": "42f8ee41d24ca4b1e8a5b0d78656127952b7909235195fa3c2bd4fb30228ff92"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url, dest, log, want=None, tries=4):
    """下载到 dest；给了 want 时校验 sha256，不符就重下 (raw CDN 偶尔回错内容)。"""
    for i in range(tries):
        if os.path.exists(dest) and (not want or sha256(dest) == want):
            return
        t = time.time()
        tmp = dest + ".part"
        try:
            with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(tmp, dest)
            log(f"下载 {url} → {os.path.getsize(dest)} B, {time.time() - t:.1f}s")
        except Exception as ex:
            log(f"下载失败 {url}: {ex}")
        time.sleep(2 * i)
    if not os.path.exists(dest) or (want and sha256(dest) != want):
        raise RuntimeError(f"{url} 下载/校验失败")


def patch_viewer(src):
    """给 main.js 打最小补丁；每处都必须恰好命中一次。"""
    reps = [
        # 1) 渲染倍率: ?dpr=N 时画布 = CSS 像素 × N；原版按场景大小在 1 与 devicePixelRatio 之间自动切换
        ("const downsample =\n        splatData.length / rowLength > 500000 ? 1 : 1 / devicePixelRatio;",
         "const downsample = params.get(\"dpr\") ? 1 / (params.get(\"dpr\") === \"native\" ? devicePixelRatio : Number(params.get(\"dpr\"))) :\n        (splatData.length / rowLength > 500000 ? 1 : 1 / devicePixelRatio);"),
        # 2) 排序完成钩子
        ("gl.DYNAMIC_DRAW);\n            vertexCount = e.data.vertexCount;\n",
         "gl.DYNAMIC_DRAW);\n            vertexCount = e.data.vertexCount;\n            window.__gsb && window.__gsb.sort();\n"),
        # 3) carousel 轨迹时间从加载完成时刻起算 (未加载完时停在 t=0)
        ("            const t = Math.sin((Date.now() - start) / 5000);",
         "            const t = Math.sin(window.__gsb && window.__gsb.t0 ? (Date.now() - window.__gsb.t0) / 5000 : 0);"),
        # 4) 帧钩子
        ("        lastFrame = now;\n        requestAnimationFrame(frame);",
         "        lastFrame = now;\n        window.__gsb && window.__gsb.frame(now, vertexCount, Math.floor(splatData.length / rowLength));\n        requestAnimationFrame(frame);"),
        # 5) 数据流读完
        ("    if (!stopLoading) {\n        if (isPly(splatData)) {",
         "    window.__gsb && (window.__gsb.loadDone = true);\n    if (!stopLoading) {\n        if (isPly(splatData)) {"),
    ]
    for a, b in reps:
        n = src.count(a)
        if n != 1:
            raise RuntimeError(f"补丁锚点命中 {n} 次: {a[:60]!r}")
        src = src.replace(a, b)
    return src


def prepare_www(www, scenes, mirror, log):
    os.makedirs(os.path.join(www, "scenes"), exist_ok=True)
    fetch(VIEWER_RAW + "main.js", os.path.join(www, "main.orig.js"), log, VIEWER_SHA256["main.js"])
    fetch(VIEWER_RAW + "index.html", os.path.join(www, "index.orig.html"), log, VIEWER_SHA256["index.html"])
    with open(os.path.join(www, "main.orig.js")) as f:
        js = patch_viewer(f.read())
    with open(os.path.join(www, "main.js"), "w") as f:
        f.write(js)
    with open(os.path.join(www, "index.orig.html")) as f:
        html = f.read()
    anchor = '<script src="main.js"></script>'
    if html.count(anchor) != 1:
        raise RuntimeError("index.html 锚点不唯一")
    with open(os.path.join(www, "index.html"), "w") as f:
        f.write(html.replace(anchor, '<script src="bench.js"></script>\n\t\t' + anchor))
    for n in ("bench.js", "caps.html", "blank.html"):
        shutil.copy(os.path.join(HERE, "web", n), os.path.join(www, n))
    info = {}
    for sc in scenes:
        base, _, cap = sc.partition("@")
        if base == "blank":
            continue
        full = os.path.join(www, "scenes", base + ".splat")
        fetch(f"{mirror}/cakewalk/splat-data/resolve/main/{base}.splat", full, log)
        total = os.path.getsize(full) // ROW
        if cap:
            n = min(int(cap), total)
            out = os.path.join(www, "scenes", f"{base}_{n}.splat")
            if not os.path.exists(out):
                with open(full, "rb") as fi, open(out, "wb") as fo:
                    fo.write(fi.read(n * ROW))
            info[sc] = {"file": f"scenes/{base}_{n}.splat", "gaussians": n, "source_total": total}
        else:
            info[sc] = {"file": f"scenes/{base}.splat", "gaussians": total, "source_total": total}
    return info


class Hub:
    """收页面 POST 的事件；宿主时间戳。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.cv = threading.Condition(self.lock)

    def add(self, path, body):
        with self.cv:
            self.events.append({"t": time.time(), "path": path, "body": body})
            self.cv.notify_all()

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        with self.cv:
            while True:
                for e in self.events:
                    if pred(e):
                        return e
                left = end - time.time()
                if left <= 0:
                    return None
                self.cv.wait(min(left, 1.0))


def serve(www, port, hub):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=www, **k)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw)
            except Exception:
                body = {"raw": raw[:2000].decode(errors="replace")}
            hub.add(self.path, body)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Sampler:
    def __init__(self, dev, n, interval=1):
        self.rows = []
        self.p = subprocess.Popen([ADB, "-s", dev.s, "shell", f"su -c 'sh {DEV_DIR}/sampler.sh {n} {interval}' </dev/null"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, stdin=subprocess.DEVNULL,
                                  pass_fds=lock_fds())
        self.th = threading.Thread(target=self._read, daemon=True)
        self.th.start()

    def _read(self):
        for line in self.p.stdout:
            if not line.startswith("S "):
                continue
            row = {"t": time.time()}
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                try:
                    row[k] = float(v)
                except ValueError:
                    row[k] = v
            self.rows.append(row)

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()


def power_stats(rows):
    """整机≈USB 输入−电池充入。电池电流单位本机为 mA (正=充入)，USB 电流 uA。"""
    out = {"n": len(rows)}
    if not rows:
        return out
    pin, pb, temps, gclk, gbusy, gpu_c, soc_c = [], [], [], [], [], [], []
    for r in rows:
        try:
            pin.append(r["uv"] * r["uc"] / 1e12)
            pb.append(r["bv"] * r["bc"] / 1e9)
        except (KeyError, TypeError):
            pass
        for k, arr, sc in (("bt", temps, 10), ("gclk", gclk, 1e6), ("gbusy", gbusy, 1), ("gpu_c", gpu_c, 1000), ("soc_c", soc_c, 1000)):
            if isinstance(r.get(k), float):
                arr.append(r[k] / sc)
    m = lambda a: round(statistics.fmean(a), 3) if a else None
    out.update(usb_in_w=m(pin), batt_in_w=m(pb), sys_w_est=round(m(pin) - m(pb), 3) if pin and pb else None,
               batt_temp_c=m(temps), batt_temp_end_c=temps[-1] if temps else None,
               gpu_mhz=m(gclk), gpu_busy=m(gbusy), gpu_temp_c=m(gpu_c), gpu_temp_max_c=max(gpu_c) if gpu_c else None,
               soc_temp_c=m(soc_c), soc_temp_max_c=max(soc_c) if soc_c else None,
               usb_current_constant=(len(set(r.get("uc") for r in rows)) == 1),
               status=sorted(set(str(r.get("st")) for r in rows)))
    return out


def frame_stats(iv):
    if not iv:
        return {}
    iv = [x for x in iv if x > 0]
    s = sorted(iv)
    n = len(s)
    worst = s[-max(1, n // 100):]
    return {"frames": n, "avg_fps": round(1000 * n / sum(iv), 2), "median_fps": round(1000 / statistics.median(s), 2),
            "low1_fps": round(1000 / statistics.fmean(worst), 2), "p99_ms": round(s[min(n - 1, int(n * 0.99))], 2),
            "max_ms": round(s[-1], 2), "min_ms": round(s[0], 2)}


def screenshot(dev, path):
    with open(path, "wb") as f:
        subprocess.run([ADB, "-s", dev.s, "exec-out", "screencap", "-p"], stdout=f, timeout=30, stdin=subprocess.DEVNULL, pass_fds=lock_fds())


def snapshot_state(dev):
    g = lambda ns, k: dev.sh(f"settings get {ns} {k}").strip()
    return {
        "screen_brightness": g("system", "screen_brightness"),
        "screen_brightness_mode": g("system", "screen_brightness_mode"),
        "stay_on_while_plugged_in": g("global", "stay_on_while_plugged_in"),
        "screen_off_timeout": g("system", "screen_off_timeout"),
        "browser_pid": dev.sh(f"pidof {BROWSER}").strip(),
        "foreground": dev.sh("dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'").strip(),
        "reverse": dev.run("reverse", "--list").strip(),
        "input_current_limit": dev.su("cat /sys/class/power_supply/usb/input_current_limit").strip(),
        "screen_on": dev.sh("dumpsys power | grep -m1 -E 'mWakefulness='").strip(),
    }


BROWSER = "com.honor.browser"


def open_url(dev, url):
    dev.sh(f"am start -a android.intent.action.VIEW -d '{url}' -e com.android.browser.application_id gs3d {BROWSER}")


def main():
    global BROWSER
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--scenes", default="blank,nike,truck@100000,truck@500000,train,truck@1000000,truck")
    ap.add_argument("--dpr", default="1,native", help="渲染倍率列表: 数字或 native (= devicePixelRatio)")
    ap.add_argument("--warm", type=int, default=10)
    ap.add_argument("--win", type=int, default=60)
    ap.add_argument("--cool", type=int, default=45, help="每轮前至少静置秒数 (页面关掉、黑底)")
    ap.add_argument("--cool-gpu-c", type=float, default=0, help=">0 时冷却到 GPU 热区低于该温度 (最多再等 --cool 秒)")
    ap.add_argument("--load-timeout", type=int, default=240)
    ap.add_argument("--port", type=int, default=18573)
    ap.add_argument("--browser", default=BROWSER)
    ap.add_argument("--mirror", default="https://huggingface.co")
    ap.add_argument("--caps-only", action="store_true")
    ap.add_argument("--out", default=os.environ.get("PF_JOB_OUT") or "out")
    ap.add_argument("--phonefarm", default=os.environ.get("PHONEFARM_BIN") or os.path.join(REPO, "phonefarm"))
    a = ap.parse_args()
    BROWSER = a.browser
    if not lock_fds() and os.path.exists(a.phonefarm):
        os.execvp(a.phonefarm, [a.phonefarm, "lease", "--serial", a.serial, "--label", f"{LOCK_PREFIX}gs3d@{a.serial}", "--",
                                sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
    out = os.path.abspath(a.out)
    os.makedirs(os.path.join(out, "shots"), exist_ok=True)
    logf = open(os.path.join(out, "bench.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    dev = Dev(a.serial, log)
    scenes = [s for s in a.scenes.split(",") if s]
    www = os.path.abspath("_www")
    info = prepare_www(www, [] if a.caps_only else scenes, a.mirror, log)
    with open(os.path.join(out, "scenes.json"), "w") as f:
        json.dump(info, f, indent=1)

    orig = snapshot_state(dev)
    with open(os.path.join(out, "state_before.json"), "w") as f:
        json.dump(orig, f, indent=1)
    log(f"原状态 {json.dumps(orig, ensure_ascii=False)}")
    hub = Hub()
    srv = serve(www, a.port, hub)
    results = []
    restore = {"steps": []}
    try:
        dev.run("reverse", f"tcp:{a.port}", f"tcp:{a.port}", check=True)
        dev.sh(f"mkdir -p {DEV_DIR}")
        dev.run("push", os.path.join(HERE, "sampler.sh"), f"{DEV_DIR}/sampler.sh", check=True)
        dev.sh("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        # 亮度: 固定手动档、保持原亮度值 (原本即手动则不改)
        if orig["screen_brightness_mode"] != "0":
            dev.sh("settings put system screen_brightness_mode 0")
        log(dev.sh("dumpsys SurfaceFlinger | grep -m3 -iE 'refresh-rate|activeMode|mode='").strip()[:600])

        # 能力探针
        open_url(dev, f"http://127.0.0.1:{a.port}/caps.html?r={int(time.time())}")
        e = hub.wait_for(lambda e: e["path"].startswith("/caps"), 60)
        caps = e["body"] if e else {"error": "60s 内没收到 caps"}
        with open(os.path.join(out, "caps.json"), "w") as f:
            json.dump(caps, f, indent=1, ensure_ascii=False)
        log(f"caps {json.dumps(caps, ensure_ascii=False)[:1500]}")
        time.sleep(2)
        screenshot(dev, os.path.join(out, "shots", "caps.png"))
        if a.caps_only:
            return

        for dpr in a.dpr.split(","):
            for sc in scenes:
                run = f"{sc.replace('@', '_')}_dpr{dpr}_{int(time.time())}"
                # 冷却: 换成空白页静置
                open_url(dev, f"http://127.0.0.1:{a.port}/blank.html?cool={run}")
                cs = Sampler(dev, a.cool * 3 + 10)
                t_c = time.time()
                while time.time() - t_c < a.cool or (a.cool_gpu_c > 0 and cs.rows and cs.rows[-1].get("gpu_c", 0) / 1000 > a.cool_gpu_c
                                                      and time.time() - t_c < 3 * a.cool):
                    time.sleep(1)
                cs.stop()
                cool = power_stats(cs.rows[-10:])
                if sc == "blank":
                    page = f"blank.html?run={run}&bench=1&dpr={dpr}&warm={a.warm}&win={a.win}"
                else:
                    page = f"index.html?url=http://127.0.0.1:{a.port}/{info[sc]['file']}&dpr={dpr}&run={run}&warm={a.warm}&win={a.win}"
                log(f"== {run}: 冷却后 GPU {cool.get('gpu_temp_c')}°C 电池 {cool.get('batt_temp_c')}°C")
                smp = Sampler(dev, a.load_timeout + a.warm + a.win + 30)
                t_open = time.time()
                open_url(dev, f"http://127.0.0.1:{a.port}/{page}")
                loaded = hub.wait_for(lambda e, r=run: e["body"].get("run") == r and e["body"].get("ev") == "loaded", a.load_timeout)
                rec = {"run": run, "scene": sc, "dpr": dpr, "gaussians": info.get(sc, {}).get("gaussians", 0),
                       "cool": cool, "t_open": t_open}
                if not loaded:
                    hb = [e["body"] for e in hub.events if e["body"].get("run") == run][-3:]
                    rec["error"] = f"{a.load_timeout}s 内未加载完成; 最后事件 {hb}"
                    log(rec["error"])
                    screenshot(dev, os.path.join(out, "shots", run + "_fail.png"))
                    smp.stop()
                    results.append(rec)
                    continue
                rec["loaded"] = loaded["body"]
                ms = hub.wait_for(lambda e, r=run: e["body"].get("run") == r and e["body"].get("ev") == "measure_start", a.warm + 30)
                res = hub.wait_for(lambda e, r=run: e["path"].startswith("/result") and e["body"].get("run") == r, a.warm + a.win + 60)
                screenshot(dev, os.path.join(out, "shots", run + ".png"))
                smp.stop()
                if not res:
                    rec["error"] = "没收到 result"
                    log(rec["error"])
                    results.append(rec)
                    continue
                t0, t1 = (ms["t"] if ms else res["t"] - a.win), res["t"]
                body = res["body"]
                rec.update(frame=frame_stats(body.get("intervals", [])),
                           power=power_stats([r for r in smp.rows if t0 <= r["t"] <= t1]),
                           load_ms=body.get("load_ms"), first_frame_ms=body.get("first_frame_ms"),
                           download_ms=body.get("download_ms"), sorts_per_s=round(body.get("sorts_win", 0) / (body.get("win_ms") or 1) * 1000, 2),
                           gl=body.get("gl"), ua=body.get("ua"))
                with open(os.path.join(out, run + "_samples.json"), "w") as f:
                    json.dump({"samples": smp.rows, "intervals": body.get("intervals"), "t0": t0, "t1": t1}, f)
                log(f"   {json.dumps({k: rec[k] for k in ('frame', 'power', 'load_ms', 'download_ms', 'sorts_per_s', 'gl')}, ensure_ascii=False)}")
                results.append(rec)
                with open(os.path.join(out, "results.json"), "w") as f:
                    json.dump(results, f, indent=1, ensure_ascii=False)
    finally:
        # 还原
        for step, fn in (
            ("关浏览器", lambda: dev.sh(f"am force-stop {BROWSER}")),
            ("撤 reverse", lambda: dev.run("reverse", "--remove", f"tcp:{a.port}")),
            ("删推送文件", lambda: dev.sh(f"rm -rf {DEV_DIR}")),
            ("亮度模式", lambda: dev.sh(f"settings put system screen_brightness_mode {orig['screen_brightness_mode']}")
             if orig["screen_brightness_mode"] not in ("", "null") else None),
            ("亮度", lambda: dev.sh(f"settings put system screen_brightness {orig['screen_brightness']}")
             if orig["screen_brightness"] not in ("", "null") else None),
            ("回桌面", lambda: dev.sh("input keyevent KEYCODE_HOME")),
        ):
            try:
                fn()
                restore["steps"].append({"step": step, "ok": True})
            except Exception as ex:  # 继续做其余还原
                restore["steps"].append({"step": step, "ok": False, "error": str(ex)})
        srv.shutdown()
        after = snapshot_state(dev)
        restore["before"], restore["after"] = orig, after
        restore["diff"] = {k: [orig[k], after[k]] for k in orig if k not in ("foreground", "browser_pid", "screen_on") and orig[k] != after[k]}
        with open(os.path.join(out, "restore.json"), "w") as f:
            json.dump(restore, f, indent=1, ensure_ascii=False)
        log(f"还原 {json.dumps(restore['diff'], ensure_ascii=False)}")
        with open(os.path.join(out, "results.json"), "w") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
