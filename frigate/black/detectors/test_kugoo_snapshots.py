import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("snapshots", Path(__file__).with_name("kugoo_snapshots.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotRefreshTest(unittest.TestCase):
    def setUp(self):
        self.original = patch.object(module, "original_is_better_thumbnail", return_value=False)
        self.original.start()
        self.addCleanup(self.original.stop)
        self.current = {"frame_time": 100, "score": .9, "area": 10000, "box": (100, 100, 200, 200)}
        self.new = {**self.current, "frame_time": 120, "score": .8, "label": "bicycle"}

    def refresh(self, **values):
        return module.is_better_thumbnail([], self.current, {**self.new, **values}, (720, 1280))

    def test_fresh_confident_native_track_can_supply_another_snapshot(self):
        self.assertTrue(self.refresh())
        self.assertTrue(self.refresh(label="motorcycle"))

    def test_other_native_labels_keep_original_policy(self):
        for label in ("person", "car", "cat", "dog"):
            self.assertFalse(self.refresh(label=label))

    def test_low_quality_stale_and_nonfinite_frames_do_not_refresh(self):
        for values in ({"frame_time": 100}, {"frame_time": 99}, {"frame_time": 119.9},
                       {"score": .69}, {"score": float("nan")}, {"area": 8999},
                       {"box": (0, 100, 200, 200)}):
            self.assertFalse(self.refresh(**values), values)

    def test_original_better_thumbnail_remains_authoritative(self):
        with patch.object(module, "original_is_better_thumbnail", return_value=True):
            self.assertTrue(self.refresh(label="cat"))


if __name__ == "__main__":
    unittest.main()
