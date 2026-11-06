#!/bin/sh
# D 轨荣耀 v2 (lead 05:5xZ 最后机会): 先启 GameLauncher(非 Q3EMain) UI 勾选加载本地引擎库,
# 再启游戏验证 SmallSR buffers/权重, F9/F8 三档截图须互不相同, 帧率读 com_showFPS 截图。
set -eu
SERIAL="${1:?usage: d_honor_sr2.sh <serial>}"
PKG=com.karin.idTech4Amm
DATA=/storage/emulated/0/Android/data/$PKG/files/diii4a
BASE=$DATA/doom3; STATE=$DATA/state
OUT="${PF_JOB_OUT:-$(pwd)/sr_out}/d_honor2_$(date +%Y%m%d-%H%M%S)-$SERIAL"
mkdir -p "$OUT"
sh_() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
dump_() { sh_ "uiautomator dump /data/local/tmp/d.xml >/dev/null 2>&1; cat /data/local/tmp/d.xml" > "$OUT/ui_$1.xml" 2>/dev/null || true; }
click_text_() { python3 - "$OUT/ui_$1.xml" "$2" <<'PY'
import re, sys
try: xml = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception: print("NONE"); raise SystemExit
best = None
for m in re.finditer(r'<node[^>]*>', xml):
    n = m.group(0)
    if sys.argv[2] in n:
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if b:
            best = ((int(b.group(1))+int(b.group(3)))//2, (int(b.group(2))+int(b.group(4)))//2)
print(f"{best[0]} {best[1]}" if best else "NONE")
PY
}
toggle_state_() { python3 - "$OUT/ui_t.xml" <<'PY'
import re, sys
try: xml = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception: print("UNKNOWN"); raise SystemExit
nodes = re.findall(r'<node[^>]*>', xml)
for i, n in enumerate(nodes):
    if "加载本地引擎库" in n or "Load local engine" in n:
        c = re.search(r'checked="(\w+)"', n)
        print("CHECKED=" + (c.group(1) if c else "attr_absent"))
        for m in nodes[max(0,i-3):i+3]:
            if 'class="android.widget.CheckBox"' in m or 'SeekBar' in m:
                c2 = re.search(r'checked="(\w+)"', m)
                print("NEIGHBOR=" + (c2.group(1) if c2 else "absent"))
        raise SystemExit
print("TEXT_ABSENT")
PY
}

sh_ "am force-stop $PKG" >/dev/null || true; sh_ "am kill-all" >/dev/null 2>&1 || true
# 1) 启动 GameLauncher 界面
sh_ "am start -n $PKG/com.n0n3m4.DIII4A.GameLauncher" >/dev/null
sleep 15
dump_ launcher
grep -c "node" "$OUT/ui_launcher.xml" >/dev/null 2>&1 && echo "LAUNCHER_DUMP_BYTES=$(wc -c < "$OUT/ui_launcher.xml")"
FOCUS=$(sh_ "dumpsys window | grep -i mCurrentFocus"); echo "FOCUS=$FOCUS"
echo "STEP1_LAUNCHER_STARTED" >> "$OUT/steps.txt"
# 2) 导航: 溢出菜单(content-desc=更多选项) → 其他 → 调试 → 加载本地引擎库(中/英都匹配)
dump_ nav; MPOS=$(click_text_ nav "更多选项")
if [ "$MPOS" != "NONE" ]; then sh_ "input tap $MPOS" >/dev/null; sleep 2; echo "MENU_OPENED at $MPOS" >> "$OUT/steps.txt"; else sh_ "input keyevent 82" >/dev/null 2>&1 || true; sleep 2; fi
nav_done=0
for stepkw in "其他" "调试" "加载本地引擎库" "Load local engine"; do
  dump_ nav
  POS=$(click_text_ nav "$stepkw")
  if [ "$POS" = "NONE" ]; then
    sh_ "input keyevent 82" >/dev/null 2>&1 || true; sleep 2
    dump_ nav; POS=$(click_text_ nav "$stepkw")
  fi
  if [ "$POS" = "NONE" ]; then
    echo "UI_MISS=$stepkw" | tee -a "$OUT/steps.txt"
  else
    sh_ "input tap $POS" >/dev/null; sleep 2
    echo "UI_HIT=$stepkw at $POS" | tee -a "$OUT/steps.txt"
    case "$stepkw" in "加载本地引擎库"|"Load local engine") nav_done=1;; esac
  fi
