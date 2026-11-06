"""Task set: stock/system apps only, each with setup, a programmatic checker, cleanup and a scripted oracle.

Fields
  id, instruction      what the agent is told
  app                  the app a GUI agent would use
  faster               "shell" | "gui" | "either": which route needs fewer steps for a competent agent
  setup                device shell lines run (after the settings snapshot) before the episode
  check_script         device shell run after the episode; its stdout goes to check(out, answer) -> (ok, detail)
  cleanup              extra device shell lines for reset (settings and cua_* files are restored generically)
  cleanup_gui          optional fn(env) for state only the GUI can undo (alarms)
  oracle               list of actions ending in finish (LAST = answer is the last shell output token)
  answer               True when the agent must report a value via finish{answer}
Settings use the platform's own keys (`settings get`), files live under /sdcard/{Download,Documents}/cua_*.
"""
import re

DL, DOC = "/sdcard/Download", "/sdcard/Documents"
UI_TEXT = ("rm -f /data/local/tmp/cua/chk.xml; mkdir -p /data/local/tmp/cua; "
           "for i in 1 2 3; do uiautomator dump /data/local/tmp/cua/chk.xml >/dev/null 2>&1 && break; sleep 1; done; "
           "echo TOP=$(dumpsys activity activities | grep -m1 topResumedActivity); "
           "cat /data/local/tmp/cua/chk.xml 2>/dev/null | tr '>' '\\n' | grep -oE 'text=\"[^\"]*\"|content-desc=\"[^\"]*\"'")


def eq(expected):
    def f(out, ans):
        v = out.strip().splitlines()[-1].strip() if out.strip() else ""
        return v == expected, f"got {v!r}, want {expected!r}"
    return f


def all_lines_ok(out, ans):
    bad = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    oks = [ln for ln in out.splitlines() if ln.startswith("OK")]
    return (not bad and bool(oks)), "; ".join(bad or oks)[:300]


def num_answer(out, ans):
    want = out.strip().splitlines()[-1].strip() if out.strip() else ""
    got = re.findall(r"-?\d+(?:\.\d+)?", str(ans or ""))
    return bool(want) and want in got, f"answer {ans!r}, want {want!r}"


def S(ns, key):
    return f"settings get {ns} {key}"


def wait_eq(ns, key, val, secs=6):
    """Poll a setting (some, like bluetooth_on, settle asynchronously)."""
    return (f"for i in $(seq {secs}); do v=$(settings get {ns} {key}); [ \"$v\" = \"{val}\" ] && break; sleep 1; done; "
            f"echo $v")


def F(cmd):
    return {"action": "finish", "answer": cmd} if cmd is not None else {"action": "finish"}


# oracle finish whose answer is the last token of the previous shell output
LAST = {"action": "finish", "answer_from_last_output": True}


def sh(cmd):
    return {"action": "shell", "cmd": cmd, "_trusted": True}


ALARM = "06:45"


def alarm_check(out, ans):
    n = len(re.findall(rf"origWhen=\d{{4}}-\d\d-\d\d {ALARM}:00", out))
    return n > 0, f"{n} enabled deskclock alarm(s) at {ALARM}"


def delete_alarms(env, hhmm=ALARM, log=print):
    """GUI cleanup: delete every alarm row showing hhmm in the stock Clock app."""
    steps = []

    def st(a):
        r, o = env.step(a)
        steps.append({"a": a, "ok": r["ok"], "out": r["output"][:200]})
        return o

    o = st(sh("am start -n com.hihonor.deskclock/com.android.deskclock.AlarmsMainActivity"))
    for _ in range(3):  # first-run dialogs: deny permission requests, acknowledge notices
        btn = next((e for e in o["elements"] if e["text"] in ("禁止", "知道了", "不允许", "Deny", "OK", "Got it")), None)
        if not btn:
            break
        o = st({"action": "tap", "x": btn["center"][0], "y": btn["center"][1]})
    rows = [e for e in o["elements"] if e["rid"] == "digital_full_time" and e["text"] == hhmm]
    if not rows:
        return {"deleted": 0, "steps": steps}
    o = st({"action": "long_press", "x": rows[0]["center"][0], "y": rows[0]["center"][1]})
    # select every matching row (the long-pressed one is preselected)
    times = [e for e in o["elements"] if e["rid"] == "digital_full_time"]
    boxes = [e for e in o["elements"] if e["rid"] == "image_remove_alarm"]
    for t, b in zip(times, boxes):
        if t["text"] == hhmm and not b.get("checked"):
            o = st({"action": "tap", "x": b["center"][0], "y": b["center"][1]})
        elif t["text"] != hhmm and b.get("checked"):
            o = st({"action": "tap", "x": b["center"][0], "y": b["center"][1]})
    o = st({"action": "tap", "rid": "item_delete"})
    o = st({"action": "tap", "rid": "button1"})  # confirm
    st(sh("am force-stop com.hihonor.deskclock"))
    return {"deleted": len(rows), "steps": steps}


