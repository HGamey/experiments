"""Figure 3: frames per second the player sees.

Left: what the ~118 is made of (final perf run b0c94110b649, 6 pairs).
Right: every 30 s window, per run (arms.jsonl seen_fps).
"""
import json, os, sys
import numpy as np
from style import plt, clean, INK, INK2, NATIVE, OURS, OURS_LIGHT, ALT, SURFACE

root, out = sys.argv[1], sys.argv[2]


def arms(job, path):
    return [json.loads(l) for l in open(os.path.join(root, job, path))]


RUNS_OLD = [("673fdb6b4bb8", "srbaseline_20261029-201102/srip_g7/arms.jsonl"),
            ("31cc1bb276f9", "srbaseline_20261030-032023/srip_g7/arms.jsonl")]
RUNS_REC = [("b6d5969c4f6e", "srbaseline_20261030-101633/srip_g7/arms.jsonl"),
            ("b0c94110b649", "srbaseline_20261030-115528/srip_g7/arms.jsonl"),
            ("622ff5adb4e7", "srbaseline_20261030-153619/srip_g7/arms.jsonl"),
            ("beed9385a1e1", "srbaseline_20261101-035225/srip_g7/arms.jsonl")]

nat, old, rec = [], [], []
for job, p in RUNS_OLD + RUNS_REC:
    for r in arms(job, p):
        if r["side"] == "A":
            nat.append(r["seen_fps"])
        elif (job, p) in RUNS_OLD:
            old.append(r["seen_fps"])
        else:
            rec.append(r["seen_fps"])
assert all("sfwait=off" in r["kind"] for job, p in RUNS_REC for r in arms(job, p) if r["side"] == "B")
assert not any("sfwait=off" in r["kind"] for job, p in RUNS_OLD for r in arms(job, p) if r["side"] == "B")

final = arms(*RUNS_REC[1])
real = np.mean([r["game_fps"] for r in final if r["side"] == "B"])
seen = np.mean([r["seen_fps"] for r in final if r["side"] == "B"])
nat_seen = np.mean([r["seen_fps"] for r in final if r["side"] == "A"])
print("final run: native seen %.2f, ours real %.2f seen %.2f" % (nat_seen, real, seen))
print("counts: native %d, old %d (<100: %d), rec %d (<110: %s)" % (
    len(nat), len(old), sum(v < 100 for v in old), len(rec), [v for v in rec if v < 110]))

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6), gridspec_kw={"width_ratios": [1.15, 1]})

# left: composition
H = 0.34
ys = [1, 0]
ax1.barh(ys[0], nat_seen, height=H, color=NATIVE)
ax1.barh(ys[1], real, height=H, color=OURS)
ax1.barh(ys[1], seen - real - 1.2, left=real + 1.2, height=H, color=OURS_LIGHT)
ax1.text(nat_seen / 2, ys[0], "真帧", ha="center", va="center", color="white", fontsize=12)
ax1.text(real / 2, ys[1], "游戏渲染的真帧", ha="center", va="center", color="white", fontsize=12)
ax1.text(real + (seen - real) / 2, ys[1], "插帧合成的帧", ha="center", va="center", color=INK, fontsize=12)
ax1.text(nat_seen + 2, ys[0], "约 30", va="center", ha="left", color=INK, fontsize=13, fontweight="bold")
ax1.text(seen + 2, ys[1], "约 118", va="center", ha="left", color=INK, fontsize=13, fontweight="bold")
ax1.set_yticks(ys, ["原生", "自研超分\n+插帧"], fontsize=13, color=INK)
ax1.set_xlim(0, 135)
ax1.set_xticks([0, 30, 60, 90, 120])
ax1.set_xlabel("每秒看到的画面（帧）")
ax1.set_title("约 118 帧里一半是真帧、一半是插出来的")
clean(ax1, "x")
ax1.set_ylim(-0.6, 1.6)

# right: every window
rng = np.random.default_rng(7)
groups = [("原生\n%d 次" % len(nat), nat, NATIVE), ("自研 · 不关\n系统等待开关\n%d 次" % len(old), old, ALT),
          ("自研 · 推荐配置\n%d 次" % len(rec), rec, OURS)]
for i, (name, vals, col) in enumerate(groups):
    x = i + rng.uniform(-0.3, 0.3, len(vals))
    ax2.scatter(x, vals, s=40, color=col, edgecolors=SURFACE, linewidths=1.5, zorder=3)
ax2.set_xticks(range(3), [g[0] for g in groups], fontsize=12, color=INK)
ax2.set_ylim(0, 130)
ax2.set_yticks([0, 30, 60, 90, 120])
ax2.set_ylabel("每秒看到的画面（帧）")
ax2.set_xlim(-0.6, 2.6)
ax2.set_title("每次开局各测 30 秒，每个点是一次")
ax2.annotate("约一半开局\n卡在 90 帧", xy=(1.3, 89.5), xytext=(1.42, 62), fontsize=11, color=INK2,
             arrowprops=dict(arrowstyle="-", color=INK2, lw=1))
clean(ax2, "y")

fig.tight_layout(w_pad=3)
fig.savefig(out, dpi=150, bbox_inches="tight")
