#!/bin/sh
# srip 单挂 30 分钟长会话采样 (长稳 97f6adb9 / 凹陷缓解 e736a7c5)。
# 面板/控制器/电池/前台每秒采样由 hold120.sh 完成；本脚本附加 gfxinfo、通知栏收起、停充与 logcat。
# 注意：gfxinfo 对原神的 Vulkan 原生渲染不可见，focus 也受系统窗口干扰，均不作游戏存活的强判据。
# PHASES/KEEPCHG 可外部覆盖；KEEPCHG=1 保持充电，用于尽早制造 43°C 穿越。
#   sh selfsr/soak30.sh [serial] [PHASES=...] [KEEPCHG=1]
set -eu
S="${1:-91253241019A}"
PKG="com.miHoYo.Yuanshen"
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"
# KEY=VALUE 参数按环境变量用 (平台任务里设不了环境变量; 对照跑: PHASES='native|hold=1800|tap=300|cool=40')
n=$#
for arg in "$@"; do
  case "$arg" in PHASES=*|KEEPCHG=*) export "$arg" ;; esac
done
shift "$n"

SRIP="srip:1:2688x1216:1.0:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto"
PHASES="${PHASES:-$SRIP|hold=1800|tap=300|cool=40}"
CHG=/sys/class/qcom-battery/charging_enabled

# 1) 窗口前: 收起通知栏 (v1/v2 游戏 ~1fps 疑似真凶), 确认充电开关状态
adb -s "$S" shell cmd statusbar collapse >/dev/null 2>&1 || true
CHG0=$(adb -s "$S" shell "su -c 'cat $CHG'" 2>/dev/null | tr -d '\r')
echo "充电开关原值=${CHG0:-读不到}; 通知栏已收起"

# 2) 后台采样器: 每 60s gfxinfo + 前台 + 温度 + 充电; 通知栏若被拉开立即收起 (自愈)
#    另每 30s 读 GPU 频率/governor (gpu_series.tsv) — 判定 srip 高温 74fps 档是 kgsl 热降频还是 SF 侧丢帧
: > "$OUT/gfx_series.tsv"
: > "$OUT/gpu_series.tsv"
( while :; do
    adb -s "$S" shell dumpsys gfxinfo "$PKG" 2>/dev/null | tr -d '\r' | awk -v t="$(date +%s)" '
      /^Total frames rendered:/ {tot=$4}
      /^Janky frames:/ {jnk=$3}
      /^50th percentile:/ {p50=$3}
      /^90th percentile:/ {p90=$3}
      END {printf "%s\t%s\t%s\t%s\t%s", t, tot+0, jnk+0, p50+0, p90+0}' >> "$OUT/gfx_series.tsv"
    foc=$(adb -s "$S" shell dumpsys window 2>/dev/null | grep -m1 'mCurrentFocus' | tr -d '\r')
    case "$foc" in *NotificationShade*|*StatusBar*)
      adb -s "$S" shell cmd statusbar collapse >/dev/null 2>&1 || true
      foc="$foc [collapsed]";; esac
    tc=$(adb -s "$S" shell dumpsys battery 2>/dev/null | grep -m1 'temperature:' | tr -dc '0-9')
    chg=$(adb -s "$S" shell "su -c 'cat $CHG'" 2>/dev/null | tr -d '\r')
    printf '\t%s\t%s.%s\t%s\n' "${foc#* }" "${tc%?}" "${tc#"${tc%?}"}" "${chg:-?}" >> "$OUT/gfx_series.tsv"
    sleep 30
    # GPU 频率 (kgsl 多候选节点全读, 存在的有值); 温度电池列对齐用
    gts=$(date +%s)
    gbat=$(adb -s "$S" shell dumpsys battery 2>/dev/null | grep -m1 'temperature:' | tr -dc '0-9')
    gpu=$(adb -s "$S" shell "su -c 'echo ==gpuclk; cat /sys/class/kgsl/kgsl-3d0/gpuclk; echo ==cur_freq; cat /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq; echo ==governor; cat /sys/class/kgsl/kgsl-3d0/devfreq/governor; echo ==minmax; cat /sys/class/kgsl/kgsl-3d0/min_pwrlevel /sys/class/kgsl/kgsl-3d0/max_pwrlevel'" 2>/dev/null | tr -d '\r' | tr '\n' ';')
    printf '%s\t%s.%s\t%s\n' "$gts" "${gbat%?}" "${gbat#"${gbat%?}"}" "$gpu" >> "$OUT/gpu_series.tsv"
    sleep 30
  done
) &
GFXPID=$!
# 3) 后台 logcat 留证 (warning 及以上)
adb -s "$S" logcat -c 2>/dev/null || true
adb -s "$S" logcat -v time '*:W' > "$OUT/soak_logcat.txt" 2>/dev/null &
LOGPID=$!

