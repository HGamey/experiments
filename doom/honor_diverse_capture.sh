#!/bin/sh
# B 轨补采(human 指令 n-0cad408146d8 / n-c6955f032745, 2026-11-06):
#   1) R7 修复装荣耀: 内联 patch pk4 -> Android/data 私有 diii4a -> 进图 -> 截一张确认图
#   2) 补采多样画面: 关 fps 显示, 脚本化走动(多个朝向+前进+扫视), 严格去重(128x72 diff>8),
#      输出 frames_kept.txt, 后处理裁掉底部系统横条(底部6%)与顶部状态条(顶部3%)
#   用法: experiment doom/honor_diverse_capture.sh <serial> [FRAMES=60] [LOOK_ONLY=0]
set -eu
SERIAL="${1:?usage: honor_diverse_capture.sh <serial> [KEY=VALUE]}"
shift || true
for kv in "$@"; do export "$kv"; done
FRAMES="${FRAMES:-60}"
PKG=com.karin.idTech4Amm
APK_SHA=34d9383bb6ccf641c9e6f09bc009e0d1c80bb27ae0207cefe5c46ba7e86a8dcd
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
CACHE="${PF_GAME_CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
OUT="${PF_JOB_OUT:-$(pwd)/doom_out}/doom_honor_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT/frames_raw" "$OUT/frames"
echo "OUT=$OUT FRAMES=$FRAMES SERIAL=$SERIAL"

sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
push_() { adb -s "$SERIAL" push "$1" "$2" 2>&1 | tail -1; }
pull_() { adb -s "$SERIAL" pull "$1" "$2" 2>&1 | tail -1; }

# 1) 素材
APK="$CACHE/blobs/$APK_SHA"; PK4="$CACHE/blobs/$PK4_SHA"; SCRIPT_SRC="$CACHE/blobs/$SCRIPT_SHA"
[ -f "$APK" ] && [ -f "$PK4" ] && [ -f "$SCRIPT_SRC" ] || { echo "FATAL: cache blobs missing"; exit 3; }

# 2) 安装: 荣耀 adb install -r 会弹 USB 安装确认框导致永久卡死(F41); v73 已装则直接复用
sh_ "am kill-all" >/dev/null || true
APKF="$OUT/idtech4amm-v73-arm64.apk"; cp "$APK" "$APKF"
HAVE=$(sh_ "pm path $PKG 2>/dev/null | head -1")
if [ -n "$HAVE" ]; then
  echo "REUSE_INSTALLED: $HAVE"
else
  adb -s "$SERIAL" install -r -g "$APKF" > "$OUT/install.log" 2>&1 &
  IPID=$!
  # 300s 内每 10s 点一次『继续』解确认框, 超时杀掉
  for t in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    sleep 10; kill -0 $IPID 2>/dev/null || break
    sh_ "uiautomator dump /data/local/tmp/doom_ui.xml >/dev/null 2>&1; grep -q '继续' /data/local/tmp/doom_ui.xml && input tap $((W * 62 / 100)) $((H * 62 / 100)) || true" >/dev/null
  done
  kill -0 $IPID 2>/dev/null && { kill $IPID; echo "FATAL: install stuck >300s"; exit 5; }
  wait $IPID || true
  INST_OUT=$(tail -2 "$OUT/install.log"); echo "INSTALL: $INST_OUT"
  echo "$INST_OUT" | grep -qi success || { echo "FATAL: install failed"; exit 5; }
fi

