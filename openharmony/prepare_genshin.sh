#!/bin/sh
# Enter installed Genshin and validate normalized SmartPerf output on OpenHarmony.
set -eu
S="${1:-hdc:88X9K26901026869}"
PKG="${2:-com.miHoYo.Yuanshen}"
OUT="${PF_JOB_OUT:-.}"
PF="${PHONEFARM_BIN:-phonefarm}"
mkdir -p "$OUT"
export DEVLOCK_LABEL="${DEVLOCK_LABEL:-job-${PF_JOB_ID:-huawei-prepare}}"

"$PF" probe --serial "$S" "bm dump -n $PKG" > "$OUT/package.txt" 2>&1 || true
"$PF" probe --serial "$S" "aa dump -a" > "$OUT/missions_before.txt" 2>&1 || true
"$PF" exec --serial "$S" "aa start -b $PKG -a UnityPlayerAbility" --yes > "$OUT/launch.txt" 2>&1
sleep 30
"$PF" probe --serial "$S" "aa dump -a" > "$OUT/missions_after.txt" 2>&1 || true
"$PF" perf --serial "$S" --source smartperf --app "$PKG" --rounds 5 --json > "$OUT/perf_probe.json" 2> "$OUT/perf_probe.stderr"
cat "$OUT/perf_probe.json"
