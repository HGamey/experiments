#!/bin/sh
# HONOR current display/thermal policy snapshot (read-only settings + UI navigation).
# Usage: sh selfsr/honor_policy_snapshot.sh [serial]
# No refresh-rate, resolution, thermal, or other settings are changed.
set -eu
S="${1:-AVXB6R5C19000481}"
OUT="${PF_JOB_OUT:-.}"
UI_REMOTE="/sdcard/honor_policy_ui.xml"
mkdir -p "$OUT"

wakefulness() {
  adb -s "$S" shell dumpsys power 2>/dev/null | grep -m1 'mWakefulness=' | tr -d '\r' || true
}
ORIG=$(wakefulness)
echo "== Original screen state: ${ORIG:-unknown} =="

cleanup() {
  RC=$?
  trap - 0 1 2 3 15
  adb -s "$S" shell input keyevent KEYCODE_HOME >/dev/null 2>&1 || true
  sleep 1
  case "$ORIG" in
    *mWakefulness=Awake*) : ;;
    *) adb -s "$S" shell input keyevent KEYCODE_SLEEP >/dev/null 2>&1 || true ;;
  esac
  adb -s "$S" shell rm -f "$UI_REMOTE" >/dev/null 2>&1 || true
  END=$(wakefulness)
  {
    echo "original=${ORIG:-unknown}"
    echo "final=${END:-unknown}"
    echo "settings_changed=false"
    echo "temporary_ui_xml_removed=true"
  } > "$OUT/restore.txt"
  echo "== Final screen state: ${END:-unknown} =="
  exit "$RC"
}
trap cleanup 0 1 2 3 15

# Wake and dismiss a non-PIN keyguard. No security credential is bypassed.
adb -s "$S" shell input keyevent KEYCODE_WAKEUP
adb -s "$S" shell wm dismiss-keyguard >/dev/null 2>&1 || true
sleep 2

# Preserve raw evidence independently of the later human-readable filtering.
set +e
adb -s "$S" shell settings list system > "$OUT/settings_system.txt" 2>&1
adb -s "$S" shell settings list secure > "$OUT/settings_secure.txt" 2>&1
adb -s "$S" shell settings list global > "$OUT/settings_global.txt" 2>&1
adb -s "$S" shell wm size > "$OUT/wm_size.txt" 2>&1
adb -s "$S" shell wm density > "$OUT/wm_density.txt" 2>&1
adb -s "$S" shell dumpsys display > "$OUT/display_dump.txt" 2>&1
adb -s "$S" shell dumpsys SurfaceFlinger --displays > "$OUT/sf_displays.txt" 2>&1
adb -s "$S" shell dumpsys SurfaceFlinger > "$OUT/sf_dump.txt" 2>&1
adb -s "$S" shell dumpsys thermalservice > "$OUT/thermal_service.txt" 2>&1
adb -s "$S" shell dumpsys battery > "$OUT/battery.txt" 2>&1
adb -s "$S" shell getprop > "$OUT/getprop.txt" 2>&1
# The variables in the single-quoted loop deliberately expand in Android shell.
# shellcheck disable=SC2016
adb -s "$S" shell 'for z in /sys/class/thermal/thermal_zone*; do [ -r "$z/type" ] && [ -r "$z/temp" ] || continue; printf "%s type=" "$z"; cat "$z/type"; printf "%s temp=" "$z"; cat "$z/temp"; done' > "$OUT/thermal_zones.txt" 2>&1
{
  echo "== Time / device =="
  date
  adb -s "$S" shell 'getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; uptime'
  echo "== Screen state =="
  wakefulness
  echo "== Physical and override resolution / density =="
  cat "$OUT/wm_size.txt" "$OUT/wm_density.txt"
  echo "== User settings: refresh / fps / resolution =="
  grep -i -E 'refresh|fps|resolution|display_size|display_density' "$OUT/settings_system.txt" "$OUT/settings_secure.txt" "$OUT/settings_global.txt"
  echo "== DisplayManager: modes / refresh / thermal =="
  grep -i -E 'DisplayDeviceInfo|DisplayMode|refreshRate|thermal|desired|vote|density|appWidth|appHeight|physicalWidth|physicalHeight' "$OUT/display_dump.txt" | head -180
  echo "== SurfaceFlinger: active mode / period / policy =="
  grep -m2 -i 'mPeriod' "$OUT/sf_dump.txt"
  sed -n '/displayManagerPolicy/,/^$/p; /activeMode=/p; /primaryRanges/p; /vsyncRate/p' "$OUT/sf_displays.txt" | head -100
  echo "== Thermal service =="
  cat "$OUT/thermal_service.txt"
  echo "== Battery =="
  cat "$OUT/battery.txt"
  echo "== Thermal zones =="
  cat "$OUT/thermal_zones.txt"
  echo "== Related device properties =="
  grep -i -E 'refresh|fps|thermal|display|density|resolution' "$OUT/getprop.txt" | head -100
} > "$OUT/policy_snapshot_on.txt" 2>&1
set -e
cat "$OUT/policy_snapshot_on.txt"

