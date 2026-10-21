import json
import importlib.util
import io
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from ocr.frigate_object_worker import (
    Encoder, Frigate, Store, bind_car_model, bind_feature_models, calibration, choose, gpu_available, reference_score, run, unit,
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

    def test_vehicle_scale_keeps_a_safety_gap_to_other_training_identities(self):
        train = [unit([1, 0])] * 3
        positive = [unit([.7, math.sqrt(1 - .7 ** 2)])] * 2
        negative = [unit([0, 1])] * 3
        self.assertFalse(calibration(train, positive, negative)["enabled"])
        self.assertTrue(calibration(train, positive, negative, minimum_similarity=.60)["enabled"])
        lookalike = unit([.68, math.sqrt(1 - .68 ** 2)])
        result = calibration(train, positive, negative, minimum_similarity=.60,
                             negative_train=[lookalike])
        self.assertFalse(result["enabled"])
        self.assertGreater(result["threshold"], .7)

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_car_rgb_and_cat_normalization_use_separate_models_and_dimensions(self):
        import numpy as np
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (80, 40), (10, 20, 30)).save(buffer, format="PNG")
        encoder = Encoder("cat-model", "gpu", car_model="car-model")
        with patch.object(encoder, "infer", side_effect=[np.ones(512), np.ones(384)]) as infer:
            self.assertEqual(512, len(encoder.encode(buffer.getvalue(), "car")))
            car_tensor = infer.call_args.args[0]
            self.assertEqual((1, 3, 208, 208), car_tensor.shape)
            self.assertEqual([10., 20., 30.], car_tensor[0, :, 0, 0].tolist())
            self.assertEqual("car-model", encoder.model)
            encoder.session = object()
            self.assertEqual(384, len(encoder.encode(buffer.getvalue(), "cat")))
            self.assertEqual((1, 3, 224, 224), infer.call_args.args[0].shape)
            self.assertEqual("cat-model", encoder.model)
            self.assertIsNone(encoder.session)
        with patch.object(encoder, "infer", return_value=np.ones(384)):
            with self.assertRaisesRegex(ValueError, "dimensions"):
                encoder.encode(buffer.getvalue(), "car")



