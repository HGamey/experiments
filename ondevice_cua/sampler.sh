#!/system/bin/sh
# Device-side sampler, read-only: one line every INTERVAL s, N lines.
#   sh sampler.sh <N> [INTERVAL=1]
# Columns: up=/proc/uptime s, bc/bv=battery current/voltage (raw sysfs units), uc/uv=USB current/voltage (raw),
#   bt=battery temp (0.1 C), cpu_c=max CPU thermal zone (m C, zones >=100 C ignored as placeholders),
#   skin_c=max skin/shell/xo zone (m C), st=charge status, f0..f7=cur CPU freq (kHz), mem=MemAvailable kB.
N=${1:?N}; IV=${2:-1}
P=/sys/class/power_supply
CZ=""; SZ=""
for z in /sys/class/thermal/thermal_zone*; do
  t=$(cat $z/type 2>/dev/null)
  case "$t" in cpu-*|cpuss*|cpu_*) CZ="$CZ $z/temp";; *skin*|*shell*|xo-therm*|sys-therm*|quiet*) SZ="$SZ $z/temp";; esac
done
mx() { m=-999999; for f in "$@"; do v=$(cat $f 2>/dev/null); [ -n "$v" ] && [ "$v" -lt 100000 ] && [ "$v" -gt "$m" ] && m=$v; done; echo $m; }
fr() { o=""; for c in 0 1 2 3 4 5 6 7; do o="$o f$c=$(cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq 2>/dev/null)"; done; echo $o; }
i=0
while [ $i -lt "$N" ]; do
  up=$(cut -d' ' -f1 /proc/uptime)
  ma=$(grep MemAvailable /proc/meminfo | tr -s ' ' | cut -d' ' -f2)
  echo "S up=$up bc=$(cat $P/battery/current_now 2>/dev/null) bv=$(cat $P/battery/voltage_now 2>/dev/null) uc=$(cat $P/usb/current_now 2>/dev/null) uv=$(cat $P/usb/voltage_now 2>/dev/null) bt=$(cat $P/battery/temp 2>/dev/null) cpu_c=$(mx $CZ) skin_c=$(mx $SZ) st=$(cat $P/battery/status 2>/dev/null | tr -d ' ') mem=$ma $(fr)"
  sleep $IV
  i=$((i + 1))
done
