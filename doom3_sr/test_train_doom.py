"""Focused synthetic-data tests for Doom training preparation."""
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from doom3_sr.train_doom import assign_split, base_patch, decode_record, deduplicate, discover, filter_near_duplicates, make_base


class DoomTrainingPreparationTests(unittest.TestCase):
    def test_base_patch_exactly_matches_full_resize_in_training_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"
            rng = np.random.default_rng(19)
            Image.fromarray(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)).save(path)
            record = decode_record({"path": path}, "pillow_u8")
            full = np.asarray(Image.fromarray(record["lr"]).resize((64, 48), Image.Resampling.BICUBIC))
            patch = 8
            checked = 0
            for y in range(2, record["lr"].shape[0] - patch - 1, 3):
                for x in range(2, record["lr"].shape[1] - patch - 1, 3):
                    actual = base_patch(record, x, y, patch, "pillow_u8")
                    expected = full[2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)]
                    np.testing.assert_array_equal(actual, expected)
                    checked += 1
            self.assertGreater(checked, 20)

    def test_float_base_patch_matches_full_resize(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"
            rng = np.random.default_rng(23)
            Image.fromarray(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)).save(path)
            record = decode_record({"path": path}, "float_a-0.5")
            self.assertEqual(record["lr"].dtype, np.float32)
            full = make_base(record, "float_a-0.5")
            patch = 8
            for y, x in ((2, 2), (3, 7), (5, 11), (8, 14)):
                actual = base_patch(record, x, y, patch, "float_a-0.5")
                expected = full[2 * y : 2 * (y + patch), 2 * x : 2 * (x + patch)]
                np.testing.assert_array_equal(actual, expected)

    def test_near_duplicate_filter_compares_with_last_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arrays = []
            for value in (0, 1):
                base = np.full((32, 40, 3), value, dtype=np.uint8)
                changed = base.copy()
                changed[0, 0] = 255
                arrays.extend((base, changed))
            descriptors = []
            for index, array in enumerate(arrays):
                path = root / f"{index}.png"
                Image.fromarray(array).save(path)
                descriptors.append({"path": path, "relative_path": path.name})
            kept, report = filter_near_duplicates(descriptors, 0.5)
            self.assertEqual([item["relative_path"] for item in kept], ["0.png", "2.png"])
            self.assertEqual(report["rejected_frames"], 2)
            self.assertEqual(report["kept_frames"], 2)
            self.assertAlmostEqual(kept[1]["mad_from_last_kept_8bit"], 1.0)

    def test_discovery_dedup_and_group_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for group in range(3):
                (root / f"map-{group}").mkdir()
                for frame in range(4):
                    value = group * 16 + frame
                    image = np.full((32, 40, 3), value, dtype=np.uint8)
                    Image.fromarray(image).save(root / f"map-{group}" / f"{frame}.png")
            # An exact duplicate must not enter either split.
            (root / "map-2" / "duplicate.png").write_bytes((root / "map-2" / "3.png").read_bytes())
            descriptors = deduplicate(discover(root))
            self.assertEqual(len(descriptors), 12)
            train, test, method = assign_split(descriptors, 0.25)
            self.assertEqual(method, "group_sha256")
            self.assertEqual(len(train) + len(test), 12)
            self.assertTrue({item["group"] for item in train}.isdisjoint({item["group"] for item in test}))
            self.assertTrue(all(item["split"] == "train" for item in train))
            self.assertTrue(all(item["split"] == "test" for item in test))


if __name__ == "__main__":
    unittest.main()
