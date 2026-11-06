#!/bin/sh
# D 轨荣耀单作业 (lead 2026-11-06T04:5xZ 指令, DDL 05:25Z):
# 1) Codex 正式包 ea5d3a9(small-sr-doom-arm64.tar.gz, tar sha 3a2243f8...) 装荣耀:
#    libdoom3.so -> Android/data 私有 doom3/(F40 引擎真读路径), sr_*.cfg -> base/
# 2) 无 root(F42): harm_load_local_engine_lib 走启动器 UI(uiautomator 逐级点, 截设置页)
# 3) 启动 + 4min 加载 + 点屏跳过过场
# 4) F9/F8 切 r_smallSR 0/1/2 三档, 每档 SurfaceFlinger latency 测 fps + 截图(含 com_showFPS 1)
#    日志/logcat 抓 SmallSR buffers + 权重 6cc44b53 证据
set -eu
SERIAL="${1:?usage: d_honor_sr.sh <serial>}"
PKG=com.karin.idTech4Amm
TAR_SHAS="3a2243f8 ea5d3a9"
WEIGHTS_SHA_PREFIX=6cc44b53
DATA=/storage/emulated/0/Android/data/$PKG/files/diii4a
BASE=$DATA/doom3
STATE=$DATA/state
CACHE="${CACHE:-/Users/mac/hgamey/phonefarm/_games/cache}"
OUT="${PF_JOB_OUT:-$(pwd)/sr_out}/d_honor_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
SZ=$(sh_ "wm size | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p'" | tail -1)
W=${SZ% *}; H=${SZ#* }
echo "OUT=$OUT SERIAL=$SERIAL SCREEN=${W}x${H}"

# 0) 停游戏
sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true

# 1) 取包: 缓存 blob 优先, 否则 git codex 分支 artifacts
TARBLOB=""
for p in $TAR_SHAS; do f=$(ls "$CACHE"/blobs/${p}* 2>/dev/null | head -1); [ -n "$f" ] && TARBLOB="$f" && break; done
W="$OUT/src"; mkdir -p "$W"
if [ -n "$TARBLOB" ]; then echo "TARBLOB=$TARBLOB"; cp "$TARBLOB" "$OUT/small-sr.tar.gz"
else
  echo "TARBLOB missing; clone codex branch"
  git clone --depth 1 -b codex/doom3-realtime-sr https://github.com/HGamey/experiments.git "$W/experiments" 2>&1 | tail -1 || true
  T=$(ls "$W"/experiments/doom3-sr/realtime/artifacts/small-sr-doom-arm64.tar.gz 2>/dev/null | head -1)
  [ -n "$T" ] && cp "$T" "$OUT/small-sr.tar.gz"
fi
[ -f "$OUT/small-sr.tar.gz" ] || { echo "FATAL: small-sr tar not found"; echo "STEP1_FAIL=tar" >> "$OUT/steps.txt"; exit 3; }
echo "TAR_SHA=$(shasum -a 256 "$OUT/small-sr.tar.gz" | cut -d ' ' -f 1)"
tar xzf "$OUT/small-sr.tar.gz" -C "$W"
ls -la "$W" | head -15
SO="$W/libdoom3.so"; [ -f "$SO" ] || SO=$(find "$W" -name libdoom3.so | head -1)
[ -n "$SO" ] || { echo "FATAL: no libdoom3.so in tar"; exit 3; }
echo "SO_SHA=$(shasum -a 256 "$SO" | cut -d ' ' -f 1)"
# 权重文件证据 (期望 sha 前缀 6cc44b53)
find "$W" -type f | while read -r f; do echo "PKGFILE $(basename "$f") $(shasum -a 256 "$f" | cut -c1-8) $(stat -f %z "$f" 2>/dev/null || stat -c %s "$f")"; done | tee "$OUT/pkg_files.txt"
grep -i "PKGFILE.*$WEIGHTS_SHA_PREFIX" "$OUT/pkg_files.txt" && echo "WEIGHTS_MATCH=yes" || echo "WEIGHTS_MATCH=not_in_tar"

# 2) 放置 (shell 直写, 荣耀 FUSE 可写): libdoom3.so 与 base 平级, sr_*.cfg 进 base/base
adb -s "$SERIAL" push "$SO" /data/local/tmp/libdoom3.so >/dev/null
sh_ "cp /data/local/tmp/libdoom3.so $BASE/libdoom3.so && chmod 664 $BASE/libdoom3.so && ls -la $BASE/libdoom3.so" || { echo "FATAL: libdoom3.so placement failed"; exit 4; }
put_cfg() { printf '%s' "$2" | base64 -d > "$OUT/$1"; adb -s "$SERIAL" push "$OUT/$1" "/data/local/tmp/$1" >/dev/null; sh_ "cp /data/local/tmp/$1 $BASE/base/$1 && chmod 664 $BASE/base/$1"; }
put_cfg sr_keys.cfg "YmluZCBGOCAidG9nZ2xlIHJfc21hbGxTUiAxIDIiCmJpbmQgRjkgImV4ZWMgc3JfbmF0aXZlLmNmZyIKZWNobyAiRjg6IGJpY3ViaWMvbW9kZWw7IEY5OiBuYXRpdmUiCg=="
put_cfg sr_native.cfg "c2V0IHJfc21hbGxTUiAwCnNldCBjb21fc2hvd0ZQUyAxCmVjaG8gIlNtYWxsU1IgbmF0aXZlIGZ1bGwtcmVzb2x1dGlvbiByZWZlcmVuY2UiCg=="
put_cfg sr_off.cfg "c2V0IHJfc21hbGxTUiAxCnNldCBjb21fc2hvd0ZQUyAxCmVjaG8gIlNtYWxsU1IgT0ZGOiBzYW1lIGhhbGYtc2l6ZSByZW5kZXIgKyBiaWN1YmljIgo="
put_cfg sr_on.cfg "c2V0IHJfc21hbGxTUiAyCnNldCBjb21fc2hvd0ZQUyAxCmVjaG8gIlNtYWxsU1IgT046IGhhbGYtc2l6ZSByZW5kZXIgKyBtb2RlbCIK"
sh_ "ls -la $BASE/base/sr_*.cfg" | tee -a "$OUT/steps.txt"
echo "STEP2_DONE=placement" >> "$OUT/steps.txt"

# 3) UI 开「加载本地引擎库」: 启动器无 extras 冷启, uiautomator 逐级找可点文本
sh_ "am start -n $PKG/com.n0n3m4.q3e.Q3EMain" >/dev/null
sleep 12
dump_() { sh_ "uiautomator dump /data/local/tmp/d.xml >/dev/null 2>&1; cat /data/local/tmp/d.xml" > "$OUT/ui_$1.xml" 2>/dev/null || true; }
click_text_() { # 在最新 dump 找含关键字的 clickable 节点中心并 tap
  python3 - "$OUT/ui_$1.xml" "$2" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
kw = sys.argv[2]
best = None
for m in re.finditer(r'<node[^>]*>', xml):
    n = m.group(0)
    if kw in n and 'clickable="true"' in n:
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if b:
            x = (int(b.group(1)) + int(b.group(3))) // 2; y = (int(b.group(2)) + int(b.group(4))) // 2
            best = (x, y)
print(f"{best[0]} {best[1]}" if best else "NONE")
PY
}
# 依次尝试: 菜单(keyevent 82 或右上角三点) → 其他 → 调试 → 游戏 → 加载本地引擎库
for stepkw in "其他" "调试" "游戏" "加载本地引擎库"; do
  dump_ nav
  POS=$(click_text_ nav "$stepkw")
  if [ "$POS" = "NONE" ]; then sh_ "input keyevent 82" >/dev/null 2>&1 || true; sleep 2; dump_ nav; POS=$(click_text_ nav "$stepkw"); fi
  if [ "$POS" = "NONE" ]; then echo "UI_MISS=$stepkw" | tee -a "$OUT/steps.txt"; sh_ "input keyevent 4" >/dev/null || true; sleep 1; continue; fi
  sh_ "input tap $POS" >/dev/null; sleep 2
  echo "UI_HIT=$stepkw at $POS" | tee -a "$OUT/steps.txt"
done
dump_ settings
sh_ "screencap -p /data/local/tmp/settings.png" >/dev/null
adb -s "$SERIAL" pull /data/local/tmp/settings.png "$OUT/settings_page.png" >/dev/null 2>&1 || true
grep -c "加载本地引擎库" "$OUT/ui_settings.xml" 2>/dev/null && echo "SETTINGS_PAGE_HAS_TOGGLE=yes" | tee -a "$OUT/steps.txt"
grep -o '加载本地引擎库[^/]*checked="true"' "$OUT/ui_settings.xml" 2>/dev/null && echo "TOGGLE_CHECKED=yes" | tee -a "$OUT/steps.txt" || echo "TOGGLE_CHECKED=unknown" | tee -a "$OUT/steps.txt"
sh_ "input keyevent 4" >/dev/null 2>&1 || true   # 返回
sh_ "am force-stop $PKG" >/dev/null; sleep 2

# 4) 启动 + 4min 加载 + 跳过过场 + F9/F8 三档测量
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set com_showFPS 1 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +exec sr_keys.cfg +exec sr_native.cfg +map game/demo_mars_city1'" | tail -2
P=""; i=0; while [ $i -lt 30 ]; do sleep 2; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+2)); done
[ -n "$P" ] || { echo "LAUNCH_DEAD"; exit 6; }
echo "GAME_PID=$P"
sh_ "logcat -c" >/dev/null 2>&1 || true
sleep 200
sh_ "input tap $((W / 2)) $((H / 2))" >/dev/null 2>&1 || true   # 跳过过场
sleep 3
sh_ "input keyevent 111" >/dev/null 2>&1 || true                # ESC 关控制台(如有)
sleep 20
sh_ "logcat -d 2>/dev/null | grep -iE 'SmallSR|local engine|libdoom3' | tail -n 40" > "$OUT/logcat_sr.txt" || true
head -12 "$OUT/logcat_sr.txt" || true
echo "STEP4_LAUNCH_DONE" >> "$OUT/steps.txt"

