#!/usr/bin/env python3
"""Side-by-side pass table from several run.py output dirs.

    python3 phonecua/compare.py oracle=<dir> gui=<dir> hybrid=<dir>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tasks import TASKS  # noqa: E402


def main():
    runs = {}
    for arg in sys.argv[1:]:
        name, d = arg.split("=", 1)
        with open(os.path.join(d, "results.json")) as f:
            runs[name] = {r["task"]: r for r in json.load(f)["results"]}
    names = list(runs)
    print("| task | faster | " + " | ".join(f"{n} (steps)" for n in names) + " |")
    print("|---|---|" + "---|" * len(names))
    tot = {n: [0, 0] for n in names}
    by_faster = {}
    for t in TASKS:
        cells = []
        for n in names:
            r = runs[n].get(t["id"])
            if not r:
                cells.append("-")
                continue
            tot[n][0] += bool(r["passed"])
            tot[n][1] += 1
            by_faster.setdefault((n, t["faster"]), [0, 0])
            by_faster[(n, t["faster"])][0] += bool(r["passed"])
            by_faster[(n, t["faster"])][1] += 1
            mix = f"{r['n_steps']}" + (f", {r.get('shell_steps', 0)} sh" if r.get("shell_steps") else "")
            cells.append(f"{'PASS' if r['passed'] else 'fail'} ({mix})")
        print(f"| {t['id']} | {t['faster']} | " + " | ".join(cells) + " |")
    print("| **total** | | " + " | ".join(f"**{p}/{n}**" for p, n in tot.values()) + " |")
    for f in ("shell", "either", "gui"):
        print(f"| faster={f} | | " + " | ".join("{}/{}".format(*by_faster.get((n, f), [0, 0])) for n in names) + " |")


if __name__ == "__main__":
    main()
