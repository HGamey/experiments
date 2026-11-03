#!/bin/sh
# 探路: 按固定步骤操作一款游戏并逐步截图, 确认进门路径、操作方式和 HUD 位置, 不算采集。
#   sh dataset/probe_game.sh <serial> <包名> '<步骤>'
# 步骤用 ; 分隔: s:秒 (等待)  t:x,y (点, 横屏比例坐标)  m:x,y,dx,dy,毫秒 (划或按住, 比例坐标)  M:同 m 但不等它结束, 用来在按住时截图  shot:名字 (截图)  deny (点权限弹窗的「禁止」)
# 产物 ($PF_JOB_OUT/probe_<时间>/): <序号>_<名字>.png, steps.txt, ui_*.xml
# 不改游戏设置; 媒体音量置 0, 收尾写回, 强停游戏, 开跑前熄屏则熄屏。
set -u
SERIAL=$1; PKG=$2; STEPS=$3
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${PF_ROOT:-$(cd "$HERE/../.." && pwd)}
PF="${PHONEFARM_BIN:-$ROOT/src/target/release/phonefarm}"
if [ -z "${PF_LOCK_FDS:-}" ]; then
  exec "$PF" lease --serial "$SERIAL" --label "${DEVLOCK_LABEL:+$DEVLOCK_LABEL-}probe@$SERIAL" -- sh "$HERE/$(basename "$0")" "$@"
fi
OUT="${PF_JOB_OUT:-$ROOT/_runs}/probe_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
a() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
WH=$(a wm size | sed -n 's/^Physical size: *//p' | tail -1); W=${WH%x*}; H=${WH#*x}
[ "$W" -lt "$H" ] && { t=$W; W=$H; H=$t; }
px() { python3 -I -c "print(*(round(float(v) * s) for v, s in zip('$1'.split(','), ($W, $H, $W, $H))))"; }
VOL0=$(a cmd media_session volume --stream 3 --get | sed -n 's/.*volume is \([0-9]*\).*/\1/p' | head -1)
WAKE0=$(a dumpsys power | sed -n 's/.*mWakefulness=//p' | head -1)
{ echo "screen=$WH"; echo "volume=$VOL0"; echo "wakefulness=$WAKE0"; a dumpsys package "$PKG" | grep -m2 -E 'versionName|versionCode'; } > "$OUT/steps.txt"
a input keyevent KEYCODE_WAKEUP >/dev/null
a am kill-all >/dev/null
a cmd audio set-volume 3 0 >/dev/null
a am force-stop "$PKG" >/dev/null
a monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null
T0=$(date +%s); n=0
log() { echo "$(( $(date +%s) - T0 ))s $1" >> "$OUT/steps.txt"; }
IFS=';'
for step in $STEPS; do
  unset IFS
  n=$((n + 1))
  case "$step" in
    s:*) sleep "${step#s:}" ;;
    t:*) a input tap $(px "${step#t:}") >/dev/null ;;
    m:*) v=${step#m:}; ms=${v##*,}; v=${v%,*}; set -- $(px "$v")
         a input swipe "$1" "$2" "$(($1 + $3))" "$(($2 + $4))" "$ms" >/dev/null ;;
    M:*) v=${step#M:}; ms=${v##*,}; v=${v%,*}; set -- $(px "$v")
         a input swipe "$1" "$2" "$(($1 + $3))" "$(($2 + $4))" "$ms" >/dev/null & ;;
    shot:*) adb -s "$SERIAL" exec-out screencap -p > "$OUT/$(printf %02d "$n")_${step#shot:}.png"
            log "focus $(a dumpsys window | grep -m1 mCurrentFocus)" ;;
    deny) a 'uiautomator dump /sdcard/ui.xml >/dev/null; cat /sdcard/ui.xml; rm -f /sdcard/ui.xml' > "$OUT/ui_$n.xml"
          b=$(tr '>' '\n' < "$OUT/ui_$n.xml" | grep -E 'text="(禁止|拒绝|Deny)[^"]*"' | sed -E 's/.*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]".*/\1 \2 \3 \4/' | head -1)
          if [ -n "$b" ]; then set -- $b; a input tap $((($1 + $3) / 2)) $((($2 + $4) / 2)) >/dev/null; log "deny tapped $b"; else log "deny none"; fi ;;
    *) log "unknown step $step" ;;
  esac
  log "$step"
  IFS=';'
done
unset IFS
wait
a dumpsys SurfaceFlinger > "$OUT/sf.txt"
a am force-stop "$PKG" >/dev/null
[ -n "$VOL0" ] && a cmd audio set-volume 3 "$VOL0" >/dev/null
[ "$WAKE0" = Awake ] || a input keyevent KEYCODE_SLEEP >/dev/null
log "done volume=$(a cmd media_session volume --stream 3 --get | sed -n 's/.*volume is \([0-9]*\).*/\1/p' | head -1) running=$(a pidof "$PKG" | grep -c .)"
echo "输出: $OUT"
