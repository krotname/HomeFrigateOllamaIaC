import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from ocr.frigate_object_worker import (
    Encoder, Frigate, Store, calibration, choose, gpu_available, reference_score, run, unit,
)
from ocr.frigate_caption_worker import RemoteError, request_bytes


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

    def test_calibration_abstains_on_a_difficult_minority_without_lowering_threshold(self):
        good, bad = unit([1, 0]), unit([0, 1])
        policy = calibration([good] * 3, [good] * 3 + [bad], [bad] * 3)
        self.assertTrue(policy["enabled"])
        self.assertEqual(.85, policy["threshold"])
        self.assertEqual(.75, policy["positive_coverage"])
        self.assertFalse(calibration([good] * 3, [good] * 2 + [bad] * 2, [bad] * 3)["enabled"])

    def test_duplicate_views_cannot_supply_two_encounter_scores(self):
        good, bad = unit([1, 0]), unit([0, 1])
        self.assertEqual(.5, reference_score(good, [good, bad], ["one", "two"]))
        self.assertEqual(.5, reference_score(good, [good] * 20 + [bad], ["one"] * 20 + ["two"]))
        profile = {"id": "a", "label": "A", "kind": "car",
                   "calibration": {"enabled": True, "threshold": .85, "margin": .06}}
        refs = [{"profile_id": "a", "kind": "car", "role": "train", "vector": v, "group_id": g}
                for v, g in [(good, "one"), (good, "one"), (bad, "two"), (bad, "three")]]
        self.assertIsNone(choose(good, [profile], refs, "car"))



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
        self.store.remember("active", "1", ["cat-a"], "observing")
        self.store.remember("uncertain", "1", ["cat-a"], "pending")
        self.store.add_reference("event-four", None, "cat", "validation", [0, 1])
        self.assertFalse(self.store.profiles()[0]["calibration"].get("enabled", False))
        row = self.store.db.execute("SELECT frame,votes FROM events WHERE id='active'").fetchone()
        self.assertEqual(("", "[]"), tuple(row))
        self.assertEqual("pending", self.store.db.execute("SELECT status FROM events WHERE id='uncertain'").fetchone()[0])

    def test_enrollment_cannot_have_its_invalidation_overwritten_by_concurrent_calibration(self):
        finished, started, errors = threading.Event(), threading.Event(), []

        def enroll():
            started.set()
            try:
                other = Store(self.directory.name)
                try:
                    other.add_reference("concurrent", None, "cat", "validation", [0, 1])
                finally:
                    other.db.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=enroll)

        def calibrate(*_):
            thread.start()
            self.assertTrue(started.wait(2))
            finished.wait(.2)
            return {"enabled": False, "reason": "old_gallery"}

        with patch("ocr.frigate_object_worker.calibration", side_effect=calibrate):
            self.store.calibrate("cat-a")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({}, self.store.profiles()[0]["calibration"])

    def test_calibration_rejects_held_out_positives_that_fail_runtime_margin(self):
        for i in range(3):
            self.store.add_reference(f"train-{i}", "cat-a", "cat", "train", [1, 0], group_id=f"train-visit-{i}")
            self.store.add_reference(f"other-{i}", None, "cat", "validation", [0, 1], group_id=f"other-visit-{i}")
        for i in range(2):
            self.store.add_reference(f"test-{i}", "cat-a", "cat", "validation", [1, 0], group_id=f"test-visit-{i}")
        self.assertTrue(self.store.calibrate("cat-a")["enabled"])
        self.store.profile("cat-b", "cat", "Cat B")
        self.store.add_reference("close-competitor", "cat-b", "cat", "train", [1, .05], group_id="competitor-visit")
        policy = self.store.calibrate("cat-a")
        self.assertFalse(policy["enabled"])
        self.assertEqual("validation_margin_failed", policy["reason"])

    def test_uncertain_write_marker_survives_age_and_capacity_pruning(self):
        self.store.remember("uncertain", "1", [], "pending")
        self.store.db.execute("UPDATE events SET updated=0 WHERE id='uncertain'")
        self.store.db.commit()
        with patch("ocr.frigate_object_worker.MAX_EVENTS", 2):
            for i in range(4):
                self.store.remember(f"ordinary-{i}", "1", [], "complete_unknown")
        rows = dict(self.store.db.execute("SELECT id,status FROM events"))
        self.assertEqual("pending", rows["uncertain"])
        self.assertEqual(3, len(rows))

    def test_dense_easy_visit_cannot_hide_low_coverage_of_other_visits(self):
        for i in range(3):
            self.store.add_reference(f"train-{i}", "cat-a", "cat", "train", [1, 0], group_id=f"train-{i}")
            self.store.add_reference(f"other-{i}", None, "cat", "validation", [0, 1], group_id=f"other-{i}")
        for i in range(8):
            self.store.add_reference(f"easy-{i}", "cat-a", "cat", "validation", [1, 0], group_id="easy-visit")
        for i in range(3):
            self.store.add_reference(f"hard-{i}", "cat-a", "cat", "validation", [0, 1], group_id=f"hard-{i}")
        policy = self.store.calibrate("cat-a")
        self.assertFalse(policy["enabled"])
        # Frame coverage itself is insufficient here, regardless of frame density.
        self.assertLess(policy["positive_coverage"], .75)
        self.store.add_reference("easy-second", "cat-a", "cat", "validation", [1, 0], group_id="easy-second")
        policy = self.store.calibrate("cat-a")
        self.assertFalse(policy["enabled"])
        self.assertEqual(.75, policy["positive_coverage"])
        self.assertEqual(.4, policy["positive_group_coverage"])

    def test_full_uncertain_write_set_blocks_new_pending_before_post(self):
        with patch("ocr.frigate_object_worker.MAX_PENDING", 1):
            self.store.remember("uncertain", "1", [], "pending")
            with self.assertRaises(RuntimeError):
                self.store.remember("another", "2", [], "pending")
        self.assertIsNone(self.store.db.execute("SELECT id FROM events WHERE id='another'").fetchone())

    def test_stale_scan_cannot_restore_votes_cleared_by_another_connection(self):
        other = Store(self.directory.name)
        self.addCleanup(other.db.close)
        version = self.store.data_version()
        other.add_reference("new", None, "cat", "validation", [0, 1])
        self.assertFalse(self.store.remember_current(version, "active", "1", ["cat-a"], "observing"))
        self.assertIsNone(self.store.db.execute("SELECT id FROM events WHERE id='active'").fetchone())


