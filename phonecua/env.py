"""Hybrid GUI + CLI environment over one Android phone.

    env = PhoneEnv(LocalAdb(serial), out_dir="out")
    snap = env.begin(setup_cmds)          # snapshot settings, run task setup, go home
    obs = env.observe()                   # {"focus", "elements", "xml", "shot"}
    res, obs = env.step({"action": "tap", "x": 300, "y": 900})
    res, obs = env.step({"action": "shell", "cmd": "settings get secure ui_night_mode"})
    env.reset(snap, cleanup_cmds)         # kill launched apps, home, undo settings diff, remove cua_* files

Action space (JSON dicts):
    GUI : tap{x,y | element} long_press{x,y | element} swipe{x1,y1,x2,y2,ms | direction} type{text}
          key{key} back home wait{seconds} finish{answer?}
    CLI : shell{cmd}   (runs as the adb shell user, 30 s timeout, output truncated; guarded by a denylist)
Each step is one device round trip: the action, a settle delay, then the observation (focus window, uiautomator
hierarchy, and a screenshot when the transport can pull files).
"""
import base64
import json
import os
import re
import shlex
import time
import xml.etree.ElementTree as ET

DEV_TMP = "/data/local/tmp/cua"
NAMESPACES = ("system", "secure", "global")
# Directories where tasks and agents may create files; reset removes cua_* entries there.
SCRATCH_DIRS = ("/sdcard/Download", "/sdcard/Documents")
KEEP_RUNNING = ("com.hihonor.android.launcher", "com.android.systemui", "com.android.launcher3", "com.baidu.input",
                "com.sohu.inputmethod", "com.google.android.inputmethod", "android", "com.android.permissioncontroller")
# Settings the system rewrites on its own; restoring them is pointless and can fight the OS.
VOLATILE = re.compile(r"timestamp|_count$|^last_|_last_|elapsed|_ms$|^next_alarm_formatted$|^screen_brightness_float$|"
                      r"^boot_count$|^ota_|^sysui_|^cloud_|^card_batch_no$|^keyguard_|^ThermalTempLevel$", re.I)
# Commands an agent may not run through shell{}: they would disturb the shared phone or touch personal data.
SHELL_DENY = [
    (r"\breboot\b|\bsvc\s+power\b|\bsetprop\b", "rebooting / system properties"),
    (r"\bpm\s+(uninstall|clear|disable|hide|install|grant|revoke|suspend)", "package state changes"),
    (r"\bstay_on_while_plugged_in\b|\bscreen_off_timeout\b.*\b0\b", "farm keep-alive settings"),
    (r"\bsvc\s+(wifi|data|usb)\b|\bairplane", "connectivity of the shared phone"),
    (r"\b(rm|mv)\b(?![^;&|]*\bcua_)", "rm/mv only on cua_* paths"),
    (r"\bcontent\s+(insert|update|delete)\b|contacts|sms|mms|call_log|DCIM|Pictures|WeiXin|Tencent", "personal data"),
    (r"\bkill(all)?\b|\bpkill\b", "process killing"),
    (r"\bsu\b", "root"),
]
KEYS = {"enter": 66, "del": 67, "backspace": 67, "delete": 67, "tab": 61, "space": 62, "back": 4, "home": 3,
        "menu": 82, "app_switch": 187, "recents": 187, "search": 84, "escape": 111, "up": 19, "down": 20,
        "left": 21, "right": 22, "power": 26, "wakeup": 224}


def b64(s):
    return base64.b64encode(s.encode()).decode()


def check_shell(cmd):
    for pat, why in SHELL_DENY:
        if re.search(pat, cmd, re.I):
            return why
    return None


def parse_bounds(b):
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b or "")
    return tuple(int(v) for v in m.groups()) if m else None


