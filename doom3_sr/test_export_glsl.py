"""Focused tests for the SmallSR GLSL exporter; all weights are synthetic."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from doom3_sr.export_glsl import ARRAYS, export
from qrisp_sr.numpy_sr import SmallSR


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("export_glsl.py")
SHAPES = {
    "w1": (5, 5, 3, 16), "b1": (16,),
    "w2": (3, 3, 16, 16), "b2": (16,),
    "w3": (3, 3, 16, 12), "b3": (12,),
}
FLOAT_LITERAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?")


def synthetic_weights():
    """Unique, non-integer float32 values expose layout and precision errors."""
    return {
        name: ((np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
                + np.float32(0.25)) * np.float32(0.001))
        for name, shape in SHAPES.items()
    }


def synthetic_structure(**changes):
    structure = {
        "layout": "NHWC", "scale": 2, "input_channels": 3,
        "channels": 16, "bicubic_residual": True,
    }
    structure.update(changes)
    return structure


def parse_array(inc, name):
    match = re.search(
        rf"const float {name}\[(\d+)\] = float\[(\d+)\]\(\n(.*?)\n\);",
        inc, re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing or malformed array {name}")
    assert match.group(1) == match.group(2)
    tokens = [token.strip() for token in match.group(3).split(",")]
    for token in tokens:
        if FLOAT_LITERAL.fullmatch(token) is None or "." not in token.split("e")[0]:
            raise AssertionError(f"not a decimal GLSL float literal: {token!r}")
    values = np.array([np.float32(token) for token in tokens], dtype=np.float32)
    return int(match.group(1)), values


class ExportGLSLTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model = self.root / "synthetic.npz"
        self.structure = self.root / "structure.json"
        np.savez(self.model, **synthetic_weights())
        self.structure.write_text(json.dumps(synthetic_structure()), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_cli_literals_layout_manifest_and_hashes(self):
        prefix = self.root / "small_sr"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--model", str(self.model),
             "--structure", str(self.structure), "--output-prefix", str(prefix)],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        inc_path = Path(str(prefix) + ".inc")
        manifest_path = Path(str(prefix) + ".manifest.json")
        self.assertEqual(result.stdout.splitlines(), [str(inc_path), str(manifest_path)])

        source = synthetic_weights()
        inc_bytes = inc_path.read_bytes()
        inc = inc_bytes.decode("utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["layout"], "NHWC")
        self.assertEqual(manifest["weight_order"], "HWIO")
        self.assertEqual(manifest["flattening_order"], "C")
        self.assertEqual(manifest["source_npz_sha256"],
                         hashlib.sha256(self.model.read_bytes()).hexdigest())
        self.assertEqual(manifest["generated_inc_sha256"],
                         hashlib.sha256(inc_bytes).hexdigest())

        total_bytes = 0
        for glsl_name, npz_name in ARRAYS:
            with self.subTest(array=glsl_name):
                declared_size, actual = parse_array(inc, glsl_name)
                expected = source[npz_name]
                self.assertEqual(declared_size, expected.size)
                np.testing.assert_array_equal(actual, expected.ravel(order="C"))
                entry = manifest["arrays"][glsl_name]
                self.assertEqual(entry["shape"], list(expected.shape))
                self.assertEqual(entry["length"], expected.size)
                self.assertEqual(entry["float32_bytes"], expected.nbytes)
                total_bytes += expected.nbytes
        self.assertEqual(manifest["float32_weight_bytes"], total_bytes)

        _, w1 = parse_array(inc, "W1")
        index = (((4 * 5 + 4) * 3 + 2) * 16 + 15)
        self.assertEqual(w1[index], source["w1"][4, 4, 2, 15])
        self.assertIn("(sub_y * 2 + sub_x) * 3 + rgb", inc)
        self.assertIn("No bicubic residual is implemented here", inc)

    def test_scalar_glsl_semantics_match_smallsr_predict(self):
        weights = synthetic_weights()
        for name, values in weights.items():
            signs = np.where(np.arange(values.size) % 2 == 0, 1.0, -1.0)
            weights[name] = values * signs.astype(np.float32).reshape(values.shape)
        np.savez(self.model, **weights)
        inc_path, _ = export(self.model, self.structure, self.root / "semantic")
        inc = inc_path.read_text(encoding="utf-8")
        arrays = {npz_name: parse_array(inc, glsl_name)[1]
                  for glsl_name, npz_name in ARRAYS}

        def scalar_conv(x, weight, bias, kernel, output_channels):
            """Independent scalar loops using the INC's documented HWIO index."""
            height, width, input_channels = x.shape
            result = np.zeros((height, width, output_channels), dtype=np.float64)
            for y in range(height):
                for x_pos in range(width):
                    for co in range(output_channels):
                        value = float(bias[co])
                        for ky in range(kernel):
                            for kx in range(kernel):
                                source_y = y + ky - kernel // 2
                                source_x = x_pos + kx - kernel // 2
                                if not (0 <= source_y < height and 0 <= source_x < width):
                                    continue  # Same zero padding as SmallSR.
                                for ci in range(input_channels):
                                    index = (((ky * kernel + kx) * input_channels + ci)
                                             * output_channels + co)
                                    value += (float(x[source_y, source_x, ci])
                                              * float(weight[index]))
                        result[y, x_pos, co] = value
            return result

        lr = np.linspace(-0.7, 0.9, 18, dtype=np.float32).reshape(1, 2, 3, 3)
        hidden1 = scalar_conv(lr[0], arrays["w1"], arrays["b1"], 5, 16)
        hidden1[hidden1 < 0.0] = 0.0
        hidden2 = scalar_conv(hidden1, arrays["w2"], arrays["b2"], 3, 16)
        hidden2[hidden2 < 0.0] = 0.0
        conv3 = scalar_conv(hidden2, arrays["w3"], arrays["b3"], 3, 12)

        # A smooth, non-identity stand-in for the caller's bicubic 2x base.
        base = np.empty((1, 4, 6, 3), dtype=np.float32)
        for y in range(4):
            for x_pos in range(6):
                for rgb in range(3):
                    base[0, y, x_pos, rgb] = (
                        0.1 + 0.02 * x_pos - 0.01 * y + 0.001 * x_pos * x_pos
                        - 0.0005 * y * y + 0.0002 * x_pos * y + 0.03 * rgb
                    )
        actual = base[0].astype(np.float64)
        for y in range(2):
            for x_pos in range(3):
                for sub_y in range(2):
                    for sub_x in range(2):
                        for rgb in range(3):
                            channel = (sub_y * 2 + sub_x) * 3 + rgb
                            actual[y * 2 + sub_y, x_pos * 2 + sub_x, rgb] += conv3[
                                y, x_pos, channel
                            ]

        model = SmallSR.load(self.model, synthetic_structure())
        expected = model.predict(lr, base)[0]
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5)

    def test_rejects_unsupported_structures(self):
        cases = (
            {"input_channels": 1}, {"scale": 3},
            {"channels": 8}, {"bicubic_residual": False},
        )
        for number, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.structure.write_text(json.dumps(synthetic_structure(**changes)),
                                          encoding="utf-8")
                prefix = self.root / f"invalid_structure_{number}"
                with self.assertRaises(ValueError):
                    export(self.model, self.structure, prefix)
                self.assertFalse(Path(str(prefix) + ".inc").exists())
                self.assertFalse(Path(str(prefix) + ".manifest.json").exists())

    def test_rejects_nonfinite_values(self):
        for key, value in (("w2", np.nan), ("b3", np.inf)):
            with self.subTest(key=key, value=value):
                weights = synthetic_weights()
                weights[key].flat[0] = value
                np.savez(self.model, **weights)
                prefix = self.root / "nonfinite"
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    export(self.model, self.structure, prefix)
                self.assertFalse(Path(str(prefix) + ".inc").exists())

    def test_rejects_bad_npz_keys_and_shapes(self):
        weights = synthetic_weights()
        del weights["b2"]
        np.savez(self.model, **weights)
        with self.assertRaisesRegex(ValueError, "weight keys"):
            export(self.model, self.structure, self.root / "bad_keys")

        weights = synthetic_weights()
        weights["w1"] = np.zeros(3, dtype=np.float32)
        np.savez(self.model, **weights)
        with self.assertRaisesRegex(ValueError, "shape mismatch"):
            export(self.model, self.structure, self.root / "bad_shape")


if __name__ == "__main__":
    unittest.main()
