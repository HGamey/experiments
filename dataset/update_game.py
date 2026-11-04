#!/usr/bin/env python3
"""update_game.py — 覆盖更新手机上的游戏客户端: 打开游戏, 每隔几秒截图 OCR, 只按 --rule 点, 认不出就停下留图。不卸载、不清数据、不挂层。

    python3 -I dataset/update_game.py <serial> [--pkg 包名] [--rule 上下文=按钮 ...] [--max-seconds 5400] [--stall-s 300]
平台上: pf-node --experiment <提交> --device-access write --resource device:<serial> --detach experiment dataset/update_game.py <serial>

--rule 上下文=按钮: 屏幕文字含「上下文」时点文字含「按钮」的框; 缺省只有 发现新版本=确认。下一屏要点什么, 看了停下时的截图再加规则重跑。
碰到登录、验证码、付费、账号或客户端异常的字样, 不管有没有规则都停。有按钮但没有规则也停; 下载、解压这类进度页就等, 文字 --stall-s 秒不变也停。
版本号变了且回到「点击进入」算完成, 这时强停游戏; 停在别处时游戏留在那一屏, 重跑接着走。
输出 ($PF_JOB_OUT/game_update_<时间>/): update.json (前后版本、下载量、耗时、结果), steps.jsonl, shots/
退出码: 0 更新完成, 3 停下等人看, 1 出错或超时。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PF_ROOT") or os.path.dirname(os.path.dirname(HERE))
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(ROOT, "src", "target", "release", "phonefarm")
if __name__ == "__main__" and not os.environ.get("PF_LOCK_FDS"):
    serial = next((x for x in sys.argv[1:] if not x.startswith("-")), None)
    if not serial:
        sys.exit(__doc__)
    label = os.environ.get("DEVLOCK_LABEL", "")
    os.execv(PF, [PF, "lease", "--serial", serial, "--label", (label + "-" if label else "") + f"game-update@{serial}", "--",
                  sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "selfsr")]
from PIL import Image  # noqa: E402
import framedump_probe as fp  # noqa: E402
from cruise import lock_fds  # noqa: E402

ab = fp.ab
STOP_WORDS = ("验证码", "重新登录", "请登录", "登录失效", "登录过期", "账号异常", "账号存在", "封禁", "封停", "违规", "安全验证", "滑块", "实名", "身份证",
              "支付", "充值", "购买", "客户端异常", "数据异常", "检测到", "不支持")   # 这些要人处理
BUTTONS = ("确认", "确定", "取消", "前往", "同意", "拒绝", "允许", "继续", "安装", "更新", "打开", "完成")
WAIT_WORDS = ("下载", "解压", "校验", "加载", "连接", "正在", "%", "抵制", "资源", "准备")


def ocr(path):
    """[(文字, [x1,y1,x2,y2])], 图片像素, 和 input tap 同坐标。"""
    out = subprocess.run([os.environ.get("PF_OCR_BIN") or os.path.join(ROOT, "ocr"), path], capture_output=True, text=True, timeout=120).stdout
    return [(r["t"], r["b"]) for r in map(json.loads, filter(None, out.splitlines()))]


def version(dev, pkg):
    t = dev.shell(f"dumpsys package {pkg}")
    return {"name": (re.search(r"versionName=(\S+)", t) or [None, None])[1], "code": (re.search(r"versionCode=(\d+)", t) or [None, None])[1]}


def sizes(dev, pkg):
    """APK 和游戏数据的大小 (KB)。"""
    apk = dev.shell(f"pm path {pkg}").replace("package:", "").split()
    kb = lambda p: int((dev.su(f"du -sk {p} 2>/dev/null", timeout=300).split() or ["0"])[0])
    return {"apk_paths": apk, "apk_kb": sum(kb(p) for p in apk), "data_kb": kb(f"/data/data/{pkg}"), "external_kb": kb(f"/sdcard/Android/data/{pkg}"), "free": dev.shell("df -k /data | tail -1").strip()}


def net_rx(dev):
    """整机各网卡收到的字节, 含游戏以外的流量。"""
    return sum(int(ln.split(":")[1].split()[0]) for ln in dev.shell("cat /proc/net/dev").splitlines() if ":" in ln and not ln.strip().startswith("lo:"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--pkg", default="com.miHoYo.Yuanshen")
    ap.add_argument("--rule", action="append", default=[], metavar="上下文=按钮")
    ap.add_argument("--max-seconds", type=int, default=5400)
    ap.add_argument("--stall-s", type=int, default=300)
    ap.add_argument("--interval", type=float, default=10)
    a = ap.parse_args()
    rules = [r.split("=", 1) for r in ["发现新版本=确认", *a.rule]]
    out = os.path.join(os.environ.get("PF_JOB_OUT") or os.path.join(ROOT, "_runs"), f"game_update_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(out, "shots"))
    dev = ab.Dev(a.serial)
    png = os.path.join(out, "_shot.png")
    t0 = time.time()
    rec = {"serial": a.serial, "package": a.pkg, "rules": rules, "before": {"version": version(dev, a.pkg), **sizes(dev, a.pkg)}, "net_rx_start": net_rx(dev), "taps": [], "status": None, "reason": None}
    def save():
        rec["seconds"] = round(time.time() - t0, 1)
        rec["net_rx_bytes"] = net_rx(dev) - rec["net_rx_start"]
        json.dump(rec, open(os.path.join(out, "update.json"), "w"), ensure_ascii=False, indent=1)
    steps = open(os.path.join(out, "steps.jsonl"), "w")
    dev.shell("input keyevent KEYCODE_WAKEUP")
    if not dev.shell(f"pidof {a.pkg}").strip():
        dev.shell(f"monkey -p {a.pkg} -c android.intent.category.LAUNCHER 1")
        time.sleep(15)
    last_text, last_change, seen_unknown, n, code = None, time.time(), 0, 0, 3
    try:
        while True:
            if time.time() - t0 > a.max_seconds:
                rec.update(status="TIMEOUT", reason=f"{a.max_seconds} 秒没更新完")
                code = 1
                break
            dev.shell("input keyevent KEYCODE_WAKEUP")
            with open(png, "wb") as f:
                subprocess.run(["adb", "-s", a.serial, "exec-out", "screencap -p"], stdout=f, timeout=60, pass_fds=lock_fds())
            items = ocr(png)
            text = " ".join(t for t, _ in items)
            focus = (re.search(r"mCurrentFocus=\S+ \S+ (\S+?)\}", dev.shell("dumpsys window | grep mCurrentFocus")) or [None, None])[1]
            ver = version(dev, a.pkg)
            changed = text != last_text
            if changed:
                last_change = time.time()
                im = Image.open(png).convert("RGB")
                shot = f"{n:04d}.jpg"
                im.resize((960, round(960 * im.height / im.width))).save(os.path.join(out, "shots", shot), quality=80)
                rec["last_shot"], rec["last_text"] = shot, text
                n += 1
            steps.write(json.dumps({"t_s": round(time.time() - t0, 1), "focus": focus, "version": ver, "text": text, "net_rx": net_rx(dev) - rec["net_rx_start"]}, ensure_ascii=False) + "\n")
            steps.flush()
            last_text = text
            save()
            hit = next((w for w in STOP_WORDS if w in text), None)
            if hit:
                rec.update(status="NEEDS_HUMAN", reason=f"屏幕上有「{hit}」")
                break
            if "点击进入" in text and ver != rec["before"]["version"]:
                rec.update(status="UPDATED", reason=None)
                code = 0
                break
            rule = next(((c, b) for c, b in rules if c in text), None)
            box = next((b for t, b in items if rule and rule[1] in t), None) if rule else None
            if box:
                if sum(1 for x in rec["taps"] if x["rule"] == rule) >= 5:
                    rec.update(status="NEEDS_LOOK", reason=f"规则 {rule} 点了 5 次还在")
                    break
                x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
                dev.shell(f"input tap {x} {y}")
                rec["taps"].append({"t_s": round(time.time() - t0, 1), "rule": rule, "at": [x, y], "text": text[:200]})
                seen_unknown = 0
                time.sleep(a.interval)
                continue
            if focus and not focus.startswith(a.pkg) and ver != rec["before"]["version"] and not items:
                dev.shell(f"monkey -p {a.pkg} -c android.intent.category.LAUNCHER 1")   # 装完回了桌面
                time.sleep(15)
                continue
            has_button = any(t.strip() in BUTTONS or any(t.strip().startswith(b) and len(t.strip()) <= 6 for b in BUTTONS) for t, _ in items)
            waiting = not has_button and (not items or any(w in text for w in WAIT_WORDS)) and (focus or "").startswith(a.pkg)
            if waiting:
                seen_unknown = 0
                if time.time() - last_change > a.stall_s:
                    rec.update(status="NEEDS_LOOK", reason=f"{a.stall_s} 秒画面文字没变")
                    break
            else:
                seen_unknown += 1
                if seen_unknown >= 2:   # 隔一个间隔再看一眼, 过渡画面不算
                    rec.update(status="NEEDS_LOOK", reason=f"认不出这一屏 (前台 {focus})")
                    break
            time.sleep(a.interval)
    except Exception as e:
        rec.update(status="ERROR", reason=f"{type(e).__name__}: {e}")
        code = 1
    rec["after"] = {"version": version(dev, a.pkg), **sizes(dev, a.pkg), "client_string": next((m for m in re.findall(r"CNREL\S+", rec.get("last_text") or "")), None)}
    if code == 0:
        dev.shell(f"am force-stop {a.pkg}")
    rec["game_running"] = bool(dev.shell(f"pidof {a.pkg}").strip())
    save()
    print(json.dumps({k: rec.get(k) for k in ("status", "reason", "seconds", "net_rx_bytes", "last_shot", "last_text", "game_running")}, ensure_ascii=False))
    print(json.dumps({"before": rec["before"], "after": rec["after"], "taps": rec["taps"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
