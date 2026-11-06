#!/bin/sh
# 荣耀 R8 (B轨根因修复): v73 引擎要求每个武器对象声明 WEAPON_NETFIRING(kimi 只修了 weapon_base,
# weapon_fists 等子类仍缺 → 玩家生成即 Map Shutdown)。本脚本给 pk4 内所有缺该字段的 weapon_*.script
# 在 object 声明块插入 boolean WEAPON_NETFIRING; + weapon_base override, 双目录铺放, 进图→跳过场→多样补采。
set -eu
SERIAL="${1:?usage: honor_r8.sh <serial>}"
PKG=com.karin.idTech4Amm
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
CACHE="${PF_GAME_CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
OUT="${PF_JOB_OUT:-$(pwd)/doom_out}/doom_honorR8_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT/frames_raw" "$OUT/frames"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL"

# 1) 构建全量补丁 pk4 (host python)
PK4SRC="$CACHE/blobs/$PK4_SHA"; OVR="$CACHE/blobs/$SCRIPT_SHA"
[ -f "$PK4SRC" ] && [ -f "$OVR" ] || { echo "FATAL: blobs missing"; exit 3; }
python3 - "$PK4SRC" "$OVR" "$OUT/demo00_r8.pk4" <<'PY'
import zipfile, sys, re, hashlib, shutil
src, ovr, dst = sys.argv[1:4]
zin = zipfile.ZipFile(src)
override = open(ovr, "rb").read()
assert hashlib.sha256(override).hexdigest().startswith("5870c79c")
zout = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
patched, added = [], []
for item in zin.infolist():
    n = item.filename
    data = zin.read(n)
    if n == "script/weapon_base.script":
        data = override; patched.append(n)
    elif re.match(r"script/weapon_\w+\.script$", n) and b"WEAPON_NETFIRING" not in data:
        # 在 object weapon_x ... { 声明块内插入字段
        text = data.decode("utf-8", "ignore")
        m = re.search(r"(object\s+weapon_\w+\s*(?::\s*weapon_base)?\s*\{)", text)
        if m:
            ins = m.end()
            text = text[:ins] + "\n\tboolean\t\tWEAPON_NETFIRING;\n" + text[ins:]
            data = text.encode("utf-8"); patched.append(n); added.append(n)
    zout.writestr(item, data)
zout.close()
print("PATCHED_MEMBERS:", ",".join(patched))
print("FIELD_ADDED:", ",".join(added))
z = zipfile.ZipFile(dst)
assert sum(1 for n in z.namelist() if n == "script/weapon_base.script") == 1
assert b"WEAPON_NETFIRING" in z.read("script/weapon_fists.script")
print("VERIFY_OK fists has field")
PY
[ -f "$OUT/demo00_r8.pk4" ] || { echo "FATAL: build failed"; exit 4; }
echo "R8_PK4_SHA=$(shasum -a 256 "$OUT/demo00_r8.pk4" | cut -d ' ' -f 1)"
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true

# 2) 双目录铺放 (公共+私有, 均含同一修复 pk4)
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
sh_ "mkdir -p /data/local/tmp/doom_stage/state/base"
adb -s "$SERIAL" push "$OUT/demo00_r8.pk4" /data/local/tmp/doom_stage/doom3.pk4 >/dev/null 2>&1 || adb -s "$SERIAL" push "$OUT/demo00_r8.pk4" /data/local/tmp/doom.pk4 >/dev/null
sh_ "mkdir -p /storage/emulated/0/diii4a && rm -rf /storage/emulated/0/diii4a/doom3 /storage/emulated/0/diii4a/state /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3 /storage/emulated/0/Android/data/$PKG/files/diii4a/state"
sh_ "mkdir -p /storage/emulated/0/diii4a/doom3/base /storage/emulated/0/diii4a/state/base /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3/base /storage/emulated/0/Android/data/$PKG/files/diii4a/state/base"
sh_ "cp /data/local/tmp/doom.pk4 /storage/emulated/0/diii4a/doom3/base/demo00.pk4 2>/dev/null || cp /data/local/tmp/doom3.pk4 /storage/emulated/0/diii4a/doom3/base/demo00.pk4"
sh_ "cp /data/local/tmp/doom.pk4 /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4 2>/dev/null || cp /data/local/tmp/doom3.pk4 /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4"
R8_SHA=$(shasum -a 256 "$OUT/demo00_r8.pk4" | cut -d ' ' -f 1)
sh_ "sha256sum /storage/emulated/0/diii4a/doom3/base/demo00.pk4" | grep -q "$R8_SHA" || { echo "FATAL: public sha"; exit 4; }
sh_ "sha256sum /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4" | grep -q "$R8_SHA" || { echo "FATAL: private sha"; exit 4; }
echo "STEP2_SEEDED_BOTH" | tee -a "$OUT/steps.txt"

