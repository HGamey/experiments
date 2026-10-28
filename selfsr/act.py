#!/usr/bin/env python3
"""act.py — 给已在跑的 gray-layer session 发一个动作并等回执。
   python3 selfsr/act.py <session_out> <action> ['{"params":...}']
打印回执 JSON。"""
import json, os, subprocess, sys, time, uuid
PF = os.environ.get("PHONEFARM_BIN") or os.path.join(os.environ.get("PF_ROOT") or os.path.join(os.path.dirname(__file__), "..", ".."), "phonefarm")
out, action = sys.argv[1], sys.argv[2]
params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
def pf(*a, inp=None):
    # 与 selfsr_ab.py 一样按独立会话说话 (平台任务里的 PF_JOB_* 不传给 gray-layer)
    env = {k: v for k, v in os.environ.items() if k not in ("PF_JOB_ID", "PF_JOB_OUT", "PF_JOB_LEASE_DIR")}
    r = subprocess.run([PF, "gray-layer", *a], input=inp, capture_output=True, text=True, env=env)
    try: return r.returncode, json.loads(r.stdout)
    except Exception: return r.returncode, {"raw": r.stdout, "err": r.stderr}
_, d = pf("session-status", "--out", out, "--json")
c = d["controller"]
rid = f"selfsr-{action}-{uuid.uuid4().hex[:8]}"
req = {"schema": "phonefarm/gray-control-action@1", "request_id": rid, "session_id": d["session_id"],
       "actor_id": c["actor_id"], "control_epoch": c["epoch"], "action": action, "params": params}
code, rec = pf("session-action", "--out", out, "--request", "-", "--json", inp=json.dumps(req))
for _ in range(600):
    if rec.get("status") in ("completed", "failed", "rejected", "unknown") or code not in (0,):
        break
    time.sleep(1); code, rec = pf("session-status", "--out", out, "--request-id", rid, "--json")
print(json.dumps(rec, ensure_ascii=False))
