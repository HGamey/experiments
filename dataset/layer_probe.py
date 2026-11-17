#!/usr/bin/env python3
"""layer_probe.py — 只读: 找节点上的灰层 .so, 打印修改时间、大小、sha256 前缀, 以及几个功能的特征字符串在不在 (判断是不是按新代码编的)。"""
import glob, hashlib, os, sys, time
cands = set()
for root in [os.getcwd()] + [os.environ.get(k, "") for k in ("PHONEFARM_ROOT", "PHONEFARM_HOME", "HGAMEY_ROOT")]:
    p = root
    while p and p != "/":
        cands.update(glob.glob(os.path.join(p, "**/knobs/gray/build/out/libVkLayer_refknobs.so"), recursive=False))
        cands.update(glob.glob(os.path.join(p, "knobs/gray/build/out/libVkLayer_refknobs.so")))
        cands.update(glob.glob(os.path.join(p, "*/knobs/gray/build/out/libVkLayer_refknobs.so")))
        cands.update(glob.glob(os.path.join(p, "libVkLayer_refknobs.so")))
        p = os.path.dirname(p)
for pat in ("/home/*/*/knobs/gray/build/out/libVkLayer_refknobs.so", "/home/*/*/*/knobs/gray/build/out/libVkLayer_refknobs.so",
            "/home/*/*/*/libVkLayer_refknobs.so", "/home/*/*/*/*/libVkLayer_refknobs.so", "/opt/*/*/libVkLayer_refknobs.so"):
    cands.update(glob.glob(pat))
print("cwd", os.getcwd())
for f in sorted(cands):
    b = open(f, "rb").read()
    st = os.stat(f)
    marks = {m: (m.encode() in b) for m in ("debug.knobs.target", "debug.knobs.framedump_att", "fmt", "sigs_seen")}
    print(f, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)), len(b), hashlib.sha256(b).hexdigest()[:16], marks)
