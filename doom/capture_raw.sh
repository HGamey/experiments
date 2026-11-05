#!/bin/sh
# B 轨 Doom3 原始采集（E2E 第一段）：装 idTech4A++ + 推 demo 数据 + 固定脚本走动 + 原生分辨率截帧。
#   用法: experiment doom/capture_raw.sh <serial> [KEY=VALUE]
# KEY: FRAMES (目标帧数, 缺省 80) LOOK_ONLY=1 (只转视角不移动)
#      LIB_SHA (可选: 缓存 blobs/<sha> 的 libdoom3.so, 放进 fs_basepath 目录并开 harm_load_local_engine_lib; 需 root)
#      MSAA (缺省 0, 传 +set r_multiSamples)
# 启动法 (2026-10-14 pilot 冻结命令, 不需要缺失的 .demo 种子):
#   am start -W -S -n com.karin.idTech4Amm/com.n0n3m4.q3e.Q3EMain --es game doom3 \
#     --es command 'game.arm +set fs_basepath <BASE> +set fs_savepath <STATE> … +map game/demo_mars_city1'
# 帧写 $PF_JOB_OUT/frames/ 留在本机宿主, 不跨机搬运。产物 summary.json + qconsole 尾部 + 帧清单。
# hdc:* 开头的 serial 走 hdc (华为), 其余走 adb。全程 am kill-all 清后台, 收尾 force-stop。
set -eu
SERIAL="${1:?usage: capture_raw.sh <serial> [KEY=VALUE]}"
shift || true
for kv in "$@"; do export "$kv"; done
FRAMES="${FRAMES:-80}"
PKG=com.karin.idTech4Amm
APK_SHA=34d9383bb6ccf641c9e6f09bc009e0d1c80bb27ae0207cefe5c46ba7e86a8dcd
PK4_SHA=16d7dfe6b9f7f0765b23169dfab79e927514457b069b9ece9583666f423ec6c1
SCRIPT_SHA=5870c79cabda6beec0a6a873bc6544ae840ad7b049da66eb4acce21e610b5226
CACHE="${PF_GAME_CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
OUT="${PF_JOB_OUT:-$(pwd)/doom_out}/doom_raw_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT/frames"
echo "OUT=$OUT FRAMES=$FRAMES SERIAL=$SERIAL"

if [ "${SERIAL#hdc:}" != "$SERIAL" ]; then TKEY="${SERIAL#hdc:}"
  sh_() { hdc -t "$TKEY" shell "$@" 2>&1 | tr -d '\r'; }
  push_() { hdc -t "$TKEY" file send "$1" "$2" 2>&1 | tail -1; }
  pull_() { hdc -t "$TKEY" file recv "$1" "$2" 2>&1 | tail -1; }
  inst_() { hdc -t "$TKEY" install -r "$1" 2>&1 | tail -2; }
else
  sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
  push_() { adb -s "$SERIAL" push "$1" "$2" 2>&1 | tail -1; }
  pull_() { adb -s "$SERIAL" pull "$1" "$2" 2>&1 | tail -1; }
  inst_() { adb -s "$SERIAL" install -r -g "$1" 2>&1 | tail -2; }
fi
ABI=$(sh_ "getprop ro.product.cpu.abi" | tr -d '\r')
echo "OUT=$OUT FRAMES=$FRAMES SERIAL=$SERIAL DEVICE_ABI=$ABI"

# 1) 宿主缓存按 sha 取三件素材
APK="$CACHE/blobs/$APK_SHA"; PK4="$CACHE/blobs/$PK4_SHA"
[ -f "$APK" ] || { echo "FATAL: APK blob missing"; exit 3; }
[ -f "$PK4" ] || { echo "FATAL: demo00.pk4 blob missing"; exit 3; }
ls -la "$APK" "$PK4"

# 2) 清后台 + 安装 (adb/hdc 要求文件名以 .apk 结尾, 缓存 blob 需先复制)
sh_ "am kill-all" >/dev/null || true
APKF="$OUT/idtech4amm-v73-arm64.apk"
cp "$APK" "$APKF"
INST_OUT=$(inst_ "$APKF")
echo "INSTALL: $INST_OUT"
echo "$INST_OUT" | grep -qi 'success' || { echo "FATAL: install failed"; exit 5; }

