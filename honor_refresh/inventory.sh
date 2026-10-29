#!/bin/sh
# Read-only inventory for selecting an installed HONOR game and its SF layer.
set -eu
S="${1:-AVXB6R5C19000481}"
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"

adb -s "$S" shell pm list packages -3 > "$OUT/third_party_pm.txt"
# The loop and variables deliberately run in Android shell.
# shellcheck disable=SC2016
adb -s "$S" shell 'for line in $(pm list packages -3); do p=${line#package:}; printf "%s\t" "$p"; dumpsys package "$p" | grep -m1 versionName || echo "versionName=unknown"; done' > "$OUT/third_party_versions.txt"
adb -s "$S" shell dumpsys SurfaceFlinger > "$OUT/sf_dump.txt"
adb -s "$S" shell dumpsys SurfaceFlinger --displays > "$OUT/sf_displays.txt"
adb -s "$S" shell dumpsys window > "$OUT/window_dump.txt"
adb -s "$S" shell 'settings list system; settings list secure; settings list global' > "$OUT/settings_all.txt"
adb -s "$S" shell 'getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.display.id; wm size; wm density' > "$OUT/device.txt"
{
  echo "== Device =="
  cat "$OUT/device.txt"
  echo "== Third-party packages and versions =="
  cat "$OUT/third_party_versions.txt"
  echo "== Foreground / focused windows =="
  grep -i -E 'mCurrentFocus|mFocusedApp|mTopFocusedDisplayId' "$OUT/window_dump.txt" | head -20 || true
  echo "== Candidate game-like SF layers =="
  grep -i -E 'SurfaceView|BLAST|game|miHoYo|tencent|netease|unity|ue4' "$OUT/sf_dump.txt" | head -100 || true
} > "$OUT/inventory_summary.txt"
cat "$OUT/inventory_summary.txt"
