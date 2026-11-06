#!/usr/bin/env python3
"""Run an agent over the phonecua task set and record trajectories + pass/fail.

On the execution node (phone attached; the script takes the device lock itself via `phonefarm lease`):
    python3 phonecua/run.py <serial> --agent oracle [--tasks id,id] [--repeat N]
From elsewhere, through the platform (each env round trip is one locked `pf-node adb` job):
    python3 phonecua/run.py <serial> --agent llm --mode hybrid --remote <node>/<device> --pfnode ./pf-node \
        --relay-env ~/.config/yao-relay.env [--model kimi/k3]

Per task: snapshot settings -> setup -> home -> agent loop (obs, action, result) -> checker -> GUI cleanup (if any)
-> reset (force-stop apps seen, remove cua_* files, restore every changed setting, verify).
Output (--out, default $PF_JOB_OUT or ./out): <task>/traj.json (+ obs_###.xml/png), results.json, results.md,
restore.json (per-task restore records; ok=false means something could not be restored).
"""
import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from agents import LLMAgent, NoopAgent, OracleAgent  # noqa: E402
from env import PhoneEnv, dump_json  # noqa: E402
from tasks import TASKS  # noqa: E402
from transport import LocalAdb, PfNodeAdb, lock_fds  # noqa: E402

GUI_ONLY = {"tap", "long_press", "swipe", "type", "key", "back", "home", "wait", "finish"}


