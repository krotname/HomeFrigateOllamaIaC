import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from ocr.frigate_object_archive import detections, event_pages, recording_spans, sample_plan, scan
from ocr.frigate_caption_worker import RemoteError
from ocr.frigate_object_worker import Frigate, Store


class ArchiveTests(unittest.TestCase):
    def test_sampling_records_gaps_instead_of_claiming_missing_video(self):
        samples, gaps = sample_plan([
            {"start_time": 0, "end_time": 5}, {"start_time": 20, "end_time": 40}], 0, 40, 10)
        self.assertEqual([0., 20., 30.], samples)
        self.assertEqual(1, gaps)

    def test_pagination_includes_many_events_sharing_a_timestamp(self):
        events = [{"id": str(n), "start_time": n // 150} for n in range(300)]

        def fetch(path):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            low, high = float(query["after"][0]), float(query["before"][0])
            return [event for event in events if low < event["start_time"] < high][:200]

        self.assertEqual(300, len(event_pages(-1, 2, fetch)))

    def test_overlapping_recordings_do_not_hide_frames_or_inflate_coverage(self):
        recordings = [{"start_time": 0, "end_time": 40}, {"start_time": 10, "end_time": 15},
                      {"start_time": 35, "end_time": 60}]
        self.assertEqual([[5, 50]], recording_spans(recordings, 5, 50))
        self.assertEqual(([0., 10., 20., 30., 40., 50.], 0), sample_plan(recordings, 0, 60, 10))

    def test_large_sampling_plan_stops_at_budget(self):
        with self.assertRaises(ValueError):
            sample_plan([{"start_time": 0, "end_time": 604800}], 0, 604800, 1, max_samples=10)

    def test_saturated_equal_timestamp_fails_closed(self):
        with self.assertRaises(RuntimeError):
            event_pages(0, .0001, lambda path: [{"id": str(n), "start_time": 0} for n in range(200)])

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy available in GPU runtime")
    def test_detector_does_not_turn_a_person_into_a_car(self):
        import numpy as np
        output = np.zeros((1, 84, 2100), dtype=np.float32)
        output[0, :4, :3] = np.array([[160, 160, 160], [160, 160, 160],
                                             [100, 100, 100], [100, 100, 100]])
        output[0, 4, 0] = .95  # person must win over the secondary car score
        output[0, 6, 0] = .6
        output[0, 6, 1] = .9
        output[0, 19, 2] = .8
        result = detections(output)
        self.assertEqual(["car", "cat"], [item["kind"] for item in result])
        self.assertAlmostEqual(.9, result[0]["score"], places=5)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow available in GPU runtime")
    def test_snapshot_explicitly_disables_native_bounding_box(self):
        from PIL import Image
        data = io.BytesIO()
        Image.new("RGB", (640, 480)).save(data, format="JPEG")
        event = {"id": "1-a", "label": "car", "camera": "common_camera02",
                 "data": {"snapshot_frame_time": 10, "box": [.1, .1, .5, .5]}}
        with patch("ocr.frigate_object_worker.request_bytes", return_value=(data.getvalue(), {"x-frame-time": "10"})) as request:
            Frigate().snapshot(event)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(["0"], query["bounding_box"])
        self.assertNotIn("bbox", query)

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_missing_video_does_not_claim_cuda_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=directory, after=0, before=1, interval=1, event_after=0,
                max_frames=2, max_seconds=10, max_crops_per_kind=1, detector="d", model="m", gpu="gpu")
            plan = {"after": 0, "before": 1, "event_after": 0, "interval": 1,
                "events": [], "coverage": {}, "jobs": [{"id": "f1", "camera": "common_camera01",
                                                       "time": 0, "event": None}]}
            with patch("ocr.frigate_object_archive.build_plan", return_value=plan), \
                    patch("ocr.frigate_object_archive.request_bytes", side_effect=RemoteError(404, "absent")):
                scan(args)
            with open(directory + "/report.json", encoding="utf-8") as stream:
                report = json.load(stream)
            self.assertTrue(report["complete"])
            self.assertEqual({"error": 1}, report["status_counts"])
            self.assertEqual({}, report["executed_providers"])

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_full_car_budget_still_accepts_a_cat(self):
        from PIL import Image
        from pathlib import Path
        image = Image.new("RGB", (64, 64))
        data = io.BytesIO()
        image.save(data, format="JPEG")

        class FakeEncoder:
            def __init__(self, *args, audit=False):
                self.audit, self.load_mib = audit, 1
            def infer(self, tensor):
                return None
            def encode(self, data):
                return [1., 0.]
            def audit_providers(self):
                return ["CUDAExecutionProvider"]
            def unload(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=directory, after=0, before=1, interval=1, event_after=0,
                max_frames=2, max_seconds=10, max_crops_per_kind=1, detector="d", model="m", gpu="gpu")
            plan = {"after": 0, "before": 1, "event_after": 0, "interval": 1,
                "events": [], "coverage": {}, "jobs": [{"id": "f1", "camera": "common_camera01",
                                                       "time": 0, "event": None}]}
            Path(directory, "results.jsonl").write_text(json.dumps({"id": "old", "camera": "common_camera01",
                "status": "sampled", "crops": [{"kind": "car", "fingerprint": 0}]}) + "\n", encoding="utf-8")
            plan["jobs"].insert(0, {"id": "old", "camera": "common_camera01", "time": 0, "event": None})
            objects = [{"kind": kind, "box": [0, 0, 1, 1], "score": .9} for kind in ("car", "cat")]
            with patch("ocr.frigate_object_archive.build_plan", return_value=plan), \
                    patch("ocr.frigate_object_archive.Encoder", FakeEncoder), \
                    patch("ocr.frigate_object_archive.request_bytes", return_value=data.getvalue()), \
                    patch("ocr.frigate_object_archive.detections", return_value=objects), \
                    patch("ocr.frigate_object_archive.crop_image", return_value=(image, 7, 10)):
                scan(args)
            report = json.loads(Path(directory, "report.json").read_text(encoding="utf-8"))
            self.assertEqual({"car": 1, "cat": 1}, report["crops"])
            self.assertEqual({"car": 1}, report["budget_skips"])
            self.assertTrue(report["complete"])

    def test_encounter_cannot_leak_between_train_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.profile("car-a", "car", "Car A")
                store.add_reference("frame-a", "car-a", "car", "train", [1, 0], group_id="visit-a")
                with self.assertRaises(ValueError):
                    store.add_reference("frame-b", "car-a", "car", "validation", [1, 0], group_id="visit-a")
                self.assertEqual(1, len(store.refs()))
            finally:
                store.db.close()

    def test_many_frames_from_one_visit_do_not_enable_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.profile("car-a", "car", "Car A")
                for n in range(3):
                    store.add_reference(f"train-{n}", "car-a", "car", "train", [1, 0], group_id="one-visit")
                for n in range(2):
                    store.add_reference(f"positive-{n}", "car-a", "car", "validation", [1, 0])
                for n in range(3):
                    store.add_reference(f"negative-{n}", None, "car", "validation", [0, 1])
                policy = store.calibrate("car-a")
                self.assertFalse(policy["enabled"])
                self.assertEqual(1, policy["independent_groups"]["train"])
            finally:
                store.db.close()

    def test_existing_gallery_migrates_without_losing_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(directory + "/objects.sqlite")
            db.execute("CREATE TABLE refs(source TEXT PRIMARY KEY,profile_id TEXT,kind TEXT,role TEXT,vector TEXT,model TEXT)")
            db.execute("INSERT INTO refs VALUES('old',NULL,'car','validation','[1,0]','mobilenetv2-12-gap-v1')")
            db.commit()
            db.close()
            store = Store(directory)
            try:
                self.assertEqual("old", store.refs()[0]["group_id"])
                self.assertEqual([1, 0], store.refs()[0]["vector"])
            finally:
                store.db.close()
