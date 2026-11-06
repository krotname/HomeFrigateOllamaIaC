import copy
import http.client
import importlib.util
import io
import json
from pathlib import Path
import threading
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
        self.assertTrue(returned[1].endswith("token=fresh"))

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
        configured = MODULE.configure_public_live(source, "http://172.19.0.1:18786")
        self.assertEqual(source, before)
        self.assertEqual(configured, MODULE.configure_public_live(configured, "http://172.19.0.1:18786"))
        self.assertEqual(list(configured["cameras"]), list(source["cameras"]))
        for setting in ("ffmpeg", "detect", "record"):
            self.assertEqual(configured["cameras"][MODULE.ANCHOR][setting], source["cameras"][MODULE.ANCHOR][setting])
        self.assertEqual(configured["record"], source["record"])
        self.assertEqual(len(configured["go2rtc"]["streams"]), 4)
        self.assertNotIn("token=", json.dumps(configured))
        self.assertTrue(all(v.startswith("ffmpeg:http://172.19.0.1:18786/") for k,v in configured["go2rtc"]["streams"].items() if k in MODULE.SOURCES))

    def test_http_resolves_only_allowlisted_requests_and_does_not_cache(self):
        with MODULE.ResolverServer(("127.0.0.1", 0), MODULE.ResolverHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                with patch.object(MODULE, "resolve_source", side_effect=["https://sochi.camera:8443/cam_445/index.m3u8?token=first", "https://sochi.camera:8443/cam_445/index.m3u8?token=second"]) as resolve:
                    for token in ("first", "second"):
                        conn.request("GET", "/public_adler_station.m3u8")
                        response = conn.getresponse()
                        self.assertEqual(response.status, 302)
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        self.assertTrue(response.getheader("Location").endswith("token="+token))
                        response.read()
                    conn.request("GET", "/?url=http://127.0.0.1/")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                    self.assertEqual(resolve.call_count, 2)
            finally:
                conn.close()
                server.shutdown()
                worker.join(timeout=3)

    def test_rejects_broad_or_external_resolver_binds(self):
        for base in ("http://0.0.0.0:18786", "http://8.8.8.8:18786", "http://[::1]:18786", "http://127.0.0.1:18786/?url=x"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                MODULE.configure_public_live({"cameras": {MODULE.ANCHOR: {}}}, base)

    def test_does_not_log_signed_urls_on_error(self):
        with patch.object(MODULE, "resolve_source", side_effect=ValueError("token=private")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(MODULE.main(["resolve", "public_adler_station"]), 1)
            self.assertNotIn("private", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
