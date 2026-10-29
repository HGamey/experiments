#!/bin/sh
# Collect raw game FPS/temperature CSV in bounded SmartPerf batches.
set -eu
S="${1:-hdc:88X9K26901026869}"
PKG="${2:-com.miHoYo.Yuanshen}"
COUNT="${3:-10}"
SETTLE="${4:-60}"
BATCH="${5:-100}"
OUT="${PF_JOB_OUT:-.}"
PF="${PHONEFARM_BIN:-phonefarm}"
SERIAL="${S#hdc:}"
REMOTE_BASE="/data/local/tmp/huawei_default_${PF_JOB_ID:-probe}"
mkdir -p "$OUT"
export DEVLOCK_LABEL="${DEVLOCK_LABEL:-job-${PF_JOB_ID:-huawei-measure}}"

cleanup() {
  RC=$?
  trap - 0 1 2 3 15
  "$PF" exec --serial "$S" "SP_daemon -stop" --yes > "$OUT/stop_collector.txt" 2>&1 || true
  "$PF" exec --serial "$S" "aa force-stop $PKG" --yes > "$OUT/restore_game.txt" 2>&1 || true
  exit "$RC"
}
trap cleanup 0 1 2 3 15

command -v hdc >/dev/null 2>&1
"$PF" exec --serial "$S" "aa start -b $PKG -a UnityPlayerAbility" --yes > "$OUT/launch.txt" 2>&1
sleep "$SETTLE"
DONE=0
INDEX=0
while [ "$DONE" -lt "$COUNT" ]; do
  REMAINING=$((COUNT - DONE))
  if [ "$REMAINING" -gt "$BATCH" ]; then
    N="$BATCH"
  else
    N="$REMAINING"
  fi
  INDEX=$((INDEX + 1))
  REMOTE="${REMOTE_BASE}_${INDEX}.csv"
  LOCAL="$OUT/batch_${INDEX}.csv"
  "$PF" exec --serial "$S" "SP_daemon -N $N -PKG $PKG -f -t -profilerfps -OUT $REMOTE" --yes > "$OUT/capture_${INDEX}.txt" 2>&1
  "$PF" exec --serial "$S" "SP_daemon -stop" --yes > "$OUT/stop_${INDEX}.txt" 2>&1 || true
  hdc -t "$SERIAL" file recv "$REMOTE" "$LOCAL" > "$OUT/recv_${INDEX}.txt" 2>&1
  python3 - "$LOCAL" "$N" <<'PY'
import csv, sys
from pathlib import Path
p, expected = Path(sys.argv[1]), int(sys.argv[2])
with open(p, encoding='utf-8', errors='replace', newline='') as f:
    rows = list(csv.reader(f))
if len(rows) < 2:
    raise SystemExit(f'empty SmartPerf batch: expected {expected}')
print(f'batch_rows={len(rows) - 1} expected={expected} bytes={p.stat().st_size}')
PY
  "$PF" exec --serial "$S" "rm -f $REMOTE" --yes > "$OUT/remove_${INDEX}.txt" 2>&1 || true
  DONE=$((DONE + N))
done

python3 - "$OUT" <<'PY'
import csv, re, sys
from pathlib import Path
out = Path(sys.argv[1])
paths = sorted(out.glob('batch_*.csv'), key=lambda p: int(re.search(r'batch_(\d+)\.csv$', p.name).group(1)))
if not paths:
    raise SystemExit('no downloaded batches')
with open(out / 'samples.csv', 'w', encoding='utf-8', newline='') as dest:
    writer = csv.writer(dest)
    total = 0
    for index, path in enumerate(paths):
        with open(path, encoding='utf-8', errors='replace', newline='') as src:
            rows = list(csv.reader(src))
        if not rows:
            raise SystemExit(f'empty batch file: {path}')
        writer.writerows(rows if index == 0 else rows[1:])
        total += len(rows) - 1
print(f'combined_samples={total} batches={len(paths)}')
if total <= 0:
    raise SystemExit('empty combined CSV')
PY
