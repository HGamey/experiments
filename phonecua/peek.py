#!/usr/bin/env python3
"""Developer tool: run a few actions on a phone and print the compact observation after each.

    python3 phonecua/peek.py <serial> [--remote NODE/DEVICE --pfnode ./pfn.sh] '{"action":"home"}' ...
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import PhoneEnv  # noqa: E402
from transport import LocalAdb, PfNodeAdb  # noqa: E402


def show(obs):
    print("FOCUS", obs["focus"])
    for e in obs["elements"]:
        print(f'  [{e["id"]}] {e["cls"]} "{e["text"]}" desc="{e["desc"]}" rid={e["rid"]} c={e["center"]} {e.get("flags", "")}'
              f'{" checked=" + str(e["checked"]) if "checked" in e else ""}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("serial")
    ap.add_argument("--remote")
    ap.add_argument("--pfnode")
    ap.add_argument("actions", nargs="*")
    a = ap.parse_args()
    t = PfNodeAdb(a.serial, a.remote, a.pfnode, os.path.abspath("pfout")) if a.remote else LocalAdb(a.serial)
    env = PhoneEnv(t, shots=False)
    if not a.actions:
        show(env.observe())
    for s in a.actions:
        act = json.loads(s)
        res, obs = env.step(act)
        print("RESULT", json.dumps(res, ensure_ascii=False)[:1500])
        if obs:
            show(obs)


if __name__ == "__main__":
    main()
