import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from ocr.frigate_object_worker import (
    Encoder, Frigate, Store, calibration, choose, gpu_available, run, unit,
)
from ocr.frigate_caption_worker import request_bytes


class MatchingTests(unittest.TestCase):
    def test_empty_or_nonfinite_vectors_rejected(self):
        for vector in ([], [0, 0], [float("nan"), 1], [float("inf")]):
            with self.assertRaises(ValueError):
                unit(vector)

    def test_calibration_requires_independent_positive_and_negative_examples(self):
        good, bad = unit([1, 0]), unit([0, 1])
        self.assertFalse(calibration([good] * 2, [good] * 2, [bad] * 3)["enabled"])
        self.assertFalse(calibration([good] * 3, [good] * 2, [bad] * 2)["enabled"])
        self.assertFalse(calibration([good] * 3, [good] * 2, [good] * 3)["enabled"])
        self.assertTrue(calibration([good] * 3, [good] * 2, [bad] * 3)["enabled"])

    def test_unknown_competitor_and_unvalidated_profile_prevent_assignment(self):
        vector = unit([1, 0])
        policy = {"enabled": True, "threshold": .85, "margin": .06}
        profile = {"id": "car-a", "label": "Blue car", "kind": "car", "calibration": policy}
        refs = [{"profile_id": "car-a", "kind": "car", "role": "train", "vector": vector}]
        self.assertEqual("car-a", choose(vector, [profile], refs, "car")["id"])
        self.assertIsNone(choose(vector, [profile], refs, "cat"))
        refs.append({"profile_id": None, "kind": "car", "role": "validation", "vector": vector})
        self.assertIsNone(choose(vector, [profile], refs, "car"))
        profile["calibration"] = {}
        self.assertIsNone(choose(vector, [profile], refs[:1], "car"))

    def test_close_known_classes_remain_unknown(self):
        a, b = unit([1, 0]), unit([1, .05])
        profiles = [{"id": x, "label": x, "kind": "car",
                     "calibration": {"enabled": True, "threshold": .85, "margin": .06}}
                    for x in ("a", "b")]
        refs = [{"profile_id": x, "kind": "car", "role": "train", "vector": v}
                for x, v in (("a", a), ("b", b))]
        self.assertIsNone(choose(a, profiles, refs, "car"))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.store.profile("cat-a", "cat", "Cat A")

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def test_source_cannot_be_both_train_and_validation(self):
        self.store.add_reference("event-one", "cat-a", "cat", "train", [1, 0])
        with self.assertRaises(ValueError):
            self.store.add_reference("event-one", "cat-a", "cat", "validation", [1, 0])
        self.assertEqual(1, len(self.store.refs()))

    def test_people_and_wrong_kind_never_enter_gallery(self):
        with self.assertRaises(ValueError):
            self.store.profile("someone", "person", "Someone")
        with self.assertRaises(ValueError):
            self.store.add_reference("event-two", "cat-a", "car", "train", [1, 0])
        with self.assertRaises(ValueError):
            self.store.add_reference("event-three", None, "person", "train", [1, 0])

    def test_insufficient_examples_persist_disabled_policy_and_health(self):
        self.assertFalse(self.store.calibrate("cat-a")["enabled"])
        self.store.remember("event-one", "1", [None], "complete_unknown")
        self.store.health(state="waiting_for_references", gpu_loaded=False)
        saved = json.loads((self.store.directory / "health.json").read_text())
        self.assertEqual({"complete_unknown": 1}, saved["events"])
        self.assertFalse(saved["gpu_loaded"])

    def test_new_reference_invalidates_old_calibration(self):
        self.store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
        self.store.db.commit()
        self.store.add_reference("event-four", None, "cat", "validation", [0, 1])
        self.assertFalse(self.store.profiles()[0]["calibration"].get("enabled", False))


