#!/bin/sh
# charge_probe.sh <serial> — 停充保持探针 (Qualcomm charging_enabled)。
# 先把设备侧脚本 push 到 /data/local/tmp 再以 root 运行 (adb shell 会把长命令按空格拆散,
# su -c 必须只接一个词; power_sample.sh 同款做法)。写 0 后每 2s 回读节点/电流共 30s, 结束恢复 1。
S=${1:?用法: sh selfsr/charge_probe.sh <serial>}
T=$(mktemp)
cat > "$T" <<'EOF'
#!/system/bin/sh
B=/sys/class/qcom-battery/charging_enabled
P=/sys/class/power_supply/battery
U=/sys/class/power_supply/usb
[ -w "$B" ] || { echo "no $B (writable check failed)"; ls -l "$B"; exit 3; }
echo "before enabled=$(cat "$B") cur=$(cat "$P/current_now")"
echo 0 > "$B"
i=0
while [ "$i" -lt 15 ]; do
  echo "t=$((i*2)) enabled=$(cat "$B") cur=$(cat "$P/current_now") usb_cur=$(cat "$U/current_now" 2>/dev/null || echo NA) temp=$(cat "$P/temp")"
  sleep 2
  i=$((i+1))
done
echo 1 > "$B"
echo "after enabled=$(cat "$B") cur=$(cat "$P/current_now")"
EOF
adb -s "$S" push "$T" /data/local/tmp/charge_probe_dev.sh >/dev/null
rm -f "$T"
adb -s "$S" shell su -c "sh /data/local/tmp/charge_probe_dev.sh"
adb -s "$S" shell su -c "rm -f /data/local/tmp/charge_probe_dev.sh"
