#!/usr/bin/env python3
"""Probe the host training runtime without changing the host or using a phone.

The probe itself imports only the Python standard library. If TensorFlow is
already visible, it runs one bounded synthetic 2x residual-SR gradient step;
this is a mechanism check, never Doom3 data or a quality result.
"""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
import time


def main():
    started = time.perf_counter()
    report = {
        "schema": "hgamey/doom3-training-runtime-probe@1",
        "host": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "python_executable": sys.executable,
        "experiment_commit": os.environ.get("PF_EXPERIMENT_COMMIT"),
        "modules": {
            name: importlib.util.find_spec(name) is not None
            for name in ("tensorflow", "numpy", "PIL", "h5py")
        },
        "synthetic_mechanism": "not_run",
        "doom3_data_used": False,
        "quality_claim": False,
    }
    if report["modules"]["tensorflow"]:
        try:
            t0 = time.perf_counter()
            tf = importlib.import_module("tensorflow")
            report["tensorflow_version"] = tf.__version__
            report["tensorflow_import_s"] = round(time.perf_counter() - t0, 3)
            tf.config.set_visible_devices([], "GPU")
            tf.random.set_seed(23)
            inp = tf.keras.Input((8, 8, 3))
            x = tf.keras.layers.Conv2D(4, 3, padding="same", activation="relu")(inp)
            x = tf.keras.layers.Conv2D(12, 3, padding="same")(x)
            x = tf.keras.layers.Lambda(lambda t: tf.nn.depth_to_space(t, 2))(x)
            up = tf.keras.layers.UpSampling2D(2, interpolation="bilinear")(inp)
            model = tf.keras.Model(inp, tf.keras.layers.Add()([up, x]))
            lr = tf.random.uniform((1, 8, 8, 3), seed=23)
            hr = tf.image.resize(lr, (16, 16), method="bilinear")
            with tf.GradientTape() as tape:
                pred = model(lr, training=True)
                loss = tf.reduce_mean(tf.abs(pred - hr))
            grads = tape.gradient(loss, model.trainable_variables)
            grad_norm = float(tf.linalg.global_norm(grads).numpy())
            if not grads or grad_norm <= 0:
                raise RuntimeError(f"invalid synthetic gradients: norm={grad_norm}")
            tf.keras.optimizers.Adam(1e-3).apply_gradients(zip(grads, model.trainable_variables))
            report.update(
                synthetic_mechanism="passed",
                synthetic_loss=float(loss.numpy()),
                synthetic_gradient_norm=grad_norm,
                parameters=int(model.count_params()),
                operators=["Conv2D", "ReLU", "depth_to_space", "bilinear_resize", "Add"],
            )
        except Exception as exc:
            report["synthetic_mechanism"] = "failed"
            report["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    out = Path(os.environ.get("PF_JOB_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    report["wall_s"] = round(time.perf_counter() - started, 3)
    path = out / "training-runtime.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