class StorageTests(unittest.TestCase):
    def test_two_wheeler_model_change_invalidates_its_gallery_only(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            model, car = Path(directory, "base"), Path(directory, "car")
            model.write_bytes(b"base-v1")
            car.write_bytes(b"car-v1")
            encoder = Encoder(str(model), "gpu", car_model=str(car))
            bind_feature_models(self.store, encoder)
            self.store.profile("bike", "two_wheeler", "Bike")
            self.store.profile("cat", "cat", "Cat")
            self.store.add_reference("bike-event", "bike", "two_wheeler", "train", [1, 0], group_id="bike-visit")
            self.store.add_reference("cat-event", "cat", "cat", "train", [1, 0], group_id="cat-visit")
            model.write_bytes(b"base-v2-different")
            bind_feature_models(self.store, encoder)
            self.assertEqual(["cat-event"], [row["source"] for row in self.store.refs()])

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

    def test_changed_car_weights_invalidate_only_car_references_and_keep_uncertain_writes(self):
        self.store.profile("car-a", "car", "Car A")
        self.store.bind_model("car", "a" * 64)
        self.store.add_reference("cat-frame", "cat-a", "cat", "train", [1, 0], group_id="cat-visit")
        self.store.add_reference("car-frame", "car-a", "car", "train", [1, 0], group_id="car-visit")
        self.store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
        self.store.db.commit()
        self.store.remember("observed", "20", ["car-a"], "observing")
        self.store.remember("uncertain", "21", ["car-a", "car-a"], "pending")
        self.assertFalse(self.store.bind_model("car", "a" * 64))
        self.assertEqual(2, len(self.store.refs()))
        self.assertTrue(self.store.bind_model("car", "b" * 64))
        self.assertEqual(["cat-frame"], [r["source"] for r in self.store.refs()])
        profiles = {p["id"]: p for p in self.store.profiles()}
        self.assertEqual({}, profiles["car-a"]["calibration"])
        self.assertTrue(profiles["cat-a"]["calibration"]["enabled"])
        self.assertEqual("[]", self.store.db.execute("SELECT votes FROM events WHERE id='observed'").fetchone()[0])
        self.assertEqual("pending", self.store.db.execute("SELECT status FROM events WHERE id='uncertain'").fetchone()[0])
        self.store.add_reference("car-frame", "car-a", "car", "train", [0, 1], group_id="car-visit")
        self.assertEqual(2, len(self.store.refs()))

    def test_replaced_car_model_is_rejected_before_inference_and_rebind_discards_old_gallery(self):
        from pathlib import Path
        model = Path(self.directory.name, "car.onnx")
        model.write_bytes(b"old")
        encoder = Encoder("cat.onnx", "gpu", car_model=str(model))
        bind_car_model(self.store, encoder)
        self.store.profile("car-a", "car", "Car A")
        self.store.add_reference("car-frame", "car-a", "car", "train", [1, 0], group_id="car-visit")
        model.write_bytes(b"new")
        # The check precedes image parsing and any GPU session load.
        if importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"):
            with self.assertRaisesRegex(RuntimeError, "Car model changed"):
                encoder.encode(b"unused", "car")
        bind_car_model(self.store, encoder)
        self.assertEqual([], self.store.refs())

    def test_stale_reference_reencoding_replaces_at_capacity_and_invalidates_policy(self):
        self.store.add_reference("old-frame", "cat-a", "cat", "validation", [1, 0], group_id="visit-a")
        self.store.db.execute("UPDATE refs SET model='previous-feature-space'")
        self.store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
        self.store.db.commit()
        self.store.remember("event-one", "123", ["cat-a"], "observing")
        with patch("ocr.frigate_object_worker.MAX_REFERENCES", 1):
            self.store.add_reference("old-frame", "cat-a", "cat", "validation", [0, 1], group_id="visit-a")
        reference = self.store.refs()[0]
        self.assertEqual(1, len(self.store.refs()))
        self.assertEqual([0, 1], reference["vector"])
        self.assertEqual(("cat-a", "validation", "visit-a"),
                         (reference["profile_id"], reference["role"], reference["group_id"]))
        self.assertEqual({}, self.store.profiles()[0]["calibration"])
        self.assertEqual("[]", self.store.db.execute("SELECT votes FROM events").fetchone()[0])

    def test_stale_reference_reencoding_cannot_change_identity_role_or_encounter(self):
        self.store.profile("cat-b", "cat", "Cat B")
        self.store.add_reference("old-frame", "cat-a", "cat", "train", [1, 0], group_id="visit-a")
        self.store.db.execute("UPDATE refs SET model='previous-feature-space'")
        self.store.db.commit()
        for profile, role, group in (("cat-b", "train", "visit-b"),
                                     ("cat-a", "validation", "visit-b"),
                                     ("cat-a", "train", "visit-b")):
            with self.assertRaisesRegex(ValueError, "Re-encoding must preserve"):
                self.store.add_reference("old-frame", profile, "cat", role, [0, 1], group_id=group)
        row = self.store.db.execute("SELECT model,vector,role,group_id FROM refs").fetchone()
        self.assertEqual("previous-feature-space", row["model"])
        self.assertEqual([1, 0], json.loads(row["vector"]))
        self.assertEqual(("train", "visit-a"), (row["role"], row["group_id"]))

    def test_feature_revision_change_invalidates_thresholds_and_temporal_votes(self):
        self.store.add_reference("old-frame", "cat-a", "cat", "train", [1, 0], group_id="visit-a")
        self.store.remember("event-one", "123", ["cat-a"], "observing")
        self.store.remember("previous-unknown", "124", [], "complete_unknown")
        self.store.db.execute("UPDATE refs SET model='previous-feature-space'")
        self.store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
        self.store.db.commit()
        reopened = Store(self.directory.name)
        try:
            self.assertEqual([], reopened.refs())
            self.assertEqual({}, reopened.profiles()[0]["calibration"])
            event = reopened.db.execute("SELECT * FROM events WHERE id='event-one'").fetchone()
            self.assertEqual("[]", event["votes"])
            self.assertEqual("", event["frame"])
            unknown = reopened.db.execute("SELECT * FROM events WHERE id='previous-unknown'").fetchone()
            self.assertEqual("observing", unknown["status"])
            self.assertEqual("", unknown["frame"])
            self.assertEqual(1, reopened.db.execute("SELECT count(*) FROM refs").fetchone()[0])
        finally:
            reopened.db.close()

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

    def test_one_good_frame_per_hard_visit_does_not_make_dense_validation_pass(self):
        for i in range(3):
            self.store.add_reference(f"train-{i}", "cat-a", "cat", "train", [1, 0], group_id=f"train-{i}")
            self.store.add_reference(f"other-{i}", None, "cat", "validation", [0, 1], group_id=f"other-{i}")
        for group, count, accepted in (("easy", 100, 100), ("hard-a", 10, 1),
                                       ("hard-b", 10, 1), ("hard-c", 10, 1)):
            for i in range(count):
                self.store.add_reference(f"{group}-{i}", "cat-a", "cat", "validation",
                                         [1, 0] if i < accepted else [0, 1], group_id=group)
        policy = self.store.calibrate("cat-a")
        self.assertFalse(policy["enabled"])
        self.assertGreater(policy["positive_coverage"], .75)
        self.assertEqual(4, policy["accepted_positive_groups"])
        self.assertAlmostEqual(.325, policy["positive_group_coverage"])

    def test_stale_scan_cannot_restore_votes_cleared_by_another_connection(self):
        other = Store(self.directory.name)
        self.addCleanup(other.db.close)
        version = self.store.data_version()
        other.add_reference("new", None, "cat", "validation", [0, 1])
        self.assertFalse(self.store.remember_current(version, "active", "1", ["cat-a"], "observing"))
        self.assertIsNone(self.store.db.execute("SELECT id FROM events WHERE id='active'").fetchone())


class IntegrationPolicyTests(unittest.TestCase):
    def test_two_wheeler_class_flip_keeps_identity_and_confirmation_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            self.addCleanup(store.db.close)
            store.profile("scooter-a", "two_wheeler", "Scooter A")
            store.db.execute("UPDATE profiles SET calibration=?", (json.dumps({"enabled": True}),))
            store.db.commit()
            api, encoder = Frigate(), Encoder("/unused", "GPU-unused")
            match = {"id": "scooter-a", "label": "Scooter A"}
            with patch.object(api, "snapshot", return_value=b"jpg"), patch.object(
                    encoder, "encode", return_value=[1, 0]) as encode, patch(
                    "ocr.frigate_object_worker.choose", return_value=match) as choose_match, patch.object(
                    api, "apply", return_value=True) as write:
                for label, frame in (("bicycle", 1), ("motorcycle", 1), ("motorcycle", 2)):
                    event = {"id": "1-a", "label": label, "camera": "common_camera02",
                             "data": {"snapshot_frame_time": frame}}
                    with patch.object(api, "candidates", return_value=[event]):
                        run(store, encoder, api, once=True)
                    self.assertEqual(int(frame == 2), write.call_count)
                self.assertEqual(2, encode.call_count)
                self.assertTrue(all(call.args[1] == "two_wheeler" for call in encode.call_args_list))
                self.assertTrue(all(call.args[3] == "two_wheeler" for call in choose_match.call_args_list))

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
            self.assertFalse(api.apply("1-a", "Other label", expected_kind="car"))
            self.assertEqual(1, request.call_count)

    def test_auto_path_rejects_people_even_without_an_existing_label(self):
        api = Frigate()
        with patch.object(api, "json", return_value={"id": "1-a", "label": "person",
                          "camera": "common_camera02", "sub_label": None}) as request:
            with self.assertRaises(ValueError):
                api.apply("1-a", "Someone")
            self.assertEqual(1, request.call_count)

    def test_auto_write_rechecks_gallery_but_accepts_two_wheeler_class_flip(self):
        api = Frigate()
        for current, accepted in (("car", False), ("cat", False),
                                  ("bicycle", True), ("motorcycle", True)):
            with self.subTest(current=current):
                event = {"id": "1-a", "label": current, "camera": "common_camera02",
                         "sub_label": "Scooter A"}
                with patch.object(api, "json", return_value=event) as request:
                    if accepted:
                        self.assertTrue(api.apply("1-a", "Scooter A", expected_kind="two_wheeler"))
                    else:
                        with self.assertRaises(ValueError):
                            api.apply("1-a", "Scooter A", expected_kind="two_wheeler")
                    self.assertFalse(any(call.args[0].endswith("/sub_label")
                                         for call in request.call_args_list))

    def test_label_write_requires_authoritative_readback(self):
        api = Frigate()
        empty = {"id": "1-a", "label": "car", "camera": "common_camera02", "sub_label": None}
        with patch.object(api, "json", return_value=empty), patch("ocr.frigate_object_worker.time.sleep"):
            with self.assertRaises(RuntimeError):
                api.apply("1-a", "Blue car", expected_kind="car")

    def test_async_label_update_is_confirmed_without_repeating_post(self):
        api = Frigate()
        empty = {"id": "1-a", "label": "car", "camera": "common_camera02", "sub_label": None}
        saved = {**empty, "sub_label": "Blue car"}
        with patch.object(api, "json", side_effect=[empty, {"success": True}, empty, empty, saved]) as request:
            with patch("ocr.frigate_object_worker.time.sleep"):
                self.assertTrue(api.apply("1-a", "Blue car", expected_kind="car"))
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
                    def uncertain(*_, expected_kind=None, before_write=None):
                        self.assertEqual("car", expected_kind)
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

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "image library available in GPU runtime")
    def test_car_snapshot_uses_native_recording_but_cat_keeps_verified_jpeg(self):
        from PIL import Image
        def jpeg(size):
            output = io.BytesIO()
            Image.new("RGB", size).save(output, format="JPEG")
            return output.getvalue()
        small, native = jpeg((352, 288)), jpeg((1920, 1080))
        for kind in ("car", "cat"):
            event = {"id": "1-a", "label": kind, "camera": "common_camera02",
                     "data": {"snapshot_frame_time": 20, "box": [.25, .25, .5, .5]}}
            responses = [(small, {"x-frame-time": "20"})] + ([native] if kind == "car" else [])
            with self.subTest(kind=kind), patch("ocr.frigate_object_worker.request_bytes", side_effect=responses) as request, \
                    patch.object(Frigate, "json", return_value=[{"id": "segment", "start_time": 19, "end_time": 21}]):
                crop = Frigate().snapshot(event)
                with Image.open(io.BytesIO(crop)) as image:
                    self.assertEqual(kind == "car", image.width > 1000)
                self.assertEqual(2 if kind == "car" else 1, request.call_count)
                if kind == "car":
                    self.assertIn("/common_camera02/recordings/20.0/snapshot.jpg?height=1080", request.call_args.args[0])
                    self.assertTrue(request.call_args.kwargs["bypass_cache"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "image library available in GPU runtime")
    def test_car_recording_requires_matching_event_frame_and_missing_segment_is_retryable(self):
        event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                 "data": {"snapshot_frame_time": 20, "box": [.25, .25, .5, .5]}}
        with patch("ocr.frigate_object_worker.request_bytes", return_value=(b"jpg", {"x-frame-time": "19"})) as request:
            with self.assertRaises(ValueError):
                Frigate().snapshot(event)
            self.assertEqual(1, request.call_count)
        with patch("ocr.frigate_object_worker.request_bytes", side_effect=[
                (b"jpg", {"x-frame-time": "20"}), RemoteError(404)]) as request, \
                patch.object(Frigate, "json", return_value=[{"id": "segment", "start_time": 19, "end_time": 21}]):
            with self.assertRaises(RemoteError):
                Frigate().snapshot(event)
            self.assertEqual(2, request.call_count)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "image library available in GPU runtime")
    def test_recording_gaps_and_retention_changes_cannot_supply_a_substituted_frame(self):
        event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                 "data": {"snapshot_frame_time": 20, "box": [.25, .25, .5, .5]}}
        for segments in ([], [{"id": "rounded-up", "start_time": 20.1, "end_time": 21}]):
            with self.subTest(segments=segments), patch.object(Frigate, "json", return_value=segments), \
                    patch("ocr.frigate_object_worker.request_bytes", return_value=(b"jpg", {"x-frame-time": "20"})) as request:
                with self.assertRaisesRegex(ValueError, "exact best-frame"):
                    Frigate().snapshot(event)
                self.assertEqual(1, request.call_count)
        with patch.object(Frigate, "json", side_effect=[[{"id": "expired", "start_time": 19, "end_time": 21}], []]), \
                patch("ocr.frigate_object_worker.request_bytes", side_effect=[(b"jpg", {"x-frame-time": "20"}), b"native"]):
            with self.assertRaisesRegex(ValueError, "coverage changed"):
                Frigate().snapshot(event)

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

            def enroll(_, kind):
                self.assertEqual("car", kind)
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
