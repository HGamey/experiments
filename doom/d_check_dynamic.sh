#!/bin/sh
# D 轨 v13: 开关已ON(v11像素+亮度验证) → 进图 → 先验画面动态性(两张5s差分) → 动态才切三档F9/F8(138/139)
set -eu
SERIAL="${1:?usage: d_check_dynamic.sh <serial>}"
PKG=com.karin.idTech4Amm
BASE=/storage/emulated/0/Android/data/$PKG/files/diii4a/doom3
STATE=/storage/emulated/0/Android/data/$PKG/files/diii4a/state
OUT="${PF_JOB_OUT:-$(pwd)/sr_out}/d_dyn_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT"
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set com_showFPS 1 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +exec sr_keys.cfg +exec sr_native.cfg +map game/demo_mars_city1'" | tail -2
P=""; i=0; while [ $i -lt 20 ]; do sleep 3; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+3)); done
[ -n "$P" ] || { echo "LAUNCH_DEAD"; exit 6; }
echo "GAME_PID=$P" | tee -a "$OUT/steps.txt"
sleep 150
sh_ "input tap 636 700" >/dev/null 2>&1 || true; sleep 3
sh_ "input keyevent 111" >/dev/null 2>&1 || true; sleep 12
# 动态性: 两张5s差
sh_ "screencap -p /data/local/tmp/dyn1.png" >/dev/null
sleep 5
sh_ "screencap -p /data/local/tmp/dyn2.png" >/dev/null
adb -s "$SERIAL" pull /data/local/tmp/dyn1.png "$OUT/dyn1.png" >/dev/null 2>&1 || true
adb -s "$SERIAL" pull /data/local/tmp/dyn2.png "$OUT/dyn2.png" >/dev/null 2>&1 || true
DYN=$(python3 - "$OUT/dyn1.png" "$OUT/dyn2.png" 2>/dev/null <<'PY'
import sys
try:
    from PIL import Image, ImageChops
    a=Image.open(sys.argv[1]).convert("L").resize((160,80)); b=Image.open(sys.argv[2]).convert("L").resize((160,80))
    d=ImageChops.difference(a,b).getdata()
    print(round(sum(d)/len(d),2))
except Exception: print("?")
PY
)
echo "DYNAMIC_DIFF=$DYN (>2=画面在动)" | tee -a "$OUT/steps.txt"
# 三档: F9(139)=0, F8(138)=1, F8(138)=2
shot_() { sh_ "screencap -p /data/local/tmp/t_$1_$2.png" >/dev/null; adb -s "$SERIAL" pull "/data/local/tmp/t_$1_$2.png" "$OUT/t_$1_$2.png" >/dev/null 2>&1 || true; }
sh_ "input keyevent 139" >/dev/null 2>&1 || true; sleep 8
shot_ native 1; sleep 8; shot_ native 2
sh_ "input keyevent 138" >/dev/null 2>&1 || true; sleep 8
shot_ off 1; sleep 8; shot_ off 2
sh_ "input keyevent 138" >/dev/null 2>&1 || true; sleep 8
shot_ on 1; sleep 8; shot_ on 2
shasum -a 256 "$OUT"/t_*.png 2>/dev/null > "$OUT/shots_sha.txt" || true
UNIQ=$(cut -d ' ' -f 1 "$OUT/shots_sha.txt" 2>/dev/null | sort -u | wc -l)
echo "UNIQUE_SHOTS=$UNIQ" | tee -a "$OUT/steps.txt"
cat "$OUT/shots_sha.txt" 2>/dev/null | head -8
sh_ "logcat -d 2>/dev/null | grep -icE 'smallsr' || true" > "$OUT/logcat_smallsr_count.txt" || true
echo "LOGCAT_SMALLSR_COUNT=$(cat "$OUT/logcat_smallsr_count.txt")" | tee -a "$OUT/steps.txt"
sh_ "am force-stop $PKG" >/dev/null || true
echo "D13_DONE" | tee -a "$OUT/steps.txt"
