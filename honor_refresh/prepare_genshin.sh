#!/bin/sh
# Enter Genshin's stable world scene and preserve evidence for SF layer selection.
set -eu
S="${1:-AVXB6R5C19000481}"
PKG="${2:-com.miHoYo.Yuanshen}"
OUT="${PF_JOB_OUT:-.}"
PF="${PHONEFARM_BIN:-phonefarm}"
mkdir -p "$OUT"
export DEVLOCK_LABEL="${DEVLOCK_LABEL:-job-${PF_JOB_ID:-honor-refresh}}"

adb -s "$S" shell input keyevent KEYCODE_WAKEUP
adb -s "$S" shell wm dismiss-keyguard >/dev/null 2>&1 || true
# HONOR's swipe keyguard remains after wm dismiss-keyguard; no PIN is bypassed.
adb -s "$S" shell input swipe 636 2200 636 600 300
sleep 2
# Dismiss the USB connection-mode dialog through its existing Cancel button.
adb -s "$S" shell uiautomator dump /sdcard/honor_refresh_ui.xml >/dev/null 2>&1 || true
adb -s "$S" pull /sdcard/honor_refresh_ui.xml "$OUT/pre_ui.xml" >/dev/null 2>&1 || true
CANCEL=$(python3 - "$OUT/pre_ui.xml" <<'PY'
import re, sys
try:
    text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
except OSError:
    raise SystemExit(0)
for match in re.finditer(r'<node[^>]*>', text):
    tag = match.group(0)
    if 'resource-id="android:id/button2"' in tag and re.search(r'text="(取消|Cancel)"', tag):
        bounds = re.search(r'bounds="([^"]+)"', tag)
        values = re.findall(r'\d+', bounds.group(1)) if bounds else []
        if len(values) == 4:
            x1, y1, x2, y2 = map(int, values)
            print((x1+x2)//2, (y1+y2)//2)
        break
PY
)
if [ -n "$CANCEL" ]; then
  IFS=' ' read -r CX CY <<EOF
$CANCEL
EOF
  adb -s "$S" shell input tap "$CX" "$CY"
  sleep 1.5
fi
adb -s "$S" shell rm -f /sdcard/honor_refresh_ui.xml >/dev/null 2>&1 || true
"$PF" onboard --package "$PKG" --serial "$S" --out "$OUT/onboard" --enter-only --no-charge-suspend
adb -s "$S" shell dumpsys SurfaceFlinger --list > "$OUT/sf_layers.txt"
adb -s "$S" shell dumpsys SurfaceFlinger > "$OUT/sf_dump.txt"
adb -s "$S" shell dumpsys SurfaceFlinger --displays > "$OUT/sf_displays.txt"
adb -s "$S" shell dumpsys window > "$OUT/window_dump.txt"
adb -s "$S" shell dumpsys package "$PKG" > "$OUT/package_dump.txt"
adb -s "$S" exec-out screencap -p > "$OUT/genshin_scene.png"
python3 - "$OUT" "$PKG" <<'PY'
import re, sys
from pathlib import Path
out, pkg = Path(sys.argv[1]), sys.argv[2]
text = (out / 'sf_layers.txt').read_text(encoding='utf-8', errors='replace')
candidates = [line.strip() for line in text.splitlines() if pkg in line and ('SurfaceView' in line or 'BLAST' in line)]
(out / 'game_layer.txt').write_text((candidates[-1] + '\n') if candidates else '', encoding='utf-8')
version = re.search(r'versionName=([^\s]+)', (out / 'package_dump.txt').read_text(encoding='utf-8', errors='replace'))
focus = (out / 'window_dump.txt').read_text(encoding='utf-8', errors='replace')
summary = [
    'package=' + pkg,
    'version=' + (version.group(1) if version else 'unknown'),
    'foreground=' + str(pkg in focus),
    'layer_candidates=' + repr(candidates),
]
(out / 'scene_summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
print('\n'.join(summary))
if not candidates:
    raise SystemExit('no SF game layer found')
if pkg not in focus:
    raise SystemExit('game is not foreground')
PY