def run_task(env, agent, task, mode, max_steps, out_dir, log):
    os.makedirs(out_dir, exist_ok=True)
    env.out_dir, env.n_obs = out_dir, 0
    rec = {"task": task["id"], "agent": agent.name, "mode": mode, "instruction": task["instruction"],
           "faster": task["faster"], "steps": [], "answer": None, "t0": time.time()}
    snap, setup_out, obs = env.begin(task.get("setup", []))
    rec["setup_output"] = setup_out[-500:]
    agent.reset(task, mode)
    history = []
    try:
        for i in range(1, max_steps + 1):
            with open(os.path.join(out_dir, f"obs_{i:03d}.xml"), "w") as f:
                f.write(obs.get("xml") or "")
            t = time.time()
            try:
                action, raw = agent.act(task, obs, history)
            except Exception as ex:  # relay failure etc.: end the episode, record why
                rec["agent_error"] = f"{type(ex).__name__}: {ex}"
                log(f"  agent error {rec['agent_error']}")
                break
            think_s = time.time() - t
            if mode == "gui" and action.get("action") not in GUI_ONLY:
                res = {"action": action, "ok": False, "output": "action not available in GUI-only mode"}
            elif action.get("action") == "finish":
                res = {"action": action, "ok": True, "output": ""}
            else:
                res, obs = env.step(action)
            step = {"step": i, "obs": {"pkg": obs.get("pkg"), "focus": obs.get("focus"), "n_elements": len(obs.get("elements") or []),
                                       "xml": f"obs_{i:03d}.xml", "shot": os.path.basename(obs["shot"]) if obs.get("shot") else None},
                    "action": action, "result": {k: v for k, v in res.items() if k != "action"},
                    "agent_s": round(think_s, 2), "env_s": round(time.time() - t - think_s, 2)}
            if raw is not None:
                step["raw"] = raw[-1500:]
            rec["steps"].append(step)
            history.append(step)
            log(f"  {i:2d} {json.dumps(action, ensure_ascii=False)[:140]} -> {'ok' if res['ok'] else 'FAIL'} "
                f"{(res.get('output') or '').replace(chr(10), ' ')[:100]}")
            if action.get("action") == "finish":
                rec["answer"] = action.get("answer")
                rec["finished"] = True
                break
        out = env.sh(task["check_script"])
        ok, detail = task["check"](out, rec["answer"])
        rec.update(passed=bool(ok), check_detail=detail, check_output=out[-1500:])
    except Exception as ex:
        rec.update(passed=False, error=f"{type(ex).__name__}: {ex}", tb=traceback.format_exc()[-2000:])
    finally:
        restore = {}
        if task.get("cleanup_gui"):
            try:
                restore["gui_cleanup"] = task["cleanup_gui"](env)
            except Exception as ex:
                restore["gui_cleanup"] = {"error": f"{type(ex).__name__}: {ex}"}
        restore.update(env.reset(snap, task.get("cleanup", []), task.get("packages", [])))
        rec["restore_ok"] = restore["ok"]
        rec["t1"] = time.time()
        rec["n_steps"] = len(rec["steps"])
        dump_json(os.path.join(out_dir, "traj.json"), rec)
    return rec, restore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--agent", choices=["oracle", "noop", "llm"], default="oracle")
    ap.add_argument("--mode", choices=["gui", "hybrid"], default="hybrid")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--model")
    ap.add_argument("--relay-env", default="~/.config/yao-relay.env")
    ap.add_argument("--remote", help="<node_id>/<device_id>: drive the phone through pf-node jobs")
    ap.add_argument("--pfnode", default="pf-node")
    ap.add_argument("--research")
    ap.add_argument("--out", default=os.environ.get("PF_JOB_OUT") or "out")
    ap.add_argument("--phonefarm", default=os.environ.get("PHONEFARM_BIN") or os.path.join(os.environ.get("PF_ROOT", ""), "phonefarm"))
    a = ap.parse_args()
    if not a.remote and not lock_fds() and os.path.exists(a.phonefarm):
        label = (os.environ["DEVLOCK_LABEL"] + "-" if os.environ.get("DEVLOCK_LABEL") else "") + f"phonecua@{a.serial}"
        os.execvp(a.phonefarm, [a.phonefarm, "lease", "--serial", a.serial, "--label", label, "--",
                                sys.executable, "-I", os.path.abspath(__file__), *sys.argv[1:]])
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    logf = open(os.path.join(out, "run.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    if a.remote:
        transport = PfNodeAdb(a.serial, a.remote, a.pfnode, os.path.join(out, "_pfnode"), a.research)
    else:
        transport = LocalAdb(a.serial)
    env = PhoneEnv(transport, out_dir=out, log=log)
    agent = {"oracle": OracleAgent, "noop": NoopAgent}[a.agent]() if a.agent != "llm" else LLMAgent(a.relay_env, a.model, log=log)
    ids = [t for t in a.tasks.split(",") if t]
    tasks = [t for t in TASKS if not ids or t["id"] in ids]
    log(f"agent={agent.name} mode={a.mode} serial={a.serial} remote={a.remote} tasks={len(tasks)} repeat={a.repeat}")
    results, restores = [], {}
    for r in range(a.repeat):
        for task in tasks:
            tag = task["id"] + (f"_r{r}" if a.repeat > 1 else "")
            log(f"== {tag}: {task['instruction']}")
            rec, restore = run_task(env, agent, task, a.mode, a.max_steps, os.path.join(out, tag), log)
            restores[tag] = restore
            row = {k: rec.get(k) for k in ("task", "passed", "n_steps", "answer", "check_detail", "restore_ok", "faster",
                                           "error", "agent_error")}
            row.update(run=r, seconds=round(rec["t1"] - rec["t0"], 1),
                       shell_steps=sum(1 for s in rec["steps"] if s["action"].get("action") == "shell"),
                       gui_steps=sum(1 for s in rec["steps"] if s["action"].get("action") not in ("shell", "finish")))
            results.append(row)
            log(f"   -> {'PASS' if rec.get('passed') else 'FAIL'} ({rec.get('check_detail')}) restore_ok={rec['restore_ok']}")
            dump_json(os.path.join(out, "results.json"), {"agent": agent.name, "mode": a.mode, "serial": a.serial,
                                                          "results": results, "usage": getattr(agent, "usage", None),
                                                          "pfnode_jobs": getattr(transport, "jobs", None)})
            dump_json(os.path.join(out, "restore.json"), restores)
    n = len(results)
    p = sum(1 for x in results if x["passed"])
    with open(os.path.join(out, "results.md"), "w") as f:
        f.write(f"# {agent.name} / {a.mode} on {a.serial}: {p}/{n} passed\n\n| task | faster | pass | steps | shell | gui | s | restore |\n"
                "|---|---|---|---|---|---|---|---|\n")
        for x in results:
            f.write(f"| {x['task']} | {x['faster']} | {'Y' if x['passed'] else 'N'} | {x['n_steps']} | {x['shell_steps']} | "
                    f"{x['gui_steps']} | {x['seconds']} | {'ok' if x['restore_ok'] else 'CHECK'} |\n")
    log(f"DONE {p}/{n} passed; restore not ok: {[x['task'] for x in results if not x['restore_ok']]}")


if __name__ == "__main__":
    main()
