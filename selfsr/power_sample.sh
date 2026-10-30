#!/system/bin/sh
# 整机功耗采样 (高通机, 要 root): 停充 → 等电流稳定 (最多 PRE 秒, 每 5s 重写停充并查电流, 转负即停等)
# → 每秒读一次电池电流/电压/温度, 共 N 次 → 恢复充电。
#   sh power_sample.sh <N> [PRE, 缺省 25]
# 每行输出 "<电流 uA> <电压 uV> <温度 0.1°C>"; 放电电流为负。停充后 USB 不再给手机供电 (usb current_now = 0),
# 整机耗电全部来自电池, 所以 -电流 × 电压 就是整机功耗。中途被杀也会在 N+PRE+30 秒后自己恢复充电。
# 2026-10-30: 高温+游戏态下停充可能不被充电 IC 理会 (43°C A 臂两轮采样全程正电流), 改为 PRE 期间每 5s
# 重写停充并回读电流, 连续 2 次读到负电流才开始采样; PRE 耗尽仍正电流则输出 STOPCHARGE_FAILED 并退出 4,
# 让上层把该臂功耗判无效 (2026-10-30 探针 47e21052ace6: 机制本身完好, 空闲 36°C 下 6-8s 平滑转放电)。
N=${1:?N}; PRE=${2:-25}
B=/sys/class/qcom-battery/charging_enabled; P=/sys/class/power_supply/battery
[ -w $B ] || { echo "no $B"; exit 3; }
(sleep $((N + PRE + 30)); echo 1 > $B) >/dev/null 2>&1 &
neg=0; waited=0
while [ $waited -lt "$PRE" ]; do
  echo 0 > $B
  sleep 5
  waited=$((waited + 5))
  c=$(cat $P/current_now)
  if [ "$c" -lt 0 ]; then neg=$((neg + 1)); else neg=0; fi
  [ "$neg" -ge 2 ] && break
done
if [ "$neg" -lt 2 ]; then
  echo "STOPCHARGE_FAILED last_cur=$(cat $P/current_now)"
  exit 4
fi
i=0
while [ $i -lt "$N" ]; do
  echo "$(cat $P/current_now) $(cat $P/voltage_now) $(cat $P/temp)"
  sleep 1
  i=$((i + 1))
done
echo 1 > $B
