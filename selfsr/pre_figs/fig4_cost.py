"""Figure 4: cost. Whole-phone power (charging stopped, 30 s windows, 3 pairs each) and GPU clock.

power: 622ff5adb4e7 (interp to 120) and 0ba1b349ae36 (interp to 60), report.json power_mw.
GPU clock: time-weighted kgsl clock in each 30 s arm trace of b0c94110b649 (gpu_freq.py logic).
"""
import json, os, re, glob, sys
import numpy as np
from style import plt, clean, INK, INK2, NATIVE, OURS

root, out = sys.argv[1], sys.argv[2]


def report(job, path):
    return json.load(open(os.path.join(root, job, path)))["metrics"]


p120 = report("622ff5adb4e7", "srbaseline_20261030-153619/srip_g7/report.json")["power_mw"]
p60 = report("0ba1b349ae36", "selfsr_20261101-170854/7_sr_interp/report.json")["power_mw"]


def mean_clock(trace):
    freq = last = t0 = t1 = None
    acc = dur = 0.0
    for line in open(trace, errors="replace"):
        m = re.search(r"\s(\d+\.\d+): (kgsl_gpu_frequency|kgsl_pwrlevel|\w+):", line)
        if not m:
            continue
        t = float(m.group(1))
        t0 = t if t0 is None else t0
        t1 = t
        g = None
        if m.group(2) == "kgsl_gpu_frequency":
            g = re.search(r"gpu_freq=(\d+)", line)
            v = int(g.group(1)) / 1000 if g else None
        elif m.group(2) == "kgsl_pwrlevel":
            g = re.search(r" freq=(\d+)", line)
            v = int(g.group(1)) / 1e6 if g else None
        if g:
            if freq is not None:
                acc += freq * (t - last); dur += t - last
            freq, last = v, t
    if freq is None:  # no change during the window: read the start clock from the header
        hdr = open(trace, errors="replace").read(4000)
        return int(re.search(r"gpu_clk=(\d+)", hdr).group(1)) / 1e6
    acc += freq * (t1 - last); dur += t1 - last
    return acc / dur


tr = os.path.join(root, "b0c94110b649", "srbaseline_20261030-115528/srip_g7/traces")
clk_a = [mean_clock(os.path.join(tr, "A%d.txt" % i)) for i in range(1, 7)]
clk_b = [mean_clock(os.path.join(tr, "B%d.txt" % i)) for i in range(1, 7)]
print("power 120: %.0f -> %.0f mW; power 60: %.0f -> %.0f mW" % (p120["a_mean"], p120["b_mean"], p60["a_mean"], p60["b_mean"]))
print("clock native", [round(x) for x in clk_a], "ours", [round(x) for x in clk_b])

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.2), gridspec_kw={"width_ratios": [1.5, 1]})
W = 0.3
xs = np.array([0, 1])
na = [p120["a_mean"] / 1000, p60["a_mean"] / 1000]
ob = [p120["b_mean"] / 1000, p60["b_mean"] / 1000]
ax1.bar(xs - W / 2 - 0.015, na, W, color=NATIVE, label="原生")
ax1.bar(xs + W / 2 + 0.015, ob, W, color=OURS, label="自研超分+插帧")
for x, v in zip(xs - W / 2 - 0.015, na):
    ax1.text(x, v + 0.15, "%.1f" % v, ha="center", va="bottom", color=INK, fontsize=12)
for x, v in zip(xs + W / 2 + 0.015, ob):
    ax1.text(x, v + 0.15, "%.1f" % v, ha="center", va="bottom", color=INK, fontsize=12, fontweight="bold")
ax1.set_xticks(xs, ["上屏约 118 帧（推荐配置）", "上屏限到 60 帧"], fontsize=12, color=INK)
ax1.set_ylabel("整机功耗（瓦）")
ax1.set_ylim(0, 11)
ax1.set_title("整机功耗约为原生的 2.7 倍；上屏降到 60 帧也不省")
ax1.legend(loc="upper left", ncol=2, fontsize=11)
clean(ax1, "y")

ca, cb = np.mean(clk_a), np.mean(clk_b)
ax2.bar([0], [ca], 0.42, color=NATIVE)
ax2.bar([1], [cb], 0.42, color=OURS)
ax2.text(0, ca + 25, "约 %d" % round(ca, -1), ha="center", va="bottom", color=INK, fontsize=12)
ax2.text(1, cb + 25, "约 %d" % round(cb, -1), ha="center", va="bottom", color=INK, fontsize=12, fontweight="bold")
ax2.set_xticks([0, 1], ["原生", "自研超分+插帧"], fontsize=12, color=INK)
ax2.set_ylabel("GPU 频率（MHz）")
ax2.set_ylim(0, 1250)
ax2.set_xlim(-0.6, 1.6)
ax2.set_title("GPU 从最低档升到约 1 GHz")
clean(ax2, "y")

fig.tight_layout(w_pad=3)
fig.savefig(out, dpi=150, bbox_inches="tight")
