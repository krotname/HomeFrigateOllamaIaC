import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("frigate_black_prepare", ROOT / "frigate/black/prepare.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
DEPLOY_SPEC = importlib.util.spec_from_file_location("frigate_black_deploy", ROOT / "frigate/black/deploy.py")
DEPLOY = importlib.util.module_from_spec(DEPLOY_SPEC)
DEPLOY_SPEC.loader.exec_module(DEPLOY)


class MigrationTests(unittest.TestCase):
    def test_native_kugoo_limits_two_wheeler_tracking_to_measured_views(self):
        source = {"model": {"path": "yolov9-t-320.onnx"},
                  "detectors": {"objects": {"type": "onnx"}},
                  "objects": {"track": ["person", "bicycle"]},
                  "cameras": {"cam1_ds_i202": {}, "common_camera03": {},
                              "other": {"objects": {"track": ["car", "cat", "bicycle", "motorcycle"]}},
                              "inherited": {}}}
        result = MODULE.configure_kugoo_detector(source)
        self.assertEqual(["car", "cat"], result["cameras"]["other"]["objects"]["track"])
        self.assertEqual(["person"], result["cameras"]["inherited"]["objects"]["track"])
        self.assertEqual(["person", "bicycle", "motorcycle"],
                         result["cameras"]["cam1_ds_i202"]["objects"]["track"])

    def test_native_kugoo_keeps_connections_recording_and_filter_thresholds(self):
        source = self.source()
        source.update(model={"path": "/config/model_cache/yolov9-t-320.onnx",
                             "width": 320, "height": 320},
                      detectors={"objects": {"type": "onnx", "device": "GPU"}})
        for name in ("cam1_ds_i202", "common_camera03"):
            source["cameras"][name] = {
                "ffmpeg": {"inputs": [{"path": "rtsp://private/stream", "roles": ["record", "detect"]}]},
                "record": {"enabled": True},
                "objects": {"track": ["car", "cat", "bicycle"], "filters": {
                    "bicycle": {"threshold": .7, "min_score": .5},
                    "cat": {"threshold": .8}}}}
        before = copy.deepcopy(source)
        converted = MODULE.configure_kugoo_detector(source)
        self.assertEqual(source, before)
        self.assertEqual(converted, MODULE.configure_kugoo_detector(converted))
        self.assertEqual(source["cameras"]["living"], converted["cameras"]["living"])
        for name in ("cam1_ds_i202", "common_camera03"):
            for field in ("ffmpeg", "record"):
                self.assertEqual(source["cameras"][name][field], converted["cameras"][name][field])
            self.assertEqual(source["cameras"][name]["objects"]["filters"],
                             converted["cameras"][name]["objects"]["filters"])
        self.assertEqual(converted["model"]["width"], 960)
        self.assertEqual(converted["detectors"]["objects"]["supplement_threshold"], .7)

    def test_two_wheeler_update_preserves_implicit_person_tracking(self):
        source = {"cameras": {"cam3_ds_i200": {}}}
        converted = MODULE.configure_two_wheelers(source)
        self.assertEqual(["person", "bicycle", "motorcycle"],
                         converted["cameras"]["cam3_ds_i200"]["objects"]["track"])
        self.assertEqual({"cameras": {"cam3_ds_i200": {}}}, source)

    def test_two_wheeler_update_preserves_thresholds_and_other_cameras(self):
        source = self.source()
        source["cameras"]["cam3_ds_i200"] = {"objects": {"track": ["car"],
            "filters": {"car": {"threshold": .8}}}, "review": {"alerts": {"labels": ["person"]}}}
        converted = MODULE.configure_two_wheelers(source)
        self.assertEqual(source["cameras"]["living"], converted["cameras"]["living"])
        camera = converted["cameras"]["cam3_ds_i200"]
        self.assertEqual(["car", "bicycle", "motorcycle"], camera["objects"]["track"])
        self.assertEqual(source["cameras"]["cam3_ds_i200"]["objects"]["filters"], camera["objects"]["filters"])
        self.assertEqual({"labels": ["person"]}, camera["review"]["alerts"])
        self.assertEqual(converted, MODULE.configure_two_wheelers(converted))

    def source(self):
        return {
            "auth": {"enabled": False}, "mqtt": {"enabled": False},
            "go2rtc": {"streams": {"living": ["rtsp://user:secret@camera/stream"]}},
            "cameras": {"living": {"detect": {"width": 640, "height": 480},
                "ffmpeg": {"inputs": [{"path": "rtsp://127.0.0.1:8554/living", "roles": ["record"]}]}}},
        }

    def test_credentials_connections_dimensions_and_source_are_preserved(self):
        source = self.source()
        converted = MODULE.black_config(source)
        self.assertEqual(source["go2rtc"]["streams"], converted["go2rtc"]["streams"])
        self.assertEqual(source["cameras"]["living"]["ffmpeg"], converted["cameras"]["living"]["ffmpeg"])
        self.assertEqual(480, converted["cameras"]["living"]["detect"]["height"])
        self.assertNotIn("record", source)
        self.assertEqual("preset-record-generic-audio-copy", converted["ffmpeg"]["output_args"]["record"])

    def test_lan_ice_candidates_preserve_custom_routes_and_are_idempotent(self):
        source = self.source()
        source["go2rtc"]["webrtc"] = {"listen": ":8555", "candidates": ["custom.lan:8555", "192.168.1.138:8555"]}
        converted = MODULE.black_config(source)
        self.assertEqual(["custom.lan:8555", "192.168.1.138:8555", "192.168.1.242:8555"],
                         converted["go2rtc"]["webrtc"]["candidates"])
        self.assertEqual(":8555", converted["go2rtc"]["webrtc"]["listen"])
        self.assertEqual(converted, MODULE.black_config(converted))
        self.assertEqual(2, len(source["go2rtc"]["webrtc"]["candidates"]))

    def test_staging_has_no_archive_writer_and_cpu_fallback_has_no_cuda_decoder(self):
        config = MODULE.black_config(self.source(), staging=True, cpu=True)
        self.assertFalse(config["record"]["enabled"])
        self.assertNotIn("hwaccel_args", config["ffmpeg"])
        self.assertEqual("CPU", config["detectors"]["objects"]["device"])

    def test_car_events_remain_available_after_preparing_camera_overrides(self):
        source = self.source()
        source["cameras"]["living"]["objects"] = {"track": ["person", "car"]}
        converted = MODULE.black_config(source)
        self.assertIn("car", converted["objects"]["track"])
        self.assertNotIn("objects", converted["cameras"]["living"])
        self.assertEqual(["person", "car"], source["cameras"]["living"]["objects"]["track"])

    def test_empty_source_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.black_config({})

    def test_courtyard_review_only_keeps_recording_and_unrelated_cameras(self):
        source = self.source()
        source["review"] = {"alerts": {"enabled": True, "labels": ["person"]},
                            "detections": {"enabled": True, "labels": ["cat", "dog"]}}
        source["cameras"] = {
            "cam1_ds_i202": {"record": {"continuous": {"days": 3}}},
            "common_camera01": {"review": {"alerts": {"enabled": True, "labels": ["person"]},
                                             "detections": {"labels": ["car", "dog"]}},
                                "record": {"continuous": {"days": 3}}},
            "common_camera02": {"review": {"alerts": {"enabled": True, "labels": ["person"]}}},
            "common_camera03": {"review": {"alerts": {"enabled": True, "labels": ["person"]}}},
            "common_camera04": {"review": {"alerts": {"enabled": True, "labels": ["person"]}}},
        }
        before = copy.deepcopy(source)
        converted = MODULE.configure_courtyard_review(source)
        self.assertEqual(before, source)
        self.assertEqual(converted, MODULE.configure_courtyard_review(converted))
        self.assertEqual(source.get("record"), converted.get("record"))
        self.assertEqual(source["cameras"]["common_camera04"], converted["cameras"]["common_camera04"])
        self.assertEqual(source["cameras"]["common_camera01"]["record"],
                         converted["cameras"]["common_camera01"]["record"])
        self.assertTrue(converted["cameras"]["common_camera02"]["review"]["alerts"]["enabled"])
        for name in MODULE.COURTYARD_SECONDARY:
            review = converted["cameras"][name]["review"]
            self.assertFalse(review["alerts"]["enabled"])
            self.assertTrue(review["detections"]["enabled"])
            self.assertIn("person", review["detections"]["labels"])
        self.assertEqual(["person", "car", "dog"],
                         converted["cameras"]["common_camera01"]["review"]["detections"]["labels"])

    def test_courtyard_policy_requires_all_four_angles(self):
        with self.assertRaises(ValueError):
            MODULE.configure_courtyard_review({"cameras": {"common_camera02": {}}})

    def test_unrestricted_detection_labels_remain_unrestricted(self):
        source = {"cameras": {name: {} for name in
                  (MODULE.COURTYARD_PRIMARY, *MODULE.COURTYARD_SECONDARY)}}
        converted = MODULE.configure_courtyard_review(source)
        for name in MODULE.COURTYARD_SECONDARY:
            detections = converted["cameras"][name]["review"]["detections"]
            self.assertTrue(detections["enabled"])
            self.assertNotIn("labels", detections)
        source["cameras"]["common_camera01"] = {"review": {"detections": {"labels": None}}}
        converted = MODULE.configure_courtyard_review(source)
        self.assertIsNone(converted["cameras"]["common_camera01"]["review"]["detections"]["labels"])

    def test_proxy_switch_preserves_auth_and_unrelated_backends(self):
        source = 'auth_basic "home"; proxy_pass https://127.0.0.1:18971; proxy_pass http://127.0.0.1:6800;'
        converted = DEPLOY.proxy_config(source)
        self.assertIn('auth_basic "home";', converted)
        self.assertIn('https://192.168.1.242:18971', converted)
        self.assertIn('http://127.0.0.1:6800', converted)
        self.assertEqual(converted, DEPLOY.proxy_config(converted))
        with self.assertRaises(ValueError):
            DEPLOY.proxy_config('proxy_pass http://127.0.0.1:6800;')


if __name__ == "__main__":
    unittest.main()
