"""Baseline agents.

OracleAgent  replays the task's scripted solution (proves setup/checker/reset work end to end).
LLMAgent     OpenAI-compatible chat model choosing one JSON action per step from the UI element list.
             mode="gui"    : tap/long_press/swipe/type/key/back/home/wait/finish
             mode="hybrid" : the same plus shell{cmd} (adb shell on the phone)
Credentials come from an env file (RELAY_URL, RELAY_KEY, RELAY_FAST_MODEL); the key is never logged.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request


class OracleAgent:
    name = "oracle"

    def reset(self, task, mode):
        self.queue = list(task["oracle"])

    def act(self, task, obs, history):
        a = dict(self.queue.pop(0)) if self.queue else {"action": "finish"}
        if a.pop("answer_from_last_output", False):
            out = history[-1]["result"]["output"] if history else ""
            lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("rc=")]
            a["answer"] = lines[-1].strip() if lines else ""
        return a, None


def load_env_file(path):
    d = {}
    with open(os.path.expanduser(path)) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return d


GUI_ACTIONS = """\
{"action":"tap","element":ID}            tap the centre of element ID (or {"action":"tap","x":X,"y":Y})
{"action":"long_press","element":ID}
{"action":"swipe","direction":"up|down|left|right"}   finger direction; "up" scrolls the list down
{"action":"type","text":"..."}           types ASCII into the focused field (tap the field first)
{"action":"key","key":"enter|del|..."}
{"action":"back"}  {"action":"home"}  {"action":"wait","seconds":2}
{"action":"finish","answer":"..."}       when the task is done (answer only for questions; omit otherwise)"""
SHELL_ACTION = """\
{"action":"shell","cmd":"..."}           run a command with `adb shell` on the phone (Android toybox: settings, am,
                                         cmd, dumpsys, pm list, ls/mkdir/cp/mv/rm/cat/echo, getprop, wm ...) and see
                                         its output. Settings: `settings get|put system|secure|global KEY VALUE`."""

SYSTEM = """You operate a real Android phone (MagicOS, Chinese UI) to complete a task.
Each turn you get the task, your previous actions with results, and the current screen as a list of UI elements
[id] class "text" desc=... rid=resource-id @(x,y) flags. Screen is about 1272x2800 px.
Reply with exactly ONE JSON object, no other text. Available actions:
{actions}
Rules: do not sign into accounts, do not open account/payment pages, do not touch personal data (photos, contacts,
messages), do not install or uninstall apps. Internal storage root is /sdcard (shown as 内部存储 in Files).
{mode_rule}
Finish as soon as the task is done; do not verify more than once."""
MODE_RULE = {
    "gui": "You may only use GUI actions (no shell). Open apps from the home screen (swipe left/right for more pages).",
    "hybrid": "Mix GUI actions and shell commands freely; prefer whichever is faster and more reliable.",
}


def render_obs(obs, max_lines=110):
    lines = [f"foreground: {obs.get('pkg') or '?'}  ({obs.get('focus', '')[:160]})"]
    els = obs.get("elements") or []
    if not els:
        lines.append("(UI hierarchy unavailable on this screen: the app never went idle; act from memory or use back)")
    for e in els[:max_lines]:
        s = f"[{e['id']}] {e['cls']}"
        if e["text"]:
            s += f' "{e["text"]}"'
        if e["desc"]:
            s += f" desc={e['desc']!r}"
        if e["rid"]:
            s += f" rid={e['rid']}"
        s += f" @({e['center'][0]},{e['center'][1]})"
        fl = [f for f in e.get("flags", []) if f in ("clickable", "scrollable")]
        if "checked" in e:
            fl.append("on" if e["checked"] else "off")
        if e.get("editable"):
            fl.append("editable")
        if fl:
            s += " " + ",".join(fl)
        lines.append(s)
    return "\n".join(lines)


class LLMAgent:
    def __init__(self, env_file, model=None, temperature=0.0, log=print):
        cfg = load_env_file(env_file)
        self.url = cfg["RELAY_URL"].rstrip("/") + "/chat/completions"
        self.key = cfg["RELAY_KEY"]
        self.model = model or cfg.get("RELAY_FAST_MODEL")
        self.temperature = temperature
        self.log = log
        self.name = f"llm:{self.model}"
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}

    def reset(self, task, mode):
        self.mode = mode

    def _chat(self, messages):
        body = json.dumps({"model": self.model, "messages": messages, "temperature": self.temperature,
                           "max_tokens": 1500}).encode()
        last = None
        for i in range(4):
            req = urllib.request.Request(self.url, data=body, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
            t = time.time()
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    d = json.load(r)
                self.usage["calls"] += 1
                self.usage["seconds"] += time.time() - t
                u = d.get("usage") or {}
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                self.usage["completion_tokens"] += u.get("completion_tokens", 0)
                return d["choices"][0]["message"].get("content") or ""
            except urllib.error.HTTPError as ex:
                last = f"HTTP {ex.code}"
                if ex.code not in (429, 500, 502, 503, 504, 529):
                    raise RuntimeError(last) from None
            except (urllib.error.URLError, TimeoutError, OSError) as ex:
                last = f"{type(ex).__name__}: {ex}"
            time.sleep(5 * (i + 1))
        raise RuntimeError(f"relay failed: {last}")

    def act(self, task, obs, history):
        actions = GUI_ACTIONS + ("\n" + SHELL_ACTION if self.mode == "hybrid" else "")
        sys_p = SYSTEM.replace("{actions}", actions).replace("{mode_rule}", MODE_RULE[self.mode])
        hist = []
        for h in history[-10:]:
            out = (h["result"].get("output") or "").replace("\n", " | ")[:400]
            hist.append(f"{h['step']}. {json.dumps(h['action'], ensure_ascii=False)} -> "
                        f"{'ok' if h['result']['ok'] else 'FAILED'}{': ' + out if out else ''}")
        user = (f"TASK: {task['instruction']}\n\nPREVIOUS ACTIONS ({len(history)} so far):\n"
                + ("\n".join(hist) or "(none)") + "\n\nCURRENT SCREEN:\n" + render_obs(obs)
                + "\n\nNext action (one JSON object):")
        raw = self._chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user}])
        txt = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        m = None
        for cand in re.findall(r"\{[^{}]*\}", txt):
            try:
                m = json.loads(cand)
                if "action" in m:
                    break
            except json.JSONDecodeError:
                continue
        if not m or "action" not in m:
            return {"action": "invalid", "raw": txt[:300]}, raw
        m.pop("_trusted", None)
        return m, raw
