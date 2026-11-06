#!/bin/sh
# 诊断31 (lead/Codex 指示): zzz_pf_fix.pk4(唯一成员 script/weapon_base.script, 不带base/前缀)
# 放 /storage/emulated/0/diii4a/doom3/base/, fs_restrict 0 + fs_debug 1, force-stop 后重启。
# 依据: v73 散文件先于pk4、pk4文件名倒序; fs_restrict=1 会跳过散装 .script (Codex FileSystem.cpp:2465-2523)。
set -eu
SERIAL="${1:?usage: lead_zfix_launch.sh <serial>}"
PKG=com.karin.idTech4Amm
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
UID_=$(adb -s "$SERIAL" shell "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://'" | tr -d ' \r')
[ -n "$UID_" ] || UID_=10499
OUT="${PF_JOB_OUT:-$(pwd)/pk4_out}/leadzfix_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
echo "OUT=$OUT SERIAL=$SERIAL UID=$UID_"

CACHE=/Users/mac/hgamey/phonefarm/_games/cache
mkdir -p "$OUT/pkg/script"
cp "$CACHE/blobs/$SCRIPT_SHA" "$OUT/pkg/script/weapon_base.script"
(cd "$OUT/pkg" && zip -q -r ../zzz_pf_fix.pk4 script)
unzip -l "$OUT/zzz_pf_fix.pk4"
adb -s "$SERIAL" push "$OUT/zzz_pf_fix.pk4" /data/local/tmp/zzz_pf_fix.pk4 >/dev/null
sh_ "su -c 'am force-stop $PKG; mkdir -p /data/media/0/diii4a/doom3/base; cp /data/local/tmp/zzz_pf_fix.pk4 /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && chown $UID_:$UID_ /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && chmod 660 /data/media/0/diii4a/doom3/base/zzz_pf_fix.pk4 && ls -la /data/media/0/diii4a/doom3/base/'"

BASE=/storage/emulated/0/diii4a/doom3
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $BASE +set fs_restrict 0 +set fs_debug 1 +set developer 1 +set com_showFPS 1 +set r_multiSamples 0 +map game/demo_mars_city1'" | tail -2
sleep 85
echo "PID=$(sh_ "pidof $PKG" | tr -d '\r')"
sh_ "screencap -p /data/local/tmp/v1.png"
adb -s "$SERIAL" pull /data/local/tmp/v1.png "$OUT/v1.png" >/dev/null
sh_ "input swipe 1800 1300 1100 1350 400"
sleep 3
sh_ "screencap -p /data/local/tmp/v2.png"
adb -s "$SERIAL" pull /data/local/tmp/v2.png "$OUT/v2.png" >/dev/null
python3 - "$OUT/v1.png" "$OUT/v2.png" <<'PYEOF' || true
import sys
from PIL import Image
import numpy as np
imgs=[np.asarray(Image.open(p).convert('L'),dtype=np.float32) for p in sys.argv[1:3]]
for p,a in zip(sys.argv[1:3],imgs):
    b=(a>200); ys,xs=np.where(b)
    print(p.split('/')[-1],'mean=%.1f var=%.0f bright_bbox y%d-%d'%(a.mean(),a.var(),ys.min(),ys.max()))
d=np.abs(imgs[0]-imgs[1]); print('diff mean=%.2f frac>10=%.4f'%(d.mean(),(d>10).mean()))
PYEOF
echo LEADZFIX_DONE "$OUT"
