"""Figure 5: 30 minutes unplugged, recommended config.

Battery: 0eb94025a536 traces/P1_hold.tsv (native) and P2_hold.tsv (ours), 1 s rows.
Frame rate: 2 s sample every 60 s, traces/P2_seen.tsv of 0eb94025a536 and of the independent run 6c9ee0e74442.
Samples taken right after the test script's keep-awake tap (every 300 s) are left out: they show the
sub-layer pausing for about a second after the tap, not the frame rate.
"""
import csv, os, re, sys
import numpy as np
from style import plt, clean, INK, INK2, NATIVE, OURS, SURFACE

root, out = sys.argv[1], sys.argv[2]


def hold(job, phase):
    rows = csv.DictReader(open(os.path.join(root, job, "traces", phase + "_hold.tsv")), delimiter="\t")
    t, c = [], []
    for r in rows:
        m = re.search(r"temperature: (\d+)", r.get("bat") or "")
        if m:
            t.append(int(r["t"]) / 60)
            c.append(int(m.group(1)) / 10)
    return np.array(t), np.array(c)


def seen(job):
    rows = list(csv.DictReader(open(os.path.join(root, job, "traces", "P2_seen.tsv")), delimiter="\t"))
    keep, tapped = [], []
    for r in rows:
        t, f = int(r["t"]), float(r["fps"])
        after_tap = t % 300 <= 4 and t >= 300 and f < 100
        (tapped if after_tap else keep).append((t / 60, f))
    return np.array(keep), tapped


tn, cn = hold("0eb94025a536", "P1")
to, co = hold("0eb94025a536", "P2")
s1, tap1 = seen("0eb94025a536")
s2, tap2 = seen("6c9ee0e74442")
print("native %.0f->%.0f, ours %.0f->%.0f max %.0f" % (cn[0], cn[-1], co[0], co[-1], co.max()))
print("first >=56 at %.1f min" % to[np.argmax(co >= 56)])
print("dropped tap samples", tap1, tap2)
for name, s in (("run1", s1), ("run2", s2)):
    late = s[s[:, 0] >= 16]
    print(name, "after 16 min: %d samples, %d below 100" % (len(late), (late[:, 1] < 100).sum()))

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6.6), sharex=True, gridspec_kw={"height_ratios": [1, 1.1]})

hot = co >= 56
start = to[np.argmax(hot)]
for ax in (ax1, ax2):
    ax.axvspan(start, 30, color="#f6e3dc", lw=0, zorder=0)

ax1.plot(tn, cn, color=NATIVE, lw=2)
ax1.plot(to, co, color=OURS, lw=2)
ax1.text(30.3, cn[-1], "原生", va="center", color=INK, fontsize=12)
ax1.text(30.3, co[-1], "自研超分+插帧", va="center", color=INK, fontsize=12)
ax1.axhline(56, color=INK2, lw=1, ls=(0, (2, 2)))
ax1.text(0.2, 56.4, "56°C", color=INK2, fontsize=11, va="bottom")
ax1.set_ylabel("电池温度（°C）")
ax1.set_ylim(36, 60)
ax1.set_title("不插电连续 30 分钟：电池温度")
clean(ax1, "y")

ax2.scatter(s1[:, 0], s1[:, 1], s=62, color=OURS, edgecolors=SURFACE, linewidths=1.2, zorder=3, label="第一次")
ax2.scatter(s2[:, 0], s2[:, 1], s=46, facecolors=SURFACE, edgecolors=OURS, linewidths=1.6, zorder=3,
            label="第二次（独立重复）")
ax2.set_ylim(0, 135)
ax2.set_yticks([0, 30, 60, 90, 120])
ax2.set_ylabel("每秒看到的画面（帧）")
ax2.set_xlabel("分钟")
ax2.set_xlim(0, 30)
ax2.set_title("自研超分+插帧：每分钟抽 2 秒看帧率")
ax2.text(start + 0.3, 20, "电池升到 55–57°C 之后：GPU 被降频，\n约四成抽样掉到 75 帧上下",
         color=INK2, fontsize=11, va="bottom")
ax2.legend(loc="lower left", fontsize=11)
clean(ax2, "y")

fig.tight_layout(h_pad=1.5)
fig.savefig(out, dpi=150, bbox_inches="tight")
