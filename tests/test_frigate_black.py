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
        self.assertEqual(source["go2rtc"], converted["go2rtc"])
        self.assertEqual(source["cameras"]["living"]["ffmpeg"], converted["cameras"]["living"]["ffmpeg"])
        self.assertEqual(480, converted["cameras"]["living"]["detect"]["height"])
        self.assertNotIn("record", source)
        self.assertEqual("preset-record-generic-audio-copy", converted["ffmpeg"]["output_args"]["record"])

    def test_staging_has_no_archive_writer_and_cpu_fallback_has_no_cuda_decoder(self):
        config = MODULE.black_config(self.source(), staging=True, cpu=True)
        self.assertFalse(config["record"]["enabled"])
        self.assertNotIn("hwaccel_args", config["ffmpeg"])
        self.assertEqual("CPU", config["detectors"]["objects"]["device"])

    def test_empty_source_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.black_config({})

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