# Navigate to the visible display settings, then find the refresh-rate entry.
adb -s "$S" shell am start -a android.settings.DISPLAY_SETTINGS >/dev/null
sleep 2.5
uidump() {
  adb -s "$S" shell uiautomator dump "$UI_REMOTE" >/dev/null 2>&1 || return 1
  adb -s "$S" pull "$UI_REMOTE" "$1" >/dev/null 2>&1 || return 1
  adb -s "$S" shell rm -f "$UI_REMOTE" >/dev/null 2>&1 || true
}
dialog_title() {
  python3 - "$1" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8', errors='replace').read()
for match in re.finditer(r'<node[^>]*>', xml):
    tag = match.group(0)
    if 'resource-id="android:id/alertTitle"' in tag:
        title = re.search(r'text="([^"]*)"', tag)
        print(title.group(1) if title else 'system dialog')
        break
PY
}
find_tap() {
  python3 - "$1" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8', errors='replace').read()
def center(bounds):
    values = re.findall(r'\d+', bounds)
    return ((int(values[0])+int(values[2]))//2, (int(values[1])+int(values[3]))//2) if len(values) == 4 else None
for match in re.finditer(r'<node[^>]*>', xml):
    tag = match.group(0)
    def attr(name):
        found = re.search(name + r'="([^"]*)"', tag)
        return found.group(1) if found else ''
    label = attr('text') or attr('content-desc')
    if label and ('刷新率' in label or 'refresh rate' in label.lower()):
        point = center(attr('bounds'))
        if point:
            print(point[0], point[1])
            break
PY
}
SWIPE=$(python3 - "$OUT/wm_size.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
match = re.search(r'(\d+)x(\d+)', text)
if match:
    w, h = map(int, match.groups())
    print(w // 2, int(h * .82), w // 2, int(h * .28))
else:
    print(608, 2000, 608, 700)
PY
)
TAP=""
for I in 1 2 3 4 5 6; do
  if uidump "$OUT/display_page.xml"; then
    DIALOG=$(dialog_title "$OUT/display_page.xml" || true)
    if [ -n "$DIALOG" ]; then
      echo "== Dismissing system dialog without changing its setting: $DIALOG =="
      adb -s "$S" shell input keyevent KEYCODE_BACK
      sleep 1
      adb -s "$S" shell am start -a android.settings.DISPLAY_SETTINGS >/dev/null
      sleep 2
      uidump "$OUT/display_page.xml" || true
    fi
    TAP=$(find_tap "$OUT/display_page.xml" || true)
  fi
  [ -n "$TAP" ] && break
  echo "== Refresh-rate entry not found in pass $I; scrolling =="
  IFS=' ' read -r SX1 SY1 SX2 SY2 <<EOF
$SWIPE
EOF
  adb -s "$S" shell input swipe "$SX1" "$SY1" "$SX2" "$SY2" 350
  sleep 1.2
done
echo "== Refresh-rate entry coordinates: ${TAP:-not found} =="

if [ -n "$TAP" ]; then
  adb -s "$S" exec-out screencap -p > "$OUT/display_page.png"
  IFS=' ' read -r TX TY <<EOF
$TAP
EOF
  adb -s "$S" shell input tap "$TX" "$TY"
  sleep 2
  uidump "$OUT/refresh_rate_page.xml" || true
else
  echo "No refresh-rate entry found; preserving the display-settings page instead." >&2
fi
adb -s "$S" exec-out screencap -p > "$OUT/refresh_rate_page.png"

# Preserve every visible label so missing checked= attributes do not hide evidence.
if [ -f "$OUT/refresh_rate_page.xml" ]; then
  python3 - "$OUT/refresh_rate_page.xml" > "$OUT/refresh_rate_nodes.txt" <<'PY'
import re, sys
xml = open(sys.argv[1], encoding='utf-8', errors='replace').read()
for match in re.finditer(r'<node[^>]*>', xml):
    tag = match.group(0)
    def attr(name):
        found = re.search(name + r'="([^"]*)"', tag)
        return found.group(1) if found else ''
    label, desc = attr('text'), attr('content-desc')
    if label or desc:
        print(f"text={label!r} desc={desc!r} checked={attr('checked')} selected={attr('selected')} bounds={attr('bounds')}")
PY
fi
{
  echo "== Policy while the captured page is foreground =="
  adb -s "$S" shell dumpsys SurfaceFlinger --displays > "$OUT/page_sf_displays.txt" 2>&1 || true
  grep -i -E 'activeFrameRateMode|activeDisplayMode|displayManagerPolicy|mPeriod' "$OUT/page_sf_displays.txt" || true
  adb -s "$S" shell 'settings list system; settings list secure; settings list global' | grep -i -E 'refresh|fps|resolution' || true
  adb -s "$S" shell 'dumpsys battery | grep -E "status:|level:|temperature:"' || true
  echo "== Visible page nodes =="
  cat "$OUT/refresh_rate_nodes.txt" 2>/dev/null || true
} > "$OUT/policy_on_page.txt" 2>&1

echo "Snapshot: $OUT/policy_snapshot_on.txt"
echo "Screenshot: $OUT/refresh_rate_page.png"
