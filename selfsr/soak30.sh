#!/bin/sh
# 第 7 组推荐配置 (srip 单挂) 30 分钟长稳测试 v2 (研究 97f6adb9)。
# v1 教训: ①起始 40°C 未降温+边充电边跑, t=70s 即穿 43°C, 峰值 50°C; ②无输入静态 30 分钟, 游戏第 1 分钟后
#   冻结 (gfx 累计 65 帧后 20 分钟不动), 采了 20 分钟死游戏; ③层统计在游戏死后查询为空。
# v2: ①开跑前等电池 ≤40°C (cool=40); ②窗口内停充 (charging_enabled=0, 模拟真实 unplugged 游玩, 收尾/中断恢复);
#   ③每 300s 轻点屏幕一次 (tap=300, 排除无输入冻结路径); ④汇总检测游戏冻结/早退, 违规则整场判无效。
#   sh selfsr/soak30.sh [serial]        # 约 45-60 分钟 (含降温等待)
set -eu
S="${1:-91253241019A}"
PKG="com.miHoYo.Yuanshen"
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"

SRIP="srip:1:2688x1216:1.0:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto"
CHG=/sys/class/qcom-battery/charging_enabled

# 1) 后台采样器: 每 60s gfxinfo (游戏进程帧/janky/p50/p90) + 前台 + 电池温度
: > "$OUT/gfx_series.tsv"
( while :; do
    adb -s "$S" shell dumpsys gfxinfo "$PKG" 2>/dev/null | tr -d '\r' | awk -v t="$(date +%s)" '
      /^Total frames rendered:/ {tot=$4}
      /^Janky frames:/ {jnk=$3}
      /^50th percentile:/ {p50=$3}
      /^90th percentile:/ {p90=$3}
      END {printf "%s\t%s\t%s\t%s\t%s", t, tot+0, jnk+0, p50+0, p90+0}' >> "$OUT/gfx_series.tsv"
    foc=$(adb -s "$S" shell dumpsys window 2>/dev/null | grep -m1 'mCurrentFocus' | tr -d '\r')
    tc=$(adb -s "$S" shell dumpsys battery 2>/dev/null | grep -m1 'temperature:' | tr -dc '0-9')
    printf '\t%s\t%s\t%s\n' "${foc#* }" "${tc%?}.${tc#"${tc%?}"}" "$(adb -s "$S" shell cat $CHG 2>/dev/null | tr -d '\r')" >> "$OUT/gfx_series.tsv"
    sleep 60
  done
) &
GFXPID=$!
cleanup() { kill "$GFXPID" 2>/dev/null || true; adb -s "$S" shell su -c "echo 1 > $CHG" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

# 2) 窗口内停充 (真实长期使用 = 不插电玩), 记录原值
CHG0=$(adb -s "$S" shell su -c "cat $CHG" | tr -d '\r')
echo "充电原值=$CHG0, 窗口内置 0"
adb -s "$S" shell su -c "echo 0 > $CHG" >/dev/null 2>&1 || echo "!! 停充失败, 继续但记录"

# 3) 30 分钟 hold 阶段: cool=40 (开跑前等电池 ≤40°C), tap=300 (每 5 分钟轻点), HOLD_KEEP 看门狗
status=0
PHASES="$SRIP|hold=1800|tap=300|cool=40" HOLD_KEEP=1 SETTLE=15 sh "$HERE/hold120.sh" "$S" "$OUT" "$PKG" || status=$?

# 4) 恢复充电, 收采样器, 汇总 (标准库)
adb -s "$S" shell su -c "echo 1 > $CHG" >/dev/null 2>&1 || true
kill "$GFXPID" 2>/dev/null || true
wait "$GFXPID" 2>/dev/null || true
python3 - "$OUT" "$PKG" <<'PY'
import json, sys, glob, os, re
out, pkg = sys.argv[1], sys.argv[2]
res = {"gfx_minutes": [], "valid": False, "invalid_reasons": []}
rows = [l.rstrip('\n').split('\t') for l in open(f"{out}/gfx_series.tsv") if l.strip()]
prev = None; frozen_streak = 0; max_frozen = 0
for r in rows:
    t, tot, jnk, p50, p90 = int(r[0]), int(r[1]), int(r[2]), float(r[3]), float(r[4])
    foc = r[5] if len(r) > 5 else ''; tc = r[6] if len(r) > 6 else ''
    alive = pkg in foc
    if prev is not None:
        grew = tot > prev[1]
        frozen_streak = 0 if grew else frozen_streak + 1
        max_frozen = max(max_frozen, frozen_streak)
        res["gfx_minutes"].append({"t": t, "frames": tot - prev[1] if tot >= prev[1] else None,
                                   "janky": jnk - prev[2] if jnk >= prev[2] else None,
                                   "p50_ms": p50, "p90_ms": p90, "game_fg": alive, "temp_c": tc})
    prev = (t, tot, jnk, p50, p90)
# hold 每秒聚合 (列: 0 wall,1 t,2 ctrl,3 mode,4 mperiod,5 active_hz,6 range,7 bat,8 focus,9 tap,10 reassert,11 ok)
for f in sorted(glob.glob(f"{out}/traces/P*_hold.tsv")):
    res["hold_tsv"] = os.path.basename(f)
    buckets, cur = [], None
    dips = reasserts = fg_game_s = total_s = 0
    temps, hzs = [], []
    for line in open(f):
        p = line.rstrip('\n').split('\t')
        if p[0] == 'wall': continue
        total_s += 1
        t = int(p[1]); hz = float(p[5]) if p[5] else 0.0
        m = re.search(r'temperature: (\d+)', p[7]); tc2 = int(m.group(1))/10 if m else None
        if pkg in (p[8] if len(p) > 8 else ''): fg_game_s += 1
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
    res.update({'total_s': total_s, 'fg_game_s': fg_game_s, 'dip_seconds': dips, 'reassert_events': reasserts,
                'temp_min': min(temps) if temps else None, 'temp_max': max(temps) if temps else None,
                'hz_min': min(hzs) if hzs else None, 'hz_mean': round(sum(hzs)/len(hzs), 2) if hzs else None,
                'minutes': buckets})
    # 有效性判定: 游戏前台 ≥90% 且 gfx 无连续 ≥3 分钟冻结 且 总采样 ≥1700s
    reasons = []
    if total_s < 1700: reasons.append(f"采样不足({total_s}s)")
    if fg_game_s / max(1, total_s) < 0.9: reasons.append(f"游戏前台仅 {fg_game_s}/{total_s}s")
    if max_frozen >= 3: reasons.append(f"gfx 冻结连续 {max_frozen} 分钟")
    res["invalid_reasons"] = reasons
    res["valid"] = not reasons
json.dump(res, open(f"{out}/soak_summary.json", "w"), ensure_ascii=False, indent=1)
print("soak_summary:", json.dumps({k: v for k, v in res.items() if k not in ('minutes', 'gfx_minutes')}, ensure_ascii=False))
PY
echo "长稳 v2 结束 (hold120 退出码 $status)"
exit "$status"
