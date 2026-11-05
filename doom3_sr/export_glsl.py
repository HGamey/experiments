#!/usr/bin/env python3
"""Export SmallSR weights as a GLSL ES 3.x include and a JSON manifest.

Outputs ``<output-prefix>.inc`` and ``<output-prefix>.manifest.json``.  The
include contains weights only; the shader must provide the bicubic residual.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

# Support both ``python doom3_sr/export_glsl.py`` and module imports.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qrisp_sr.numpy_sr import SmallSR  # noqa: E402


ARRAYS = (("W1", "w1"), ("B1", "b1"), ("W2", "w2"), ("B2", "b2"),
          ("W3", "w3"), ("B3", "b3"))


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _float_literal(value):
    """Nine significant digits round-trip every finite float32 value."""
    text = format(float(value), ".9g")
    if "e" in text:
        mantissa, exponent = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        return mantissa + "e" + exponent
    return text if "." in text else text + ".0"


def _array_definition(name, values):
    literals = [_float_literal(value) for value in values]
    lines = [", ".join(literals[start:start + 4])
             for start in range(0, len(literals), 4)]
    body = ",\n".join(lines)
    size = len(literals)
    return f"const float {name}[{size}] = float[{size}](\n{body}\n);\n"


def _glsl(model):
    pieces = [
        "// SmallSR weights; include from a GLSL ES 3.x shader.\n"
        "// Weights are flattened in NumPy C-order HWIO. Biases use [co].\n"
        "// Weight index: (((ky * K + kx) * Cin + ci) * Cout + co).\n"
        "#define W1_INDEX(ky, kx, ci, co) ((((ky) * 5 + (kx)) * 3 + (ci)) * 16 + (co))\n"
        "#define W2_INDEX(ky, kx, ci, co) ((((ky) * 3 + (kx)) * 16 + (ci)) * 16 + (co))\n"
        "#define W3_INDEX(ky, kx, ci, co) ((((ky) * 3 + (kx)) * 16 + (ci)) * 12 + (co))\n"
        "// Use W1[W1_INDEX(...)], B1[co], and analogously for layers 2/3.\n"
        "// Apply ReLU after conv1 and conv2, but not after conv3.\n"
        "// Pixel shuffle: conv3 channel = (sub_y * 2 + sub_x) * 3 + rgb;\n"
        "// sub_y/sub_x are in {0,1}, and HR xy = LR xy * 2 + (sub_x,sub_y).\n"
        "// No bicubic residual is implemented here; add the caller-provided base.\n\n"
    ]
    pieces.extend(_array_definition(glsl_name, model.params[npz_name].ravel(order="C"))
                  for glsl_name, npz_name in ARRAYS)
    return "".join(pieces).encode("utf-8")


def export(model_path, structure_path, output_prefix):
    """Validate and export a SmallSR NPZ, returning (INC, manifest) paths."""
    model_path = Path(model_path)
    structure_path = Path(structure_path)
    prefix = Path(output_prefix)
    inc_path = Path(str(prefix) + ".inc")
    manifest_path = Path(str(prefix) + ".manifest.json")

    with structure_path.open("r", encoding="utf-8") as source:
        structure = json.load(source)
    if not isinstance(structure, dict):
        raise ValueError("structure JSON must contain an object")
    if structure.get("input_channels", 3) != 3:
        raise ValueError("SmallSR export requires input_channels == 3")
    if structure.get("scale") != 2:
        raise ValueError("SmallSR export requires scale == 2")
    if structure.get("bicubic_residual") is not True:
        raise ValueError("SmallSR export requires bicubic_residual == true")
    if structure.get("channels") != 16:
        raise ValueError("SmallSR export requires channels == 16")

    model = SmallSR.load(model_path, structure)
    for name, values in model.params.items():
        if not np.isfinite(values).all():
            raise ValueError(f"{name} contains non-finite values")

    source_sha256 = _sha256(model_path)
    inc_bytes = _glsl(model)
    arrays = {}
    for glsl_name, npz_name in ARRAYS:
        values = model.params[npz_name]
        arrays[glsl_name] = {
            "npz_key": npz_name,
            "shape": list(values.shape),
            "length": int(values.size),
            "float32_bytes": int(values.nbytes),
        }
    manifest = {
        "format": "doom-small-sr-glsl-v1",
        "layout": "NHWC",
        "weight_order": "HWIO",
        "flattening_order": "C",
        "scale": 2,
        "input_channels": 3,
        "channels": 16,
        "bicubic_residual": True,
        "arrays": arrays,
        "float32_weight_bytes": sum(item["float32_bytes"] for item in arrays.values()),
        "source_npz_sha256": source_sha256,
        "generated_inc_sha256": hashlib.sha256(inc_bytes).hexdigest(),
    }

    inc_path.parent.mkdir(parents=True, exist_ok=True)
    inc_path.write_bytes(inc_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return inc_path, manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="SmallSR NPZ weights")
    parser.add_argument("--structure", required=True, help="SmallSR structure JSON")
    parser.add_argument("--output-prefix", required=True,
                        help="output path prefix; appends .inc and .manifest.json")
    args = parser.parse_args(argv)
    try:
        inc_path, manifest_path = export(args.model, args.structure, args.output_prefix)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(inc_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
