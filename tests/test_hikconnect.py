import contextlib
import importlib.util
import io
import json
import signal
import struct
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "frigate/hikconnect" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELAY, CONFIG = load("relay"), load("add_cameras")


class CameraTests(unittest.TestCase):
    def test_preserves_three_cameras_secrets_watchdog_related_settings_and_global_configuration(self):
        source = {"version": "0.18-0", "auth": {"enabled": True}, "ffmpeg": {"hwaccel_args": "preset-nvidia"},
                  "face_recognition": {"enabled": True}, "record": {"continuous": {"days": 3}},
                  "go2rtc": {"streams": {"original": ["rtsp://original"]}, "webrtc": {"listen": ":8555"}},
                  "cameras": {f"existing{i}": {"detect": {"fps": 3}, "live": {"streams": {"Camera": "original"}}}
                              for i in range(3)}}
        result = CONFIG.add_cameras(source)
        for key in ("auth", "ffmpeg", "face_recognition", "record"):
            self.assertEqual(source[key], result[key])
        self.assertEqual(source["go2rtc"]["streams"]["original"], result["go2rtc"]["streams"]["original"])
        for name, value in source["cameras"].items():
            self.assertEqual(value, result["cameras"][name])
        self.assertEqual(3, len(source["cameras"]))
        self.assertEqual(7, len(result["cameras"]))
        camera = result["cameras"]["common_camera01"]
        self.assertEqual([["record"], ["detect"]], [v["roles"] for v in camera["ffmpeg"]["inputs"]])
        self.assertEqual(["person"], camera["review"]["alerts"]["labels"])
        self.assertEqual(CONFIG.LABELS[1:], camera["review"]["detections"]["labels"])
        self.assertFalse(camera["face_recognition"]["enabled"])
        self.assertEqual(result, CONFIG.add_cameras(result))

    def test_main_fallback_and_alias_conflicts(self):
        source = {"version": "0.18-0", "cameras": {"existing": {}}}
        camera = CONFIG.add_cameras(source, substream=False)["cameras"]["common_camera01"]
        self.assertEqual(["record", "detect"], camera["ffmpeg"]["inputs"][0]["roles"])
        self.assertEqual(1920, camera["detect"]["width"])
        source["cameras"]["common_camera01"] = {"enabled": False}
        with self.assertRaises(ValueError):
            CONFIG.add_cameras(source)


class SessionTests(unittest.TestCase):
    def test_expired_session_refreshes_and_tokens_are_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "session.json"
            RELAY.atomic_json(cache, {"sid": "expired", "expires": 9})
            sessions = RELAY.Sessions({"api_base": "https://example.invalid", "token_base": "https://example.invalid",
                                      "account": "example", "password": "fixture"}, cache, lambda: 10)
            calls = []

            def request(base, path, data, headers):
                calls.append(path)
                return ({"meta": {"code": 200}, "loginSession": {"sessionId": "fresh"}}
                        if "login" in path else {"resultCode": 0, "tokenArray": ["first", "second"]})

            sessions.request = request
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual("first", sessions.token())
                self.assertEqual("second", sessions.token())
            self.assertEqual(2, len(calls))
            state = json.loads(cache.read_text())
            self.assertEqual("fresh", state["sid"])
            self.assertEqual([], state["tokens"])
            self.assertEqual(0o600, cache.stat().st_mode & 0o777)

    def test_server_session_rejection_invalidates_cache_without_logging_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "session.json"
            RELAY.atomic_json(cache, {"sid": "sensitive-session", "expires": 100, "tokens": []})
            sessions = RELAY.Sessions({"token_base": "https://example.invalid"}, cache, lambda: 10)
            sessions.request = lambda *args: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://example.invalid/sensitive-session", 401, "secret", {}, None))
            with self.assertRaises(urllib.error.HTTPError):
                sessions.token()
            self.assertEqual({}, json.loads(cache.read_text()))
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                RELAY.log("reconnecting", camera="common_camera01", error="HTTPError")
            self.assertNotIn("sensitive-session", output.getvalue())
            self.assertNotIn("secret", output.getvalue())


