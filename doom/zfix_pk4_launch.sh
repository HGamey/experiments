#!/bin/sh
# 诊断27: lead 建议的 idTech 补丁法 — 小 pk4 (zzz_pf_fix.pk4, 字母序在 demo00 后) 只含
# script/weapon_base.script (2662B override), 放 /storage/emulated/0/diii4a/doom3/base/。
# 同时恢复原始 demo00.pk4 (undo 26b 的内联 patch), 并检查原 pk4 内该成员的 NETFIRING 状态。
set -eu
SERIAL="${1:?usage: zfix_pk4_launch.sh <serial>}"
PKG=com.karin.idTech4Amm
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
UID_=$(adb -s "$SERIAL" shell "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://'" | tr -d ' \r')
[ -n "$UID_" ] || UID_=10499
OUT="${PF_JOB_OUT:-$(pwd)/pk4_out}/zfix_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL UID=$UID_"

CACHE=/Users/mac/hgamey/phonefarm/_games/cache
# 1) 检查原 pk4 内 script/weapon_base.script 是否已有 NETFIRING (决定错误来源)
cp "$CACHE/blobs/$PK4_SHA" "$OUT/orig_demo00.pk4"; chmod u+w "$OUT/orig_demo00.pk4"
unzip -p "$OUT/orig_demo00.pk4" script/weapon_base.script 2>/dev/null | grep -c WEAPON_NETFIRING > "$OUT/pk4_netfiring_count.txt" || echo 0 > "$OUT/pk4_netfiring_count.txt"
echo "PK4_MEMBER_NETFIRING_COUNT=$(cat "$OUT/pk4_netfiring_count.txt")"
unzip -l "$OUT/orig_demo00.pk4" | grep -E 'script/weapon' | head -10 || echo "PK4_SCRIPT_MEMBERS=none"

# 2) 恢复原始 demo00.pk4 到设备 (undo 26b patch)
adb -s "$SERIAL" push "$OUT/orig_demo00.pk4" /data/local/tmp/demo00_orig.pk4 >/dev/null
sh_ "su -c 'am force-stop $PKG; cp /data/local/tmp/demo00_orig.pk4 /data/media/0/diii4a/doom3/base/demo00.pk4 && chown $UID_:$UID_ /data/media/0/diii4a/doom3/base/demo00.pk4 && chmod 660 /data/media/0/diii4a/doom3/base/demo00.pk4 && sha256sum /data/media/0/diii4a/doom3/base/demo00.pk4'"

# 3) 造小补丁 pk4 (字母序 demo00 < zzz_pf_fix, 后加载应覆盖)
mkdir -p "$OUT/zfix/script"
cp "$CACHE/blobs/$SCRIPT_SHA" "$OUT/zfix/script/weapon_base.script"
(cd "$OUT/zfix" && zip -q -r ../zzz_pf_fix.pk4 script)
unzip -l "$OUT/zzz_pf_fix.pk4"
adb -s "$SERIAL" push "$OUT/zzz_pf_fix.pk4" /data/local/tmp/zzz_pf_fix.pk4 >/dev/null
sh_ "su -c 'cp /data/local/tmp/zzz_pf_fix.pk4 /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && chown $UID_:$UID_ /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && chmod 660 /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && ls -la /data/media/0/diii4a/doom3/base/'"

# 4) 顺手清掉诊断23-25散落的 loose 副本 (隔离变量: 只留 zzz_pf_fix.pk4 这一处 override)
sh_ "su -c 'rm -f /data/media/0/diii4a/doom3/base/script/weapon_base.script /data/media/0/diii4a/doom3/script/weapon_base.script /data/media/0/diii4a/state/script/weapon_base.script; rm -rf /data/media/0/diii4a/doom3/base/script /data/media/0/diii4a/doom3/script /data/media/0/diii4a/state/script; ls /data/media/0/diii4a/doom3/base/ /data/media/0/diii4a/state/base/'"

# 5) 启动 + 截图两张 (第二张前划视角; 若进图画面应有明显差异)
BASE=/storage/emulated/0/diii4a/doom3
STATE=/storage/emulated/0/diii4a/state
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set developer 1 +set com_showFPS 1 +set r_multiSamples 0 +map game/demo_mars_city1'" | tail -2
sleep 85
echo "PID=$(sh_ "pidof $PKG" | tr -d '\r')"
sh_ "screencap -p /data/local/tmp/z1.png"
adb -s "$SERIAL" pull /data/local/tmp/z1.png "$OUT/z1.png" >/dev/null
sh_ "input swipe 1800 1300 1100 1350 400"
sleep 3
sh_ "screencap -p /data/local/tmp/z2.png"
adb -s "$SERIAL" pull /data/local/tmp/z2.png "$OUT/z2.png" >/dev/null
python3 - "$OUT/z1.png" "$OUT/z2.png" <<'PYEOF' || true
import sys
from PIL import Image
import numpy as np
imgs=[np.asarray(Image.open(p).convert('L'),dtype=np.float32) for p in sys.argv[1:3]]
for p,a in zip(sys.argv[1:3],imgs): print(p.split('/')[-1],'mean=%.1f var=%.0f'%(a.mean(),a.var()))
d=np.abs(imgs[0]-imgs[1]); print('diff mean=%.2f frac>10=%.4f'%(d.mean(),(d>10).mean()))
PYEOF
echo ZFIX_DONE "$OUT"
