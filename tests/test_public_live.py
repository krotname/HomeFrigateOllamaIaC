import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("public_live", Path(__file__).resolve().parents[1] / "frigate/black/public_live.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response(io.BytesIO):
    def geturl(self):
        return "https://sochi.camera/vse-kamery/cam-445/?format=json"


class PublicLiveTests(unittest.TestCase):
    def resolve(self, url):
        def opener(request, timeout):
            self.assertEqual(timeout, 12)
            self.assertEqual(request.full_url, "https://sochi.camera/vse-kamery/cam-445/?format=json")
            return Response(b"\xef\xbb\xbf" + json.dumps({"video_url": url}).encode())
        return MODULE.resolve_source("public_adler_station", opener)

    def test_refetches_signed_link_for_each_connection(self):
        urls = [f"https://sochi.camera:8443/cam_445/index.m3u8?token={token}" for token in ("old", "fresh")]
        returned = [self.resolve(url) for url in urls]
        self.assertNotEqual(*returned)
        self.assertTrue(returned[1].endswith("token=fresh#video=copy"))

    def test_rejects_host_path_and_command_injection(self):
        for url in (
            "https://127.0.0.1:8443/cam_445/index.m3u8?token=x",
            "https://sochi.camera:8443/cam_170/index.m3u8?token=x",
            "https://sochi.camera:8443/cam_445/index.m3u8?token=x#video=h264",
            "https://sochi.camera:8443/cam_445/index.m3u8?token=x\n-extra",
            "https://user@sochi.camera:8443/cam_445/index.m3u8?token=x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.resolve(url)

    def test_profile_preserves_recording_and_has_no_public_capture_input(self):
        source = {"record": {"enabled": True}, "cameras": {MODULE.ANCHOR: {
            "ffmpeg": {"inputs": [{"path": "rtsp://existing", "roles": ["detect", "record"]}]},
            "detect": {"enabled": True}, "record": {"enabled": True},
            "live": {"streams": {"Existing": "existing_main"}},
        }}, "go2rtc": {"streams": {"existing_main": "rtsp://existing"}}}
        before = copy.deepcopy(source)
        configured = MODULE.configure_public_live(source)
        self.assertEqual(source, before)
        self.assertEqual(configured, MODULE.configure_public_live(configured))
        self.assertEqual(list(configured["cameras"]), list(source["cameras"]))
        for setting in ("ffmpeg", "detect", "record"):
            self.assertEqual(configured["cameras"][MODULE.ANCHOR][setting], source["cameras"][MODULE.ANCHOR][setting])
        self.assertEqual(configured["record"], source["record"])
        self.assertEqual(len(configured["go2rtc"]["streams"]), 4)
        self.assertNotIn("token=", json.dumps(configured))

    def test_does_not_log_signed_urls_on_error(self):
        with patch.object(MODULE, "resolve_source", side_effect=ValueError("token=private")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(MODULE.main(["resolve", "public_adler_station"]), 1)
            self.assertNotIn("private", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
