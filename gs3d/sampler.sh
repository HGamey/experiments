#!/system/bin/sh
# 设备侧采样 (root): 每 INTERVAL 秒一行，共 N 行。只读 sysfs，不写任何节点。
#   sh sampler.sh <N> [INTERVAL=1]
# 列: up=/proc/uptime 秒  bc=电池电流(本机 mA, 正=充入)  bv=电池电压 uV  uc=USB 电流 uA  uv=USB 电压 uV
#     bt=电池温度 0.1C  gclk=GPU 频率 Hz  gbusy=GPU 忙碌%  gpu_c/soc_c=GPU / CPU 热区最高温 (m°C)  st=充电状态
N=${1:?N}; IV=${2:-1}
P=/sys/class/power_supply; K=/sys/class/kgsl/kgsl-3d0
GZ=""; CZ=""
for z in /sys/class/thermal/thermal_zone*; do
  t=$(cat $z/type 2>/dev/null)
  case "$t" in gpuss*|gpu*) GZ="$GZ $z/temp";; cpu-*|cpuss*) CZ="$CZ $z/temp";; esac
done
mx() { m=-999999; for f in "$@"; do v=$(cat $f 2>/dev/null); [ -n "$v" ] && [ "$v" -gt "$m" ] && m=$v; done; echo $m; }
i=0
while [ $i -lt "$N" ]; do
  up=$(cut -d' ' -f1 /proc/uptime)
  echo "S up=$up bc=$(cat $P/battery/current_now) bv=$(cat $P/battery/voltage_now) uc=$(cat $P/usb/current_now) uv=$(cat $P/usb/voltage_now) bt=$(cat $P/battery/temp) gclk=$(cat $K/gpuclk) gbusy=$(cat $K/gpu_busy_percentage | tr -d ' %') gpu_c=$(mx $GZ) soc_c=$(mx $CZ) st=$(cat $P/battery/status)"
  sleep $IV
  i=$((i + 1))
done
