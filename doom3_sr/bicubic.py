"""Continuous NumPy bicubic resize with GPU-style clamp-to-edge sampling.

Exact semantics for shader/Codex parity:

* Input/output are HWC with one or three channels.  Height and width have
  independent scales ``out_size / in_size`` and output centers map to source
  coordinates ``(dst + 0.5) / scale - 0.5``.
* For ``x=abs(d)``, the Keys cubic is ``(a+2)x^3-(a+3)x^2+1``
  for ``x<=1``, ``a*x^3-5a*x^2+8a*x-4a`` for ``1<x<2``, and zero
  otherwise (default ``a=-0.5``).  For downsampling, distances are multiplied
  by the scale and support becomes ``2 / scale``; upsampling uses unscaled
  distances and support two.  Weights are normalized on each destination sample.
* Every integer tap whose distance is at most the support is visited in
  increasing source-index order.  Indices clamp to the nearest edge, retaining
  duplicate edge taps rather than merging them.
* Coordinates and cubic weights are evaluated in float64.  Weights are cast
  to float32 and normalized with a float32 sum.  Horizontal then vertical
  accumulation is explicitly float32 in tap order.  There is no clipping or
  integer rounding.
"""

import math
import operator

import numpy as np


def _cubic(distance, a):
    distance = np.abs(np.asarray(distance, dtype=np.float64))
    squared = distance * distance
    cubed = squared * distance
    result = np.zeros_like(distance)
    inner = distance <= 1.0
    result[inner] = ((a + 2.0) * cubed[inner] - (a + 3.0) * squared[inner] + 1.0)
    outer = (distance > 1.0) & (distance < 2.0)
    result[outer] = (a * cubed[outer] - 5.0 * a * squared[outer]
                     + 8.0 * a * distance[outer] - 4.0 * a)
    return result


def _axis_taps(input_size, output_size, a):
    scale = output_size / input_size
    filter_scale = min(1.0, scale)
    support = 2.0 / filter_scale
    taps = []
    for destination in range(output_size):
        source = (destination + 0.5) / scale - 0.5
        first = math.ceil(source - support)
        last = math.floor(source + support)
        indices = np.arange(first, last + 1, dtype=np.int64)
        weights = _cubic((indices - source) * filter_scale, a).astype(np.float32)
        weights /= np.sum(weights, dtype=np.float32)
        taps.append((np.clip(indices, 0, input_size - 1), weights))
    return taps


def resize_bicubic(image, out_height, out_width, a=-0.5):
    """Resize an HWC array to ``(out_height, out_width, C)`` as float32."""
    source = np.asarray(image, dtype=np.float32)
    if source.ndim != 3 or source.shape[2] not in (1, 3):
        raise ValueError("image must be HWC with one or three channels")
    if source.shape[0] < 1 or source.shape[1] < 1:
        raise ValueError("input height and width must be positive")
    out_height = operator.index(out_height)
    out_width = operator.index(out_width)
    if out_height < 1 or out_width < 1:
        raise ValueError("output height and width must be positive")
    a = float(a)
    if not math.isfinite(a):
        raise ValueError("a must be finite")

    horizontal_taps = _axis_taps(source.shape[1], out_width, a)
    vertical_taps = _axis_taps(source.shape[0], out_height, a)
    horizontal = np.empty((source.shape[0], out_width, source.shape[2]), dtype=np.float32)
    for destination, (indices, weights) in enumerate(horizontal_taps):
        accumulated = np.zeros((source.shape[0], source.shape[2]), dtype=np.float32)
        for index, weight in zip(indices, weights):
            accumulated += source[:, index, :] * weight
        horizontal[:, destination, :] = accumulated

    result = np.empty((out_height, out_width, source.shape[2]), dtype=np.float32)
    for destination, (indices, weights) in enumerate(vertical_taps):
        accumulated = np.zeros((out_width, source.shape[2]), dtype=np.float32)
        for index, weight in zip(indices, weights):
            accumulated += horizontal[index, :, :] * weight
        result[destination, :, :] = accumulated
    return result
