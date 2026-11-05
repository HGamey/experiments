"""End-to-end validation tests using a tiny synthetic formal training run."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
TRAIN_SCRIPT = ROOT / "doom3_sr" / "train_doom.py"
VALIDATE_SCRIPT = ROOT / "doom3_sr" / "validate_run.py"


class ValidateRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.train_data = cls.root / "train-hr"
        cls.test_data = cls.root / "test-hr"
        cls.train_data.mkdir()
        cls.test_data.mkdir()
        rng = np.random.default_rng(20261105)
        for directory, count, label in ((cls.train_data, 10, "train"), (cls.test_data, 3, "test")):
            for index in range(count):
                pixels = rng.integers(0, 256, (96, 128, 3), dtype=np.uint8)
                Image.fromarray(pixels).save(directory / f"{label}-{index}.png")
        cls.original_run = cls.root / "formal-run"
        environment = os.environ.copy()
        environment.update(PF_JOB_OUT=str(cls.original_run), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, str(TRAIN_SCRIPT), "--data", str(cls.train_data),
             "--test-data", str(cls.test_data), "--steps", "1", "--patch", "8",
             "--batch-size", "1", "--min-frame-mad", "0", "--seed", "17"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
            timeout=120, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"synthetic training failed:\n{result.stdout}\n{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.root)
        self.work = Path(self.temporary.name)
        self.run = self.work / "run"
        shutil.copytree(self.original_run, self.run)
        self.output = self.work / "validation.json"

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self):
        return subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "--run", str(self.run),
             "--data", str(self.train_data), "--test-data", str(self.test_data),
             "--output", str(self.output)],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
            env=os.environ.copy() | {"PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_tiny_synthetic_run_is_valid(self):
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["checks"])
        self.assertTrue(all(check["valid"] for check in report["checks"].values()))
        self.assertIn("heldout_predictions", report["checks"])
        self.assertIn("independently recomputed", report["checks"]["heldout_predictions"]["detail"])

    def test_tampered_metric_is_rejected(self):
        summary_path = self.run / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        selected = summary["selected_variant"]
        summary["variants"][selected]["frames"][0]["rgb_psnr_db"] += 0.5
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        result = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INVALID", result.stderr)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertFalse(report["valid"])
        self.assertFalse(report["checks"]["aggregate_metrics"]["valid"])

    def test_tampered_export_hash_is_rejected(self):
        manifest_path = self.run / "doom_small_sr.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["generated_inc_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        result = self.validate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INVALID", result.stderr)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertFalse(report["valid"])
        self.assertFalse(report["checks"]["export_hashes"]["valid"])


if __name__ == "__main__":
    unittest.main()
