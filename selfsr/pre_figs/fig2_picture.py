"""Figure 2: same spot, adjacent runs; native (A4) vs ours (B4) with nearest-neighbour zooms.

Source: job b0c94110b649 srbaseline_20261030-115528/srip_g7/shots/{A4,B4}.png (2688x1216 screenshots).
"""
import os, sys
from PIL import Image, ImageDraw, ImageFont
from fonts import font_paths

root, out = sys.argv[1], sys.argv[2]
shots = os.path.join(root, "b0c94110b649", "srbaseline_20261030-115528/srip_g7/shots")
REGULAR, BOLD = font_paths()
f_head = ImageFont.truetype(BOLD, 30)
f_row = ImageFont.truetype(REGULAR, 24)
f_tag = ImageFont.truetype(BOLD, 34)

INK, INK2, SURF = (11, 11, 11), (82, 81, 78), (252, 252, 251)
NAT, OURS = (138, 137, 132), (42, 120, 214)
BOXC = (255, 214, 0)

a = Image.open(os.path.join(shots, "A4.png")).convert("RGB")
b = Image.open(os.path.join(shots, "B4.png")).convert("RGB")

W = 960
M, G = 24, 22
# (label, box in native, offset to apply in ours, zoom)
crops = [
    ("① 任务文字（放大 4 倍）", (150, 345, 390, 425), (0, 0), 4),
    ("② 右上角图标（放大 3 倍）", (1870, 15, 2190, 115), (0, 0), 3),
    ("③ 人物（放大 3 倍）", (1170, 400, 1490, 600), (-6, 0), 3),
]

full_h = round(1216 * W / 2688)
heights = [46, full_h] + sum([[44, (bx[3] - bx[1]) * z] for _, bx, _, z in crops], [])
H = M + sum(heights) + 16 * len(heights) + M
canvas = Image.new("RGB", (M * 2 + W * 2 + G, H), SURF)
d = ImageDraw.Draw(canvas)
x_l, x_r = M, M + W + G
y = M

# column heads with a colour key
for x, text, col in ((x_l, "原生", NAT), (x_r, "自研超分+插帧", OURS)):
    d.rectangle((x, y + 8, x + 10, y + 38), fill=col)
    d.text((x + 20, y + 2), text, font=f_head, fill=INK)
y += 46 + 16

# full frames with crop boxes
s = W / 2688
for img, x, off in ((a, x_l, False), (b, x_r, True)):
    canvas.paste(img.resize((W, full_h), Image.LANCZOS), (x, y))
    for i, (_, bx, o, _) in enumerate(crops):
        dx, dy = o if off else (0, 0)
        r = [x + (bx[0] + dx) * s, y + (bx[1] + dy) * s, x + (bx[2] + dx) * s, y + (bx[3] + dy) * s]
        d.rectangle(r, outline=BOXC, width=3)
        d.text((r[2] + 4, r[1] - 6), "①②③"[i], font=f_tag, fill=BOXC, stroke_width=2, stroke_fill=(0, 0, 0))
y += full_h + 16

for label, bx, o, z in crops:
    d.text((x_l, y + 4), label, font=f_row, fill=INK2)
    y += 44 + 16
    w, h = bx[2] - bx[0], bx[3] - bx[1]
    ca = a.crop(bx).resize((w * z, h * z), Image.NEAREST)
    bb = (bx[0] + o[0], bx[1] + o[1], bx[2] + o[0], bx[3] + o[1])
    cb = b.crop(bb).resize((w * z, h * z), Image.NEAREST)
    canvas.paste(ca, (x_l + (W - w * z) // 2, y))
    canvas.paste(cb, (x_r + (W - w * z) // 2, y))
    y += h * z + 16

canvas = canvas.crop((0, 0, canvas.width, y + M - 16))
canvas.save(out, optimize=True)
print(canvas.size)
