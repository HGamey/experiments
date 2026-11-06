#!/bin/sh
# 诊断32 (lead/Codex 源码结论): Q3EMain 同时读 game+command extra; 必须 force-stop(singleTask 不重读命令)。
# 最小入口(无 +map): 启动后逐条向控制台键入 fs_basepath/fs_savepath/path/fs_debug 1/
#   touchFile script/weapon_base.script/fs_debug 0, 每条截图——touchFile 会打印 found in '<真实位置>'。
# 确认命中补丁脚本后, 再键入 map 进图, 截游戏画面。
set -eu
SERIAL="${1:?usage: console_touchfile_probe.sh <serial>}"
PKG=com.karin.idTech4Amm
OUT="${PF_JOB_OUT:-$(pwd)/probe_out}/console_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
shot() { sh_ "screencap -p /data/local/tmp/$1.png"; adb -s "$SERIAL" pull "/data/local/tmp/$1.png" "$OUT/$1.png" >/dev/null; }
echo "OUT=$OUT SERIAL=$SERIAL"

# 1) 最小入口启动 (lead 给的命令, force-stop 先行)
sh_ "am force-stop $PKG"
sleep 1
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath /storage/emulated/0/diii4a/doom3 +set fs_savepath /storage/emulated/0/diii4a/doom3 +set fs_restrict 0 +set com_allowConsole 1'" | tail -2
sleep 45
echo "PID45=$(sh_ "pidof $PKG" | tr -d '\r')"
shot s00_startup

# 2) 控制台逐条键入 (input text 空格用 %s; 回车 keyevent 66)
cmd_type() { sh_ "input text '$1'"; sh_ "input keyevent 66"; sleep 4; shot "$2"; }
cmd_type "fs_basepath" s01_fsbasepath
cmd_type "fs_savepath" s02_fssavepath
cmd_type "path" s03_path
cmd_type "fs_debug%s1" s04_fsdebug
cmd_type "touchFile%sscript/weapon_base.script" s05_touchfile
cmd_type "fs_debug%s0" s06_fsdebug_off

# 3) 进图
cmd_type "map%sgame/demo_mars_city1" s07_map_cmd
sleep 85
echo "PID_MAP=$(sh_ "pidof $PKG" | tr -d '\r')"
shot s08_map_early
sh_ "input swipe 1800 1300 1100 1350 400"
sleep 3
shot s09_map_swipe
sleep 3
sh_ "input swipe 900 1800 900 1200 500"
sleep 3
shot s10_map_swipe2

# 4) 统计
python3 - "$OUT"/s0*.png "$OUT"/s10*.png <<'PYEOF' || true
import sys, glob
from PIL import Image
import numpy as np
for p in sorted(glob.glob(sys.argv[1]+'/*.png')):
    a=np.asarray(Image.open(p).convert('L'),dtype=np.float32)
    b=(a>200); bb=''
    if b.any():
        ys,xs=np.where(b); bb=' bright_bbox y%d-%d'%(ys.min(),ys.max())
    print(p.split('/')[-1],'mean=%.1f var=%.0f%s'%(a.mean(),a.var(),bb))
PYEOF
echo CONSOLE_PROBE_DONE "$OUT"
