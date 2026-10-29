#!/bin/sh
# Read-only OpenHarmony game and measurement-capability inventory.
set -eu
S="${1:-hdc:88X9K26901026869}"
OUT="${PF_JOB_OUT:-.}"
PF="${PHONEFARM_BIN:-phonefarm}"
mkdir -p "$OUT"
export DEVLOCK_LABEL="${DEVLOCK_LABEL:-job-${PF_JOB_ID:-huawei-inventory}}"

probe() {
  echo "== $1 ==" | tee -a "$OUT/commands.log"
  "$PF" probe --serial "$S" "$1" > "$OUT/$2" 2>&1 || true
}
probe "bm dump -a" bundles.txt
probe "aa dump -a" missions.txt
probe "hidumper -s BatteryService" battery.txt
probe "hidumper -s RenderService" render.txt
probe "hidumper -s ThermalService" thermal.txt
probe "hidumper -s" system_abilities.txt
probe "which SP_daemon; SP_daemon --help" smartperf.txt
probe "cat /sys/class/thermal/thermal_zone*/temp" thermal_zones.txt
probe "param get const.product.model; param get const.build.os_version; param get const.product.software.version" device.txt

{
  echo "== Device =="
  cat "$OUT/device.txt"
  echo "== Bundle lines suggesting games =="
  grep -i -E 'miHoYo|HoYoverse|tencent|kurogame|unity|megacity|hypergryph|game|sky|endfield|wuthering|genshin' "$OUT/bundles.txt" | head -100 || true
  echo "== Battery =="
  cat "$OUT/battery.txt"
  echo "== SmartPerf =="
  cat "$OUT/smartperf.txt"
  echo "== Foreground missions =="
  cat "$OUT/missions.txt"
} > "$OUT/inventory_summary.txt"
cat "$OUT/inventory_summary.txt"