class VideoTests(unittest.TestCase):
    def decoder(self):
        decoder = RELAY.Hevc.__new__(RELAY.Hevc)
        decoder.fragment, decoder.seq, decoder.parameters, decoder.ready = None, None, {}, False
        class Xor:
            def update(self, data): return bytes(value ^ 255 for value in data)
            def finalize(self): return b""
        decoder.cipher = Xor
        return decoder

    def rtp(self, seq, payload):
        return struct.pack(">BBHII", 128, 96, seq, 90000, 1) + payload

    def test_only_parameter_sets_and_irap_are_decrypted(self):
        decoder = self.decoder()
        for kind in (32, 33, 34):
            self.assertEqual(b"", decoder.nal(bytes([kind << 1, 1]) + b"\xff" * 16))
            self.assertEqual(bytes([kind << 1, 1]) + b"\0" * 16, decoder.parameters[kind])
        keyframe = decoder.nal(bytes([19 << 1, 1]) + b"\xff" * 16)
        self.assertTrue(keyframe.endswith(bytes([19 << 1, 1]) + b"\0" * 16))
        clear_p = b"\x02\x01" + b"\xff" * 16
        self.assertEqual(RELAY.PREFIX + clear_p, decoder.nal(clear_p))

    def test_fragment_loss_waits_for_a_new_keyframe(self):
        decoder = self.decoder()
        decoder.ready = True
        self.assertEqual(b"", decoder.feed(self.rtp(65535, b"\x62\x01\x81abc")))
        self.assertEqual(RELAY.PREFIX + b"\x02\x01abcdef", decoder.feed(self.rtp(0, b"\x62\x01\x41def")))
        decoder.feed(self.rtp(1, b"\x62\x01\x81abc"))
        self.assertEqual(b"", decoder.feed(self.rtp(3, b"\x62\x01\x41def")))
        self.assertFalse(decoder.ready)
        self.assertEqual(b"", decoder.feed(self.rtp(4, b"\x02\x01abc")))

    def test_lost_fu_end_resets_before_new_start_or_single_nal(self):
        for payload in (b"\x02\x01abc", b"\x62\x01\x81abc"):
            decoder = self.decoder()
            decoder.ready = True
            decoder.feed(self.rtp(10, b"\x62\x01\x81abc"))
            self.assertEqual(b"", decoder.feed(self.rtp(12, payload)))
            self.assertFalse(decoder.ready)

    def test_clock_preserves_wraps_and_reconnect_elapsed_time(self):
        clock = RELAY.RtpClock()
        self.assertEqual(0, clock.advance(0xFFFFFFF0, 1))
        self.assertEqual(32, clock.advance(16, 2))
        self.assertEqual(180032, clock.advance(0, 4))

    def test_publication_preserves_source_clock_and_fragment_boundaries(self):
        publisher = RELAY.Publisher.__new__(RELAY.Publisher)
        packets = []
        publisher.connection = type("Socket", (), {"sendall": lambda _, packet: packets.append(packet)})()
        publisher.clock, publisher.sequence, publisher.ssrc = RELAY.RtpClock(), 0, 1
        publisher.write(RELAY.PREFIX + b"\x26\x01" + b"a" * 3000, 100, False)
        publisher.write(RELAY.PREFIX + b"\x02\x01x", 6100, True)
        self.assertEqual(4, len(packets))
        self.assertTrue(packets[0][18] & 128)  # HEVC FU start.
        self.assertTrue(packets[2][18] & 64)   # HEVC FU end.
        self.assertEqual(6000, int.from_bytes(packets[-1][8:12], "big"))
        self.assertEqual([0, 0, 0, 128], [packet[5] & 128 for packet in packets])

    def test_cloud_broken_pipe_reconnects_instead_of_ending_worker(self):
        handlers, connections = {}, []

        def connect(*args):
            connection = type("Socket", (), {"close": lambda _: None})()
            connections.append(connection)
            if len(connections) == 2:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return connection, {4: b"session"}

        with patch.object(RELAY.signal, "signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
             patch.object(RELAY, "Sessions") as sessions, patch.object(RELAY, "Hevc"), \
             patch.object(RELAY, "connect", side_effect=connect), \
             patch.object(RELAY.time, "monotonic", side_effect=[0, 5, 6]), \
             patch.object(RELAY.time, "sleep"), \
             patch.object(RELAY, "send", side_effect=BrokenPipeError()), \
             contextlib.redirect_stderr(io.StringIO()):
            sessions.return_value.token.return_value = "fixture"
            RELAY.stream({"cameras": {"camera": 1}, "verification_code": "fixture"}, "camera", "main")
        self.assertEqual(2, len(connections))

    def test_private_redirect_and_dns_rebinding_are_blocked(self):
        with self.assertRaises(RELAY.CloudError):
            RELAY.relay_address("ysproto://192.168.1.1:8554/live", {})
        addresses = [(2, 1, 6, "", ("8.8.8.8", 8554))]
        with patch.object(RELAY.socket, "getaddrinfo", return_value=addresses):
            self.assertEqual(("8.8.8.8", 8554), RELAY.relay_address(
                "ysproto://vtm.ezvizru.com:8554/live", {"vtm_host": "vtm.ezvizru.com"}))


if __name__ == "__main__":
    unittest.main()
