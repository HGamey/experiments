#!/bin/sh
# 查「测量窗口内屏幕被系统压回 60 Hz」(2026-10-28 第 7 组正式跑 6/6 srip 臂全因此作废):
#   按臂挂层/冷启动进大世界/设 120 Hz (同 selfsr_ab 的臂), 然后每秒采样厂商控制器结果、SF 周期与范围、
#   电池/前台 (traces/P*_hold.tsv), 不开测量窗口。阶段可选点屏 (tap) 和掉落时重设 (reassert) 对照。
#   sh selfsr/hold120.sh <serial> [输出目录] [包名] [KEY=VALUE ...]
# 阶段 PHASES 分号分隔, 每段: <臂>|panel=120|tap=<秒>|reassert=1|hold=<秒>; 臂写法同 repro.sh。
# 缺省四阶段 (各 300 秒, 顺序: 复现 → 点屏 → 重设 → 无层对照), 整轮约 35–45 分钟。
#   PHASES="srip:1:2688x1216:1.0:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto|hold=300;
#           srip:...|tap=20|hold=300; srip:...|reassert=1|hold=300; native|panel=120|hold=300"
# 其余 KEY=VALUE: SETTLE (进世界后静置秒, 缺省 15) SR_SHARP (1.0) POWER (不支持, 探测不停充)。
# 收尾与 repro.sh 相同: Game Mode/层属性/刷新率/风扇/screen_off_timeout 写回并核对, 层由会话摘除。
set -eu
n=$#
for arg in "$@"; do
  case $arg in
    PHASES=*|SETTLE=*|SR_SHARP=*|SESSION_SECONDS=*)
      export "$arg" ;;
    *) set -- "$@" "$arg" ;;
  esac
done
shift "$n"
SERIAL="${1:?用法: sh selfsr/hold120.sh <serial> [输出目录] [包名] [KEY=VALUE ...]}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${PF_ROOT:-$(cd "$HERE/../.." && pwd)}
LAYER="$HERE/gray"; [ -d "$LAYER" ] || LAYER="$ROOT/knobs/gray"
OUT="${2:-${PF_JOB_OUT:-$ROOT/_runs}/hold120_$(date +%Y%m%d-%H%M%S)}"
PKG="${3:-com.miHoYo.Yuanshen}"
PANEL=$(adb -s "$SERIAL" shell wm size | tr -d '\r' | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p' | tail -1 | awk '{print ($1>$2)?$1"x"$2:$2"x"$1}')
SR_FULL="$PANEL:${SR_SHARP:-1.0}"
SRIP="srip:1:$SR_FULL:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto"
PHASES="${PHASES:-$SRIP|hold=300;$SRIP|tap=20|hold=300;$SRIP|reassert=1|hold=300;native|panel=120|hold=300}"
# 灰档会话必须覆盖最长 hold；否则 1200s 默认预算会在长采样中途关层（1800s 长稳/缓解无效样本的根因）。
MAX_HOLD=$(printf '%s\n' "$PHASES" | grep -o 'hold=[0-9][0-9]*' | cut -d= -f2 | sort -nr | head -1)
SESSION_SECONDS="${SESSION_SECONDS:-$((${MAX_HOLD:-0} + 600))}"
[ "$SESSION_SECONDS" -ge 1200 ] || SESSION_SECONDS=1200
[ "$(adb -s "$SERIAL" shell su -c id 2>/dev/null | grep -c 'uid=0')" = 1 ] || { echo "这台手机没有 root, 层装不进游戏"; exit 2; }
bash "$LAYER/build/build_layer.sh" >/dev/null
mkdir -p "$OUT"
SO="$OUT/libVkLayer_refknobs.so"
cp "$LAYER/build/out/libVkLayer_refknobs.so" "$SO"
BIN="${PHONEFARM_BIN:-}"
if [ -z "$BIN" ]; then
  BIN="$ROOT/src/target/release/phonefarm"
  [ -x "$BIN" ] || (cd "$ROOT/src" && cargo build --release)
  codesign --force --sign - "$BIN" 2>/dev/null || true
fi
echo "阶段: $PHASES (灰档会话上限 ${SESSION_SECONDS}s)"
PHASES="$PHASES" python3 -I "$HERE/selfsr_ab.py" hold --serial "$SERIAL" --pkg "$PKG" --out "$OUT" \
  --so "$SO" --phonefarm "$BIN" --fan --settle "${SETTLE:-15}" --phases "$PHASES" --session-seconds "$SESSION_SECONDS"
echo "汇总: $OUT/hold_summary.json  每秒采样: $OUT/traces/P*_hold.tsv"
