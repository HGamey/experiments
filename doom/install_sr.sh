#!/bin/sh
# D 轨试装 (Codex SmallSR fixture): 放 libdoom3.so + sr_*.cfg, root 开 harm_load_local_engine_lib,
# 三个档位各启动一次测帧率; 每档截图 3 张到宿主; 结束报加载行/档位 echo/fps。
#   用法: experiment doom/install_sr.sh <serial> [KEY=VALUE]
# KEY: ARMS=native,off,on  WINDOW (测量秒, 缺省 20)  SO_SHA (缺省 fixture bca5e7…)
# 数据目录沿用采集脚本的 hgamey-doom-glm-001 (须已由 capture_raw.sh 部署)。帧/日志全留宿主。
set -eu
SERIAL="${1:?usage: install_sr.sh <serial> [KEY=VALUE]}"; shift || true
for kv in "$@"; do export "$kv"; done
PKG=com.karin.idTech4Amm
SO_SHA="${SO_SHA:-bca5e78894ad4ea258da26e14099d72681dc2c72a674debf4a6ed8e84791285a}"
SR_GIT="${SR_GIT:-https://github.com/HGamey/experiments.git}"
SR_REF="${SR_REF:-codex/doom3-realtime-sr}"
SR_COMMIT="${SR_COMMIT:-65b00e2}"; [ ${#SR_COMMIT} -eq 40 ] || SR_COMMIT="65b00e29cb5c4be0184b3ba10e7ff5108d7040de"
ARMS="${ARMS:-native,off,on}"
WINDOW="${WINDOW:-20}"
# F40: 引擎固定读应用私有默认路径, 命令行 fs_basepath/fs_savepath 不生效
DATA=/storage/emulated/0/Android/data/$PKG/files/diii4a
BASE=$DATA/doom3
STATE=$DATA/state
OUT="${PF_JOB_OUT:-$(pwd)/sr_out}/sr_install_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }

echo "OUT=$OUT SERIAL=$SERIAL ARMS=$ARMS"
[ "$(sh_ "getprop ro.product.cpu.abi")" = arm64-v8a ] || { echo "FATAL: ABI not arm64-v8a"; exit 3; }
sh_ "su -c '[ -f /data/media/0/Android/data/$PKG/files/diii4a/doom3/base/demo00.pk4 ]'" || { echo "FATAL: game data not deployed ($BASE)"; exit 3; }
sh_ "su -c true" >/dev/null 2>&1 || { echo "FATAL: root needed for pref edit"; exit 3; }

CACHE="${CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
# 1) 取包 (宿主 git; 缓存 blob 优先)
SO_SRC="$CACHE/blobs/$SO_SHA"
if [ ! -f "$SO_SRC" ]; then
  W="$OUT/src"; mkdir -p "$W"
  git clone --depth 1 -b "$SR_REF" "$SR_GIT" "$W/experiments" 2>&1 | tail -1
  echo "BRANCH_HEAD=$(git -C "$W/experiments" rev-parse HEAD) (期望 $SR_COMMIT, 允许前移)"
  tar xzf "$W/experiments/doom3-sr/realtime/artifacts/small-sr-pipeline-fixture-arm64.tar.gz" -C "$W"
  SO_SRC="$W/libdoom3.so"
  [ "$(shasum -a 256 "$SO_SRC" | cut -d ' ' -f 1)" = "$SO_SHA" ] || { echo "FATAL: fixture libdoom3.so sha mismatch"; exit 3; }
fi
echo "SO_SHA256=$(shasum -a 256 "$SO_SRC" | cut -d ' ' -f 1)"

# 2) 放置: libdoom3.so 与 base 平级; sr_*.cfg 进 base/; pref 开本地引擎库
adb -s "$SERIAL" push "$SO_SRC" /data/local/tmp/libdoom3.so >/dev/null
sh_ "su -c 'cp /data/local/tmp/libdoom3.so $BASE/libdoom3.so && chmod 664 $BASE/libdoom3.so'" >/dev/null
put_cfg() { printf '%s' "$2" | base64 -d > "$OUT/$1"; adb -s "$SERIAL" push "$OUT/$1" "/data/local/tmp/$1" >/dev/null; sh_ "su -c 'cp /data/local/tmp/$1 $BASE/base/$1 && chmod 664 $BASE/base/$1'" >/dev/null; }
put_cfg sr_keys.cfg "YmluZCBGOCAidG9nZ2xlIHJfc21hbGxTUiAxIDIiCmJpbmQgRjkgImV4ZWMgc3JfbmF0aXZlLmNmZyIKZWNobyAiRjg6IGJpY3ViaWMvbW9kZWw7IEY5OiBuYXRpdmUiCg=="
put_cfg sr_native.cfg "c2V0IHJfc21hbGxTUiAwCnNldCByX3NjcmVlbkZyYWN0aW9uIDEwMAplY2hvICJTbWFsbFNSIG5hdGl2ZSBmdWxsLXJlc29sdXRpb24gcmVmZXJlbmNlIgo="
put_cfg sr_off.cfg "c2V0IHJfc21hbGxTUiAxCmVjaG8gIlNtYWxsU1IgT0ZGOiBzYW1lIGhhbGYtc2l6ZSByZW5kZXIgKyBiaWN1YmljIgo="
put_cfg sr_on.cfg "c2V0IHJfc21hbGxTUiAyCmVjaG8gIlNtYWxsU1IgT046IGhhbGYtc2l6ZSByZW5kZXIgKyBtb2RlbCIK"
sh_ "su -c 'ls -la $BASE/libdoom3.so $BASE/base/sr_*.cfg'" || true
PREF=/data/user/0/$PKG/shared_prefs/${PKG}_preferences.xml
sh_ "su -c 'am force-stop $PKG; sleep 1; [ -f $PREF ] && sed \"/name=\\\"harm_load_local_engine_lib\\\"/d; /<\\/map>/i\\\\    <boolean name=\\\"harm_load_local_engine_lib\\\" value=\\\"true\\\" />\" $PREF > $PREF.p && chown \$(stat -c %u /data/data/$PKG):\$(stat -c %g /data/data/$PKG) $PREF.p && chmod 600 $PREF.p && mv $PREF.p $PREF && restorecon $PREF && grep -c harm_load_local_engine_lib $PREF'" || { echo "FATAL: pref edit failed"; exit 4; }

# 3) 逐档启动: +exec 对应 cfg + com_showFPS; 稳定后测量窗内 SF latency + 截图
CFG="native:sr_native.cfg off:sr_off.cfg on:sr_on.cfg"
LAUNCH() { # LAUNCH <extra-cfg>
  sh_ "input keyevent 224; wm dismiss-keyguard" >/dev/null 2>&1 || true
  CMD="game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set logfile 2 +set developer 1 +set com_showFPS 1 +set r_multiSamples 0 +set r_useShadowMapping 1 +set harm_r_renderToolsMultithread 1 +set harm_r_autoAspectRatio 1 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +exec sr_keys.cfg +exec $1 +map game/demo_mars_city1"
  sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command \"$CMD\"" | tail -2
}
FPSLINES() { # 从 qconsole 抓 fps 行
  sh_ "grep -E 'fps|SmallSR' $STATE/base/qconsole.log 2>/dev/null | tail -n 80"
}
for a in $CFG; do
  name=${a%%:*}; cfg=${a#*:}
  echo "=== ARM=$name ($cfg)"
  LAUNCH "$cfg"
  P=""
  i=0; while [ $i -lt 30 ]; do sleep 2; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+2)); done
  [ -n "$P" ] || { echo "ARM_${name}_DEAD"; continue; }
  sleep 35   # 加载地图 + shader 首载等数秒
  LAYER=$(sh_ "dumpsys SurfaceFlinger --list 2>/dev/null | grep -m1 'SurfaceView\['" | tr -d '\r')
  echo "LAYER=$LAYER"
  sh_ "dumpsys SurfaceFlinger --latency '$LAYER'" > "$OUT/latency_${name}_pre.txt" 2>/dev/null || true
  T0=$(python3 -c 'import time; print(time.time())')
  j=0; while [ "$j" -lt 5 ]; do
    sleep 3   # 固定画面测量: 出生点本身有动画, 不注入输入
    j=$((j+1))
  done
  sh_ "dumpsys SurfaceFlinger --latency '$LAYER'" > "$OUT/latency_${name}.txt" 2>/dev/null || true
  T1=$(python3 -c 'import time; print(time.time())')
  echo "MEASURE_WINDOW=$(python3 -c "print(round($T1-$T0,1))")s"
  k=1; while [ "$k" -le 3 ]; do
    sh_ "screencap -p /data/local/tmp/sr_${name}_$k.png" >/dev/null
    adb -s "$SERIAL" pull "/data/local/tmp/sr_${name}_$k.png" "$OUT/sr_${name}_$k.png" >/dev/null 2>&1 || true
    k=$((k+1))
  done
  FPSLINES | tail -n 40 > "$OUT/fps_${name}.txt" || true
  grep -iE 'SmallSR|Load local engine' "$OUT/fps_${name}.txt" || true
  sh_ "am force-stop $PKG" >/dev/null
  sleep 2
done

# 4) 切换不重启验证: on 档启动后 keyevent F8 (KEYCODE 142) / F9 (143), qconsole echo 应变
LAUNCH sr_on.cfg >/dev/null
sleep 35
sh_ "input keyevent 142" >/dev/null 2>&1 || true; sleep 3   # F8: toggle 1/2 → 应到 bicubic
sh_ "input keyevent 143" >/dev/null 2>&1 || true; sleep 3   # F9: native
sh_ "grep -E 'SmallSR|r_smallSR' $STATE/base/qconsole.log | tail -n 12" > "$OUT/switch_test.txt" || true
cat "$OUT/switch_test.txt"
sh_ "am force-stop $PKG" >/dev/null; sh_ "am kill-all" >/dev/null 2>&1 || true
echo "SR_INSTALL_DONE $OUT"
