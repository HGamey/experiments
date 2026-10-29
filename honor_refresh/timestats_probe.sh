#!/bin/sh
# Probe actual game-layer frames through SurfaceFlinger timestats (no settings changes).
set -eu
S="${1:-AVXB6R5C19000481}"
PKG="${2:-com.miHoYo.Yuanshen}"
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"
adb -s "$S" shell 'dumpsys SurfaceFlinger --timestats -disable -clear >/dev/null; dumpsys SurfaceFlinger --timestats -enable >/dev/null; input swipe 1900 650 1000 650 450; sleep 5; input swipe 1000 650 1900 650 450; sleep 5; dumpsys SurfaceFlinger --timestats -dump -maxlayers 24; dumpsys SurfaceFlinger --timestats -disable >/dev/null' > "$OUT/timestats.txt"
python3 - "$OUT" "$PKG" <<'PY'
import json, re, sys
from pathlib import Path
out, pkg = Path(sys.argv[1]), sys.argv[2]
text = (out / 'timestats.txt').read_text(encoding='utf-8', errors='replace')
result = {}
for key in ('missedFrames', 'displayOnTime', 'displayRefreshRate'):
    match = re.search(r'^' + key + r' = (\d+)', text, re.M)
    result[key] = int(match.group(1)) if match else None
layers = []
for block in text.split('layerName = ')[1:]:
    name = block.splitlines()[0]
    if pkg in name:
        match = re.search(r'^totalFrames = (\d+)', block, re.M)
        layers.append({'name': name, 'totalFrames': int(match.group(1)) if match else None})
result['layers'] = layers
(out / 'timestats_analysis.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
PY