# 3) patch pk4 (单一 weapon_base.script 成员 = 2662B override)
BASE=/storage/emulated/0/Android/data/$PKG/files/diii4a/doom3
STATE=/storage/emulated/0/Android/data/$PKG/files/diii4a/state
UID_=$(sh_ "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://' | tr -d ' '" | head -1)
[ -n "$UID_" ] || UID_=$(sh_ "su -c 'stat -c %u /data/data/$PKG'" | head -1 | tr -d ' ')
echo "APP_UID=$UID_"
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
sh_ "mkdir -p /data/local/tmp/doom_stage/doom3/base /data/local/tmp/doom_stage/state/base/script"
PK4F="$OUT/demo00_fixed.pk4"; cp "$PK4" "$PK4F"; chmod u+w "$PK4F"
zip -q -d "$PK4F" 'script/weapon_base.script' 2>/dev/null || true
mkdir -p "$OUT/zfix/script"; cp "$SCRIPT_SRC" "$OUT/zfix/script/weapon_base.script"
(cd "$OUT/zfix" && zip -q "$PK4F" script/weapon_base.script)
[ "$(unzip -p "$PK4F" script/weapon_base.script | shasum -a 256 | cut -d ' ' -f 1)" = "$SCRIPT_SHA" ] || { echo "FATAL: patch member mismatch"; exit 4; }
[ "$(unzip -l "$PK4F" | grep -c 'script/weapon_base.script')" = "1" ] || { echo "FATAL: duplicate member"; exit 4; }
FIXED_SHA=$(shasum -a 256 "$PK4F" | cut -d ' ' -f 1); echo "PK4_FIXED_SHA=$FIXED_SHA"
push_ "$PK4F" /data/local/tmp/doom_stage/doom3/base/demo00.pk4 >/dev/null
sh_ "sha256sum /data/local/tmp/doom_stage/doom3/base/demo00.pk4" | grep -q "$FIXED_SHA" || { echo "FATAL: pk4 sha mismatch on device"; exit 4; }
# 清旧私有目录残留(R4 旧包会盖住修复), 重铺
sh_ "su -c 'rm -rf /data/media/0/Android/data/$PKG/files/diii4a && mkdir -p /data/media/0/Android/data/$PKG/files/diii4a && cp -r /data/local/tmp/doom_stage/doom3 /data/local/tmp/doom_stage/state /data/media/0/Android/data/$PKG/files/diii4a/ && chown -R $UID_:$UID_ /data/media/0/Android/data/$PKG/files/diii4a && find /data/media/0/Android/data/$PKG/files/diii4a -type d -exec chmod 770 {} \; && find /data/media/0/Android/data/$PKG/files/diii4a -type f -exec chmod 660 {} \;'"
sh_ "su -c 'sha256sum /data/media/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4'" | grep -q "$FIXED_SHA" || { echo "FATAL: pk4 not in place"; exit 9; }
echo "PRIVATE_DIR_SEEDED=ok"

# 4) 启动: fps 显示关闭(human 要求), 无 logfile(F38), Q3EMain game+command
sh_ "input keyevent 224; wm dismiss-keyguard" >/dev/null 2>&1 || true
CMD="game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set com_showFPS 0 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +map game/demo_mars_city1"
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command \"$CMD\"" | tail -3
sleep 25
P=$(sh_ "pidof $PKG || true" | tr -d '\r'); echo "GAME_PID=$P"
[ -n "$P" ] || { echo "FATAL: game process not running"; exit 6; }
sleep 60
sh_ "uiautomator dump /data/local/tmp/doom_ui.xml" >/dev/null 2>&1 || true
sh_ "cat /data/local/tmp/doom_ui.xml 2>/dev/null" > "$OUT/ui_dump.xml" || true
if grep -qE '数据文件|没有被发现|not.*found|Game data' "$OUT/ui_dump.xml" 2>/dev/null; then
  echo "FATAL: launcher error dialog:"; grep -oE 'text="[^"]{0,200}' "$OUT/ui_dump.xml" | head -8; exit 7
fi

# 5) 进图确认截图(给 human) — 采样画面中央统计判别菜单 vs 3D 画面
sh_ "screencap -p /data/local/tmp/doom_stage/confirm.png" >/dev/null
pull_ /data/local/tmp/doom_stage/confirm.png "$OUT/confirm_enter_map.png" >/dev/null
python3 - "$OUT/confirm_enter_map.png" <<'PY' > "$OUT/confirm_stats.txt" 2>&1 || true
import sys
try:
    from PIL import Image
except ImportError:
    print("PIL unavailable"); sys.exit(0)
im = Image.open(sys.argv[1]).convert("L").resize((128, 128)); px = list(im.getdata())
mean = sum(px)/len(px); var = sum((p-mean)**2 for p in px)/len(px)
print("mean=", round(mean,1), "var=", round(var,1), "uniq=", len(set(px)))
PY
cat "$OUT/confirm_stats.txt"