class IntegrationPolicyTests(unittest.TestCase):
    def test_confirmation_requires_consecutive_matches_on_newer_frames(self):
        for votes, timestamp, expected in ((["car-a", "car-b"], 2, False),
                                           (["car-a", None], 2, False),
                                           ([None, "car-a"], 2, True),
                                           (["car-a"], .5, False),
                                           (["car-a"], float("nan"), False)):
            with self.subTest(votes=votes, timestamp=timestamp), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                self.addCleanup(store.db.close)
                store.profile("car-a", "car", "Car A")
                store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
                store.db.commit()
                store.remember("1-a", "1", votes, "observing")
                event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                         "data": {"snapshot_frame_time": timestamp}}
                api, encoder = Frigate(), Encoder("/unused", "GPU-unused")
                with patch.object(api, "candidates", return_value=[event]), patch.object(api, "snapshot", return_value=b"jpg"):
                    with patch.object(encoder, "encode", return_value=[1, 0]) as inference, patch(
                            "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                        with patch.object(api, "apply", return_value=True) as write:
                            run(store, encoder, api, once=True)
                            self.assertEqual(int(expected), write.call_count)
                            if timestamp < 1 or timestamp != timestamp:
                                inference.assert_not_called()

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
        self.assertTrue(gpu_available({"free_mib": 3500, "util_percent": 100}, loaded=True,
                                      check_utilization=False))
        self.assertFalse(gpu_available({"free_mib": 2000, "util_percent": 100}, loaded=True,
                                       check_utilization=False))

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
                    def uncertain(*_, before_write=None):
                        before_write()
                        raise RuntimeError("Uncertain write")

                    with patch.object(api, "apply", side_effect=uncertain) as write:
                        run(store, encoder, api, once=True)
                        run(store, encoder, api, once=True)
                        self.assertEqual(1, write.call_count)
            self.assertEqual("pending", store.db.execute("SELECT status FROM events WHERE id='1-a'").fetchone()[0])

    def test_path_injection_is_rejected_before_request(self):
        with self.assertRaises(ValueError):
            Frigate().event("../config")

    def test_concurrent_enrollment_stops_stale_gallery_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store, other = Store(directory), Store(directory)
            self.addCleanup(store.db.close)
            self.addCleanup(other.db.close)
            store.profile("car-a", "car", "Car A")
            store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
            store.db.commit()
            store.remember("1-a", "1", ["car-a"], "observing")
            event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                     "data": {"snapshot_frame_time": 2}}
            api, encoder = Frigate(), Encoder("/unused", "GPU-unused")

            def enroll(_):
                other.add_reference("another-car", None, "car", "validation", [0, 1])
                return [1, 0]

            with patch.object(api, "candidates", return_value=[event]), patch.object(api, "snapshot", return_value=b"jpg"):
                with patch.object(encoder, "encode", side_effect=enroll), patch(
                        "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                    with patch.object(api, "apply") as write:
                        run(store, encoder, api, once=True)
                        write.assert_not_called()
            self.assertFalse(store.profiles()[0]["calibration"].get("enabled", False))

    def test_bad_snapshot_does_not_starve_the_next_event(self):
        for failure in (RemoteError(404), OSError("Corrupt JPEG")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                self.addCleanup(store.db.close)
                store.profile("car-a", "car", "Car A")
                store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
                store.db.commit()
                store.remember("valid", "1", ["car-a"], "observing")
                event = {"id": "valid", "label": "car", "camera": "common_camera02",
                         "data": {"snapshot_frame_time": 2}}
                api, encoder = Frigate(), Encoder("/unused", "GPU-unused")
                with patch.object(api, "candidates", return_value=[event, {**event, "id": "bad"}]), patch.object(
                        api, "snapshot", side_effect=[failure, b"jpg"]):
                    with patch.object(encoder, "encode", return_value=[1, 0]), patch(
                            "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                        with patch.object(api, "apply", return_value=True) as write:
                            run(store, encoder, api, once=True)
                            self.assertEqual(1, write.call_count)
                            self.assertEqual(("valid", "Car A"), write.call_args.args)

    def test_failed_authoritative_read_can_retry_without_creating_a_pending_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            self.addCleanup(store.db.close)
            store.profile("car-a", "car", "Car A")
            store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
            store.db.commit()
            store.remember("1-a", "1", ["car-a"], "observing")
            event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                     "data": {"snapshot_frame_time": 2}}
            saved = {**event, "sub_label": "Car A"}
            api, encoder = Frigate(), Encoder("/unused", "GPU-unused")
            with patch.object(api, "candidates", return_value=[event]), patch.object(api, "snapshot", return_value=b"jpg"):
                with patch.object(encoder, "encode", return_value=[1, 0]), patch(
                        "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                    with patch.object(api, "json", side_effect=[RemoteError(0), event, {}, saved]) as request:
                        run(store, encoder, api, once=True)
                        self.assertEqual("observing", store.db.execute("SELECT status FROM events").fetchone()[0])
                        run(store, encoder, api, once=True)
                        self.assertEqual("applied", store.db.execute("SELECT status FROM events").fetchone()[0])
                        self.assertEqual(1, sum(call.args[0].endswith("/sub_label") for call in request.call_args_list))

    def test_gallery_changed_during_authoritative_read_prevents_post(self):
        with tempfile.TemporaryDirectory() as directory:
            store, other = Store(directory), Store(directory)
            self.addCleanup(store.db.close)
            self.addCleanup(other.db.close)
            store.profile("car-a", "car", "Car A")
            store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
            store.db.commit()
            store.remember("1-a", "1", ["car-a"], "observing")
            event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                     "data": {"snapshot_frame_time": 2}}
            api, encoder = Frigate(), Encoder("/unused", "GPU-unused")

            def authoritative_read(_):
                other.add_reference("new", None, "car", "validation", [0, 1])
                return event

            with patch.object(api, "candidates", return_value=[event]), patch.object(api, "snapshot", return_value=b"jpg"):
                with patch.object(encoder, "encode", return_value=[1, 0]), patch(
                        "ocr.frigate_object_worker.choose", return_value={"id": "car-a", "label": "Car A"}):
                    with patch.object(api, "json", side_effect=authoritative_read) as request:
                        run(store, encoder, api, once=True)
                        self.assertEqual(1, request.call_count)
                        self.assertEqual("observing", store.db.execute("SELECT status FROM events").fetchone()[0])

    def test_unload_releases_reference_to_gpu_session(self):
        encoder = Encoder("/unused", "GPU-unused")
        encoder.session = object()
        encoder.unload()
        self.assertIsNone(encoder.session)


if __name__ == "__main__":
    unittest.main()
