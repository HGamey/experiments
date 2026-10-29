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
