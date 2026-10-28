import copy
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("storage_profile", Path(__file__).resolve().parents[1] / "frigate/black/storage_profile.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StorageProfileTests(unittest.TestCase):
    def source(self):
        return {
            "record": {"continuous": {"days": 3}, "motion": {"days": 30}},
            "go2rtc": {"streams": {"common_camera03_main": "ffmpeg:rtsp://relay:8554/camera#video=h264#hardware=cuda"}},
            "cameras": {
                "local": {"detect": {"fps": 3}},
                "common_camera03": {
                    "ffmpeg": {"inputs": [{"path": "rtsp://127.0.0.1:8554/common_camera03_main", "roles": ["record", "detect"]}]},
                    "live": {"streams": {"Main": "common_camera03_main"}},
                    "record": {"continuous": {"days": 3}, "alerts": {"retain": {"days": 30, "mode": "active_objects"}}},
                    "zones": {"door": {"coordinates": "0,0,1,1"}},
                },
            },
        }

    def test_archive_preserves_live_detection_and_unrelated_configuration(self):
        original = self.source()
        unchanged = copy.deepcopy(original)
        result = module.storage_profile(original)
        camera = result["cameras"]["common_camera03"]
        self.assertEqual(original, unchanged)
        self.assertEqual(result["go2rtc"]["streams"]["common_camera03_main"], original["go2rtc"]["streams"]["common_camera03_main"])
        self.assertEqual(result["go2rtc"]["streams"]["common_camera03_archive"], "rtsp://relay:8554/camera")
        self.assertEqual(camera["ffmpeg"]["inputs"][0]["roles"], ["detect"])
        self.assertEqual(camera["ffmpeg"]["inputs"][1]["roles"], ["record"])
        self.assertEqual(camera["live"], original["cameras"]["common_camera03"]["live"])
        self.assertEqual(camera["zones"], original["cameras"]["common_camera03"]["zones"])
        self.assertEqual(result["cameras"]["local"], original["cameras"]["local"])
        self.assertEqual(camera["record"]["alerts"]["retain"]["mode"], "active_objects")
        self.assertEqual(module.storage_profile(result), result)

    def test_unexpected_recording_source_is_rejected(self):
        source = self.source()
        source["cameras"]["common_camera03"]["ffmpeg"]["inputs"][0]["path"] = "rtsp://unrelated:8554/camera"
        with self.assertRaises(ValueError):
            module.storage_profile(source)


if __name__ == "__main__":
    unittest.main()
