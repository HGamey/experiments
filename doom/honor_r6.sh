#!/bin/sh
# 荣耀 B 轨 R6 (lead 无root方案): pm clear 清私有目录 → 数据只放公共 /storage/emulated/0/diii4a/doom3
# → Q3EMain+command 启动(force-stop在前) → 进图确认截图; 若动态画面则就地多样走动补采。
set -eu
SERIAL="${1:?usage: honor_r6.sh <serial>}"
PKG=com.karin.idTech4Amm
APK_SHA=34d9383bb6ccf641c9e6f09bc009e0d1c80bb27ae0207cefe5c46ba7e86a8dcd
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
CACHE="${PF_GAME_CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
OUT="${PF_JOB_OUT:-$(pwd)/doom_out}/doom_honorR6_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT/frames_raw" "$OUT/frames"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL"

# 1) 清: force-stop + pm clear (无需 root, 连私有目录与 prefs 一起清)
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true
sh_ "pm clear $PKG" | tee -a "$OUT/steps.txt"
echo "STEP1_CLEARED" >> "$OUT/steps.txt"

# 2) 只铺公共目录: 修复 pk4 (内联 patch 单成员) + state
BASE=/storage/emulated/0/diii4a/doom3; STATE=/storage/emulated/0/diii4a/state
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
sh_ "mkdir -p /data/local/tmp/doom_stage/doom3/base /data/local/tmp/doom_stage/state/base/script"
PK4F="$OUT/demo00_fixed.pk4"; cp "$CACHE/blobs/$PK4_SHA" "$PK4F"; chmod u+w "$PK4F"
zip -q -d "$PK4F" 'script/weapon_base.script' 2>/dev/null || true
mkdir -p "$OUT/zfix/script"; cp "$CACHE/blobs/$SCRIPT_SHA" "$OUT/zfix/script/weapon_base.script"
(cd "$OUT/zfix" && zip -q "$PK4F" script/weapon_base.script)
[ "$(unzip -p "$PK4F" script/weapon_base.script | shasum -a 256 | cut -d ' ' -f 1)" = "$SCRIPT_SHA" ] || { echo "FATAL: patch mismatch"; exit 4; }
FIXED_SHA=$(shasum -a 256 "$PK4F" | cut -d ' ' -f 1); echo "PK4_FIXED_SHA=$FIXED_SHA"
adb -s "$SERIAL" push "$PK4F" /data/local/tmp/doom_stage/doom3/base/demo00.pk4 >/dev/null
sh_ "mkdir -p /storage/emulated/0/diii4a && rm -rf $BASE $STATE && cp -r /data/local/tmp/doom_stage/doom3 /data/local/tmp/doom_stage/state /storage/emulated/0/diii4a/"
sh_ "sha256sum $BASE/base/demo00.pk4" | grep -q "$FIXED_SHA" || { echo "FATAL: public pk4 sha mismatch"; exit 4; }
# R6(53f79b84816f)证实: pm clear后私有目录为空, 仅公共数据引擎不起(无pid) => 双铺(R5 cae1e123857e已验证可进图)
UID_=$(sh_ "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://' | tr -d ' '" | head -1)
PDATA=/storage/emulated/0/Android/data/$PKG/files/diii4a
sh_ "mkdir -p $PDATA && cp -r /data/local/tmp/doom_stage/doom3 /data/local/tmp/doom_stage/state $PDATA/"
sh_ "sha256sum $PDATA/doom3/base/demo00.pk4" | grep -q "$FIXED_SHA" || { echo "FATAL: private pk4 sha mismatch"; exit 4; }
echo "PUBLIC_SEEDED=ok PRIVATE_SEEDED=ok UID=$UID_" | tee -a "$OUT/steps.txt"

