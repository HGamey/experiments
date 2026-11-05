"""Tests for continuous float32 bicubic resizing and shader-relevant borders."""

import math
import unittest

import numpy as np

from doom3_sr.bicubic import resize_bicubic


def _naive_cubic(distance, a):
    distance = abs(distance)
    if distance <= 1.0:
        return (a + 2.0) * distance**3 - (a + 3.0) * distance**2 + 1.0
    if distance < 2.0:
        return (a * distance**3 - 5.0 * a * distance**2
                + 8.0 * a * distance - 4.0 * a)
    return 0.0


def _naive_resize(image, out_height, out_width, a=-0.5):
    """Direct 2D scalar convolution, normalized jointly instead of separably."""
    source = np.asarray(image, dtype=np.float64)
    in_height, in_width, channels = source.shape
    scale_y = out_height / in_height
    scale_x = out_width / in_width
    filter_y = min(1.0, scale_y)
    filter_x = min(1.0, scale_x)
    support_y = 2.0 / filter_y
    support_x = 2.0 / filter_x
    output = np.empty((out_height, out_width, channels), dtype=np.float64)
    for out_y in range(out_height):
        source_y = (out_y + 0.5) / scale_y - 0.5
        y_indices = range(math.ceil(source_y - support_y),
                          math.floor(source_y + support_y) + 1)
        for out_x in range(out_width):
            source_x = (out_x + 0.5) / scale_x - 0.5
            x_indices = range(math.ceil(source_x - support_x),
                              math.floor(source_x + support_x) + 1)
            for channel in range(channels):
                weighted_sum = 0.0
                weight_sum = 0.0
                for in_y in y_indices:
                    weight_y = _naive_cubic((in_y - source_y) * filter_y, a)
                    clamped_y = min(max(in_y, 0), in_height - 1)
                    for in_x in x_indices:
                        weight_x = _naive_cubic((in_x - source_x) * filter_x, a)
                        clamped_x = min(max(in_x, 0), in_width - 1)
                        weight = weight_y * weight_x
                        weighted_sum += source[clamped_y, clamped_x, channel] * weight
                        weight_sum += weight
                output[out_y, out_x, channel] = weighted_sum / weight_sum
    return output


class BicubicTests(unittest.TestCase):
    def test_constant_images_and_borders_remain_constant(self):
        image = np.full((3, 5, 3), np.float32(12.34), dtype=np.float32)
        for output_shape in ((8, 2), (1, 1), (6, 10)):
            with self.subTest(output_shape=output_shape):
                actual = resize_bicubic(image, *output_shape)
                np.testing.assert_allclose(actual, image[0, 0, 0], rtol=0, atol=5e-6)

    def test_identity_is_exact_and_does_not_clip(self):
        rng = np.random.default_rng(71)
        image = (rng.normal(size=(5, 7, 3)) * 300).astype(np.float32)
        actual = resize_bicubic(image, 5, 7)
        np.testing.assert_array_equal(actual, image)

    def test_fractional_output_and_natural_cubic_overshoot(self):
        image = np.array([[[0.123], [100.456]]], dtype=np.float32)
        actual = resize_bicubic(image, 1, 4)
        self.assertTrue(np.any(np.abs(actual - np.round(actual)) > 1e-4))
        self.assertLess(float(actual.min()), 0.0)
        self.assertGreater(float(actual.max()), 100.456)

    def test_small_arrays_match_independent_naive_reference(self):
        rng = np.random.default_rng(19)
        cases = ((3, 4, 1, 5, 3), (4, 6, 3, 2, 3))  # Mixed scale and exact 0.5x.
        for height, width, channels, out_height, out_width in cases:
            with self.subTest(shape=(height, width, channels),
                              output=(out_height, out_width)):
                image = rng.normal(100, 50, (height, width, channels)).astype(np.float32)
                actual = resize_bicubic(image, out_height, out_width)
                expected = _naive_resize(image, out_height, out_width)
                np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=5e-5)

    def test_shapes_dtype_and_one_or_three_channels(self):
        for channels, output_shape in ((1, (7, 5)), (3, (2, 3))):
            with self.subTest(channels=channels):
                image = np.arange(5 * 7 * channels, dtype=np.float64).reshape(5, 7, channels)
                actual = resize_bicubic(image, *output_shape)
                self.assertEqual(actual.shape, (*output_shape, channels))
                self.assertEqual(actual.dtype, np.float32)

    def test_expanded_patch_two_x_exactly_matches_full_frame(self):
        rng = np.random.default_rng(93)
        lr = rng.normal(127, 60, (12, 14, 3)).astype(np.float32)
        full = resize_bicubic(lr, 24, 28)
        padded = np.pad(lr, ((4, 4), (4, 4), (0, 0)), mode="edge")
        patch_height, patch_width = 3, 4
        # Each interior patch is at least two source pixels from every outer border.
        for y, x in ((2, 2), (7, 8), (3, 5)):
            with self.subTest(patch=(y, x)):
                expanded = padded[y:y + patch_height + 8, x:x + patch_width + 8]
                resized = resize_bicubic(
                    expanded, 2 * (patch_height + 8), 2 * (patch_width + 8)
                )
                patch = resized[8:8 + 2 * patch_height, 8:8 + 2 * patch_width]
                expected = full[2 * y:2 * (y + patch_height), 2 * x:2 * (x + patch_width)]
                np.testing.assert_array_equal(patch, expected)


if __name__ == "__main__":
    unittest.main()