# 3) 启动 + 过场跳过 + 动态验证
sh_ "input keyevent 224; wm dismiss-keyguard" >/dev/null 2>&1 || true
CMD="game.arm +set fs_basepath /storage/emulated/0/Android/data/$PKG/files/diii4a/doom3 +set fs_savepath /storage/emulated/0/Android/data/$PKG/files/diii4a/state +set com_showFPS 1 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +map game/demo_mars_city1"
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command \"$CMD\"" | tail -2
P=""; i=0; while [ $i -lt 20 ]; do sleep 3; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+3)); done
[ -n "$P" ] || { echo "LAUNCH_DEAD"; exit 6; }
echo "GAME_PID=$P" | tee -a "$OUT/steps.txt"
sleep 120
sh_ "input tap 636 700" >/dev/null 2>&1 || true; sleep 3
sh_ "input keyevent 111" >/dev/null 2>&1 || true; sleep 12
sh_ "screencap -p /data/local/tmp/dyn1.png" >/dev/null; sleep 5
sh_ "screencap -p /data/local/tmp/dyn2.png" >/dev/null
adb -s "$SERIAL" pull /data/local/tmp/dyn1.png "$OUT/dyn1.png" >/dev/null 2>&1 || true
adb -s "$SERIAL" pull /data/local/tmp/dyn2.png "$OUT/dyn2.png" >/dev/null 2>&1 || true
DYN=$(python3 - "$OUT/dyn1.png" "$OUT/dyn2.png" 2>/dev/null <<'PY'
import sys
try:
    from PIL import Image, ImageChops
    a=Image.open(sys.argv[1]).convert("L").resize((160,80)); b=Image.open(sys.argv[2]).convert("L").resize((160,80))
    d=ImageChops.difference(a,b).getdata(); print(round(sum(d)/len(d),2))
except Exception: print("?")
PY
)
echo "DYNAMIC_DIFF=$DYN" | tee -a "$OUT/steps.txt"

# 4) 多样走动补采 (R5 同款 6 阶段)
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
frame=0
snap() { frame=$((frame+1)); sh_ "screencap -p /data/local/tmp/f.png" >/dev/null; adb -s "$SERIAL" pull /data/local/tmp/f.png "$OUT/frames_raw/frame_$(printf %04d $frame).png" >/dev/null; }
LX=$((W * 22 / 100)); MY=$((H * 70 / 100)); RY=$((H * 50 / 100))
fwd() { sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 3000" >/dev/null; }
for k in 1 2 3; do fwd & sleep 0.7; snap; sleep 0.7; snap; sleep 0.7; snap; wait || true; done
for k in 1 2; do sh_ "input swipe $((W * 90 / 100)) $RY $((W * 12 / 100)) $RY 2200" >/dev/null & for j in 1 2 3 4 5; do sleep 0.4; snap; done; wait || true; done
sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 4000" >/dev/null & for j in 1 2 3 4 5 6 7 8; do sh_ "input swipe $((W * 70 / 100)) $RY $((W * 62 / 100)) $((RY + 20)) 350" >/dev/null; sleep 0.35; snap; done; wait || true
sh_ "input swipe $((W * 15 / 100)) $MY $((W * 30 / 100)) $MY 2500" >/dev/null & for j in 1 2 3 4 5 6 7 8; do sleep 0.3; snap; done; wait || true
echo "FRAMES_RAW=$frame" | tee -a "$OUT/steps.txt"

# 5) 裁条 + 严格去重 + frames_kept.txt
python3 - "$OUT" <<'PY'
import glob, os, sys
from PIL import Image
out = sys.argv[1]
kept, prev = [], None
for f in sorted(glob.glob(out + "/frames_raw/frame_*.png")):
    im = Image.open(f); w, h = im.size
    im = im.crop((0, int(h*0.03), w, int(h*0.94)))
    g = im.convert("L").resize((128, 72)); px = list(g.getdata())
    if prev is not None:
        if sum(abs(a-b) for a, b in zip(px, prev)) / len(px) <= 8.0: continue
    prev = px
    name = os.path.basename(f).replace("frame_", "kept_")
    im.save(os.path.join(out, "frames", name)); kept.append(name)
open(os.path.join(out, "frames_kept.txt"), "w").write("\n".join(kept) + "\n")
print("KEPT=", len(kept))
PY
ALIVE=$(sh_ "pidof $PKG || true" | tr -d '\r'); echo "ALIVE_AFTER=$ALIVE"
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "rm -rf /data/local/tmp/doom_stage /data/local/tmp/doom.pk4 /data/local/tmp/doom3.pk4" >/dev/null
echo "HONOR_R8_DONE KEPT=$(ls "$OUT/frames" 2>/dev/null | grep -c png || true) RAW=$frame" | tee -a "$OUT/steps.txt"