CALC_ORACLE = ([sh("am start -n com.hihonor.calculator/.Calculator"), {"action": "tap", "rid": "op_clr"}]
               + [{"action": "tap", "rid": r} for r in ["digit_3", "digit_7", "op_mul", "digit_4", "digit_8", "eq"]]
               + [F("1776")])


def calc_check(out, ans):
    on_screen = "com.hihonor.calculator" in out and re.search(r'text="[^"]*1,?776[^"]*"', out) is not None
    said = "1776" in re.sub(r"[,\s]", "", str(ans or ""))
    return on_screen and said, f"calculator shows 1776: {on_screen}; answer {ans!r}"


TASKS = [
    # ---------------- settings ----------------
    dict(id="dark_mode_on", app="Settings", faster="shell",
         instruction="Turn on dark mode.",
         setup=["cmd uimode night no"], check_script=S("secure", "ui_night_mode"), check=eq("2"),
         oracle=[sh("cmd uimode night yes"), F(None)]),
    dict(id="screen_timeout_2min", app="Settings", faster="shell",
         instruction="Set the screen sleep timeout (screen turns off after inactivity) to 2 minutes.",
         setup=[], check_script=S("system", "screen_off_timeout"), check=eq("120000"),
         oracle=[sh("settings put system screen_off_timeout 120000"), F(None)]),
    dict(id="time_24h", app="Settings", faster="shell",
         instruction="Switch the clock to 24-hour time format.",
         setup=["settings put system time_12_24 12"], check_script=S("system", "time_12_24"), check=eq("24"),
         oracle=[sh("settings put system time_12_24 24"), F(None)]),
    dict(id="auto_rotate_off", app="Settings", faster="either",
         instruction="Turn off auto-rotate so the screen stays in portrait.",
         setup=["settings put system accelerometer_rotation 1"], check_script=S("system", "accelerometer_rotation"),
         check=eq("0"), oracle=[sh("settings put system accelerometer_rotation 0"), F(None)]),
    dict(id="dnd_on", app="Settings", faster="either",
         instruction="Turn on Do Not Disturb.",
         setup=["cmd notification set_dnd off"],
         check_script="v=$(settings get global zen_mode); [ \"$v\" != 0 ] && echo OK zen_mode=$v || echo FAIL zen_mode=$v",
         check=all_lines_ok, oracle=[sh("cmd notification set_dnd on"), F(None)]),
    dict(id="bluetooth_off", app="Settings", faster="either",
         instruction="Turn Bluetooth off.",
         setup=["cmd bluetooth_manager enable", "sleep 2"], check_script=wait_eq("global", "bluetooth_on", "0"),
         check=eq("0"), oracle=[sh("cmd bluetooth_manager disable"), F(None)]),
    dict(id="show_touches_on", app="Settings > Developer options", faster="shell",
         instruction="Enable the developer option that shows visual feedback for taps (show touches).",
         setup=["settings put system show_touches 0"], check_script=S("system", "show_touches"), check=eq("1"),
         oracle=[sh("settings put system show_touches 1"), F(None)]),
    dict(id="auto_brightness_on", app="Settings", faster="either",
         instruction="Turn on automatic brightness adjustment.",
         setup=["settings put system screen_brightness_mode 0"], check_script=S("system", "screen_brightness_mode"),
         check=eq("1"), oracle=[sh("settings put system screen_brightness_mode 1"), F(None)]),
    # ---------------- files ----------------
    dict(id="mkdir_reports", app="Files", faster="shell",
         instruction="Create a folder named cua_reports inside the Download folder of internal storage.",
         setup=[f"rm -rf {DL}/cua_reports"],
         check_script=f"[ -d {DL}/cua_reports ] && echo OK dir || echo FAIL no {DL}/cua_reports", check=all_lines_ok,
         oracle=[sh(f"mkdir -p {DL}/cua_reports"), F(None)]),
    dict(id="rename_note", app="Files", faster="shell",
         instruction="In the Download folder, rename the file cua_note.txt to cua_note_final.txt.",
         setup=[f"echo draft > {DL}/cua_note.txt", f"rm -f {DL}/cua_note_final.txt"],
         check_script=(f"[ -f {DL}/cua_note_final.txt ] && echo OK new || echo FAIL no new; "
                       f"[ -e {DL}/cua_note.txt ] && echo FAIL old still there || echo OK old gone; "
                       f"grep -q draft {DL}/cua_note_final.txt && echo OK content || echo FAIL content"),
         check=all_lines_ok, oracle=[sh(f"mv {DL}/cua_note.txt {DL}/cua_note_final.txt"), F(None)]),
    dict(id="delete_log", app="Files", faster="shell",
         instruction="Delete the file cua_tmp.log from the Download folder. Do not delete anything else.",
         setup=[f"echo tmp > {DL}/cua_tmp.log", f"echo keep > {DL}/cua_keep.log"],
         check_script=(f"[ -e {DL}/cua_tmp.log ] && echo FAIL tmp still there || echo OK tmp gone; "
                       f"[ -f {DL}/cua_keep.log ] && echo OK keep kept || echo FAIL keep deleted"),
         check=all_lines_ok, oracle=[sh(f"rm {DL}/cua_tmp.log"), F(None)]),
    dict(id="write_hello", app="Files/Notes", faster="shell",
         instruction="Create a plain text file cua_hello.txt in the Documents folder of internal storage whose "
                     "content is exactly: hello world",
         setup=[f"mkdir -p {DOC}", f"rm -f {DOC}/cua_hello.txt"],
         check_script=f"cat {DOC}/cua_hello.txt 2>/dev/null | tr -d '\\r' | sed -e 's/[[:space:]]*$//' | tail -1",
         check=eq("hello world"), oracle=[sh(f"echo 'hello world' > {DOC}/cua_hello.txt"), F(None)]),
    dict(id="move_file", app="Files", faster="shell",
         instruction="Move the file cua_move.txt from the Download folder to the Documents folder.",
         setup=[f"mkdir -p {DOC}", f"echo 'move me' > {DL}/cua_move.txt", f"rm -f {DOC}/cua_move.txt"],
         check_script=(f"[ -f {DOC}/cua_move.txt ] && echo OK moved || echo FAIL not in Documents; "
                       f"[ -e {DL}/cua_move.txt ] && echo FAIL still in Download || echo OK source gone"),
         check=all_lines_ok, oracle=[sh(f"mv {DL}/cua_move.txt {DOC}/cua_move.txt"), F(None)]),
    # ---------------- information retrieval (answer via finish) ----------------
    dict(id="android_version", app="Settings > About phone", faster="either", answer=True,
         instruction="What Android version is this phone running? Answer with the major version number.",
         setup=[], check_script="getprop ro.build.version.release", check=num_answer,
         oracle=[sh("getprop ro.build.version.release"), LAST]),
    dict(id="count_jpg", app="Files", faster="either", answer=True,
         instruction="How many .jpg files are in the folder Download/cua_pics? Answer with a number.",
         setup=[f"mkdir -p {DL}/cua_pics"] + [f"echo x{i} > {DL}/cua_pics/img_{i}.jpg" for i in range(1, 8)]
               + [f"echo y{i} > {DL}/cua_pics/shot_{i}.png" for i in range(1, 4)],
         check_script=f"ls {DL}/cua_pics | grep -c '\\.jpg$'", check=num_answer,
         oracle=[sh(f"ls {DL}/cua_pics | grep -c '\\.jpg$'"), LAST]),
    dict(id="count_vendor_pkgs", app="Settings > Apps", faster="shell", answer=True,
         instruction="How many installed packages have a package name starting with 'com.hihonor.'? "
                     "Count system packages too. Answer with a number.",
         setup=[], check_script="pm list packages | grep -c '^package:com\\.hihonor\\.'", check=num_answer,
         oracle=[sh("pm list packages | grep -c '^package:com\\.hihonor\\.'"), LAST]),
    dict(id="screen_resolution", app="Settings", faster="shell", answer=True,
         instruction="What is the physical screen resolution of this phone in pixels? Answer as WIDTHxHEIGHT.",
         setup=[], check_script="wm size | sed -n 's/Physical size: //p' | head -1",
         check=lambda out, ans: ((lambda w: bool(w) and all(n in re.findall(r"\d+", str(ans or "")) for n in w))(
             re.findall(r"\d+", out)), f"answer {ans!r}, want {out.strip()!r}"),
         oracle=[sh("wm size | sed -n 's/Physical size: //p'"), LAST]),
    # ---------------- apps ----------------
    dict(id="alarm_0645", app="Clock", faster="either",
         instruction=f"Create an alarm for {ALARM} AM in the Clock app and make sure it is turned on.",
         setup=[], check_script="dumpsys alarm | grep -A1 'deskclock.ALARM_ALERT' | grep origWhen", check=alarm_check,
         cleanup_gui=delete_alarms, packages=["com.hihonor.deskclock"],
         oracle=[sh("am start -a android.intent.action.SET_ALARM --ei android.intent.extra.alarm.HOUR 6 "
                    "--ei android.intent.extra.alarm.MINUTES 45 --ez android.intent.extra.alarm.SKIP_UI true"),
                 {"action": "wait", "seconds": 2}, F(None)]),
    dict(id="open_dev_options", app="Settings", faster="either",
         instruction="Open the Developer options page in Settings and leave it on screen.",
         setup=["am force-stop com.android.settings"],
         check_script="dumpsys activity activities | grep -m1 topResumedActivity",
         check=lambda out, ans: ("DevelopmentSettings" in out or "DevelopmentSettingsDashboard" in out, out.strip()[-160:]),
         packages=["com.android.settings"],
         oracle=[sh("am start -a android.settings.APPLICATION_DEVELOPMENT_SETTINGS"), F(None)]),
    dict(id="calc_multiply", app="Calculator", faster="gui", answer=True,
         instruction="Use the Calculator app to compute 37 × 48. Leave the result visible in the Calculator and "
                     "report it as your answer.",
         setup=["am force-stop com.hihonor.calculator"], check_script=UI_TEXT, check=calc_check,
         packages=["com.hihonor.calculator"], oracle=CALC_ORACLE),
]

BY_ID = {t["id"]: t for t in TASKS}

