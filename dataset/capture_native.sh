#!/bin/sh
# 方法 C 原料采集 (DATASET-COLLECTION §3.3): 原神大世界巡航截原生帧, 附整批设备/游戏元数据。
#   sh dataset/capture_native.sh <serial> [KEY=VALUE ...]
# 平台上: pf-node --experiment <提交> --research <研究ID> --device-access write --resource device:<serial> --detach \
#           experiment dataset/capture_native.sh <serial> FRAMES=300
#   (写了 --device-access write 必须同时给 --resource device:<serial>, 否则平台按全局预留, 占住所有手机)
# KEY: CAPTURER (缺省 cruise: phonefarm onboard 冷启动进大世界, 再由 cruise.py 触屏转视角+走位+screencap;
#      phonefarm = phonefarm capture 自带巡航, 靠虚拟 Xbox 手柄 (adb shell hid) 输入; worker 崩溃已在平台 83d1a44 起修复,
#      但在当前 root 测试机上内核不给该 UHID 设备绑定输入驱动, 手柄注册不上, 采集停在 hid_register, 所以缺省仍用 cruise)
#      FRAMES (目标保留帧, 缺省 300) SETTLE_MS (每步后等待, 缺省 800) WALK_MS (cruise 每步推摇杆毫秒, 缺省 4000)
#      SEED (cruise 随机种子, 缺省 0) MODE (phonefarm 用, 缺省 navigate) TIMEOUT_S (缺省 5400) MAX_STEPS (phonefarm 用)
# 产物 ($PF_JOB_OUT/dataset_capture_<时间>/):
#   frames/{frame_*.png, manifest.jsonl} (cruise) 或 capture-<token>/frames/ (phonefarm)  截图字节不重编码
#   capture.json capture.log onboard/   巡航报告与进度, 进门记录
#   meta_before.txt meta_after.txt  包版本/屏幕/系统/GPU/root/Game Mode/画质档位 的前后快照
#   sf_<秒>.txt                     采集中途的 SurfaceFlinger 快照, 取游戏图层 Source Crop 当实际渲染尺寸
#   batch.json                      §3.2/§3.10 整批元数据; restore.json 收尾核对 (ok=true 才可释放手机)
# 不改游戏设置与 Game Mode。cruise: onboard 会把媒体音量置 0, 收尾按开跑前原值写回; 结束强停游戏, 开跑前熄屏则熄屏。
# 收尾前后对比 Game Mode 配置、画质档位、stay-on、媒体音量、屏幕状态, 并确认游戏已停。
set -u
SERIAL=""
for arg in "$@"; do case "$arg" in *=*) ;; *) [ -n "$SERIAL" ] || SERIAL="$arg" ;; esac; done
[ -n "$SERIAL" ] || { echo "用法: sh dataset/capture_native.sh <serial> [KEY=VALUE ...]"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${PF_ROOT:-$(cd "$HERE/../.." && pwd)}
PF="${PHONEFARM_BIN:-$ROOT/src/target/release/phonefarm}"
# 整个脚本 (前后快照 + capture + 收尾核对) 在同一把设备锁下; capture 继承这把锁原地执行
if [ -z "${PF_LOCK_FDS:-}" ]; then
  exec "$PF" lease --serial "$SERIAL" --label "${DEVLOCK_LABEL:+$DEVLOCK_LABEL-}dataset-capture@$SERIAL" -- \
    sh "$HERE/$(basename "$0")" "$@"
fi
for arg in "$@"; do
  case "$arg" in
    CAPTURER=*|FRAMES=*|SETTLE_MS=*|WALK_MS=*|SEED=*|MODE=*|TIMEOUT_S=*|MAX_STEPS=*) export "$arg" ;;
    *=*) echo "不认识的参数: $arg"; exit 2 ;;
  esac