done
sleep 1; dump_ t; TS=$(toggle_state_); echo "TOGGLE_STATE=$TS" | tee -a "$OUT/steps.txt"
sh_ "screencap -p /data/local/tmp/settings2.png" >/dev/null
adb -s "$SERIAL" pull /data/local/tmp/settings2.png "$OUT/settings_page.png" >/dev/null 2>&1 || true
# 若未勾上: 再点该行右端(复选框通常在行右侧), 依次试右端/中心/右端偏内
if [ "$TS" != "CHECKED=true" ]; then
  dump_ nav
  RP=$(python3 - "$OUT/ui_nav.xml" <<'PY'
import re, sys
try: xml = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception: print("NONE"); raise SystemExit
best = None
for m in re.finditer(r'<node[^>]*>', xml):
    n = m.group(0)
    if ("加载本地引擎库" in n or "Load local engine" in n):
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if b:
            x1, y1, x2, y2 = map(int, b.groups())
            best = f"{x2-50} {(y1+y2)//2} {x2} {y1} {y2}"
print(best if best else "NONE")
PY
)
  echo "ROW_BOUNDS=$RP" | tee -a "$OUT/steps.txt"
  if [ "$RP" != "NONE" ]; then
    X=$(echo "$RP" | cut -d ' ' -f 1); Y=$(echo "$RP" | cut -d ' ' -f 2)
    sh_ "input tap $X $Y" >/dev/null; sleep 2; dump_ t; TS=$(toggle_state_); echo "TOGGLE_RIGHT_STATE=$TS" | tee -a "$OUT/steps.txt"
  fi
  if [ "$TS" != "CHECKED=true" ]; then
    dump_ nav; POS=$(click_text_ nav "加载本地引擎库")
    [ "$POS" != "NONE" ] && { sh_ "input tap $POS" >/dev/null; sleep 2; dump_ t; TS=$(toggle_state_); echo "TOGGLE_CENTER_STATE=$TS" | tee -a "$OUT/steps.txt"; }
  fi
  sh_ "screencap -p /data/local/tmp/settings3.png" >/dev/null
  adb -s "$SERIAL" pull /data/local/tmp/settings3.png "$OUT/settings_page_retry.png" >/dev/null 2>&1 || true
fi
sh_ "input keyevent 4" >/dev/null 2>&1 || true; sh_ "input keyevent 4" >/dev/null 2>&1 || true
sh_ "am force-stop $PKG" >/dev/null; sleep 2
echo "STEP2_UI_DONE nav_done=$nav_done" | tee -a "$OUT/steps.txt"

# 3) 启动游戏(Q3EMain), 180s 加载 + 跳过过场
sh_ "logcat -c" >/dev/null 2>&1 || true
sh_ "am start -W -S -n $PKG/com.n0n3m4.q3e.Q3EMain --es game doom3 --es command 'game.arm +set fs_basepath $BASE +set fs_savepath $STATE +set com_showFPS 1 +set r_multiSamples 0 +set r_screenFraction 100 +set r_maxFps 60 +set g_skill 1 +set com_allowConsole 1 +exec sr_keys.cfg +exec sr_native.cfg +map game/demo_mars_city1'" | tail -2
P=""; i=0; while [ $i -lt 20 ]; do sleep 3; P=$(sh_ "pidof $PKG || true" | tr -d '\r'); [ -n "$P" ] && break; i=$((i+3)); done
[ -n "$P" ] || { echo "LAUNCH_DEAD"; exit 6; }
echo "GAME_PID=$P"
sleep 150
sh_ "input tap 636 700" >/dev/null 2>&1 || true; sleep 3
sh_ "input keyevent 111" >/dev/null 2>&1 || true; sleep 15
sh_ "logcat -d 2>/dev/null | grep -iE 'SmallSR|local engine|libdoom3|6cc44b53' | tail -n 60" > "$OUT/logcat_sr.txt" || true
echo "LOGCAT_SMALLSR_LINES=$(wc -l < "$OUT/logcat_sr.txt")"; head -15 "$OUT/logcat_sr.txt" || true
echo "STEP3_LAUNCH_DONE" >> "$OUT/steps.txt"

# 4) 三档: native(F9) -> F8(1) -> F8(2); 每档 2 张截图, 校验 sha 互异
shot_() { sh_ "screencap -p /data/local/tmp/t_$1_$2.png" >/dev/null; adb -s "$SERIAL" pull "/data/local/tmp/t_$1_$2.png" "$OUT/t_$1_$2.png" >/dev/null 2>&1 || true; }
sh_ "input keyevent 143" >/dev/null 2>&1 || true; sleep 8   # F9: r_smallSR 0
shot_ native 1; sleep 10; shot_ native 2
sh_ "input keyevent 142" >/dev/null 2>&1 || true; sleep 10  # F8: -> 1 (off)
shot_ off 1; sleep 10; shot_ off 2
sh_ "input keyevent 142" >/dev/null 2>&1 || true; sleep 10  # F8: -> 2 (on)
shot_ on 1; sleep 10; shot_ on 2
shasum -a 256 "$OUT"/t_*.png > "$OUT/shots_sha.txt" 2>/dev/null || true
cat "$OUT/shots_sha.txt"
UNIQ=$(cut -d ' ' -f 1 "$OUT/shots_sha.txt" 2>/dev/null | sort -u | wc -l)
echo "UNIQUE_SHOTS=$UNIQ" | tee -a "$OUT/steps.txt"
echo "D2_DONE $OUT"
cat "$OUT/steps.txt"
