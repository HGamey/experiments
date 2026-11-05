"""QRISP motion-vector alignment implemented with NumPy."""
import numpy as np


def bilinear_sample(image, x, y):
    """Sample NHWC or HWC image at floating HW coordinates; zero outside."""
    if image.ndim == 2:
        image = image[:, :, None]
    height, width = image.shape[:2]
    valid = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (y >= 0) & (x <= width - 1) & (y <= height - 1)
    x = np.where(valid, x, 0).astype(np.float32)
    y = np.where(valid, y, 0).astype(np.float32)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    wx = x - x0
    wy = y - y0
    wa = (1 - wx) * (1 - wy)
    wb = wx * (1 - wy)
    wc = (1 - wx) * wy
    wd = wx * wy
    output = (
        image[y0, x0].astype(np.float32) * wa[..., None]
        + image[y0, x1].astype(np.float32) * wb[..., None]
        + image[y1, x0].astype(np.float32) * wc[..., None]
        + image[y1, x1].astype(np.float32) * wd[..., None]
    )
    return (output * valid[..., None]).astype(np.float32), valid


def align_previous(previous_lr, motion_rg, scale=-1.0):
    """Align previous LR to current LR using normalized QRISP motion R/G.

    QRISP's inspected non-jittered pair validates
    current_pixel - motion_rg * (width, height), hence the default scale -1.
    The returned validity mask is false for missing samples and non-finite MVs.
    """
    height, width = previous_lr.shape[:2]
    if motion_rg.shape != (height, width, 2):
        raise ValueError(f"unexpected motion shape: {motion_rg.shape}")
    y, x = np.mgrid[:height, :width].astype(np.float32)
    motion_rg = np.asarray(motion_rg, dtype=np.float32)
    finite = np.isfinite(motion_rg).all(axis=2)
    safe_motion = np.where(finite[..., None], motion_rg, 0)
    source_x = x + safe_motion[:, :, 0] * width * scale
    source_y = y + safe_motion[:, :, 1] * height * scale
    aligned, valid = bilinear_sample(previous_lr, source_x, source_y)
    valid &= finite
    return aligned * valid[..., None], valid