class IntegrationPolicyTests(unittest.TestCase):
    def test_old_parked_event_is_included_and_recent_active_event_is_not_duplicated(self):
        api = Frigate()
        parked = {"id": "old-car", "start_time": 1, "data": {"snapshot_frame_time": 2}}
        recent = {"id": "new-car", "start_time": 100, "data": {"snapshot_frame_time": 100}}
        advanced = {**recent, "data": {"snapshot_frame_time": 101}}
        with patch.object(api, "json", side_effect=[[recent], [parked, advanced]]):
            events = api.candidates()
        self.assertEqual([advanced, parked], events)

    def test_image_bytes_and_frame_time_are_returned_without_changing_default_api(self):
        seen_headers = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_headers.append(self.headers.get("X-Cache-Bypass"))
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.send_header("X-Frame-Time", "17.25")
                self.end_headers()
                self.wfile.write(b"jpg")

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/snapshot"
            self.assertEqual(b"jpg", request_bytes(url))
            data, headers = request_bytes(url, return_headers=True, bypass_cache=True)
            self.assertEqual(b"jpg", data)
            self.assertEqual("17.25", headers["x-frame-time"])
            self.assertEqual([None, "1"], seen_headers)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_headroom_accounts_for_load_and_other_gpu_work(self):
        self.assertFalse(gpu_available({"free_mib": 3500, "util_percent": 0}))
        self.assertTrue(gpu_available({"free_mib": 3500, "util_percent": 0}, loaded=True))
        self.assertFalse(gpu_available({"free_mib": 6000, "util_percent": 25}))
        self.assertFalse(gpu_available({"free_mib": 2000, "util_percent": 0}, loaded=True))

    def test_existing_manual_label_is_preserved_without_post(self):
        api = Frigate()
        with patch.object(api, "json", return_value={"id": "1-a", "label": "car",
                          "camera": "common_camera02", "sub_label": "Human label"}) as request:
            self.assertFalse(api.apply("1-a", "Other label"))
            self.assertEqual(1, request.call_count)

    def test_auto_path_rejects_people_even_without_an_existing_label(self):
        api = Frigate()
        with patch.object(api, "json", return_value={"id": "1-a", "label": "person",
                          "camera": "common_camera02", "sub_label": None}) as request:
            with self.assertRaises(ValueError):
                api.apply("1-a", "Someone")
            self.assertEqual(1, request.call_count)

    def test_label_write_requires_authoritative_readback(self):
        api = Frigate()
        empty = {"id": "1-a", "label": "car", "camera": "common_camera02", "sub_label": None}
        with patch.object(api, "json", return_value=empty), patch("ocr.frigate_object_worker.time.sleep"):
            with self.assertRaises(RuntimeError):
                api.apply("1-a", "Blue car")

    def test_async_label_update_is_confirmed_without_repeating_post(self):
        api = Frigate()
        empty = {"id": "1-a", "label": "car", "camera": "common_camera02", "sub_label": None}
        saved = {**empty, "sub_label": "Blue car"}
        with patch.object(api, "json", side_effect=[empty, {"success": True}, empty, empty, saved]) as request:
            with patch("ocr.frigate_object_worker.time.sleep"):
                self.assertTrue(api.apply("1-a", "Blue car"))
            self.assertEqual(1, sum(call.args[0].endswith("/sub_label") for call in request.call_args_list))

    def test_uncertain_write_is_not_sent_again_on_next_worker_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            self.addCleanup(store.db.close)
            store.profile("car-a", "car", "Car A")
            store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
            store.db.commit()
            store.remember("1-a", "1", ["car-a"], "observing")
            event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                     "sub_label": None, "data": {"snapshot_frame_time": 2}}
            api, encoder = Frigate(), Encoder("/unused", "GPU-unused")
            with patch.object(api, "candidates", return_value=[event]), patch.object(api, "snapshot", return_value=b"jpg"):
                with patch.object(encoder, "encode", return_value=[1, 0]), patch(
                        "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                    with patch.object(api, "apply", side_effect=RuntimeError("Uncertain write")) as write:
                        run(store, encoder, api, once=True)
                        run(store, encoder, api, once=True)
                        self.assertEqual(1, write.call_count)
            self.assertEqual("pending", store.db.execute("SELECT status FROM events WHERE id='1-a'").fetchone()[0])

    def test_path_injection_is_rejected_before_request(self):
        with self.assertRaises(ValueError):
            Frigate().event("../config")

    def test_unload_releases_reference_to_gpu_session(self):
        encoder = Encoder("/unused", "GPU-unused")
        encoder.session = object()
        encoder.unload()
        self.assertIsNone(encoder.session)


if __name__ == "__main__":
    unittest.main()