# 3) 目标目录; uid 用 dumpsys 解析 (pilot 10386 是那台机的值, 每机不同)
BASE=/storage/emulated/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3
STATE=/storage/emulated/0/Android/data/$PKG/files/hgamey-doom-glm-001/state
UID_=$(sh_ "cmd package list packages -U 2>/dev/null | grep '$PKG' | sed 's/.*uid://' | tr -d ' '" | head -1)
[ -n "$UID_" ] || UID_=$(sh_ "su -c 'stat -c %u /data/data/$PKG' 2>/dev/null" | head -1 | tr -d ' ')
echo "APP_UID=$UID_"
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
sh_ "mkdir -p /data/local/tmp/doom_stage/doom3/base /data/local/tmp/doom_stage/state/base/script /data/local/tmp/doom_stage/state/base/demos"
# weapon_base.script 与其余素材一样按 sha 从缓存 blob 推
SCRIPT_SRC="$CACHE/blobs/$SCRIPT_SHA"
[ -f "$SCRIPT_SRC" ] || SCRIPT_SRC=""
if [ -n "$SCRIPT_SRC" ]; then
  push_ "$SCRIPT_SRC" /data/local/tmp/doom_stage/state/base/script/weapon_base.script
fi
push_ "$PK4" /data/local/tmp/doom_stage/doom3/base/demo00.pk4
sh_ "sha256sum /data/local/tmp/doom_stage/doom3/base/demo00.pk4" | grep -q "$PK4_SHA" || { echo "FATAL: pk4 sha mismatch on device"; exit 4; }
sh_ "[ -f /data/local/tmp/doom_stage/state/base/script/weapon_base.script ] && sha256sum /data/local/tmp/doom_stage/state/base/script/weapon_base.script" | grep -q "$SCRIPT_SHA" || { echo "FATAL: script missing/mismatch"; exit 4; }
# 落位: root 用 su (顺带修属主), 否则试 shell 直写 Android/data
if sh_ "su -c true" >/dev/null 2>&1; then
  sh_ "su -c 'mkdir -p /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001 && cp -r /data/local/tmp/doom_stage/doom3 /data/local/tmp/doom_stage/state /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/ && mkdir -p /data/media/0/Android/data/$PKG/files/diii4a && cp -r /data/local/tmp/doom_stage/doom3 /data/media/0/Android/data/$PKG/files/diii4a/ && chown -R $UID_:$UID_ /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001 /data/media/0/Android/data/$PKG/files/diii4a && find /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001 /data/media/0/Android/data/$PKG/files/diii4a -type d -exec chmod 770 {} \; && find /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001 /data/media/0/Android/data/$PKG/files/diii4a -type f -exec chmod 660 {} \;'"
  HOW=su
else
  sh_ "mkdir -p /storage/emulated/0/Android/data/$PKG/files/hgamey-doom-glm-001 /storage/emulated/0/Android/data/$PKG/files/diii4a && cp -r /data/local/tmp/doom_stage/doom3 /data/local/tmp/doom_stage/state /storage/emulated/0/Android/data/$PKG/files/hgamey-doom-glm-001/ && cp -r /data/local/tmp/doom_stage/doom3 /storage/emulated/0/Android/data/$PKG/files/diii4a/" >/dev/null
  HOW=shell
fi
# 落位核查必须 su (FUSE 下 shell 对 Android/data 一律 EACCES, 不代表文件不存在)
sh_ "su -c 'ls -la /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3/base/demo00.pk4 /data/media/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4; sha256sum /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3/base/demo00.pk4'" || { echo "FATAL: pk4 not in place (su check failed)"; exit 9; }

