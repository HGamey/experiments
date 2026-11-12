"""Figure 1: what sits between the game and the screen, native vs ours."""
import sys
from style import plt, INK, INK2, NATIVE, OURS, SURFACE
from matplotlib.patches import FancyBboxPatch

out = sys.argv[2]  # argv[1] (data root) is unused: this figure is a diagram
fig, ax = plt.subplots(figsize=(12, 3.7))
ax.set_xlim(0, 12)
ax.set_ylim(0, 3.7)
ax.axis("off")

BOX_W, BOX_H = 2.15, 1.05


def box(x, y, title, sub, changed):
    edge = OURS if changed else NATIVE
    face = "#e8f1fc" if changed else "#f0efec"
    ax.add_patch(FancyBboxPatch((x, y), BOX_W, BOX_H, boxstyle="round,pad=0.02,rounding_size=0.12",
                                linewidth=1.6, edgecolor=edge, facecolor=face))
    ax.text(x + BOX_W / 2, y + BOX_H * (0.66 if sub else 0.5), title, ha="center", va="center",
            fontsize=13, color=INK, fontweight="bold")
    ax.text(x + BOX_W / 2, y + BOX_H * 0.28, sub, ha="center", va="center", fontsize=10.5, color=INK2)


def arrow(x0, x1, y):
    ax.annotate("", xy=(x1, y), xytext=(x0, y),
                arrowprops=dict(arrowstyle="-|>", color=INK2, lw=1.4, shrinkA=0, shrinkB=0))


# native has three boxes; its screen box lines up with ours
y1, y2 = 2.45, 0.95

xs = 1.35
step = BOX_W + 0.35
# native
nat = [(xs, "游戏渲染", "0.8 倍分辨率\n系统按温度限到 30 帧", False),
       (xs + step, "显示硬件放大", "近似双线性\n界面和字一起放大", False),
       (xs + 3 * step, "屏幕 60 Hz", "", False)]
ax.text(0.05, y1 + BOX_H / 2, "原生", ha="left", va="center", fontsize=14, fontweight="bold", color=INK)
for x, t, s, c in nat:
    box(x, y1, t, s, c)
arrow(xs + BOX_W + 0.03, xs + step - 0.03, y1 + BOX_H / 2)
arrow(xs + step + BOX_W + 0.03, xs + 3 * step - 0.03, y1 + BOX_H / 2)
ax.text(xs + 3 * step + BOX_W + 0.15, y1 + BOX_H / 2, "每秒 30 帧\n界面偏软", ha="left", va="center",
        fontsize=12.5, color=INK)

# ours
ours = [(xs, "游戏渲染", "0.8 倍分辨率\n解除限帧，约 60 帧", True),
        (xs + step, "自研超分", "放大 + 锐化\n到屏幕分辨率", True),
        (xs + 2 * step, "插帧", "每两帧之间\n合成一帧", True),
        (xs + 3 * step, "屏幕 120 Hz", "", True)]
ax.text(0.05, y2 + BOX_H / 2, "自研超分\n+插帧", ha="left", va="center", fontsize=14, fontweight="bold",
        color=INK, linespacing=1.3)
for x, t, s, c in ours:
    box(x, y2, t, s, c)
for i in range(3):
    a = xs + i * step + BOX_W + 0.03
    arrow(a, a + 0.29, y2 + BOX_H / 2)
ax.text(xs + 3 * step + BOX_W + 0.15, y2 + BOX_H / 2, "每秒约 118 帧\n界面更锐", ha="left", va="center",
        fontsize=12.5, color=INK)

ax.text(xs, 0.4, "蓝框是改动的环节：前三项在注入游戏的 Vulkan 层里完成；屏幕切到 120 Hz、"
        "关掉系统合成的一个等待开关由脚本完成。整套需要 root。",
        fontsize=11, color=INK2, va="center")

fig.savefig(out, dpi=150, bbox_inches="tight")
