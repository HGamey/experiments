#!/bin/sh
# charge_probe.sh <serial> — 停充保持探针 ( Qualcomm charging_enabled ):
# 写 0 后每 2s 回读节点值/电池电流/USB 电流共 30s, 结束恢复 1。
# 用途: 功耗轮里 A 臂「停充没生效」的根因验证 —— 厂商守护是否把节点翻回 1, 或电流仍在正读。
# 只动 charging_enabled 一个节点, 结束必恢复。注意: 多行脚本经 adb shell su -c 传参会被拆散, 必须单行。
S=${1:?用法: sh selfsr/charge_probe.sh <serial>}
adb -s "$S" shell su -c "B=/sys/class/qcom-battery/charging_enabled; P=/sys/class/power_supply/battery; U=/sys/class/power_supply/usb; [ -w \$B ] || { echo no \$B; exit 3; }; echo before enabled=\$(cat \$B) cur=\$(cat \$P/current_now); echo 0 > \$B; i=0; while [ \$i -lt 15 ]; do echo t=\$((i*2)) enabled=\$(cat \$B) cur=\$(cat \$P/current_now) usb_cur=\$(cat \$U/current_now 2>/dev/null) temp=\$(cat \$P/temp); sleep 2; i=\$((i+1)); done; echo 1 > \$B; echo after enabled=\$(cat \$B) cur=\$(cat \$P/current_now)"
