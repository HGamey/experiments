#!/bin/sh
# 超分方案 vs 基线 (红魔, 原神, 不改游戏内设置): 画质素材 (截图+录屏) 与性能配对 A/B, 复用 selfsr_ab.py。
#   sh selfsr/baseline_cmp.sh <serial> [KEY=VALUE ...]
# 组 (SETS, 缺省 "hwc sr srip"), 每组都是 selfsr_ab 冷启动配对 (ABBA, 随 ROUNDS):
#   hwc   native vs hwc:0.56                        低分辨率 + 显示路径双线性 (基线)
#   sr    native vs sr:0.56:2141x969:0.8            低分辨率 + 我们的超分 (第 3 组配方, 同一低分输入)
#   srip  native vs srip:1:$SR_FULL:bm:120:$IP_TUNE 第 7 组推荐配置 (超分+插帧)
# KEY: ROUNDS (配对数, 缺省 1) WINDOW (测量窗口秒, 缺省 10) VIDEO (每臂窗口后录屏秒, 缺省 20; 0=不录)
#      SETS SR_FULL (缺省 <屏幕>:1.0) SR_OUT (缺省 2141x969:0.8) COOL_MAX (缺省 42) PKG (缺省原神)
#      HOLD_KEEP (1 = srip 臂窗口内开 43°C 穿越看门狗, 录屏拉长了暴露时间, 重设过的凹陷不作废; 建议配 COOL_MAX=40)
# 画质流程: 每臂 shots/<臂>.png (2688x1216) + traces/<臂>_video.mp4; 离线把 native 缩回游戏送显尺寸当真值,
# hwc 截图回缩 0.56 得低分输入, 双线性/SGSR/selfsr 在同一低分帧上离线放大后对比 (baseline_quality.py)。
set -eu
n=$#
for arg in "$@"; do
  case "$arg" in
    ROUNDS=*|WINDOW=*|VIDEO=*|SETS=*|SR_FULL=*|SR_OUT=*|IP_TUNE=*|COOL_MAX=*|PKG=*|HOLD_KEEP=*) export "$arg" ;;
    *) set -- "$@" "$arg" ;;
  esac
done
shift "$n"
SERIAL="${1:?用法: sh selfsr/baseline_cmp.sh <serial> [KEY=VALUE ...]}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${PF_ROOT:-$(cd "$HERE/../.." && pwd)}
LAYER="$HERE/gray"; [ -d "$LAYER" ] || LAYER="$ROOT/knobs/gray"
OUT="${PF_JOB_OUT:-$ROOT/_runs}/srbaseline_$(date +%Y%m%d-%H%M%S)"
PKG="${PKG:-com.miHoYo.Yuanshen}"
# 满分辨率超分目标 = 本机屏幕横屏尺寸 (换机不用改)
PANEL=$(adb -s "$SERIAL" shell wm size | tr -d '\r' | sed -n 's/.*: *\([0-9]*\)x\([0-9]*\)$/\1 \2/p' | tail -1 | awk '{print ($1>$2)?$1"x"$2:$2"x"$1}')
SR_FULL="${SR_FULL:-$PANEL:1.0}"
SR_OUT="${SR_OUT:-2141x969:0.8}"
# 第 7 组插帧参数 (带 waitframe 的红魔: 子图层起游戏图层名绕开 waitframe)
IP_TUNE="${IP_TUNE:-stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto}"
COOL_MAX="${COOL_MAX:-42}"
SETS="${SETS:-hwc sr srip}"
ROUNDS="${ROUNDS:-1}"
WINDOW="${WINDOW:-10}"
VIDEO="${VIDEO:-20}"
BIN="${PHONEFARM_BIN:-}"
if [ -z "$BIN" ]; then
  BIN="$ROOT/src/target/release/phonefarm"
  [ -x "$BIN" ] || (cd "$ROOT/src" && cargo build --release)
  codesign --force --sign - "$BIN" 2>/dev/null || true
fi
[ "$(adb -s "$SERIAL" shell su -c id 2>/dev/null | grep -c 'uid=0')" = 1 ] || { echo "这台手机没有 root, 层装不进游戏"; exit 2; }
bash "$LAYER/build/build_layer.sh" >/dev/null
mkdir -p "$OUT"
SO="$OUT/libVkLayer_refknobs.so"
cp "$LAYER/build/out/libVkLayer_refknobs.so" "$SO"
VOPT=""
[ "${VIDEO}" -gt 0 ] 2>/dev/null && VOPT="--video ${VIDEO}"
run() {  # run <组名> <A> <B>
  PHONEFARM_BIN="$BIN" python3 -I "$HERE/selfsr_ab.py" run --serial "$SERIAL" --pkg "$PKG" \
    --out "$OUT/$1" --a "$2" --b "$3" --rounds "$ROUNDS" --so "$SO" --phonefarm "$BIN" --fan --cool-max "$COOL_MAX" \
    --window "$WINDOW" $VOPT
}
for g in $SETS; do
  case $g in
    hwc)  run hwc_bilinear native "hwc:0.56" ;;
    sr)   run sr_lowres    native "sr:0.56:${SR_OUT}" ;;
    srip) run srip_g7      native "srip:1:${SR_FULL}:bm:120:${IP_TUNE}" ;;
    *) echo "不认识的组: $g (可选 hwc sr srip)"; exit 2 ;;
  esac
done
echo "输出: $OUT"
