#!/usr/bin/env python3
"""On-device computer-use VLM benchmark: Mage-VL-Cowork (4B, GGUF) on an Android phone via llama.cpp CPU.

    python3 ondevice_cua/bench.py <serial> [--scales 3,2,1] [--reps 2] [--threads 8] [--bench-threads 4,6,8]

What one job does (all device work under the phonefarm lease lock):
  1. Host: fetch pinned GGUF weights (decoder Q4_K_M + vision mmproj Q8_0) into a host cache, sha256-checked;
     fetch the pinned Mage-VL-enabled llama.cpp fork as a tarball and cross-compile llama-mtmd-cli and
     llama-bench for arm64-v8a with the NDK (CPU backend). Weights and builds stay in the cache, not in git.
  2. Phone: record state, wake, go to the launcher, push binaries + weights to /data/local/tmp/ocua.
  3. Idle baseline: sampler (battery/USB current+voltage, temps, CPU freq, MemAvailable) for --idle s.
  4. llama-bench text-only pp/tg sweep over --bench-threads.
  5. Agent steps: screenshot of the live screen (raw screencap), downscaled on the host by each factor in
     --scales (1 = native resolution, as the official harness feeds raw screenshots), then one agent step =
     original hybrid system prompt + screenshot + instruction -> reasoning + <tool_call>. Per step: vision
     encode ms, image-token prefill, total prompt prefill tok/s, decode tok/s, peak RSS, power/temps.
  6. Restore: kill only our own pids, delete pushed files, return to the original foreground/screen state;
     written to restore.json.
Power: phones stay on USB. Whole-device power is estimated as USB input power - battery charge power
(same method as gs3d/bench.py), and reported as a delta over the idle baseline.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("PF_ROOT") or ""
LOCK_PREFIX = os.environ["DEVLOCK_LABEL"] + "-" if os.environ.get("DEVLOCK_LABEL") else ""
DEV_DIR = "/data/local/tmp/ocua"

# Pinned third-party sources
GGUF_REPO = "Hansda/Mage-VL-Cowork-GGUF"
GGUF_REV = "90c433e3d9ca1e6e0e8bf4bf3928a3a55a0b4a35"  # converts microsoft/Mage-VL-Cowork @ 6fbe7f91
WEIGHTS = {
    "Mage-VL-Cowork-Q4_K_M.gguf": "f7e456c897ce3419ef68c3b658080b7d69808d12432cbd3270c2f758b8688781",
    "Mage-VL-Cowork-Q3_K_M.gguf": "5b8a57e44280a3c829e52479ff64cfcd9cde9c9262c90286c736769c19f69ad6",
    "Mage-VL-Cowork-Q8_0.gguf": "4a5651887fc0bfd741e5ce1e7c3922c5480980e13130969620b73e36b5d85592",
    "mmproj-Mage-VL-Cowork-Q8_0.gguf": "e25683c7fabad27f858c6d2ae111edf2474a90bff1abc9827b3a67bf915e652f",
    "mmproj-Mage-VL-Cowork-F16.gguf": "ecb04d0b0403ad6cc2b31eb523fde7e00b509b3f7ecbfe0e4a0c9e3780a3b9a9",
}
SYS_PROMPT = "MAGE_HYBRID_SYSTEM_PROMPT.txt"
LLAMA_REPO = "ch686/llama.cpp-locateanything-magevl"  # Mage-VL support, upstream PR ggml-org/llama.cpp#26796
LLAMA_COMMIT = "e07bdc8c8ef311561ab806e48792afb5d30b8054"
NDK_LINUX_URL = "https://dl.google.com/android/repository/android-ndk-r28c-linux.zip"
INSTRUCTION = "Open the Settings app and turn on Wi-Fi."


def adb_bin():
    for c in (shutil.which("adb"), "/opt/homebrew/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")):
        if c and os.path.exists(c):
            return c
    sys.exit("adb not found")


ADB = adb_bin()


def lock_fds():
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


class Dev:
    def __init__(self, serial):
        self.s = serial
        self.root = False

    def run(self, *args, timeout=120, check=False, binary=False):
        p = subprocess.run([ADB, "-s", self.s, *args], capture_output=True, text=not binary, timeout=timeout,
                           stdin=subprocess.DEVNULL, pass_fds=lock_fds())
        if check and p.returncode:
            err = p.stderr if not binary else p.stderr.decode(errors="replace")
            raise RuntimeError(f"adb {' '.join(args)[:160]} failed: {err.strip()[-300:]}")
        return p.stdout

    def sh(self, cmd, timeout=120):
        return self.run("shell", cmd, timeout=timeout)

    def sh_maybe_root(self, cmd, timeout=120):
        if self.root:
            return self.run("shell", f"su -c {shlex.quote(cmd)} </dev/null", timeout=timeout)
        return self.sh(cmd, timeout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def fetch(urls, dest, log, want=None, tries=6):
    """Download with HTTP range resume; verify sha256 when given. Tries each mirror in turn."""
    if os.path.exists(dest) and (not want or sha256(dest) == want):
        return
    tmp = dest + ".part"
    for i in range(tries):
        url = urls[i % len(urls)]
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        t = time.time()
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "ab" if have and r.status == 206 else "wb") as f:
                if r.status != 206:
                    have = 0
                total = have + int(r.headers.get("content-length") or 0)
                shutil.copyfileobj(r, f, 1 << 22)
            got = os.path.getsize(tmp)
            log(f"download {url} -> {got} B ({(got - have) / max(1e-3, time.time() - t) / 1e6:.1f} MB/s)")
            if total and got < total:
                raise RuntimeError(f"short read {got}/{total}")
            if want and sha256(tmp) != want:
                os.remove(tmp)
                raise RuntimeError("sha256 mismatch, restarting")
            os.replace(tmp, dest)
            return
        except Exception as ex:
            log(f"download error {url}: {ex}")
            time.sleep(min(30, 3 * (i + 1)))
    raise RuntimeError(f"failed to fetch {dest}")


def find_ndk(cache, log):
    for c in (os.environ.get("ANDROID_NDK_HOME"), os.environ.get("ANDROID_NDK"),
              os.path.join(REPO, "..", "tools", "ndk", "android-ndk-r28c") if REPO else None,
              os.path.expanduser("~/Library/Android/sdk/ndk/28.2.13676358"), os.path.join(cache, "android-ndk-r28c")):
        if c and os.path.exists(os.path.join(c, "build", "cmake", "android.toolchain.cmake")):
            return os.path.abspath(c)
    if sys.platform.startswith("linux"):
        z = os.path.join(cache, "android-ndk-r28c-linux.zip")
        fetch([NDK_LINUX_URL], z, log)
        subprocess.run(["unzip", "-q", "-o", z, "-d", cache], check=True)
        os.remove(z)
        return os.path.join(cache, "android-ndk-r28c")
    raise RuntimeError("no Android NDK found: set ANDROID_NDK_HOME")


def build_llama(cache, log, opencl=False):
    """Cross-compile the pinned fork for arm64-v8a; cached by commit + flags."""
    flags = ["-DANDROID_ABI=arm64-v8a", "-DANDROID_PLATFORM=android-28", "-DCMAKE_BUILD_TYPE=Release",
             "-DCMAKE_C_FLAGS=-march=armv8.7a", "-DCMAKE_CXX_FLAGS=-march=armv8.7a", "-DGGML_OPENMP=OFF",
             "-DGGML_LLAMAFILE=OFF", "-DLLAMA_CURL=OFF", "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_UI=OFF",
             "-DBUILD_SHARED_LIBS=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_SERVER=OFF"]
    key = hashlib.sha256((LLAMA_COMMIT + " ".join(flags)).encode()).hexdigest()[:12]
    out = os.path.join(cache, f"llama-android-{key}")
    bins = [os.path.join(out, b) for b in ("llama-mtmd-cli", "llama-bench")]
    if all(os.path.exists(b) for b in bins):
        log(f"build cache hit {out}")
        return out
    ndk = find_ndk(cache, log)
    src_tgz = os.path.join(cache, f"llama-{LLAMA_COMMIT[:12]}.tar.gz")
    fetch([f"https://codeload.github.com/{LLAMA_REPO}/tar.gz/{LLAMA_COMMIT}"], src_tgz, log)
    src = os.path.join(cache, f"llama-src-{LLAMA_COMMIT[:12]}")
    if not os.path.isdir(src):
        tmp = src + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with tarfile.open(src_tgz) as tf:
            tf.extractall(tmp, filter="data")
        top = os.path.join(tmp, os.listdir(tmp)[0])
        os.replace(top, src)
        shutil.rmtree(tmp, ignore_errors=True)
    bdir = os.path.join(cache, f"llama-build-{key}")
    t = time.time()
    gen = ["-G", "Ninja"] if shutil.which("ninja") else []
    subprocess.run(["cmake", "-S", src, "-B", bdir, *gen, f"-DCMAKE_TOOLCHAIN_FILE={ndk}/build/cmake/android.toolchain.cmake",
                    *flags], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", bdir, "--target", "llama-mtmd-cli", "llama-bench", "-j", str(os.cpu_count() or 4)],
                   check=True, stdout=subprocess.DEVNULL)
    host = "darwin-x86_64" if sys.platform == "darwin" else "linux-x86_64"
    strip = os.path.join(ndk, "toolchains", "llvm", "prebuilt", host, "bin", "llvm-strip")
    tmp_out = out + ".tmp"
    shutil.rmtree(tmp_out, ignore_errors=True)
    os.makedirs(tmp_out)
    for b in ("llama-mtmd-cli", "llama-bench"):
        dst = os.path.join(tmp_out, b)
        shutil.copy(os.path.join(bdir, "bin", b), dst)
        if os.path.exists(strip):
            subprocess.run([strip, dst], check=True)
    os.replace(tmp_out, out)
    log(f"built {out} in {time.time() - t:.0f}s (ndk {ndk})")
    return out


# ---------- screenshot handling (stdlib only) ----------

def png_encode(w, h, rgb):
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def raw_screencap(dev):
    data = dev.run("exec-out", "screencap", timeout=60, binary=True, check=True)
    w, h, fmt = struct.unpack("<III", data[:12])
    hdr = len(data) - w * h * 4
    if hdr not in (12, 16) or fmt != 1:
        raise RuntimeError(f"unexpected screencap header: {w}x{h} fmt={fmt} hdr={hdr}")
    px = data[hdr:]
    rgb = bytearray(w * h * 3)
    for c in range(3):  # drop alpha
        rgb[c::3] = px[c::4]
    return w, h, bytes(rgb)


def downscale(w, h, rgb, f):
    """Box filter by an integer factor."""
    if f == 1:
        return w, h, rgb
    W, H = w // f, h // f
    out = bytearray(W * H * 3)
    row = W * 3
    for Y in range(H):
        acc = [0] * (W * 3 * f)
        for dy in range(f):
            line = rgb[(Y * f + dy) * w * 3:(Y * f + dy) * w * 3 + W * f * 3]
            acc = [a + b for a, b in zip(acc, line)]
        # acc holds f columns per output pixel interleaved by channel: sum over dx
        o = [0] * row
        for dx in range(f):
            seg = acc[dx * 3::]
            for c in range(3):
                vals = seg[c::3 * f][:W]
                for X, v in enumerate(vals):
                    o[X * 3 + c] += v
        n = f * f
        out[Y * row:(Y + 1) * row] = bytes(min(255, (v + n // 2) // n) for v in o)
    return W, H, bytes(out)


# ---------- sampling ----------

class Sampler:
    def __init__(self, dev, n, interval=1):
        self.rows = []
        cmd = f"sh {DEV_DIR}/sampler.sh {n} {interval}"
        if dev.root:
            cmd = f"su -c {shlex.quote(cmd)}"
        self.p = subprocess.Popen([ADB, "-s", dev.s, "shell", cmd + " </dev/null"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, stdin=subprocess.DEVNULL, pass_fds=lock_fds())
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
        self.th.join(5)


def norm_current(v):
    """sysfs current -> A. Units differ per vendor (mA or uA); |value| > 20000 is taken as uA."""
    return v / 1e6 if abs(v) > 20000 else v / 1e3


def power_stats(rows):
    out = {"n": len(rows)}
    if not rows:
        return out
    pin, pb, bt, cpu, skin, mem, fmax = [], [], [], [], [], [], []
    for r in rows:
        if all(isinstance(r.get(k), float) for k in ("uv", "uc")):
            pin.append(r["uv"] / 1e6 * norm_current(r["uc"]))
        if all(isinstance(r.get(k), float) for k in ("bv", "bc")):
            pb.append(r["bv"] / 1e6 * norm_current(r["bc"]))
        for k, arr, sc in (("bt", bt, 10), ("cpu_c", cpu, 1000), ("skin_c", skin, 1000), ("mem", mem, 1024)):
            v = r.get(k)
            if isinstance(v, float) and v > -999:
                arr.append(v / sc)
        fs = [r[f"f{c}"] for c in range(8) if isinstance(r.get(f"f{c}"), float)]
        if fs:
            fmax.append(max(fs) / 1000)
    m = lambda a: round(statistics.fmean(a), 3) if a else None
    out.update(usb_in_w=m(pin), batt_in_w=m(pb), sys_w_est=round(m(pin) - m(pb), 3) if pin and pb else None,
               batt_temp_c=m(bt), batt_temp_max_c=max(bt) if bt else None,
               cpu_temp_c=m(cpu), cpu_temp_max_c=max(cpu) if cpu else None,
               skin_temp_c=m(skin), skin_temp_max_c=max(skin) if skin else None,
               mem_avail_min_mb=round(min(mem)) if mem else None, cpu_fmax_mhz=m(fmax),
               status=sorted(set(str(r.get("st")) for r in rows)))
    return out


# ---------- llama.cpp output parsing ----------

def parse_mtmd(stderr, stdout):
    txt = stderr + "\n" + stdout
    g = lambda pat, cast=float: (cast(m.group(1)) if (m := re.search(pat, txt)) else None)
    img_dec = [float(x) for x in re.findall(r"image decoded \(batch \d+/\d+\) in (\d+) ms", txt)]
    r = {
        "load_ms": g(r"load time =\s*([\d.]+) ms"),
        "image_tokens": sum(int(x) for x in re.findall(r"decoding image batch \d+/\d+, n_tokens_batch = (\d+)", txt)) or None,
        "image_px": (lambda m: [int(m.group(1)), int(m.group(2))] if m else None)(re.search(r"input buffer \(nx=(\d+), ny=(\d+)\)", txt)),
        "vision_encode_ms": g(r"mtmd batch encoding done in (\d+) ms"),
        "image_prefill_ms": sum(img_dec) if img_dec else None,
        "prompt_ms": g(r"prompt eval time =\s*([\d.]+) ms"),
        "prompt_tokens": g(r"prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens", int),
        "decode_ms": g(r"\beval time =\s*([\d.]+) ms /\s*\d+ runs"),
        "decode_tokens": g(r"\beval time =\s*[\d.]+ ms /\s*(\d+) runs", int),
    }
    # Timestamps ("M.SS.mmm.uuu" log prefix) give the step breakdown; perf's "prompt eval time" also
    # absorbs the vision encode, so it is kept only as a raw field.
    def ts(pat, last=False):
        hits = re.findall(r"^(\d+)\.(\d+)\.(\d+)\.(\d+) .*" + pat, txt, re.M)
        if not hits:
            return None
        m_, s_, ms_, us_ = (int(x) for x in hits[-1 if last else 0])
        return m_ * 60000 + s_ * 1000 + ms_ + us_ / 1000
    t0 = ts(r"This is an experimental CLI")
    t_enc0, t_enc1 = ts(r"encoding mtmd batch"), ts(r"mtmd batch encoding done")
    t_img1 = ts(r"image decoded \(batch", last=True)
    t_end = ts(r"llama_perf_context_print:\s+load time")
    if None not in (t0, t_enc0, t_enc1, t_img1, t_end) and r["decode_ms"] is not None:
        r["step_ms"] = round(t_end - t0, 1)  # model already loaded: image load + all prefill + encode + decode
        r["pre_image_text_ms"] = round(t_enc0 - t0, 1)  # system prompt + chat prefix prefill (+ PNG load)
        r["ttft_ms"] = round(r["step_ms"] - r["decode_ms"], 1)
        pf_ms = r["ttft_ms"] - (r["vision_encode_ms"] or 0)
        if r["prompt_tokens"] and pf_ms > 0:
            r["prefill_tok_s"] = round(r["prompt_tokens"] / pf_ms * 1000, 2)  # all prompt tokens incl. image, excl. encode
        if r["image_tokens"] and r["prompt_tokens"] and r["pre_image_text_ms"] > 0:
            r["text_tokens"] = r["prompt_tokens"] - r["image_tokens"]
    if r["image_prefill_ms"] and r["image_tokens"]:
        r["image_prefill_tok_s"] = round(r["image_tokens"] / r["image_prefill_ms"] * 1000, 2)
    if r["decode_ms"] and r["decode_tokens"]:
        r["decode_tok_s"] = round(r["decode_tokens"] / r["decode_ms"] * 1000, 2)
    r["tool_call"] = "<tool_call>" in stdout
    m = re.search(r"<tool_call>(.*?)</tool_call>", stdout, re.S)
    r["action"] = m.group(1).strip()[:300] if m else None
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--quant", default="Q4_K_M")
    ap.add_argument("--mmproj", default="Q8_0")
    ap.add_argument("--scales", default="3,2,1", help="screenshot downscale factors; 1 = native resolution")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--bench-threads", default="4,6,8", help="llama-bench text-only sweep; empty to skip")
    ap.add_argument("--n-predict", type=int, default=512)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--idle", type=int, default=60)
    ap.add_argument("--cool", type=int, default=60, help="min rest s between steps")
    ap.add_argument("--cool-cpu-c", type=float, default=50.0, help="also wait (up to 3x --cool) until CPU zone below this")
    ap.add_argument("--step-timeout", type=int, default=1200)
    ap.add_argument("--no-home", action="store_true", help="do not switch to the launcher before the screenshot")
    ap.add_argument("--mirror", default="https://huggingface.co,https://hf-mirror.com")
    ap.add_argument("--cache", default=os.environ.get("OCUA_CACHE") or os.path.expanduser("~/.cache/hgamey-ondevice-cua"))
    ap.add_argument("--out", default=os.environ.get("PF_JOB_OUT") or "out")
    ap.add_argument("--phonefarm", default=os.environ.get("PHONEFARM_BIN") or os.path.join(REPO, "phonefarm"))
    a = ap.parse_args()
    if not lock_fds() and os.path.exists(a.phonefarm):
        os.execvp(a.phonefarm, [a.phonefarm, "lease", "--serial", a.serial, "--label", f"{LOCK_PREFIX}ocua@{a.serial}", "--",
                                sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    logf = open(os.path.join(out, "bench.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    def save(name, obj):
        with open(os.path.join(out, name), "w") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)

    # 1. host preparation (no device access yet)
    mirrors = [m.rstrip("/") for m in a.mirror.split(",") if m]
    files = [f"Mage-VL-Cowork-{a.quant}.gguf", f"mmproj-Mage-VL-Cowork-{a.mmproj}.gguf"]
    wdir = os.path.join(a.cache, "weights")
    os.makedirs(wdir, exist_ok=True)
    for fn in files + [SYS_PROMPT]:
        fetch([f"{m}/{GGUF_REPO}/resolve/{GGUF_REV}/{fn}" for m in mirrors], os.path.join(wdir, fn), log, WEIGHTS.get(fn))
    bindir = build_llama(a.cache, log)
    meta = {"gguf_repo": GGUF_REPO, "gguf_rev": GGUF_REV, "files": {f: WEIGHTS.get(f) for f in files},
            "llama_repo": LLAMA_REPO, "llama_commit": LLAMA_COMMIT, "backend": "CPU (arm64-v8a, armv8.7a)",
            "instruction": INSTRUCTION, "args": vars(a), "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT")}

    dev = Dev(a.serial)
    dev.root = dev.sh("su -c id </dev/null 2>/dev/null").find("uid=0") >= 0
    g = lambda cmd: dev.sh(cmd).strip()
    info = {"model": g("getprop ro.product.model"), "brand": g("getprop ro.product.brand"), "soc": g("getprop ro.soc.model"),
            "android": g("getprop ro.build.version.release"), "root": dev.root,
            "mem_total_kb": int(re.search(r"MemTotal:\s+(\d+)", dev.sh("cat /proc/meminfo")).group(1)),
            "cpus": g("cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq | tr '\\n' ' '")}
    meta["device"] = info
    save("meta.json", meta)
    log(f"device {json.dumps(info)}")

    def state():
        fg = dev.sh("dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'").strip()
        return {"foreground": fg, "screen": g("dumpsys power | grep -m1 -E 'mWakefulness='"),
                "dev_dir": g(f"ls -d {DEV_DIR} 2>/dev/null"),
                "stay_on_while_plugged_in": g("settings get global stay_on_while_plugged_in"),
                "screen_off_timeout": g("settings get system screen_off_timeout")}

    orig = state()
    save("state_before.json", orig)
    log(f"state before {json.dumps(orig, ensure_ascii=False)}")
    results = {"meta": meta, "idle": None, "llama_bench": None, "steps": []}
    restore = {"steps": []}
    pids = []
    try:
        # 2. device preparation
        dev.sh("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        if not a.no_home:
            dev.sh("input keyevent KEYCODE_HOME")
        dev.sh(f"mkdir -p {DEV_DIR}/bin")
        t = time.time()
        for src, dst in [(os.path.join(bindir, "llama-mtmd-cli"), "bin/llama-mtmd-cli"), (os.path.join(bindir, "llama-bench"), "bin/llama-bench"),
                         (os.path.join(wdir, files[0]), "model.gguf"), (os.path.join(wdir, files[1]), "mmproj.gguf"),
                         (os.path.join(wdir, SYS_PROMPT), "sys.txt"), (os.path.join(HERE, "sampler.sh"), "sampler.sh"),
                         (os.path.join(HERE, "run.sh"), "run.sh")]:
            dev.run("push", src, f"{DEV_DIR}/{dst}", check=True, timeout=1800)
        dev.sh(f"chmod 755 {DEV_DIR}/bin/*")
        log(f"pushed in {time.time() - t:.0f}s")

        def cool_down(tag):
            cs = Sampler(dev, a.cool * 3 + 10)
            t_c = time.time()
            while time.time() - t_c < a.cool or (cs.rows and cs.rows[-1].get("cpu_c", 0) / 1000 > a.cool_cpu_c and time.time() - t_c < 3 * a.cool):
                time.sleep(1)
            cs.stop()
            st = power_stats(cs.rows[-10:])
            log(f"[{tag}] cooled {time.time() - t_c:.0f}s cpu {st.get('cpu_temp_c')}C batt {st.get('batt_temp_c')}C")
            return st

        def run_on_device(tag, argv, timeout):
            local = os.path.join(out, f"{tag}.cmd.sh")
            with open(local, "w") as f:
                f.write(f"cd {DEV_DIR}\nexec " + " ".join(shlex.quote(x) for x in argv) + "\n")
            return run_on_device_prepared(dev, tag, local, timeout, out, pids)

        # 3. idle baseline
        time.sleep(20)  # settle after push
        s = Sampler(dev, a.idle + 5)
        time.sleep(a.idle)
        s.stop()
        results["idle"] = power_stats(s.rows)
        with open(os.path.join(out, "idle_samples.json"), "w") as f:
            json.dump(s.rows, f)
        log(f"idle {json.dumps(results['idle'])}")
        save("results.json", results)

        # 4. llama-bench (text only)
        if a.bench_threads:
            cool_down("bench")
            rr, so, se, rows = run_on_device("llama_bench", ["bin/llama-bench", "-m", "model.gguf", "-p", "512", "-n", "128",
                                                             "-t", a.bench_threads, "-r", "2", "-o", "json"], 1800)
            try:
                rr["results"] = [{k: x.get(k) for k in ("n_threads", "n_prompt", "n_gen", "avg_ts", "stddev_ts", "avg_ns")} for x in json.loads(so)]
            except Exception as ex:
                rr["error"] = f"parse: {ex}; {se[-300:]}"
            rr["power"] = power_stats(rows)
            results["llama_bench"] = rr
            log(f"llama-bench {json.dumps(rr)}")
            save("results.json", results)

        # 5. agent steps
        w, h, rgb = raw_screencap(dev)
        log(f"screenshot {w}x{h}")
        imgs = {}
        for f in sorted({int(x) for x in a.scales.split(",") if x}):
            W, H, small = downscale(w, h, rgb, f)
            name = f"screen_s{f}.png"
            with open(os.path.join(out, name), "wb") as fo:
                fo.write(png_encode(W, H, small))
            dev.run("push", os.path.join(out, name), f"{DEV_DIR}/{name}", check=True)
            imgs[f] = (name, W, H)
        for f in [int(x) for x in a.scales.split(",") if x]:
            name, W, H = imgs[f]
            for rep in range(a.reps):
                tag = f"step_s{f}_r{rep}"
                cool = cool_down(tag)
                argv = ["bin/llama-mtmd-cli", "-m", "model.gguf", "--mmproj", "mmproj.gguf", "--image", name, "--jinja",
                        "-c", str(a.ctx), "-n", str(a.n_predict), "--temp", "0", "-t", str(a.threads),
                        "-sys", "@SYS@", "-p", INSTRUCTION, "--perf", "-lv", "4"]
                # system prompt is read on device to avoid shell quoting of the long tool schema
                cmdline = " ".join('"$(cat sys.txt)"' if x == "@SYS@" else shlex.quote(x) for x in argv)
                local = os.path.join(out, f"{tag}.cmd.sh")
                with open(local, "w") as fo:
                    fo.write(f"cd {DEV_DIR}\nexec {cmdline}\n")
                rr, so, se, rows = run_on_device_prepared(dev, tag, local, a.step_timeout, out, pids)
                rec = {"tag": tag, "scale": f, "image_wh": [W, H], "rep": rep, "threads": a.threads, "cool": cool, **rr,
                       **parse_mtmd(se, so), "power": power_stats(rows)}
                if rec["power"].get("sys_w_est") is not None and results["idle"].get("sys_w_est") is not None:
                    rec["power"]["delta_w_vs_idle"] = round(rec["power"]["sys_w_est"] - results["idle"]["sys_w_est"], 3)
                results["steps"].append(rec)
                log(f"{tag}: {json.dumps({k: rec.get(k) for k in ('rc', 'image_tokens', 'vision_encode_ms', 'prompt_tokens', 'prefill_tok_s', 'image_prefill_tok_s', 'decode_tokens', 'decode_tok_s', 'step_ms', 'wall_s', 'peak_rss_mb', 'tool_call')})}")
                log(f"   power {json.dumps(rec['power'])}")
                save("results.json", results)
    finally:
        def kill_own():
            for p in pids:  # only our own recorded pids, never by name
                dev.sh(f"kill {p} 2>/dev/null")
        steps = [("kill own pids", kill_own), ("remove pushed files", lambda: dev.sh(f"rm -rf {DEV_DIR}"))]
        fg = re.search(r"\{[^ ]+ [^ ]+ ([\w.]+)/", orig["foreground"] or "")
        if fg and not a.no_home:
            pkg = fg.group(1)
            launcher = dev.sh("cmd package resolve-activity --brief -c android.intent.category.HOME -a android.intent.action.MAIN | tail -1").strip().split("/")[0]
            if pkg != launcher:
                steps.append(("restore foreground app", lambda: dev.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")))
        if "Asleep" in orig.get("screen", "") or "Dozing" in orig.get("screen", ""):
            steps.append(("screen back off", lambda: dev.sh("input keyevent KEYCODE_SLEEP")))
        for step, fn in steps:
            try:
                fn()
                restore["steps"].append({"step": step, "ok": True})
            except Exception as ex:
                restore["steps"].append({"step": step, "ok": False, "error": str(ex)})
        time.sleep(3)
        after = state()
        restore["before"], restore["after"] = orig, after
        restore["diff"] = {k: [orig[k], after[k]] for k in orig if orig[k] != after[k]}
        save("restore.json", restore)
        log(f"restore {json.dumps(restore['diff'], ensure_ascii=False)}")
        save("results.json", results)


def run_on_device_prepared(dev, tag, local, timeout, out, pids):
    dev.run("push", local, f"{DEV_DIR}/{tag}.cmd.sh", check=True)
    smp = Sampler(dev, timeout + 30)
    t0 = time.time()
    res = dev.sh(f"sh {DEV_DIR}/run.sh {DEV_DIR}/{tag} {DEV_DIR}/{tag}.cmd.sh", timeout=timeout)
    t1 = time.time()
    smp.stop()
    pid = dev.sh(f"cat {DEV_DIR}/{tag}.pid 2>/dev/null").strip()
    if pid:
        pids.append(pid)
    so = dev.sh(f"cat {DEV_DIR}/{tag}.stdout", timeout=60)
    se = dev.sh(f"cat {DEV_DIR}/{tag}.stderr", timeout=60)
    for suffix, text in (("stdout", so), ("stderr", se)):
        with open(os.path.join(out, f"{tag}.{suffix}.txt"), "w") as f:
            f.write(text)
    m = re.search(r"R rc=(\d+) hwm_kb=(\d+) anon_kb=(\d+)", res)
    rr = {"rc": int(m.group(1)), "peak_rss_mb": round(int(m.group(2)) / 1024), "peak_anon_mb": round(int(m.group(3)) / 1024)} if m else {"rc": None, "raw": res[-300:]}
    rr["wall_s"] = round(t1 - t0, 2)
    with open(os.path.join(out, f"{tag}_samples.json"), "w") as f:
        json.dump(smp.rows, f)
    return rr, so, se, smp.rows


if __name__ == "__main__":
    main()