cleanup() {
  pkill -P "$GFXPID" 2>/dev/null || true
  kill "$GFXPID" 2>/dev/null || true
  pkill -f "dumpsys gfxinfo $PKG" 2>/dev/null || true
  kill "$LOGPID" 2>/dev/null || true
  pkill -P "$LOGPID" 2>/dev/null || true
  pkill -f "logcat -v time" 2>/dev/null || true
  adb -s "$S" shell "su -c 'echo 1 > $CHG'" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# 4) 窗口内停充 (真实长期使用 = 不插电玩), 回读核对; KEEPCHG=1 跳过 (缓解实验要充电尽早制造穿 43°C)
if [ "${KEEPCHG:-0}" != "1" ]; then
  adb -s "$S" shell "su -c 'echo 0 > $CHG'" >/dev/null 2>&1 || true
  CHGN=$(adb -s "$S" shell "su -c 'cat $CHG'" 2>/dev/null | tr -d '\r')
  [ "$CHGN" = "0" ] && echo "停充成功 (charging_enabled=0)" || echo "!! 停充失败 (读到 ${CHGN:-?}), 继续但记录"
else
  echo "KEEPCHG=1: 保持充电 (用于尽早制造 43°C 穿越的缓解实验)"
fi

# 5) 30 分钟 hold 阶段 (PHASES 可外部覆盖; HOLD_KEEP 看门狗; 风扇/收尾/写回由 hold120.sh 管)
#    注意: PHASES 必须以命令前缀形式传给子进程 (普通赋值不 export, hold120 会回退缺省四相位 —— v3 教训)
status=0
PHASES="$PHASES" HOLD_KEEP=1 HOLD_REQUIRE_FG=1 SETTLE=15 sh "$HERE/hold120.sh" "$S" "$OUT" "$PKG" || status=$?

# 6) 恢复充电, 收采样器 (pkill 子进程防 F14 复发), 汇总 (标准库)
adb -s "$S" shell "su -c 'echo 1 > $CHG'" >/dev/null 2>&1 || true
pkill -P "$GFXPID" 2>/dev/null || true
kill "$GFXPID" 2>/dev/null || true
pkill -f "dumpsys gfxinfo $PKG" 2>/dev/null || true
kill "$LOGPID" 2>/dev/null || true
pkill -P "$LOGPID" 2>/dev/null || true
pkill -f "logcat -v time" 2>/dev/null || true
wait 2>/dev/null || true
python3 - "$OUT" "$PKG" <<'PY'
import json, sys, glob, os, re
out, pkg = sys.argv[1], sys.argv[2]
res = {"gfx_minutes": [], "shade_collapses": 0, "valid": False, "invalid_reasons": []}
rows = [l.rstrip('\n').split('\t') for l in open(f"{out}/gfx_series.tsv") if l.strip()]
prev = None; frozen_streak = 0; max_frozen = 0
for r in rows:
    t, tot, jnk, p50, p90 = int(r[0]), int(r[1]), int(r[2]), float(r[3]), float(r[4])
    foc = r[5] if len(r) > 5 else ''; tc = r[6] if len(r) > 6 else ''; chg = r[7] if len(r) > 7 else ''
    if '[collapsed]' in foc: res["shade_collapses"] += 1
    alive = pkg in foc
    if prev is not None:
        grew = tot > prev[1]
        frozen_streak = 0 if grew else frozen_streak + 1
        max_frozen = max(max_frozen, frozen_streak)
        res["gfx_minutes"].append({"t": t, "frames": tot - prev[1] if tot >= prev[1] else None,
                                   "janky": jnk - prev[2] if jnk >= prev[2] else None,
                                   "p50_ms": p50, "p90_ms": p90, "game_fg": alive, "temp_c": tc, "chg": chg})
    prev = (t, tot, jnk, p50, p90)
for f in sorted(glob.glob(f"{out}/traces/P*_hold.tsv")):
    res["hold_tsv"] = os.path.basename(f)
    buckets, cur = [], None
    dips = reasserts = fg_game_samples = focus_samples = sample_rows = coverage_s = 0
    temps, hzs = [], []
    for line in open(f):
        p = line.rstrip('\n').split('\t')
        if p[0] == 'wall': continue
        sample_rows += 1
        t = int(p[1]); coverage_s = max(coverage_s, t + 1)
        hz = float(p[5]) if p[5] else 0.0
        m = re.search(r'temperature: (\d+)', p[7]); tc2 = int(m.group(1))/10 if m else None
        if len(p) > 8 and p[8]:
            focus_samples += 1
            if pkg in p[8]: fg_game_samples += 1
        if tc2 is not None: temps.append(tc2)
        if hz:
            hzs.append(hz)
            if hz < 100: dips += 1
        if len(p) > 10 and p[10] and p[10] != '0': reasserts += 1
        b = t // 60
        if cur is None or cur['minute'] != b:
            cur = {'minute': b, 'hz_sum': 0.0, 'hz_n': 0, 'hz_min': 999.0, 't_sum': 0.0, 't_n': 0}
            buckets.append(cur)
        if hz: cur['hz_sum'] += hz; cur['hz_n'] += 1; cur['hz_min'] = min(cur['hz_min'], hz)
        if tc2 is not None: cur['t_sum'] += tc2; cur['t_n'] += 1
    for c in buckets:
        c['hz_mean'] = round(c['hz_sum']/c['hz_n'], 2) if c['hz_n'] else None
        c['hz_min'] = None if c['hz_min'] == 999 else c['hz_min']
        c['temp_mean'] = round(c['t_sum']/c['t_n'], 1) if c['t_n'] else None
        for k in ('hz_sum','hz_n','t_sum','t_n'): del c[k]
    res.update({'total_s': coverage_s, 'sample_rows': sample_rows,
                'fg_game_samples': fg_game_samples, 'focus_samples': focus_samples,
                'dip_seconds': dips, 'reassert_events': reasserts,
                'temp_min': min(temps) if temps else None, 'temp_max': max(temps) if temps else None,
                'hz_min': min(hzs) if hzs else None, 'hz_mean': round(sum(hzs)/len(hzs), 2) if hzs else None,
                'minutes': buckets})
    reasons = []
    if coverage_s < 1700: reasons.append(f"墙钟覆盖不足({coverage_s}s, {sample_rows}行)")
    # gfxinfo 对 Vulkan 渲染不可见、focus 是 5 秒稀疏采样，均不能用来否定整轮。
    # 有效性只看采样覆盖和 arms 的会话/层校验；任何臂错误都保留并判无效。
    arms_path = os.path.join(out, "arms.jsonl")
    if not os.path.exists(arms_path):
        reasons.append("缺少 arms.jsonl")
    else:
        for line in open(arms_path):
            rec = json.loads(line)
            if rec.get("layer_loaded") is False:
                reasons.append(f"{rec.get('arm')}: layer_loaded=false")
            if rec.get("error"):
                reasons.append(f"{rec.get('arm')}: {rec['error']}")
    res["invalid_reasons"] = reasons
    res["valid"] = not reasons
json.dump(res, open(f"{out}/soak_summary.json", "w"), ensure_ascii=False, indent=1)
print("soak_summary:", json.dumps({k: v for k, v in res.items() if k not in ('minutes', 'gfx_minutes')}, ensure_ascii=False))
PY
echo "长稳 v3 结束 (phase=$PHASES, hold120 退出码 $status)"
exit "$status"