# 4) 启动 (冻结命令, fs_savepath 指自己的 state; com_showFPS 开着便于人看; logfile 2 留控制台日志)
sh_ "input keyevent 224; wm dismiss-keyguard" >/dev/null 2>&1 || true
CMD="game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set logfile 2 +set developer 1 +set com_showFPS 1 +set r_multiSamples ${MSAA:-0} +set r_useShadowMapping 1 +set harm_r_globalIllumination 1 +set harm_r_renderToolsMultithread 1 +set harm_r_autoAspectRatio 1 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +map game/demo_mars_city1"
# 放回准备: 可选本地引擎库 + 启动器开关 (pilot 4802feb59a1a 验证链: pref sed → cache/lib 复制加载)
LIB_SHA="${LIB_SHA:-}"
if [ -n "$LIB_SHA" ] && [ -f "$CACHE/blobs/$LIB_SHA" ] && sh_ "su -c true" >/dev/null 2>&1; then
  push_ "$CACHE/blobs/$LIB_SHA" /data/local/tmp/doom_stage/doom3/libdoom3.so >/dev/null
  sh_ "su -c 'cp /data/local/tmp/doom_stage/doom3/libdoom3.so /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3/libdoom3.so && chown $UID_:$UID_ /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3/libdoom3.so && chmod 660 /data/media/0/Android/data/$PKG/files/hgamey-doom-glm-001/doom3/libdoom3.so'" >/dev/null
  PREF=/data/user/0/$PKG/shared_prefs/${PKG}_preferences.xml
  sh_ "su -c '[ -f $PREF ] && sed \"/name=\\\"harm_load_local_engine_lib\\\"/d; /<\\/map>/i\\\\    <boolean name=\\\"harm_load_local_engine_lib\\\" value=\\\"true\\\" />\" $PREF > $PREF.p2 && chown $UID_:$UID_ $PREF.p2 && chmod 600 $PREF.p2 && mv $PREF.p2 $PREF && restorecon $PREF'" >/dev/null && echo "LOCAL_LIB_ARMED=$LIB_SHA"
fi
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command \"$CMD\"" | tail -3
sleep 25
P=$(sh_ "pidof $PKG || true" | tr -d '\r')
echo "GAME_PID=$P"
[ -n "$P" ] || { echo "FATAL: game process not running after launch"; exit 6; }
sh_ "dumpsys window | grep -i mCurrentFocus || true"

# 4b) 进图门: 35s 后查加载器错误弹窗 (uiautomator 抓文本) 与 qconsole 进图行; 弹窗=立即失败并留证据
sleep 35
sh_ "uiautomator dump /data/local/tmp/doom_ui.xml" >/dev/null 2>&1 || true
sh_ "cat /data/local/tmp/doom_ui.xml 2>/dev/null" > "$OUT/ui_dump.xml" 2>/dev/null || true
if grep -qE '数据文件|没有被发现|not.*found|Game data' "$OUT/ui_dump.xml" 2>/dev/null; then
  echo "FATAL: launcher error dialog detected:"; grep -oE 'text="[^"]{0,200}' "$OUT/ui_dump.xml" | head -8; exit 7
fi
sh_ "su -c 'grep -m2 -iE \"Mars City|-------- Start|map:\" $STATE/base/qconsole.log'" > "$OUT/mapcheck.txt" 2>/dev/null || true
echo "MAPCHECK=$(tr '\n' ' ' < "$OUT/mapcheck.txt" | head -c 300)"
[ -s "$OUT/mapcheck.txt" ] || { echo "WARN: no map-load line in qconsole yet"; }

# 5) 确定性脚本化走动 + 原生截帧 (不逐帧操控: 固定节奏的划动序列)
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
LX=$((W * 22 / 100)); R0=$((W * 78 / 100)); MY=$((H * 70 / 100)); RY=$((H * 50 / 100))
echo "SCREEN=${W}x${H} moveX=$LX lookX=$R0"
i=1
while [ "$i" -le "$FRAMES" ]; do
  # 视角: 右半区水平划, 方向按 i 交替; 移动: 左半区前推 (LOOK_ONLY=1 时不推)
  if [ $((i % 2)) -eq 0 ]; then
    sh_ "input swipe $R0 $RY $((R0 - W / 6)) $((RY + 40)) 400" >/dev/null
  else
    sh_ "input swipe $((R0 - W / 6)) $((RY + 40)) $R0 $RY 400" >/dev/null
  fi
  [ "${LOOK_ONLY:-0}" = "1" ] || sh_ "input swipe $LX $MY $LX $((MY - H / 6)) 500" >/dev/null
  sleep 0.4
  sh_ "screencap -p /data/local/tmp/doom_stage/f.png" >/dev/null
  pull_ /data/local/tmp/doom_stage/f.png "$OUT/frames/frame_$(printf %04d $i).png" >/dev/null
  i=$((i + 1))
done
N=$(ls "$OUT/frames" | grep -c png || true)
echo "FRAMES_KEPT=$N"

