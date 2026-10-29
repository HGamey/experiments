#!/bin/sh
# Collect raw game FPS/temperature CSV from SmartPerf, then stop and clean up.
set -eu
S="${1:-hdc:88X9K26901026869}"
PKG="${2:-com.miHoYo.Yuanshen}"
COUNT="${3:-10}"
OUT="${PF_JOB_OUT:-.}"
PF="${PHONEFARM_BIN:-phonefarm}"
SERIAL="${S#hdc:}"
REMOTE="/data/local/tmp/huawei_default_${PF_JOB_ID:-probe}.csv"
DOWNLOADED=0
mkdir -p "$OUT"
export DEVLOCK_LABEL="${DEVLOCK_LABEL:-job-${PF_JOB_ID:-huawei-measure}}"

cleanup() {
  RC=$?
  trap - 0 1 2 3 15
  "$PF" exec --serial "$S" "SP_daemon -stop" --yes > "$OUT/stop_collector.txt" 2>&1 || true
  if [ "$DOWNLOADED" -eq 1 ]; then
    "$PF" exec --serial "$S" "rm -f $REMOTE" --yes > "$OUT/remove_remote.txt" 2>&1 || true
  fi
  "$PF" exec --serial "$S" "aa force-stop $PKG" --yes > "$OUT/restore_game.txt" 2>&1 || true
  exit "$RC"
}
trap cleanup 0 1 2 3 15

command -v hdc >/dev/null 2>&1
"$PF" exec --serial "$S" "aa start -b $PKG -a UnityPlayerAbility" --yes > "$OUT/launch.txt" 2>&1
sleep 10
"$PF" exec --serial "$S" "SP_daemon -N $COUNT -PKG $PKG -f -t -profilerfps -OUT $REMOTE" --yes > "$OUT/capture.txt" 2>&1
hdc -t "$SERIAL" file recv "$REMOTE" "$OUT/samples.csv" > "$OUT/recv.txt" 2>&1
DOWNLOADED=1
python3 - "$OUT/samples.csv" <<'PY'
import csv, sys
from pathlib import Path
p = Path(sys.argv[1])
with open(p, encoding='utf-8', errors='replace', newline='') as f:
    rows = list(csv.reader(f))
print('csv_bytes=', p.stat().st_size, 'rows=', len(rows))
print('first_rows=', rows[:4])
if len(rows) < 2:
    raise SystemExit('empty SmartPerf CSV')
PY
