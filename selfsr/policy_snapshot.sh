#!/bin/sh
# 红魔刷新率策略只读快照 (验收用): 用户设置 / SF 收到的策略 / 厂商控制器实时决策, 全部写进任务输出目录。
#   sh selfsr/policy_snapshot.sh [serial]        # 缺省 91253241019A; 只读, 不改任何设备状态
set -eu
S="${1:-91253241019A}"
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"
{
  echo "== 时间 / 设备 =="; date; adb -s "$S" shell 'getprop ro.product.model; getprop ro.build.version.release; uptime'
  echo; echo "== 用户设置 (system) =="
  adb -s "$S" shell 'settings list system | grep -i -E "refresh|fps|rate"'
  echo; echo "== 用户设置 (secure) =="
  adb -s "$S" shell 'settings list secure | grep -i -E "refresh|fps"' || true
  echo; echo "== 全局里厂商控制器相关 (global) =="
  adb -s "$S" shell 'settings list global | grep -i -E "ScreenRefreshRate|refresh"' || true
  echo; echo "== SF 当前周期 (mPeriod) 与策略 (displayManagerPolicy/activeMode/primaryRanges) =="
  adb -s "$S" shell 'dumpsys SurfaceFlinger | grep -m1 mPeriod'
  adb -s "$S" shell 'dumpsys SurfaceFlinger --displays' | sed -n '/displayManagerPolicy/,/^$/p; /activeMode=/p; /primaryRanges/p; /vsyncRate/p' | head -30
  echo; echo "== DisplayManager 视角 (投票/期望刷新率) =="
  adb -s "$S" shell 'dumpsys display | grep -i -E "refreshRate|desired|vote" | head -20' || true
  echo; echo "== 厂商特性 =="
  adb -s "$S" shell 'getprop | grep -i -E "zte_feature|fps_control"' || true
  echo; echo "== 电池 (温度与 43°C 策略线相关) =="
  adb -s "$S" shell 'dumpsys battery | grep -E "status:|level:|temperature:"'
} | tee "$OUT/policy_snapshot.txt"
# 设备当前屏幕一张 (策略的可见状态)
adb -s "$S" exec-out screencap -p > "$OUT/screen.png"
echo "快照: $OUT/policy_snapshot.txt  截图: $OUT/screen.png"
