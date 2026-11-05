"""Minimal NHWC 2x residual SR network implemented only with NumPy."""
import numpy as np


def im2col(x, k):
    b, h, w, c = x.shape
    padded = np.pad(x, ((0, 0), (k // 2, k // 2), (k // 2, k // 2), (0, 0)))
    windows = np.lib.stride_tricks.sliding_window_view(padded, (k, k), axis=(1, 2))
    return np.ascontiguousarray(windows.transpose(0, 1, 2, 4, 5, 3)).reshape(
        b * h * w, k * k * c
    )


def conv_forward(x, weight, bias, cache=True):
    k = weight.shape[0]
    b, h, w, _ = x.shape
    cols = im2col(x, k)
    out = (cols @ weight.reshape(-1, weight.shape[-1]) + bias).reshape(
        b, h, w, weight.shape[-1]
    )
    return out, (x.shape, cols, weight) if cache else None


def conv_backward(dout, cache):
    x_shape, cols, weight = cache
    b, h, w, c = x_shape
    k = weight.shape[0]
    flat = dout.reshape(-1, dout.shape[-1])
    dw = (cols.T @ flat).reshape(weight.shape)
    db = flat.sum(axis=0)
    dcols = (flat @ weight.reshape(-1, weight.shape[-1]).T).reshape(
        b, h, w, k, k, c
    )
    dxp = np.zeros((b, h + k - 1, w + k - 1, c), dtype=np.float32)
    for ky in range(k):
        for kx in range(k):
            dxp[:, ky : ky + h, kx : kx + w, :] += dcols[:, :, :, ky, kx, :]
    p = k // 2
    return dxp[:, p : p + h, p : p + w, :], dw, db


def depth_to_space(x):
    b, h, w, c = x.shape
    return x.reshape(b, h, w, 2, 2, 3).transpose(0, 1, 3, 2, 4, 5).reshape(
        b, h * 2, w * 2, 3
    )


def depth_to_space_backward(dout):
    b, h, w, _ = dout.shape
    return dout.reshape(b, h // 2, 2, w // 2, 2, 3).transpose(
        0, 1, 3, 2, 4, 5
    ).reshape(b, h // 2, w // 2, 12)


class SmallSR:
    def __init__(self, channels=16, residual=True, seed=20261106, input_channels=3):
        self.channels = channels
        self.residual = residual
        self.input_channels = input_channels
        rng = np.random.default_rng(seed)
        self.params = {
            "w1": (rng.standard_normal((5, 5, input_channels, channels)) * np.sqrt(2 / (25 * input_channels))).astype(np.float32),
            "b1": np.zeros(channels, dtype=np.float32),
            "w2": (rng.standard_normal((3, 3, channels, channels)) * np.sqrt(2 / (9 * channels))).astype(np.float32),
            "b2": np.zeros(channels, dtype=np.float32),
            "w3": (rng.standard_normal((3, 3, channels, 12)) * 0.001).astype(np.float32),
            "b3": np.zeros(12, dtype=np.float32),
        }

    def forward(self, lr, base, cache=True):
        x, c1 = conv_forward(lr, self.params["w1"], self.params["b1"], cache)
        x = np.maximum(x, 0)
        x, c2 = conv_forward(x, self.params["w2"], self.params["b2"], cache)
        x = np.maximum(x, 0)
        x, c3 = conv_forward(x, self.params["w3"], self.params["b3"], cache)
        residual = depth_to_space(x)
        pred = base + residual if self.residual else residual
        return pred, (c1, c2, c3)

    def train_step(self, lr, base, target, optimizer):
        # Keep pre-activation arrays for exact ReLU masks.
        z1, c1 = conv_forward(lr, self.params["w1"], self.params["b1"], True)
        a1 = np.maximum(z1, 0)
        z2, c2 = conv_forward(a1, self.params["w2"], self.params["b2"], True)
        a2 = np.maximum(z2, 0)
        z3, c3 = conv_forward(a2, self.params["w3"], self.params["b3"], True)
        pred = depth_to_space(z3)
        if self.residual:
            pred = pred + base
        error = pred - target
        loss = float(np.mean(np.abs(error)))
        dout = (np.sign(error) / error.size).astype(np.float32)
        dz3 = depth_to_space_backward(dout)
        da2, dw3, db3 = conv_backward(dz3, c3)
        dz2 = da2 * (z2 > 0)
        da1, dw2, db2 = conv_backward(dz2, c2)
        dz1 = da1 * (z1 > 0)
        _, dw1, db1 = conv_backward(dz1, c1)
        grads = {"w1": dw1, "b1": db1, "w2": dw2, "b2": db2, "w3": dw3, "b3": db3}
        optimizer.step(self.params, grads)
        return loss

    def predict(self, lr, base):
        z1, _ = conv_forward(lr, self.params["w1"], self.params["b1"], False)
        a1 = np.maximum(z1, 0)
        z2, _ = conv_forward(a1, self.params["w2"], self.params["b2"], False)
        a2 = np.maximum(z2, 0)
        z3, _ = conv_forward(a2, self.params["w3"], self.params["b3"], False)
        pred = depth_to_space(z3)
        return pred + base if self.residual else pred

    def save(self, path):
        np.savez(path, **self.params)

    @classmethod
    def load(cls, weights_path, structure):
        model = cls(
            channels=structure["channels"],
            residual=structure["bicubic_residual"],
            input_channels=structure.get("input_channels", 3),
        )
        with np.load(weights_path, allow_pickle=False) as weights:
            expected = set(model.params)
            if set(weights.files) != expected:
                raise ValueError(f"weight keys {weights.files} do not match {sorted(expected)}")
            for key in expected:
                if weights[key].shape != model.params[key].shape:
                    raise ValueError(f"shape mismatch for {key}: {weights[key].shape}")
                model.params[key] = weights[key].astype(np.float32)
        return model

    def structure(self):
        c = self.channels
        i = self.input_channels
        conv1_parameters = 25 * i * c + c
        conv1_macs = 25 * i * c
        total_macs = conv1_macs + 9 * c * c + 108 * c
        layers = [
            {"name": "conv1", "operator": "Conv2D same", "kernel": [5, 5], "input_channels": i, "output_channels": c, "parameters": conv1_parameters, "macs_per_lr_pixel": conv1_macs},
            {"name": "relu1", "operator": "ReLU", "channels": c, "parameters": 0, "macs_per_lr_pixel": 0},
            {"name": "conv2", "operator": "Conv2D same", "kernel": [3, 3], "input_channels": c, "output_channels": c, "parameters": 9 * c * c + c, "macs_per_lr_pixel": 9 * c * c},
            {"name": "relu2", "operator": "ReLU", "channels": c, "parameters": 0, "macs_per_lr_pixel": 0},
            {"name": "conv3", "operator": "Conv2D same", "kernel": [3, 3], "input_channels": c, "output_channels": 12, "parameters": 9 * c * 12 + 12, "macs_per_lr_pixel": 108 * c},
            {"name": "upscale", "operator": "depth_to_space", "scale": 2, "input_channels": 12, "output_channels": 3, "parameters": 0, "macs_per_lr_pixel": 0},
        ]
        if self.residual:
            layers.append({"name": "residual", "operator": "Add", "other_input": "Pillow bicubic 2x", "parameters": 0, "macs_per_lr_pixel": 0})
        return {
            "layout": "NHWC",
            "scale": 2,
            "input_channels": i,
            "channels": c,
            "bicubic_residual": self.residual,
            "layers": layers,
            "parameters": sum(int(x.size) for x in self.params.values()),
            "macs_per_lr_pixel": total_macs,
            "macs_540p_frame": total_macs * 960 * 540,
            "float32_weight_bytes": sum(int(x.nbytes) for x in self.params.values()),
        }


class Adam:
    def __init__(self, params, lr=0.001):
        self.lr = lr
        self.t = 0
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}

    def step(self, params, grads):
        self.t += 1
        for key in params:
            grad = np.clip(grads[key], -1.0, 1.0)
            self.m[key] = 0.9 * self.m[key] + 0.1 * grad
            self.v[key] = 0.999 * self.v[key] + 0.001 * grad * grad
            mh = self.m[key] / (1 - 0.9**self.t)
            vh = self.v[key] / (1 - 0.999**self.t)
            params[key] -= self.lr * mh / (np.sqrt(vh) + 1e-8)
