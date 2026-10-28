#!/bin/sh
# 红魔刷新率策略快照 v2 (验收用)。
# v1 (policy_snapshot.sh) 在灭屏时执行: 截图全黑, 且灭屏时 SF 策略被钉在 [60,60],
# 厂商控制器 resultFps=60 只是息屏决策, 不代表亮屏真实策略。
# 本版流程: 亮屏解锁 → 亮屏全量策略快照 (dumps 同 v1) → 导航 设置>显示>屏幕刷新率
#   (列表可滚动, dump 找不到就下滑重试) → 页面 UI XML + 截图 + 选中项 → 回桌面并恢复熄屏。
#   sh selfsr/policy_snapshot2.sh [serial]      # 只读 + input 导航; 不改任何设置; 结束恢复屏幕原状态
set -eu
S="${1:-91253241019A}"
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"

wakefulness() { adb -s "$S" shell dumpsys power 2>/dev/null | grep -m1 'mWakefulness=' | tr -d '\r' || true; }

ORIG=$(wakefulness)
echo "== 原始屏幕状态: ${ORIG:-未知} =="

# 1) 亮屏 + 尽量解锁 (无 PIN 时直接到桌面)
adb -s "$S" shell input keyevent KEYCODE_WAKEUP
adb -s "$S" shell wm dismiss-keyguard >/dev/null 2>&1 || true
sleep 2
echo "== 唤醒后屏幕状态: $(wakefulness) =="

# 2) 亮屏状态全量策略快照 (内容与 v1 相同, 便于对照)
{
  echo "== 时间 / 设备 (亮屏快照) =="
  date; adb -s "$S" shell 'getprop ro.product.model; getprop ro.build.version.release; uptime'
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
  echo; echo "== 电池 (温度与 43°C 策略线相关) =="
  adb -s "$S" shell 'dumpsys battery | grep -E "status:|level:|temperature:"'
} | tee "$OUT/policy_snapshot_on.txt"

# 3) 导航到 设置>显示, 滚动查找「刷新率」条目并点进去
adb -s "$S" shell am start -a android.settings.DISPLAY_SETTINGS >/dev/null
sleep 2.5

uidump() { # uidump <本地文件>
  adb -s "$S" shell uiautomator dump /sdcard/snap_ui.xml >/dev/null 2>&1 || true
  adb -s "$S" pull /sdcard/snap_ui.xml "$1" >/dev/null
}
find_tap() { # find_tap <xml文件>: 打印匹配「刷新率」节点中心坐标
  python3 - "$1" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8', errors='replace').read()
def center(b):
    n = re.findall(r'\d+', b)
    return ((int(n[0])+int(n[2]))//2, (int(n[1])+int(n[3]))//2) if len(n) == 4 else None
for m in re.finditer(r'<node[^>]*>', xml):
    tag = m.group(0)
    def attr(name, tag=tag):
        mm = re.search(name + r'="([^"]*)"', tag)
        return mm.group(1) if mm else ''
    label = attr('text') or attr('content-desc')
    if label and ('刷新率' in label or 'refresh rate' in label.lower()):
        c = center(attr('bounds'))
        if c:
            print(c[0], c[1]); break
PY
}

TAP=""
for i in 1 2 3 4 5 6; do
  uidump "$OUT/display_page.xml"
  TAP=$(find_tap "$OUT/display_page.xml" || true)
  [ -n "$TAP" ] && break
  echo "== 第 $i 次未找到刷新率条目, 下滑再试 =="
  adb -s "$S" shell input swipe 608 2000 608 700 350
  sleep 1.2
done
echo "== 「刷新率」条目坐标: ${TAP:-未找到} =="

if [ -n "${TAP:-}" ]; then
  adb -s "$S" exec-out screencap -p > "$OUT/display_page.png"
  adb -s "$S" shell input tap $TAP
  sleep 2
  uidump "$OUT/refresh_rate_page.xml"
  adb -s "$S" exec-out screencap -p > "$OUT/refresh_rate_page.png"
  { # 页面停留时的实时决策 (策略「正在生效」的证据) + 页面文字里的选中项
    adb -s "$S" shell 'settings list global | grep ScreenRefreshRateController_resultFps' || true
    adb -s "$S" shell 'dumpsys SurfaceFlinger | grep -m1 mPeriod' || true
    adb -s "$S" shell 'dumpsys battery | grep temperature:' || true
    echo "== 页面节点文字 (选中项/档位) =="
    python3 - "$OUT/refresh_rate_page.xml" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8', errors='replace').read()
for m in re.finditer(r'<node[^>]*>', xml):
    tag = m.group(0)
    def attr(name, tag=tag):
        mm = re.search(name + r'="([^"]*)"', tag)
        return mm.group(1) if mm else ''
    text, desc, sel = attr('text'), attr('content-desc'), attr('checked')
    if (text or desc) and ('刷新' in (text+desc) or 'Hz' in (text+desc) or re.search(r'智能|自动|流畅|省电|自适应|高', text+desc)):
        print(f'  text={text!r} desc={desc!r} checked={sel}')
PY
  } > "$OUT/policy_on_page.txt" 2>&1
else
  echo "!! 显示设置页滚动后仍未找到刷新率条目, 截显示设置页本身" >&2
  adb -s "$S" exec-out screencap -p > "$OUT/refresh_rate_page.png"
fi

# 4) 恢复: 回桌面; 原来灭屏则熄屏
adb -s "$S" shell input keyevent KEYCODE_HOME
sleep 1
case "$ORIG" in
  *mWakefulness=Awake*) : ;;
  *) adb -s "$S" shell input keyevent KEYCODE_SLEEP || true ;;
esac
echo "== 结束时屏幕状态: $(wakefulness) =="
echo "输出目录 $OUT: policy_snapshot_on.txt display_page.xml refresh_rate_page.xml refresh_rate_page.png policy_on_page.txt"
