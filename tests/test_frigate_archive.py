import importlib.util
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from ocr.frigate_object_archive import detections, event_pages, read_results, recording_spans, sample_plan, scan
from ocr.frigate_caption_worker import RemoteError
from ocr.frigate_object_worker import Encoder, Frigate, MODEL_REVISION, Store, import_archive, wait_for_gpu


class ArchiveTests(unittest.TestCase):
    def test_two_cuda_sessions_account_only_for_their_own_calls(self):
        session = SimpleNamespace(disable_fallback=lambda: None,
            get_providers=lambda: ["CUDAExecutionProvider"],
            get_inputs=lambda: [SimpleNamespace(name="input")], run=lambda *args: ["output"])
        ort = SimpleNamespace(preload_dlls=lambda: None,
            SessionOptions=lambda: SimpleNamespace(add_session_config_entry=lambda *args: None),
            InferenceSession=lambda *args, **kwargs: session)
        stats = lambda free: {"free_mib": free, "util_percent": 0}
        detector, encoder = Encoder("detector", "gpu"), Encoder("encoder", "gpu")
        with patch.dict("sys.modules", {"onnxruntime": ort}), \
                patch("ocr.frigate_object_worker.wait_for_gpu", side_effect=[stats(x) for x in (6000, 4990, 4280)]), \
                patch("ocr.frigate_object_worker.gpu_stats", side_effect=[stats(x) for x in (5000, 4990, 4290, 4280, 4270)]), \
                patch("ocr.frigate_object_worker.time.sleep"):
            detector.infer(None)
            encoder.infer(None)
            detector.infer(None)
        self.assertEqual(1020, detector.load_mib)
        self.assertEqual(710, encoder.load_mib)

    def test_profileless_training_is_rejected_without_changing_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with self.assertRaisesRegex(ValueError, "Negative references require validation"):
                    store.add_reference("negative", None, "car", "train", [0, 1], group_id="visit")
                self.assertEqual([], store.refs())
                store.add_reference("negative", None, "car", "validation", [0, 1], group_id="visit")
                self.assertEqual(1, len(store.refs()))
            finally:
                store.db.close()

    def test_padded_encounters_are_one_visit_and_blank_is_uncurated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.profile("car-a", "car", "Car A")
                for index, group in enumerate(("visit-a", "visit-a ", " visit-a  ")):
                    store.add_reference(str(index), "car-a", "car", "train", [1, 0], group_id=group)
                self.assertEqual(1, store.calibrate("car-a")["independent_groups"]["train"])
                with self.assertRaises(ValueError):
                    store.add_reference("cross-role", "car-a", "car", "validation", [1, 0], group_id=" visit-a ")
                store.add_reference("blank", "car-a", "car", "train", [1, 0], group_id=" \t ")
                self.assertEqual("uncurated_encounters", store.calibrate("car-a")["reason"])
                store.curate_reference("blank", " visit-b ")
                self.assertEqual("visit-b", next(r for r in store.refs() if r["source"] == "blank")["group_id"])
            finally:
                store.db.close()

    def test_existing_padded_encounters_invalidate_cached_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.profile("car-a", "car", "Car A")
            store.add_reference("old", "car-a", "car", "train", [1, 0], group_id="visit-a")
            store.db.execute("UPDATE refs SET group_id='visit-a '")
            store.db.execute("UPDATE profiles SET calibration=?", ('{"enabled":true}',))
            store.db.commit()
            store.db.close()
            store = Store(directory)
            try:
                self.assertEqual("", store.refs()[0]["group_id"])
                self.assertEqual([1, 0], store.refs()[0]["vector"])
                self.assertEqual({}, store.profiles()[0]["calibration"])
            finally:
                store.db.close()

    def test_explicit_archive_import_reuses_vector_and_rejects_duplicate(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.onnx"
            model.write_bytes(b"model-a")
            (root / "plan.json").write_text(json.dumps({"models": {
                "feature_revision": MODEL_REVISION, "model": hashlib.sha256(b"model-a").hexdigest()}}))
            filename = "frame-00001-0-car.jpg"
            (root / filename).write_bytes(b"crop")
            row = {"id": "frame-00001", "camera": "common_camera02", "time": 10,
                   "status": "sampled", "event": None, "crops": [{"image": filename,
                   "kind": "car", "vector": [1.] + [0.] * 1279, "box": [.1, .1, .5, .5],
                   "confirmed_profile": None, "group_id": None}]}
            (root / "results.jsonl").write_text(json.dumps(row) + "\n")
            store = Store(root / "gallery")
            try:
                store.profile("car-a", "car", "Car A")
                with self.assertRaisesRegex(ValueError, "Negative references require validation"):
                    import_archive(store, root, filename, None, "train", "visit-negative", model)
                self.assertEqual([], store.refs())
                with patch("ocr.frigate_object_worker.Encoder") as encoder:
                    result = import_archive(store, root, filename, "car-a", "train", " visit-a ", model)
                    encoder.assert_not_called()
                self.assertTrue(result["imported"])
                self.assertEqual("visit-a", store.refs()[0]["group_id"])
                self.assertEqual(row["crops"][0]["vector"], store.refs()[0]["vector"])
                # Renumbering a proposal must not permit train/validation reuse.
                renamed = "frame-00002-0-car.jpg"
                (root / renamed).write_bytes(b"crop")
                row["crops"][0]["image"] = renamed
                (root / "results.jsonl").write_text(json.dumps(row) + "\n")
                with self.assertRaisesRegex(ValueError, "Source already enrolled"):
                    import_archive(store, root, renamed, "car-a", "validation", "visit-b", model)
                model.write_bytes(b"model-b")
                with self.assertRaisesRegex(ValueError, "original feature model identity"):
                    import_archive(store, root, renamed, "car-a", "train", "visit-a", model)
                with self.assertRaises(ValueError):
                    import_archive(store, root, "../" + filename, "car-a", "train", "visit-a", model)
                self.assertEqual(1, len(store.refs()))
            finally:
                store.db.close()

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_resume_rejects_crop_budget_below_saved_count_before_loading_gpu(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=directory, after=0, before=1, interval=1, event_after=0,
                max_frames=2, max_seconds=10, max_crops_per_kind=1, detector="d", model="m", gpu="gpu")
            plan = {"after": 0, "before": 1, "event_after": 0, "interval": 1,
                "events": [], "coverage": {}, "jobs": [{"id": "f1"}]}
            previous = json.dumps({"id": "f1", "camera": "common_camera01", "status": "sampled",
                                   "crops": [{"kind": "car"}, {"kind": "car"}]}) + "\n"
            Path(directory, "results.jsonl").write_text(previous)
            with patch("ocr.frigate_object_archive.model_identity", return_value={"test": "model-a"}), \
                    patch("ocr.frigate_object_archive.build_plan", return_value=plan), \
                    patch("ocr.frigate_object_archive.Encoder") as encoder:
                with self.assertRaisesRegex(ValueError, "crop count exceeds"):
                    scan(args)
                encoder.assert_not_called()
            self.assertEqual(previous, Path(directory, "results.jsonl").read_text())

    def test_interrupted_tail_is_recovered_but_internal_corruption_is_rejected(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "results.jsonl")
            complete = b'{"id":"first"}\n'
            tail = b'{"id":"second","text":"\xd0'
            path.write_bytes(complete + tail)
            self.assertEqual(([{"id": "first"}], len(tail)), read_results(path))
            self.assertEqual(complete, path.read_bytes())
            path.write_bytes(complete.rstrip(b"\n"))
            self.assertEqual(([{"id": "first"}], 0), read_results(path))
            self.assertEqual(complete, path.read_bytes())
            corrupt = complete + b'broken\n' + complete
            path.write_bytes(corrupt)
            with self.assertRaises(ValueError):
                read_results(path)
            self.assertEqual(corrupt, path.read_bytes())

    def test_migrated_reference_can_be_curated_without_reembedding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.profile("car-a", "car", "Car A")
                store.add_reference("old", "car-a", "car", "train", [1, 0])
                store.curate_reference("old", "visit-a")
                self.assertEqual("visit-a", store.refs()[0]["group_id"])
                self.assertEqual([1, 0], store.refs()[0]["vector"])
                store.add_reference("second-camera", "car-a", "car", "train", [1, 0], group_id="visit-a")
                with self.assertRaises(ValueError):
                    store.curate_reference("old", "visit-a", "validation")
                self.assertEqual("train", store.refs()[0]["role"])
                store.curate_reference("old", "visit-b", "validation")
                self.assertEqual("validation", store.refs()[0]["role"])
                self.assertFalse(store.profiles()[0]["calibration"].get("enabled", False))
            finally:
                store.db.close()

    def test_sampler_waits_for_stale_utilization_but_never_ignores_contention(self):
        ready = {"free_mib": 6000, "util_percent": 0}
        busy = {"free_mib": 6000, "util_percent": 60}
        with patch("ocr.frigate_object_worker.gpu_stats", side_effect=[busy, ready]), \
                patch("ocr.frigate_object_worker.time.sleep") as sleep:
            self.assertEqual(ready, wait_for_gpu("gpu", attempts=3))
            sleep.assert_called_once_with(.25)
        with patch("ocr.frigate_object_worker.gpu_stats", return_value=busy) as stats, \
                patch("ocr.frigate_object_worker.time.sleep"):
            with self.assertRaises(RuntimeError):
                wait_for_gpu("gpu", attempts=3)
            self.assertEqual(3, stats.call_count)
        with patch("ocr.frigate_object_worker.gpu_stats", return_value={"free_mib": 100, "util_percent": 0}), \
                patch("ocr.frigate_object_worker.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                wait_for_gpu("gpu", attempts=3)
            sleep.assert_not_called()

    def test_events_without_curated_visits_cannot_enable_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                store.profile("car-a", "car", "Car A")
                for i in range(3):
                    store.add_reference(f"camera-a-{i}", "car-a", "car", "train", [1, 0])
                    store.add_reference(f"negative-{i}", None, "car", "validation", [0, 1])
                for i in range(2):
                    store.add_reference(f"camera-b-{i}", "car-a", "car", "validation", [1, 0])
                policy = store.calibrate("car-a")
                self.assertFalse(policy["enabled"])
                self.assertEqual("uncurated_encounters", policy["reason"])
                self.assertEqual({"train": 0, "positive_test": 0, "negative_test": 0}, policy["independent_groups"])
            finally:
                store.db.close()

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_resume_rejects_replaced_model_at_same_path(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            model, detector = Path(directory, "encoder.onnx"), Path(directory, "detector.onnx")
            model.write_bytes(b"model-a")
            detector.write_bytes(b"detector-a")
            args = SimpleNamespace(output=directory, after=0, before=1, interval=1, event_after=0,
                max_frames=2, max_seconds=10, max_crops_per_kind=1, detector=str(detector), model=str(model), gpu="gpu")
            plan = {"after": 0, "before": 1, "event_after": 0, "interval": 1,
                "events": [], "coverage": {}, "jobs": []}
            with patch("ocr.frigate_object_archive.build_plan", return_value=plan):
                scan(args)
            model.write_bytes(b"model-b")
            with self.assertRaisesRegex(ValueError, "original model identities"):
                scan(args)
            self.assertEqual("", Path(directory, "results.jsonl").read_text())

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
        self.assertEqual(["0"], query["bbox"])
        self.assertNotIn("bounding_box", query)

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"),
                         "image libraries available in GPU runtime")
    def test_missing_video_does_not_claim_cuda_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=directory, after=0, before=1, interval=1, event_after=0,
                max_frames=2, max_seconds=10, max_crops_per_kind=1, detector="d", model="m", gpu="gpu")
            plan = {"after": 0, "before": 1, "event_after": 0, "interval": 1,
                "events": [], "coverage": {}, "jobs": [{"id": "f1", "camera": "common_camera01",
                                                       "time": 0, "event": None}]}
            with patch("ocr.frigate_object_archive.model_identity", return_value={"test": "model-a"}), \
                    patch("ocr.frigate_object_archive.build_plan", return_value=plan), \
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
            def __init__(self, *args, audit=False, wait_for_idle=False):
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
            with patch("ocr.frigate_object_archive.model_identity", return_value={"test": "model-a"}), \
                    patch("ocr.frigate_object_archive.build_plan", return_value=plan), \
                    patch("ocr.frigate_object_archive.Encoder", FakeEncoder), \
                    patch("ocr.frigate_object_archive.request_bytes", return_value=data.getvalue()), \
                    patch("ocr.frigate_object_archive.detections", return_value=objects), \
                    patch("ocr.frigate_object_archive.crop_image", return_value=(image, 7, 10)):
                scan(args)
            report = json.loads(Path(directory, "report.json").read_text(encoding="utf-8"))
            self.assertEqual({"car": 1, "cat": 1}, report["crops"])
            self.assertEqual({"car": 1}, report["budget_skips"])
            self.assertTrue(report["complete"])
            result = json.loads(Path(directory, "results.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertIsNone(result["crops"][0]["group_id"])

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
                    store.add_reference(f"positive-{n}", "car-a", "car", "validation", [1, 0], group_id=f"positive-visit-{n}")
                for n in range(3):
                    store.add_reference(f"negative-{n}", None, "car", "validation", [0, 1], group_id=f"negative-visit-{n}")
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
                self.assertEqual("", store.refs()[0]["group_id"])
                self.assertEqual([1, 0], store.refs()[0]["vector"])
            finally:
                store.db.close()
