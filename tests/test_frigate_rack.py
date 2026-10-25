import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "rack_profile", Path(__file__).parents[1] / "frigate/black/rack_profile.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RackProfileTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "mqtt": {"enabled": True},
            "record": {"continuous": {"days": 3}},
            "go2rtc": {"streams": {"cam2_main": ["main"], "cam2_sub": ["sub"]}},
            "cameras": {
                MODULE.CAMERA: {
                    "ffmpeg": {"inputs": [
                        {"path": "sub", "roles": ["detect"]},
                        {"path": "main", "roles": ["record"], "input_args": "untouched"},
                    ]},
                    "record": {"alerts": {"retain": {"days": 30}}},
                    "review": {"alerts": {"labels": ["person"]}},
                    "live": {"height": 720, "streams": {"Custom": "custom"}},
                    "detect": {"enabled": True, "width": 640, "height": 480},
                    "snapshots": {"crop": False},
                },
                "other": {"record": {"enabled": True}},
            },
        }

    def test_preserves_recording_other_cameras_and_source(self):
        original = copy.deepcopy(self.source)
        result = MODULE.rack_profile(self.source)
        before = original["cameras"][MODULE.CAMERA]
        after = result["cameras"][MODULE.CAMERA]
        self.assertEqual(self.source, original)
        for key in ("record", "review"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["ffmpeg"]["inputs"][1], after["ffmpeg"]["inputs"][1])
        self.assertEqual(original["cameras"]["other"], result["cameras"]["other"])
        self.assertEqual(original["record"], result["record"])
        self.assertEqual(original["go2rtc"]["streams"]["cam2_main"], result["go2rtc"]["streams"]["cam2_main"])
        self.assertEqual(after["live"]["height"], 720)
        self.assertEqual(after["live"]["streams"]["Custom"], "custom")
        self.assertEqual(next(iter(after["live"]["streams"].values())), "cam2_rack")
        self.assertEqual(after["detect"]["width"], 1280)

    def test_reapplying_does_not_add_duplicate_streams(self):
        result = MODULE.rack_profile(self.source)
        self.assertEqual(MODULE.rack_profile(result), result)

    def test_rejects_combined_record_input_or_missing_main(self):
        self.source["cameras"][MODULE.CAMERA]["ffmpeg"]["inputs"][0]["roles"].append("record")
        with self.assertRaises(ValueError):
            MODULE.rack_profile(self.source)
        del self.source["go2rtc"]["streams"]["cam2_main"]
        with self.assertRaises(ValueError):
            MODULE.rack_profile(self.source)

    def test_candidate_is_private_and_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.yml"
            output = Path(directory) / "candidate.yml"
            source.write_text(MODULE.yaml.safe_dump(self.source), encoding="utf-8")
            with patch("sys.argv", ["rack_profile.py", str(source), str(output)]):
                MODULE.main()
                if os.name == "posix":
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                candidate = output.read_bytes()
                with self.assertRaises(FileExistsError):
                    MODULE.main()
                self.assertEqual(output.read_bytes(), candidate)
