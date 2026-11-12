"""Shared chart style for the native vs SR+interpolation report figures."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

from fonts import font_paths

FONT, FONT_BOLD = font_paths()
fm.fontManager.addfont(FONT)
fm.fontManager.addfont(FONT_BOLD)
FAMILY = fm.FontProperties(fname=FONT).get_name()

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
NATIVE = "#8a8984"      # baseline, de-emphasised gray
OURS = "#2a78d6"        # slot 1 blue
OURS_LIGHT = "#86b6ef"  # blue step 250: synthesized frames
ALT = "#eb6834"         # slot 2 orange: configuration not recommended

plt.rcParams.update({
    "font.family": FAMILY,
    "font.size": 12,
    "axes.unicode_minus": False,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK2,
    "axes.titlecolor": INK,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "xtick.color": INK2,
    "ytick.color": INK2,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 1,
    "axes.axisbelow": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "legend.labelcolor": INK2,
})


def clean(ax, grid_axis="y"):
    ax.grid(False)
    ax.grid(True, axis=grid_axis)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(length=0)