done
CAPTURER="${CAPTURER:-cruise}"; FRAMES="${FRAMES:-300}"; SETTLE_MS="${SETTLE_MS:-800}"; MODE="${MODE:-navigate}"
TIMEOUT_S="${TIMEOUT_S:-5400}"; MAX_STEPS="${MAX_STEPS:-0}"
PKG=com.miHoYo.Yuanshen
PREFS=/data/data/$PKG/shared_prefs/$PKG.v2.playerprefs.xml
OUT="${PF_JOB_OUT:-$ROOT/_runs}/dataset_capture_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
a() { adb -s "$SERIAL" shell "$@" 2>&1 | tr -d '\r'; }
# 画质档位: playerprefs 里 GENERAL_DATA 的 (entryType, index) 列表, 只读, 不落原文件
prefs() {
  a "su -c 'cat $PREFS'" | python3 -I -c '
import re, sys, urllib.parse
m = re.search(r"name=\"GENERAL_DATA\">([^<]*)<", sys.stdin.read())
t = urllib.parse.unquote(m.group(1)) if m else ""
print(",".join(a + "=" + b for a, b in re.findall(r"entryType\\\":(\d+),\\\"index\\\":(\d+)", t)) or "null")'
}
meta() {  # meta <文件>
  {
    echo "version_name=$(a dumpsys package $PKG | sed -n 's/^ *versionName=//p' | head -1)"
    echo "version_code=$(a dumpsys package $PKG | sed -n 's/^ *versionCode=\([0-9]*\).*/\1/p' | head -1)"
    echo "screen=$(a wm size | sed -n 's/^Physical size: *//p' | tail -1)"
    echo "override_size=$(a wm size | sed -n 's/^Override size: *//p' | tail -1)"
    echo "density=$(a wm density | sed -n 's/^Physical density: *//p' | tail -1)"
    echo "fingerprint=$(a getprop ro.build.fingerprint)"
    echo "android=$(a getprop ro.build.version.release)"
    echo "platform=$(a getprop ro.board.platform)"
    echo "gles=$(a dumpsys SurfaceFlinger | grep -m1 '^GLES:' | sed 's/^GLES: *//')"
    echo "root=$(a su -c id | grep -c 'uid=0')"
    echo "stayon=$(a settings get global stay_on_while_plugged_in)"
    echo "game_running=$(a pidof $PKG | grep -c .)"
    echo "graphics_prefs=$(prefs)"
    echo "game_mode_configs=$(a cmd game list-configs $PKG | tr '\n' ' ')"
    echo "music_volume=$(a cmd media_session volume --stream 3 --get | sed -n 's/.*volume is \([0-9]*\).*/\1/p' | head -1)"
    echo "wakefulness=$(a dumpsys power | sed -n 's/.*mWakefulness=//p' | head -1)"
  } > "$1"
}
meta "$OUT/meta_before.txt"
# 渲染尺寸: 采集中途几次抓 SurfaceFlinger (只读), 收尾时从游戏图层 Source Crop 解析
#   (5 s 一查停止标记, 采集结束后探测子进程很快退出, 不留后台 sleep)
( s=0; for t in 90 240 480 900; do
    while [ "$s" -lt "$t" ]; do [ -e "$OUT/.probe_stop" ] && exit 0; sleep 5; s=$((s + 5)); done
    a dumpsys SurfaceFlinger > "$OUT/sf_$t.txt"
  done ) &
PROBE=$!
if [ "$CAPTURER" = phonefarm ]; then
  set -- --serial "$SERIAL" --out "$OUT" --frames "$FRAMES" --settle-ms "$SETTLE_MS" --mode "$MODE" --timeout-s "$TIMEOUT_S" --json
  [ "$MAX_STEPS" -gt 0 ] && set -- "$@" --max-steps "$MAX_STEPS"
  "$PF" capture "$@" > "$OUT/capture.json" 2> "$OUT/capture.log"
  CODE=$?
else
  VOL0=$(sed -n 's/^music_volume=//p' "$OUT/meta_before.txt")
  WAKE0=$(sed -n 's/^wakefulness=//p' "$OUT/meta_before.txt")
  a input keyevent KEYCODE_WAKEUP >/dev/null
  # 冷启动进大世界 (与 selfsr_ab 相同的进门), 退出码 0 = 在游戏里
  "$PF" onboard --package "$PKG" --serial "$SERIAL" --enter-only --no-guest --out "$OUT/onboard" > "$OUT/onboard.log" 2>&1
  CODE=$?
  if [ "$CODE" = 0 ]; then
    sleep "${ENTER_WAIT:-10}"   # 进世界后等加载动画和公告淡出
    python3 -I "$HERE/cruise.py" "$SERIAL" "$OUT" --frames "$FRAMES" --settle-ms "$SETTLE_MS" --walk-ms "${WALK_MS:-4000}" \
      --timeout-s "$TIMEOUT_S" --seed "${SEED:-0}" > "$OUT/capture.json" 2> "$OUT/capture.log"
    CODE=$?
  else
    echo "{\"ok\": false, \"error\": \"onboard exit $CODE\"}" > "$OUT/capture.json"
  fi
  a am force-stop "$PKG" >/dev/null
  [ -n "$VOL0" ] && a cmd audio set-volume 3 "$VOL0" >/dev/null
  [ "$WAKE0" = Awake ] || a input keyevent KEYCODE_SLEEP >/dev/null
  sleep 2
