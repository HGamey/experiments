#!/bin/sh
# 诊断: 把 weapon_base.script 覆盖 patch 进 demo00.pk4 (zip 替换成员), 启动 +map 后截图。
# 用法: experiment doom/patch_pk4_launch.sh <serial>
# 背景: loose override 放 base/script、state/script、根 script 均无效(诊断23-25),
#       控制台持续报 weapon_fists 缺 WEAPON_NETFIRING → Map Shutdown (human 读图)。
set -eu
SERIAL="${1:?usage: patch_pk4_launch.sh <serial>}"
PKG=com.karin.idTech4Amm
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
UID_=$(adb -s "$SERIAL" shell "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://'" | tr -d ' \r')
[ -n "$UID_" ] || UID_=10499
OUT="${PF_JOB_OUT:-$(pwd)/pk4_out}/patch_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL UID=$UID_"

CACHE=/Users/mac/hgamey/phonefarm/_games/cache
cp "$CACHE/blobs/$PK4_SHA" "$OUT/demo00_patched.pk4"
mkdir -p "$OUT/script"
cp "$CACHE/blobs/$SCRIPT_SHA" "$OUT/script/weapon_base.script"
sha256sum "$OUT/demo00_patched.pk4" "$OUT/script/weapon_base.script"
# pk4=zip; 成员名 script/weapon_base.script (先确认存在)
unzip -l "$OUT/demo00_patched.pk4" | grep -E 'script/weapon_base' || echo "NOTE: 原pk4无该成员, 将新增"
(cd "$OUT" && zip -q demo00_patched.pk4 script/weapon_base.script)
unzip -p "$OUT/demo00_patched.pk4" script/weapon_base.script | sha256sum | grep -q "$SCRIPT_SHA" || { echo "FATAL: patch member mismatch"; exit 4; }
echo "PATCHED_SIZE=$(stat -f %z "$OUT/demo00_patched.pk4" 2>/dev/null || stat -c %s "$OUT/demo00_patched.pk4")"

adb -s "$SERIAL" push "$OUT/demo00_patched.pk4" /data/local/tmp/demo00_patched.pk4 >/dev/null
sh_ "su -c 'am force-stop $PKG; cp /data/local/tmp/demo00_patched.pk4 /data/media/0/diii4a/doom3/base/demo00.pk4 && chown $UID_:$UID_ /data/media/0/diii4a/doom3/base/demo00.pk4 && chmod 660 /data/media/0/diii4a/doom3/base/demo00.pk4 && sha256sum /data/media/0/diii4a/doom3/base/demo00.pk4'"

BASE=/storage/emulated/0/diii4a/doom3
STATE=/storage/emulated/0/diii4a/state
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set developer 1 +set com_showFPS 1 +set r_multiSamples 0 +map game/demo_mars_city1'" | tail -2
sleep 80
echo "PID=$(sh_ "pidof $PKG" | tr -d '\r')"
sh_ "screencap -p /data/local/tmp/n6.png"
adb -s "$SERIAL" pull /data/local/tmp/n6.png "$OUT/n6.png" >/dev/null
sleep 8
sh_ "input swipe 1800 1300 1100 1350 400"
sleep 3
sh_ "screencap -p /data/local/tmp/n7.png"
adb -s "$SERIAL" pull /data/local/tmp/n7.png "$OUT/n7.png" >/dev/null
python3 - "$OUT/n6.png" "$OUT/n7.png" <<'PYEOF' || true
import sys
from PIL import Image
import numpy as np
imgs=[np.asarray(Image.open(p).convert('L'),dtype=np.float32) for p in sys.argv[1:3]]
for p,a in zip(sys.argv[1:3],imgs): print(p.split('/')[-1],'mean=%.1f var=%.0f'%(a.mean(),a.var()))
d=np.abs(imgs[0]-imgs[1]); print('diff mean=%.2f frac>10=%.4f'%(d.mean(),(d>10).mean()))
PYEOF
echo PATCH_LAUNCH_DONE "$OUT"
