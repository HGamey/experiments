#!/usr/bin/env python3
"""打印节点的 Python、numpy、PIL、ffmpeg 和 CPU 情况, 并量一帧 RGB 转 PNG 要多久, 决定转换放哪。不碰手机。"""
import json, os, shutil, sys, time, zlib
out = {"python": sys.version.split()[0], "cpu": os.cpu_count(), "ffmpeg": shutil.which("ffmpeg"), "sips": shutil.which("sips"), "node": os.environ.get("PF_NODE_ID")}
for m in ("numpy", "PIL"):
    try:
        out[m] = getattr(__import__(m), "__version__", "yes")
    except ImportError as e:
        out[m] = None
st = shutil.disk_usage(os.environ.get("PF_JOB_OUT") or ".")
out["disk_free_gb"] = round(st.free / 1e9, 1)
w, h = 2141, 969
row = bytes((x * 7) % 251 for x in range(w * 3))
raw = b"".join(b"\0" + row for _ in range(h))
t = time.time(); z = zlib.compress(raw, 6); out["zlib6_s"] = round(time.time() - t, 3)
print(json.dumps(out, ensure_ascii=False))
