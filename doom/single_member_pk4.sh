#!/bin/sh
# 诊断28: pk4 内有重复 script/weapon_base.script(2004旧2633B无NETFIRING + 2026新2662B),
# 引擎读到旧成员 → weapon_fists 报错不消。本脚本:
#  1) zip -d 删掉全部该成员, 再加入 2662B override (单一成员, 含 WEAPON_NETFIRING)
#  2) 部署到 /storage/emulated/0/diii4a/doom3/base/demo00.pk4
#  3) 启动 + 截图两张(划视角), 输出帧统计
#  4) 顺带把缓存 blob 也重建为"删旧加新"版(记录原 sha 与新 sha, 供 lead 决定是否回滚)
set -eu
SERIAL="${1:?usage: single_member_pk4.sh <serial>}"
PKG=com.karin.idTech4Amm
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
UID_=$(adb -s "$SERIAL" shell "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://'" | tr -d ' \r')
[ -n "$UID_" ] || UID_=10499
OUT="${PF_JOB_OUT:-$(pwd)/pk4_out}/single_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL UID=$UID_"

CACHE=/Users/mac/hgamey/phonefarm/_games/cache
echo "CACHE_BLOB_SHA=$(shasum -a 256 "$CACHE/blobs/$PK4_SHA" | cut -d ' ' -f 1)"
cp "$CACHE/blobs/$PK4_SHA" "$OUT/demo00_fixed.pk4"; chmod u+w "$OUT/demo00_fixed.pk4"
# 删全部旧成员再放单一 override
zip -q -d "$OUT/demo00_fixed.pk4" 'script/weapon_base.script' || true
mkdir -p "$OUT/zfix/script"
cp "$CACHE/blobs/$SCRIPT_SHA" "$OUT/zfix/script/weapon_base.script"
(cd "$OUT/zfix" && zip -q ../demo00_fixed.pk4.scriptonly script/weapon_base.script 2>/dev/null || true)
# 上面小pk4不需要; 直接把成员加进 fixed:
(cd "$OUT/zfix" && zip -q "$OUT/demo00_fixed.pk4" script/weapon_base.script)
echo "MEMBER_COUNT=$(unzip -l "$OUT/demo00_fixed.pk4" | grep -c 'script/weapon_base.script')"
echo "MEMBER_SHA=$(unzip -p "$OUT/demo00_fixed.pk4" script/weapon_base.script | shasum -a 256 | cut -d ' ' -f 1)"
[ "$(unzip -p "$OUT/demo00_fixed.pk4" script/weapon_base.script | shasum -a 256 | cut -d ' ' -f 1)" = "$SCRIPT_SHA" ] || { echo "FATAL: member mismatch"; exit 4; }
echo "FIXED_SIZE=$(stat -f %z "$OUT/demo00_fixed.pk4" 2>/dev/null || stat -c %s "$OUT/demo00_fixed.pk4")"
echo "FIXED_SHA=$(shasum -a 256 "$OUT/demo00_fixed.pk4" | cut -d ' ' -f 1)"

adb -s "$SERIAL" push "$OUT/demo00_fixed.pk4" /data/local/tmp/demo00_fixed.pk4 >/dev/null
sh_ "su -c 'am force-stop $PKG; cp /data/local/tmp/demo00_fixed.pk4 /data/media/0/diii4a/doom3/base/demo00.pk4 && chown $UID_:$UID_ /data/media/0/diii4a/doom3/base/demo00.pk4 && chmod 660 /data/media/0/diii4a/doom3/base/demo00.pk4; rm -f /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4; sha256sum /data/media/0/diii4a/doom3/base/demo00.pk4'"

BASE=/storage/emulated/0/diii4a/doom3
STATE=/storage/emulated/0/diii4a/state
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set developer 1 +set com_showFPS 1 +set r_multiSamples 0 +map game/demo_mars_city1'" | tail -2
sleep 85
echo "PID=$(sh_ "pidof $PKG" | tr -d '\r')"
sh_ "screencap -p /data/local/tmp/s1.png"
adb -s "$SERIAL" pull /data/local/tmp/s1.png "$OUT/s1.png" >/dev/null
sh_ "input swipe 1800 1300 1100 1350 400"
sleep 3
sh_ "screencap -p /data/local/tmp/s2.png"
adb -s "$SERIAL" pull /data/local/tmp/s2.png "$OUT/s2.png" >/dev/null
python3 - "$OUT/s1.png" "$OUT/s2.png" <<'PYEOF' || true
import sys
from PIL import Image
import numpy as np
imgs=[np.asarray(Image.open(p).convert('L'),dtype=np.float32) for p in sys.argv[1:3]]
for p,a in zip(sys.argv[1:3],imgs): print(p.split('/')[-1],'mean=%.1f var=%.0f'%(a.mean(),a.var()))
d=np.abs(imgs[0]-imgs[1]); print('diff mean=%.2f frac>10=%.4f'%(d.mean(),(d>10).mean()))
PYEOF
echo SINGLE_DONE "$OUT"