# 6) 脚本化多样走动: 6 个阶段 = 前进×左右扫视 / 原地大回旋×2 / 后退 / 侧移, 每阶段采 10 帧
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
LX=$((W * 22 / 100)); MY=$((H * 70 / 100)); R0=$((W * 80 / 100)); RY=$((H * 50 / 100))
echo "SCREEN=${W}x${H}"
frame=0
snap() { frame=$((frame+1)); sh_ "screencap -p /data/local/tmp/doom_stage/f.png" >/dev/null
  pull_ /data/local/tmp/doom_stage/f.png "$OUT/frames_raw/frame_$(printf %04d $frame).png" >/dev/null; }
# phase A: 前进 3s, 期间左右扫视+采帧
fwd() { sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 3000" >/dev/null; }
for k in 1 2 3; do
  case $k in 1) sh_ "input swipe $R0 $RY $((R0 - W / 5)) $((RY + 30)) 500";; 2) sh_ "input swipe $((R0 - W / 5)) $((RY + 30)) $R0 $RY 500";; 3) sh_ "input swipe $R0 $RY $((R0 - W / 5)) $((RY + 30)) 500";; esac
  fwd & sleep 0.7; snap; sleep 0.7; snap; sleep 0.7; snap; wait || true
done
# phase B: 原地大回旋(两次 180° 扫视), 每次扫视中采 5 帧
for k in 1 2; do
  sh_ "input swipe $((W * 90 / 100)) $RY $((W * 12 / 100)) $RY 2200" >/dev/null &
  for j in 1 2 3 4 5; do sleep 0.4; snap; done
  wait || true
done
# phase C: 前进+持续右转 (进新房间), 8 帧
sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 4000" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sh_ "input swipe $((W * 70 / 100)) $RY $((W * 62 / 100)) $((RY + 20)) 350" >/dev/null; sleep 0.35; snap; done
wait || true
# phase D: 侧移+扫视, 8 帧
sh_ "input swipe $((W * 15 / 100)) $MY $((W * 30 / 100)) $MY 2500" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sleep 0.3; snap; done
wait || true
# phase E: 后退+左扫, 8 帧
sh_ "input swipe $LX $MY $LX $((MY + H / 6)) 2500" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sh_ "input swipe $((W * 60 / 100)) $RY $((W * 72 / 100)) $((RY + 20)) 300" >/dev/null; sleep 0.3; snap; done
wait || true
# phase F: 前进+左扫视 (再进一个方向), 8 帧
sh_ "input swipe $LX $MY $LX $((MY - H / 5)) 3000" >/dev/null &
for j in 1 2 3 4 5 6 7 8; do sh_ "input swipe $((W * 62 / 100)) $RY $((W * 74 / 100)) $((RY + 20)) 350" >/dev/null; sleep 0.35; snap; done
wait || true
echo "FRAMES_RAW=$frame"

# 7) 后处理: 裁顶部3%+底部6%(系统条) + 严格去重(128x72 平均绝对差 > 8) -> frames_kept.txt
python3 - "$OUT" <<'PY'
import glob, os, sys
from PIL import Image
out = sys.argv[1]
raw = sorted(glob.glob(out + "/frames_raw/frame_*.png"))
kept, prev = [], None
for f in raw:
    im = Image.open(f)
    w, h = im.size
    im = im.crop((0, int(h*0.03), w, int(h*0.94)))
    g = im.convert("L").resize((128, 72))
    px = list(g.getdata())
    if prev is not None:
        d = sum(abs(a-b) for a, b in zip(px, prev)) / len(px)
        if d <= 8.0:
            continue
    prev = px
    name = os.path.basename(f).replace("frame_", "kept_")
    im.save(os.path.join(out, "frames", name))
    kept.append(name)
with open(os.path.join(out, "frames_kept.txt"), "w") as fh:
    fh.write("\n".join(kept) + "\n")
print("KEPT=", len(kept), "of", len(raw))
PY
N=$(ls "$OUT/frames" 2>/dev/null | grep -c png || true); echo "FRAMES_KEPT=$N"

# 8) 依据与清理
sh_ "su -c 'tail -n 30 $STATE/base/qconsole.log'" > "$OUT/qconsole_tail.txt" 2>/dev/null || true
ALIVE=$(sh_ "pidof $PKG || true" | tr -d '\r'); echo "ALIVE_AFTER=$ALIVE"
sh_ "am force-stop $PKG" >/dev/null || true
sh_ "am kill-all" >/dev/null 2>&1 || true
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
echo "CAPTURE_DONE $OUT KEPT=$N RAW=$frame"
