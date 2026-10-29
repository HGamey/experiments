#!/bin/sh
# 第 7 组推荐配置 (srip 单挂) 30 分钟长稳测试 (研究 97f6adb9):
#   主线: 单阶段 hold=1800s 复用 hold120.sh 的机制 (挂层/进世界/设120Hz/HOLD_KEEP看门狗/每秒采样/收尾写回)。
#   附加: 后台每 60s 采一次 dumpsys gfxinfo (游戏进程累计帧数/janky/p50/p90), 差分得每分钟掉帧趋势。
#   最后: 标准库 python 汇总 → soak_summary.json (每分钟帧率/温度/掉帧聚合)。
#   sh selfsr/soak30.sh [serial]        # 只依赖实验仓库自带机制; 30 分钟 + 前后处理约 40-50 分钟
set -eu
S="${1:-91253241019A}"
PKG="com.miHoYo.Yuanshen"
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="${PF_JOB_OUT:-.}"
mkdir -p "$OUT"

SRIP="srip:1:2688x1216:1.0:bm:120:stamp=0,panel=120,backend=sc,lead_ns=25000000,unlimit=1,sc_name=auto"

# 1) 后台 gfxinfo 采样器 (累计值, 差分出每分钟)
: > "$OUT/gfx_series.tsv"
( i=0
  while :; do
    i=$((i+1))
    adb -s "$S" shell dumpsys gfxinfo "$PKG" 2>/dev/null | tr -d '\r' | awk -v t="$(date +%s)" '
      /^Total frames rendered:/ {tot=$4}
      /^Janky frames:/ {jnk=$3; gsub(/[()%]/,"",$0)}
      /^50th percentile:/ {p50=$3}
      /^90th percentile:/ {p90=$3}
      END {printf "%s\t%s\t%s\t%s\t%s\n", t, tot+0, jnk+0, p50+0, p90+0}' >> "$OUT/gfx_series.tsv"
    sleep 60
  done
) &
GFXPID=$!
echo "gfxinfo 采样器 pid=$GFXPID, 每 60s 一次 → $OUT/gfx_series.tsv"

# 2) 30 分钟 hold 阶段 (HOLD_KEEP 看门狗随窗口生效; 风扇/收尾/写回都由 hold120.sh 管)
status=0
PHASES="$SRIP|hold=1800" HOLD_KEEP=1 SETTLE=15 sh "$HERE/hold120.sh" "$S" "$OUT" "$PKG" || status=$?

# 3) 收掉采样器, 汇总
kill "$GFXPID" 2>/dev/null || true
wait "$GFXPID" 2>/dev/null || true
python3 - "$OUT" <<'PY'
import json, sys, glob, os, re
out = sys.argv[1]
res = {"gfx_minutes": [], "hold_tsv": None}
# 每分钟 gfx 差分
rows = [l.split('\t') for l in open(f"{out}/gfx_series.tsv") if l.strip()]
prev = None
for r in rows:
    t, tot, jnk, p50, p90 = int(r[0]), int(r[1]), int(r[2]), float(r[3]), float(r[4])
    if prev and tot >= prev[1]:
        res["gfx_minutes"].append({"t": t, "frames": tot - prev[1], "janky": jnk - prev[2],
                                   "janky_pct": round((jnk - prev[2]) * 100.0 / max(1, tot - prev[1]), 2),
                                   "p50_ms": p50, "p90_ms": p90})
    prev = (t, tot, jnk, p50, p90)
# hold 每秒采样聚合 (每 60s 一桶)
for f in sorted(glob.glob(f"{out}/traces/P*_hold.tsv")):
    res["hold_tsv"] = os.path.basename(f)
    buckets, cur = [], None
    dips, reasserts, temps, hzs = 0, 0, [], []
    for line in open(f):
        p = line.rstrip('\n').split('\t')
        if p[0] == 'wall': continue
        t = int(p[1]); hz = float(p[5]) if p[5] else 0.0
        m = re.search(r'temperature: (\d+)', p[7]); tc = int(m.group(1))/10 if m else None
        if tc is not None: temps.append(tc)
        if hz: hzs.append(hz)
        if hz and hz < 100: dips += 1
        if len(p) > 9 and p[9] and p[9] != '0': reasserts += 1
        b = t // 60
        if cur is None or cur['minute'] != b:
            cur = {'minute': b, 'hz_sum': 0, 'hz_n': 0, 'hz_min': 999, 't_sum': 0, 't_n': 0}
            buckets.append(cur)
        if hz:
            cur['hz_sum'] += hz; cur['hz_n'] += 1; cur['hz_min'] = min(cur['hz_min'], hz)
        if tc is not None:
            cur['t_sum'] += tc; cur['t_n'] += 1
    for c in buckets:
        c['hz_mean'] = round(c['hz_sum']/c['hz_n'], 2) if c['hz_n'] else None
        c['hz_min'] = None if c['hz_min'] == 999 else c['hz_min']
        c['temp_mean'] = round(c['t_sum']/c['t_n'], 1) if c['t_n'] else None
        del c['hz_sum'], c['hz_n'], c['t_sum'], c['t_n']
    res['minutes'] = buckets
    res['total_s'] = sum(len([1]) for _ in open(f)) - 1
    res['dip_seconds'] = dips          # 面板 <100Hz 的秒数 (凹陷)
    res['reassert_events'] = reasserts # 看门狗重设次数
    res['temp_min'] = min(temps) if temps else None
    res['temp_max'] = max(temps) if temps else None
    res['hz_min'] = min(hzs) if hzs else None
    res['hz_mean'] = round(sum(hzs)/len(hzs), 2) if hzs else None
json.dump(res, open(f"{out}/soak_summary.json", "w"), ensure_ascii=False, indent=1)
print("soak_summary.json:", json.dumps({k: v for k, v in res.items() if k not in ('minutes','gfx_minutes')}, ensure_ascii=False))
PY
echo "长稳测试结束 (hold120 退出码 $status; 采样文件在 $OUT)"
exit "$status"