LAYER=$(sh_ "dumpsys SurfaceFlinger --list 2>/dev/null | grep -m1 'SurfaceView\['" | tr -d '\r')
echo "LAYER=$LAYER"
measure() { # measure <name>
  sh_ "dumpsys SurfaceFlinger --latency '$LAYER'" > "$OUT/latency_$1.txt" 2>/dev/null || true
  k=1; while [ "$k" -le 3 ]; do
    sh_ "screencap -p /data/local/tmp/sr_$1_$k.png" >/dev/null
    adb -s "$SERIAL" pull "/data/local/tmp/sr_$1_$k.png" "$OUT/sr_$1_$k.png" >/dev/null 2>&1 || true
    sleep 4
    k=$((k+1))
  done
  echo "MEASURED=$1"
}
measure native        # r_smallSR 0 (sr_native.cfg 已 exec)
sh_ "input keyevent 143" >/dev/null 2>&1 || true; sleep 3   # F9: exec sr_native (兜底=0)
measure native2
sh_ "input keyevent 142" >/dev/null 2>&1 || true; sleep 5   # F8: toggle -> r_smallSR 1 (off)
measure off
sh_ "input keyevent 142" >/dev/null 2>&1 || true; sleep 5   # F8: -> r_smallSR 2 (on)
measure on
echo "STEP5_TIERS_DONE" >> "$OUT/steps.txt"

sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true
echo "D_HONOR_DONE $OUT"
cat "$OUT/steps.txt"
