#!/bin/sh
# charge_probe.sh <serial> — 停充保持探针 (Qualcomm charging_enabled)。
# 写 0 后每 2s 回读节点/电池电流/USB 电流共 30s, 结束恢复 1。
# 功耗轮 A 臂「停充没生效」根因验证: 厂商守护是否把节点翻回 1, 或电流仍正读。
# 注意: 命令经 host sh → adb → 设备 sh → su -c 多层转义, 变量与算术都会丢, 必须全字面量。
S=${1:?用法: sh selfsr/charge_probe.sh <serial>}
adb -s "$S" shell su -c 'echo 0 > /sys/class/qcom-battery/charging_enabled; for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do cat /sys/class/qcom-battery/charging_enabled /sys/class/power_supply/battery/current_now /sys/class/power_supply/usb/current_now /sys/class/power_supply/battery/temp | tr "\n" " "; echo; sleep 2; done; echo 1 > /sys/class/qcom-battery/charging_enabled; echo restored; cat /sys/class/qcom-battery/charging_enabled'
