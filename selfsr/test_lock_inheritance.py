import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class LockInheritanceTests(unittest.TestCase):
    def test_phonefarm_calls_receive_the_actual_outer_fd_without_a_bypass_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "device.lock").open("w") as lock:
                environment = {"PF_LOCK_FDS": str(lock.fileno()), "PF_LOCK_SCOPE": '{"key":"A"}'}
                with patch.dict(os.environ, environment):
                    spec = importlib.util.spec_from_file_location("selfsr_fd_fixture", Path(__file__).with_name("selfsr_ab.py"))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    pf = root / "phonefarm"
                    pf.write_text(f"#!{sys.executable}\n" + "import json,os,sys\n"
                                  "assert '--no-devlock' not in sys.argv\n"
                                  "fds=[int(fd) for fd in os.environ['PF_LOCK_FDS'].split(',')]\n"
                                  "for fd in fds: os.fstat(fd)\n"
                                  "print(json.dumps({'fds':fds}))\n")
                    pf.chmod(0o755)
                    for entry in ("pf_json", "layered_onboard"):
                        with self.subTest(entry=entry):
                            if entry == "pf_json":
                                code, value = module.pf_json(str(pf), "probe")
                                self.assertEqual(code, 0, value)
                                self.assertEqual(value["fds"], [lock.fileno()])
                            else:
                                args = SimpleNamespace(enter_script=None, phonefarm=str(pf), pkg="fixture", serial="A")
                                code, diagnostic = module.onboard(args, str(root), "layer", cold=False)
                                self.assertEqual(code, 0, diagnostic)

    def test_standalone_entrypoints_acquire_before_any_device_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pf = root / "phonefarm"
            pf.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            pf.chmod(0o755)
            environment = {key:value for key,value in os.environ.items() if key not in ("PF_LOCK_FDS", "PF_LOCK_SCOPE")}
            environment["PHONEFARM_BIN"] = str(pf)
            for command in (
                [sys.executable, "-I", str(Path(__file__).with_name("selfsr_ab.py")), "run", "--serial", "A", "--out", str(root/'out'), "--phonefarm", str(pf)],
                ["sh", str(Path(__file__).with_name("panel_probe.sh")), "A", str(root/'out')],
            ):
                with self.subTest(command=command[0]):
                    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads(result.stdout)
                    self.assertEqual(argv[:3], ["lease", "--serial", "A"])
                    self.assertIn("--", argv)
                    self.assertFalse((root/'out').exists(), "acquire precedes even the output directory")


if __name__ == "__main__":
    unittest.main()