fi
touch "$OUT/.probe_stop"
wait "$PROBE" 2>/dev/null
rm -f "$OUT/.probe_stop"
meta "$OUT/meta_after.txt"
python3 -I - "$OUT" "$CODE" "$PKG" <<'PY'
import glob, json, os, re, sys
out, code, pkg = sys.argv[1], int(sys.argv[2]), sys.argv[3]
def kv(p):
    d = {}
    for line in open(p, encoding="utf-8", errors="replace"):
        k, _, v = line.rstrip("\n").partition("=")
        d[k] = v.strip() or None
    return d
before, after = kv(os.path.join(out, "meta_before.txt")), kv(os.path.join(out, "meta_after.txt"))
try:
    cap = json.load(open(os.path.join(out, "capture.json")))
except (OSError, ValueError):
    cap = {}
# 游戏图层 (SurfaceView[<包名>...]) 在 HWC 表里的 Source Crop (LTRB 浮点) → 实际渲染宽高
render = []
for p in sorted(glob.glob(os.path.join(out, "sf_*.txt"))):
    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    for i, line in enumerate(lines):
        if "SurfaceView[" + pkg in line:
            for nxt in lines[i:i + 4]:
                m = re.search(r"\|\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s*\|", nxt)
                if m:
                    l, t, r, b = map(float, m.groups())
                    render.append({"probe": os.path.basename(p), "w": round(r - l), "h": round(b - t)})
                    break
    # 新版 dumpsys 没有 HWC Source Crop 表时, 退回游戏 SurfaceView 的 BLAST 缓冲宽高 (GraphicBufferAllocator 列表)
    if not any(r["probe"] == os.path.basename(p) for r in render):
        for m in re.finditer(r"SurfaceView\[" + re.escape(pkg) + r"[^\n]*BLAST Consumer[^\n]*?w/h:(\d+)x(\d+)", "\n".join(lines)):
            render.append({"probe": os.path.basename(p), "w": int(m.group(1)), "h": int(m.group(2)), "from": "blast_buffer"})
            break
sizes = sorted({(r["w"], r["h"]) for r in render})
same = {k: before.get(k) == after.get(k) for k in ("graphics_prefs", "game_mode_configs", "stayon", "version_code",
                                                     "music_volume")}
same["screen_awake"] = (before.get("wakefulness") == "Awake") == (after.get("wakefulness") == "Awake")
# phonefarm capture 自带收尾记录; cruise 只发触屏输入, 收尾即上面这些前后快照
rec = cap.get("recovery") or ({"status": "not_applicable"} if cap.get("capturer") == "cruise" or "onboard" in str(cap.get("error")) else {})
restore = {"ok": all(same.values()) and after.get("game_running") == "0"
           and rec.get("status") in ("reported_complete", "verified", "not_applicable"),
           "unchanged": same, "game_stopped_after": after.get("game_running") == "0",
           "capture_recovery": rec, "capture_exit": code,
           "note": "脚本不改设置; 前后快照逐项一致且游戏已停才 ok"}
batch = {"schema": "hgamey/dataset-capture-batch@1", "method": "C", "capture_method": "screencap",
         "capturer": cap.get("capturer") or "phonefarm",
         "node_id": os.environ.get("PF_NODE_ID"), "job_id": os.environ.get("PF_JOB_ID"),
         "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"), "research": os.environ.get("PF_JOB_RESEARCH"),
         "package": pkg, "version_name": before.get("version_name"), "version_code": before.get("version_code"),
         "screen": before.get("screen"), "override_size": before.get("override_size"), "density": before.get("density"),
         "render_size": "%dx%d" % sizes[0] if len(sizes) == 1 else None, "render_probes": render,
         "fingerprint": before.get("fingerprint"), "android": before.get("android"), "platform": before.get("platform"),
         "gles": before.get("gles"), "root": before.get("root") == "1",
         "graphics_prefs": before.get("graphics_prefs"), "game_mode_configs": before.get("game_mode_configs"),
         "settings_changed_by_script": False, "capture": {k: cap.get(k) for k in (
             "ok", "frames", "target", "steps", "skipped_by_state", "duplicates", "portrait", "grab_failed",
             "back_presses", "mode", "segment_frames", "dup_thresh", "walk_ms", "settle_ms", "seed", "wall_s", "stop_reason",
             "session", "manifest", "error")}}
json.dump(batch, open(os.path.join(out, "batch.json"), "w"), ensure_ascii=False, indent=1)
json.dump(restore, open(os.path.join(out, "restore.json"), "w"), ensure_ascii=False, indent=1)
print(json.dumps({"out": out, "frames": cap.get("frames"), "render_size": batch["render_size"], "restore_ok": restore["ok"]}, ensure_ascii=False))
sys.exit(0 if restore["ok"] else 2)
PY
RC=$?
[ "$RC" = 0 ] || { echo "收尾核对未通过, 见 $OUT/restore.json"; exit 2; }
echo "输出: $OUT"
exit "$CODE"