# 3) 启动 (Q3EMain 读 game+command; force-stop 已做)
sh_ "input keyevent 224; wm dismiss-keyguard" >/dev/null 2>&1 || true
CMD="game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set com_showFPS 1 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +map game/demo_mars_city1"
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command \"$CMD\"" | tail -2
P=""; i=0; while [ $i -lt 20 ]; do sleep 3; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+3)); done
[ -n "$P" ] || { echo "FATAL: no game pid"; exit 6; }
echo "GAME_PID=$P"
sleep 120
sh_ "uiautomator dump /data/local/tmp/u.xml" >/dev/null 2>&1 || true
sh_ "cat /data/local/tmp/u.xml" > "$OUT/ui_first.xml" 2>/dev/null || true
if grep -qE '数据文件|没有被发现' "$OUT/ui_first.xml" 2>/dev/null; then echo "FATAL: data dialog again"; grep -oE 'text="[^"]{1,120}"' "$OUT/ui_first.xml" | grep -v 'text=""' | head -5; exit 7; fi
sh_ "screencap -p /data/local/tmp/confirm.png" >/dev/null
adb -s "$SERIAL" pull /data/local/tmp/confirm.png "$OUT/confirm_enter_map.png" >/dev/null
echo "STEP3_LAUNCHED pid=$P" >> "$OUT/steps.txt"

# 4) 跳过过场 + 多样走动补采 (6 阶段, 与 R5 同款)
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
sh_ "input tap $((W / 2)) $((H / 2))" >/dev/null 2>&1 || true; sleep 3
sh_ "input keyevent 111" >/dev/null 2>&1 || true; sleep 10
frame=0
snap() { frame=$((frame+1)); sh_ "screencap -p /data/local/tmp/f.png" >/dev/null; adb -s "$SERIAL" pull /data/local/tmp/f.png "$OUT/frames_raw/frame_$(printf %04d $frame).png" >/dev/null; }
LX=$((W * 22 / 100)); MY=$((H * 70 / 100)); RY=$((H * 50 / 100))
fwd() { sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 3000" >/dev/null; }
for k in 1 2 3; do
  fwd & sleep 0.7; snap; sleep 0.7; snap; sleep 0.7; snap; wait || true
done
for k in 1 2; do
  sh_ "input swipe $((W * 90 / 100)) $RY $((W * 12 / 100)) $RY 2200" >/dev/null &
  for j in 1 2 3 4 5; do sleep 0.4; snap; done; wait || true
done
sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 4000" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sh_ "input swipe $((W * 70 / 100)) $RY $((W * 62 / 100)) $((RY + 20)) 350" >/dev/null; sleep 0.35; snap; done; wait || true
sh_ "input swipe $((W * 15 / 100)) $MY $((W * 30 / 100)) $MY 2500" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sleep 0.3; snap; done; wait || true
echo "FRAMES_RAW=$frame" | tee -a "$OUT/steps.txt"

# 5) 裁系统条 + 严格去重 + frames_kept.txt
python3 - "$OUT" <<'PY'
import glob, os, sys
from PIL import Image
out = sys.argv[1]
raw = sorted(glob.glob(out + "/frames_raw/frame_*.png"))
kept, prev = [], None
for f in raw:
    im = Image.open(f); w, h = im.size
    im = im.crop((0, int(h*0.03), w, int(h*0.94)))
    g = im.convert("L").resize((128, 72)); px = list(g.getdata())
    if prev is not None:
        d = sum(abs(a-b) for a, b in zip(px, prev)) / len(px)
        if d <= 8.0: continue
    prev = px
    name = os.path.basename(f).replace("frame_", "kept_")
    im.save(os.path.join(out, "frames", name)); kept.append(name)
open(os.path.join(out, "frames_kept.txt"), "w").write("\n".join(kept) + "\n")
print("KEPT=", len(kept), "of", len(raw))
PY
ALIVE=$(sh_ "pidof $PKG || true" | tr -d '\r'); echo "ALIVE_AFTER=$ALIVE"
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
echo "HONOR_R6_DONE KEPT=$(ls "$OUT/frames" 2>/dev/null | grep -c png || true) RAW=$frame"