def parse_elements(xml, max_n=120):
    """Compact list of on-screen elements worth acting on or reading."""
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for n in root.iter("node"):
        a = n.attrib
        bd = parse_bounds(a.get("bounds"))
        if not bd or bd[2] - bd[0] < 4 or bd[3] - bd[1] < 4:
            continue
        text, desc = a.get("text", "").strip(), a.get("content-desc", "").strip()
        flags = [f for f in ("clickable", "checkable", "scrollable", "long-clickable") if a.get(f) == "true"]
        editable = "EditText" in a.get("class", "")
        if not (text or desc or flags or editable):
            continue
        e = {"id": len(out), "cls": a.get("class", "").split(".")[-1], "text": text[:80], "desc": desc[:60],
             "rid": a.get("resource-id", "").split("/")[-1], "bounds": list(bd),
             "center": [(bd[0] + bd[2]) // 2, (bd[1] + bd[3]) // 2], "pkg": a.get("package", "")}
        if flags:
            e["flags"] = flags
        if a.get("checkable") == "true":
            e["checked"] = a.get("checked") == "true"
        if a.get("selected") == "true":
            e["selected"] = True
        if editable:
            e["editable"] = True
        out.append(e)
        if len(out) >= max_n:
            break
    return out


def split_marked(out, tag):
    m = re.search(rf"@@CUA:{tag}:BEGIN\n?(.*?)@@CUA:{tag}:END", out, re.S)
    return m.group(1) if m else ""


OBS_SCRIPT = r"""
echo @@CUA:FOCUS:BEGIN
dumpsys activity activities 2>/dev/null | grep -m1 topResumedActivity
dumpsys window 2>/dev/null | grep -m1 mCurrentFocus
echo @@CUA:FOCUS:END
rm -f {tmp}/ui.xml; ok=0; for i in 1 2 3; do uiautomator dump {tmp}/ui.xml >/dev/null 2>&1 && ok=1 && break; sleep 1; done
echo @@CUA:XML:BEGIN
[ $ok = 1 ] && cat {tmp}/ui.xml
echo @@CUA:XML:END
{shot}
"""


class PhoneEnv:
    def __init__(self, transport, out_dir=None, shots=True, settle=1.5, log=print):
        self.t = transport
        self.out_dir = out_dir
        self.shots = shots and not transport.remote and out_dir is not None
        self.settle = settle
        self.log = log
        self.last_obs = None
        self.seen_pkgs = set()
        self.n_obs = 0
        self.shot_prefix = "obs"

    # ---------- device round trips ----------
    def sh(self, script, timeout=90):
        out, rc = self.t.sh(script, timeout)
        return out

    def _obs_script(self):
        shot = f"screencap -p {DEV_TMP}/shot.png" if self.shots else ""
        return OBS_SCRIPT.replace("{tmp}", DEV_TMP).replace("{shot}", shot)

    def _parse_obs(self, out):
        focus = split_marked(out, "FOCUS").strip()
        xml = split_marked(out, "XML").strip()
        pkgs = re.findall(r"u0 ([\w.]+)/", focus)
        self.seen_pkgs.update(pkgs)
        obs = {"focus": focus.replace("\n", " | ")[:300], "pkg": pkgs[0] if pkgs else "", "xml": xml,
               "elements": parse_elements(xml), "shot": None, "t": time.time()}
        if self.shots:
            self.n_obs += 1
            p = os.path.join(self.out_dir, f"{self.shot_prefix}_{self.n_obs:03d}.png")
            if self.t.pull(f"{DEV_TMP}/shot.png", p):
                obs["shot"] = p
        self.last_obs = obs
        return obs

    def observe(self):
        return self._parse_obs(self.sh(f"mkdir -p {DEV_TMP}\n" + self._obs_script()))

    def _resolve_xy(self, a, kx="x", ky="y"):
        if ("rid" in a or "text" in a) and self.last_obs:  # used by scripted oracles
            for e in self.last_obs["elements"]:
                if ("rid" not in a or e["rid"] == a["rid"]) and ("text" not in a or a["text"] in (e["text"], e["desc"])):
                    return e["center"]
            raise ValueError(f"no element matching {a}")
        if "element" in a and self.last_obs:
            els = self.last_obs["elements"]
            i = int(a["element"])
            if not 0 <= i < len(els):
                raise ValueError(f"no element {i}")
            return els[i]["center"]
        return int(a[kx]), int(a[ky])

    def action_script(self, a):
        """Device shell snippet for one action; prints its own result between RESULT markers."""
        k = a.get("action")
        if k == "tap":
            x, y = self._resolve_xy(a)
            return f"input tap {x} {y}"
        if k == "long_press":
            x, y = self._resolve_xy(a)
            return f"input swipe {x} {y} {x} {y} {int(a.get('ms', 800))}"
        if k == "swipe":
            if "direction" in a:  # finger movement direction; "up" scrolls content down
                w, h = 1080, 2400
                if self.last_obs and self.last_obs["elements"]:
                    w = max(e["bounds"][2] for e in self.last_obs["elements"])
                    h = max(e["bounds"][3] for e in self.last_obs["elements"])
                cx, cy = w // 2, h // 2
                d = {"up": (cx, int(h * .75), cx, int(h * .3)), "down": (cx, int(h * .3), cx, int(h * .75)),
                     "left": (int(w * .85), cy, int(w * .15), cy), "right": (int(w * .15), cy, int(w * .85), cy)}
                x1, y1, x2, y2 = d[a["direction"]]
            else:
                x1, y1, x2, y2 = (int(a[c]) for c in ("x1", "y1", "x2", "y2"))
            return f"input swipe {x1} {y1} {x2} {y2} {int(a.get('ms', 300))}"
        if k == "type":
            text = str(a["text"])
            if any(ord(c) > 126 for c in text):
                raise ValueError("type supports ASCII only (adb input text)")
            enc = b64(text.replace("%", "\\%").replace(" ", "%s"))
            return f'input text "$(echo {enc} | base64 -d)"'
        if k == "key":
            key = str(a["key"]).lower().replace("keycode_", "")
            code = KEYS.get(key, key if key.isdigit() else None)
            if code is None:
                return f"input keyevent KEYCODE_{key.upper()}"
            return f"input keyevent {code}"
        if k == "back":
            return "input keyevent 4"
        if k == "home":
            return "input keyevent 3"
        if k == "wait":
            return f"sleep {min(10, max(0, float(a.get('seconds', 2))))}"
        if k == "shell":
            cmd = str(a["cmd"])
            why = None if a.get("_trusted") else check_shell(cmd)
            if why:
                raise PermissionError(f"shell command refused ({why})")
            return (f"echo {b64(cmd)} | base64 -d > {DEV_TMP}/cmd.sh; timeout 30 sh {DEV_TMP}/cmd.sh > {DEV_TMP}/cmd.out 2>&1; "
                    f"echo \"rc=$?\"; head -c 3000 {DEV_TMP}/cmd.out")
        if k == "finish":
            return ":"
        raise ValueError(f"unknown action {k!r}")

    def step(self, a, observe=True):
        """Run one action (and observe). Returns (result dict, obs or None)."""
        res = {"action": a, "ok": True, "output": ""}
        try:
            snip = self.action_script(a)
        except (ValueError, KeyError, TypeError, PermissionError) as ex:
            res.update(ok=False, output=f"{type(ex).__name__}: {ex}")
            return res, (self.observe() if observe and self.last_obs is None else self.last_obs)
        settle = 0 if a.get("action") in ("finish", "wait") else (2.0 if a.get("action") == "shell" else self.settle)
        script = f"mkdir -p {DEV_TMP}\necho @@CUA:RESULT:BEGIN\n{snip}\necho @@CUA:RESULT:END\n"
        if observe and a.get("action") != "finish":
            script += f"sleep {settle}\n" + self._obs_script()
        out = self.sh(script)
        res["output"] = split_marked(out, "RESULT").strip()[:3000]
        if a.get("action") == "shell":
            m = re.match(r"rc=(\d+)", res["output"])
            res["rc"] = int(m.group(1)) if m else None
            res["ok"] = res["rc"] == 0
        if "@@CUA:RESULT:END" not in out:
            res.update(ok=False, output=("transport error: " + out[-500:]).strip())
        obs = self._parse_obs(out) if observe and a.get("action") != "finish" else self.last_obs
        return res, obs

    # ---------- episode bracket ----------
    def snapshot_script(self):
        lines = [f"echo @@CUA:SET_{ns}:BEGIN; settings list {ns}; echo @@CUA:SET_{ns}:END" for ns in NAMESPACES]
        lines.append("echo @@CUA:FILES:BEGIN; ls -d " + " ".join(f"{d}/cua_*" for d in SCRATCH_DIRS)
                     + " 2>/dev/null; echo @@CUA:FILES:END")
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_snapshot(out):
        snap = {}
        for ns in NAMESPACES:
            d = {}
            for line in split_marked(out, f"SET_{ns}").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    d[k] = v
            snap[ns] = d
        snap["files"] = [f for f in split_marked(out, "FILES").split() if f]
        return snap

    def begin(self, setup_cmds=()):
        """Snapshot, run task setup, go home, observe. One round trip."""
        script = (f"mkdir -p {DEV_TMP}\necho @@CUA:PRE:BEGIN; dumpsys activity activities 2>/dev/null | "
                  "grep -m1 topResumedActivity; echo @@CUA:PRE:END\n"
                  "input keyevent 224; wm dismiss-keyguard; sleep 1\n" + self.snapshot_script()
                  + "echo @@CUA:SETUP:BEGIN\n" + "\n".join(setup_cmds) + "\necho @@CUA:SETUP:END\n"
                  + "input keyevent 3\nsleep 1.5\n" + self._obs_script())
        out = self.sh(script, timeout=180)
        snap = self.parse_snapshot(out)
        if not snap["system"]:
            raise RuntimeError("snapshot failed: " + out[-400:])
        obs = self._parse_obs(out)
        # whatever was in the foreground before the episode is not ours to kill at reset
        self.initial_pkgs = set(re.findall(r"u0 ([\w.]+)/", split_marked(out, "PRE")))
        self.seen_pkgs = {obs["pkg"]} if obs["pkg"] else set()
        return snap, split_marked(out, "SETUP").strip(), obs

    def restore_script(self, snap, now):
        cmds, changes = [], []
        for ns in NAMESPACES:
            before, after = snap[ns], now[ns]
            for k in sorted(set(before) | set(after)):
                b, a = before.get(k), after.get(k)
                if b == a or VOLATILE.search(k):
                    continue
                changes.append({"ns": ns, "key": k, "before": b, "after": a})
                if (ns, k) == ("secure", "ui_night_mode") and b is not None:
                    cmds.append(f"cmd uimode night {({'1': 'no', '2': 'yes'}).get(b, 'auto')}")
                elif (ns, k) == ("global", "bluetooth_on") and b is not None:
                    v = "enable" if b == "1" else "disable"
                    cmds.append(f"cmd bluetooth_manager {v} || svc bluetooth {v}")
                elif (ns, k) == ("global", "zen_mode") and b is not None:
                    cmds.append(f"cmd notification set_dnd {'off' if b == '0' else 'on'}")
                elif (ns, k) == ("secure", "location_mode") and b is not None:
                    cmds.append(f"cmd location set-location-enabled {'false' if b == '0' else 'true'}")
                if b is None:
                    cmds.append(f"settings delete {ns} {shlex.quote(k)}")
                else:
                    cmds.append(f"settings put {ns} {shlex.quote(k)} {shlex.quote(b)}")
        return cmds, changes

    def reset(self, snap, cleanup_cmds=(), packages=()):
        """Undo the episode: force-stop apps seen in foreground, run task cleanup, remove new cua_* files,
        restore every changed setting, go home. Returns a restore record (verified by a fresh snapshot)."""
        pkgs = sorted((self.seen_pkgs | set(packages)) - {p for p in self.seen_pkgs if p.startswith(KEEP_RUNNING)}
                      - (getattr(self, "initial_pkgs", set()) - set(packages)))
        pre = (["input keyevent 3"] + [f"am force-stop {p}" for p in pkgs] + list(cleanup_cmds)
               + [f"rm -rf {d}/cua_*" for d in SCRATCH_DIRS] + [f"rm -rf {DEV_TMP}"])
        # restore needs the current settings; read them in the same trip as cleanup, then a second trip writes back
        out = self.sh("\n".join(pre) + "\n" + self.snapshot_script(), timeout=180)
        now = self.parse_snapshot(out)
        rec = {"force_stopped": pkgs, "cleanup": list(cleanup_cmds), "rounds": []}
        for _ in range(3):
            cmds, changes = self.restore_script(snap, now)
            # files that existed before the episode can't be restored by rm; flag them
            rec["preexisting_cua_files"] = snap["files"]
            if not changes:
                break
            rec["rounds"].append({"changes": changes, "cmds": cmds})
            out = self.sh("\n".join(cmds) + "\nsleep 2\ninput keyevent 3\n" + self.snapshot_script(), timeout=180)
            now = self.parse_snapshot(out)
        residual = self.restore_script(snap, now)[1]
        rec["residual"] = residual
        rec["leftover_files"] = now["files"]
        rec["ok"] = not residual and not now["files"]
        return rec


def dump_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
