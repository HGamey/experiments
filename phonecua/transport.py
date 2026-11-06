"""How shell scripts reach the phone.

LocalAdb  - the phone is attached to this machine (the normal case on the execution node).
PfNodeAdb - the phone sits behind an HGamey node; every call is one `pf-node adb -s <serial> shell <script>` job,
            routed with --device <node>/<device> and locked with --resource device:<serial>. Used when the agent
            must run elsewhere (e.g. it needs credentials that must not be shipped to the node).

Both expose sh(script, timeout) -> (stdout, rc) and pull(remote, local) -> bool.
"""
import os
import shutil
import subprocess
import time


def adb_bin():
    for c in (shutil.which("adb"), "/opt/homebrew/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")):
        if c and os.path.exists(c):
            return c
    raise SystemExit("adb not found")


def lock_fds():
    """Lock fds handed down by `phonefarm lease`; children keep them so the device lock lives for the whole run."""
    return tuple(int(fd) for fd in os.environ.get("PF_LOCK_FDS", "").split(",") if fd)


class LocalAdb:
    remote = False

    def __init__(self, serial):
        self.serial = serial
        self.adb = adb_bin()

    def _run(self, args, timeout):
        p = subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, pass_fds=lock_fds(), errors="replace")
        return p.stdout + (p.stderr if p.returncode else ""), p.returncode

    def sh(self, script, timeout=60):
        return self._run(["shell", script], timeout)

    def pull(self, remote, local):
        _, rc = self._run(["pull", remote, local], 60)
        return rc == 0


class PfNodeAdb:
    remote = True

    def __init__(self, serial, device_ref, pfnode, workdir, research=None, priority=0):
        self.serial, self.device_ref, self.pfnode = serial, device_ref, pfnode
        self.priority = priority  # >0 lets the next step of a running episode go before other queued jobs
        self.workdir = workdir  # pf-node downloads each job's output dir here
        self.research = research
        self.jobs = []
        os.makedirs(workdir, exist_ok=True)

    def sh(self, script, timeout=120):
        cmd = [self.pfnode, "--device", self.device_ref, "--device-access", "write", "--resource", f"device:{self.serial}",
               "--max-runtime-seconds", str(max(60, timeout))]
        if self.research:
            cmd += ["--research", self.research]
        if self.priority:
            cmd += ["--priority", str(self.priority)]
        cmd += ["adb", "-s", self.serial, "shell", script]
        for attempt in range(3):
            # no client timeout: a step may wait in the queue while another job holds the phone
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=self.workdir,
                               stdin=subprocess.DEVNULL, errors="replace")
            job = None
            for line in p.stderr.splitlines():
                if "job=" in line and "提交已记录" in line:
                    job = line.split("job=")[1].split()[0]
            if job:
                self.jobs.append(job)
            out, rc = p.stdout, p.returncode
            # the client can lose its connection while the job keeps running: re-attach instead of resubmitting
            for _ in range(4):
                if not job or "@@CUA:DONE" in out:
                    break
                time.sleep(5)
                w = subprocess.run([self.pfnode, "wait", job], capture_output=True, text=True, cwd=self.workdir, stdin=subprocess.DEVNULL, errors="replace")
                out, rc = w.stdout, w.returncode
            if job or "@@CUA:DONE" in out:
                return out, rc
            time.sleep(10)  # not submitted at all: safe to retry
        return out, rc