# 6) 依据: 引擎控制台日志尾部 + 进程存活 (Android 16 FUSE 下 shell 读不了 Android/data, 必须 su)
sh_ "su -c 'tail -n 40 $STATE/base/qconsole.log'" > "$OUT/qconsole_tail.txt" 2>/dev/null || sh_ "tail -n 40 $STATE/base/qconsole.log" > "$OUT/qconsole_tail.txt" 2>/dev/null || true
sh_ "su -c 'head -n 60 $STATE/base/qconsole.log'" > "$OUT/qconsole_head.txt" 2>/dev/null || true
ALIVE=$(sh_ "pidof $PKG || true" | tr -d '\r')
MEM=$(sh_ "dumpsys meminfo $PKG | grep -m1 TOTAL" || true)
echo "ALIVE_AFTER=$ALIVE"; echo "MEM=$MEM"
head -c 400 "$OUT/qconsole_tail.txt" || true
GLES=$(grep -m1 -iE 'OpenGL|GLES' "$OUT/qconsole_head.txt" "$OUT/qconsole_tail.txt" 2>/dev/null | head -1 || true)
LIBMAP=$(sh_ "su -c 'grep -m1 libdoom3 /proc/$ALIVE/maps' 2>/dev/null" || true)
echo "GLES_LINE=$GLES"
echo "LIBMAP_LINE=$LIBMAP"
MAPLINE=$(grep -m2 -iE 'Mars City|demo_mars|map:' "$OUT/qconsole_head.txt" "$OUT/qconsole_tail.txt" 2>/dev/null | head -2 || true)
echo "MAP_LINE=$MAPLINE"
# 引擎诊断: qconsole 拿不到时抓 logcat 尾部与 savepath 递归列表
[ -s "$OUT/qconsole_tail.txt" ] || {
  sh_ "logcat -d -t 300 | grep -iE 'idTech|Q3E|doom|glimp' | tail -n 40" > "$OUT/logcat_doom.txt" 2>/dev/null || true
  sh_ "ls -laR $STATE 2>/dev/null | head -n 30" > "$OUT/state_ls.txt" 2>/dev/null || true
  echo "NO_QCONSOLE: see logcat_doom.txt state_ls.txt"
}
# 帧统计 (不目视): 唯一色数/亮度方差判菜单 vs 游戏
python3 - "$OUT/frames" <<'PY' > "$OUT/frame_stats.txt" 2>&1 || true
import glob, sys
try:
    from PIL import Image
except ImportError:
    print("PIL unavailable"); sys.exit(0)
files = sorted(glob.glob(sys.argv[1] + "/frame_*.png"))[:5] + sorted(glob.glob(sys.argv[1] + "/frame_*.png"))[-5:]
for f in files:
    im = Image.open(f).convert("L").resize((128, 128))
    px = list(im.getdata())
    print(f.split("/")[-1], "uniq_luma=", len(set(px)), "mean=", round(sum(px)/len(px), 1),
          "var=", round(sum((p-sum(px)/len(px))**2 for p in px)/len(px), 1))
PY
cat "$OUT/frame_stats.txt" 2>/dev/null | head -10 || true

# 7) 清理: 停游戏 + 清后台 + 清设备临时
sh_ "am force-stop $PKG" >/dev/null || true
sh_ "am kill-all" >/dev/null 2>&1 || true
sh_ "rm -rf /data/local/tmp/doom_stage" >/dev/null
python3 - "$OUT" "$N" "$HOW" "$ALIVE" <<'PY'
import hashlib, json, os, sys
out, n, how, alive = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
fs = sorted(os.listdir(os.path.join(out, "frames")))
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()
sizes = [(f, os.path.getsize(os.path.join(out, "frames", f))) for f in fs]
rec = {"frames": n, "how": how, "alive_after": alive,
       "first_sha": sha(os.path.join(out, "frames", fs[0])) if fs else None,
       "last_sha": sha(os.path.join(out, "frames", fs[-1])) if fs else None,
       "total_bytes": sum(s for _, s in sizes), "zero_byte": [f for f, s in sizes if s == 0]}
json.dump(rec, open(os.path.join(out, "summary.json"), "w"), indent=1)
print("SUMMARY " + json.dumps(rec))
PY
echo "CAPTURE_DONE $OUT"
