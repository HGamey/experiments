"""Synthetic-image tests for focused device screenshot comparisons."""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

from doom3_sr.compare_device import METRIC_KEYS, compare


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("compare_device.py")


def constant_quality(native, other):
    """Closed-form QRISP metrics for two constant RGB images."""
    mse = (native - other) ** 2
    c1 = (0.01 * 255) ** 2
    return {
        "rgb_psnr_db": 10 * math.log10(255.0**2 / mse),
        "rgb_ssim_11x11": (2 * native * other + c1) / (native**2 + other**2 + c1),
    }


class CompareDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        self.temporary.cleanup()

    def add_item(self, name, values, size=(32, 24), **extra):
        directory = self.root / name
        directory.mkdir()
        item = {}
        for source, value in zip(("native", "bicubic", "sr"), values):
            relative = f"{name}/{source}.png"
            array = np.full((size[1], size[0], 3), value, dtype=np.uint8)
            Image.fromarray(array).save(self.root / relative)
            item[source] = relative
        item.update(extra)
        return item

    def write_manifest(self, items):
        self.manifest.write_text(json.dumps({"items": items}), encoding="utf-8")

    def assert_metric_close(self, key, actual, expected):
        # Constant-image SSIM has slight float32 cancellation in gaussian_blur.
        tolerance = 1e-6 if key == "rgb_psnr_db" else 5e-4
        self.assertAlmostEqual(actual, expected, delta=tolerance)

    def assert_metrics_close(self, actual, expected):
        for key in METRIC_KEYS:
            self.assert_metric_close(key, actual[key], expected[key])

    def test_cli_numeric_summary_sheets_and_fps(self):
        specifications = [("one", (100, 90, 105)), ("two", (120, 116, 118))]
        items = [self.add_item(name, values, label=name) for name, values in specifications]
        self.write_manifest(items)
        out = self.root / "out"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--manifest", str(self.manifest),
             "--out", str(out), "--fps-native", "60", "--fps-bicubic", "40",
             "--fps-sr", "50", "--fps-low1-native", "55", "--fps-low1-bicubic", "25",
             "--fps-low1-sr", "30", "--full-thumbnails"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue((out / "contact_sheet.png").is_file())
        self.assertTrue((out / "contact_sheet_full.png").is_file())
        self.assertEqual(summary["sheets"],
                         {"crops": "contact_sheet.png", "full": "contact_sheet_full.png"})

        expected_items = []
        for index, (_, (native, bicubic, sr)) in enumerate(specifications):
            item = summary["items"][index]
            bicubic_quality = constant_quality(native, bicubic)
            sr_quality = constant_quality(native, sr)
            self.assert_metrics_close(item["bicubic_vs_native"], bicubic_quality)
            self.assert_metrics_close(item["sr_vs_native"], sr_quality)
            for key in METRIC_KEYS:
                self.assert_metric_close(key, item["sr_minus_bicubic"][key],
                                         sr_quality[key] - bicubic_quality[key])
            expected_items.append((bicubic_quality, sr_quality))
        for comparison, position in (("bicubic_vs_native", 0), ("sr_vs_native", 1)):
            for key in METRIC_KEYS:
                expected = np.mean([qualities[position][key] for qualities in expected_items])
                self.assert_metric_close(key, summary["mean"][comparison][key], expected)
        for key in METRIC_KEYS:
            expected = np.mean([sr[key] - bicubic[key]
                                for bicubic, sr in expected_items])
            self.assert_metric_close(key, summary["mean"]["sr_minus_bicubic"][key], expected)
        self.assertEqual(summary["fps"], {
            "native": 60.0, "bicubic": 40.0, "sr": 50.0,
            "sr_minus_bicubic": 10.0, "sr_over_bicubic": 1.25,
            "low1": {
                "native": 55.0, "bicubic": 25.0, "sr": 30.0,
                "sr_minus_bicubic": 5.0, "sr_over_bicubic": 1.2,
            },
        })

    def test_custom_and_default_centered_crops_and_optional_native_fps(self):
        item = self.add_item("frame", (100, 90, 105), size=(300, 300),
                             crop=[3, 4, 16, 12])
        self.write_manifest([item])
        custom = compare(self.manifest, self.root / "custom", fps_bicubic=40, fps_sr=50)
        self.assertEqual(custom["items"][0]["crop"], [3, 4, 16, 12])
        self.assertNotIn("native", custom["fps"])
        self.assertEqual(custom["fps"]["sr_minus_bicubic"], 10.0)
        self.assertEqual(custom["fps"]["sr_over_bicubic"], 1.25)
        with Image.open(self.root / "custom" / "contact_sheet.png") as sheet:
            self.assertEqual(sheet.size, (48, 56))  # Three unscaled crops plus labels.
        self.assertFalse((self.root / "custom" / "contact_sheet_full.png").exists())

        del item["crop"]
        self.write_manifest([item])
        default = compare(self.manifest, self.root / "default")
        self.assertEqual(default["items"][0]["crop"], [22, 22, 256, 256])
        self.assertEqual(default["fps"], {})
        with Image.open(self.root / "default" / "contact_sheet.png") as sheet:
            self.assertEqual(sheet.size, (768, 300))

    def test_rejects_low1_without_matching_mean(self):
        item = self.add_item("frame", (100, 90, 105))
        self.write_manifest([item])
        with self.assertRaisesRegex(ValueError, "requires the corresponding mean"):
            compare(self.manifest, self.root / "bad_low1", fps_low1_sr=30)

    def test_rejects_crop_errors(self):
        item = self.add_item("frame", (100, 90, 105), size=(32, 24))
        bad_crops = (
            [-1, 0, 8, 8], [0, 0, 0, 8], [30, 20, 4, 4],
            [0, 0, 33, 24], [0, 0, 8], [0, 0, 8.5, 8],
        )
        for number, crop in enumerate(bad_crops):
            with self.subTest(crop=crop):
                self.write_manifest([item | {"crop": crop}])
                with self.assertRaisesRegex(ValueError, "crop"):
                    compare(self.manifest, self.root / f"bad_crop_{number}")

    def test_rejects_escaping_absolute_and_empty_manifests(self):
        item = self.add_item("frame", (100, 90, 105))
        for key, value in (("native", "../escape.png"),
                           ("bicubic", str(self.root / "frame" / "bicubic.png"))):
            with self.subTest(path=value):
                self.write_manifest([item | {key: value}])
                with self.assertRaisesRegex(ValueError, "relative|escapes"):
                    compare(self.manifest, self.root / "bad_path")
        for document in ({}, {"items": []}, []):
            with self.subTest(document=document):
                self.manifest.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "items|at least one"):
                    compare(self.manifest, self.root / "empty")

    def test_rejects_mismatched_dimensions(self):
        item = self.add_item("frame", (100, 90, 105), size=(32, 24))
        Image.fromarray(np.zeros((24, 30, 3), dtype=np.uint8)).save(
            self.root / item["sr"])
        self.write_manifest([item])
        with self.assertRaisesRegex(ValueError, "dimensions must match"):
            compare(self.manifest, self.root / "mismatch")


if __name__ == "__main__":
    unittest.main()
