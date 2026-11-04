import ast
import importlib.util
from pathlib import Path
import threading
import time
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = load("snapshot_end_queue")
adapter = load("prepare_snapshot_patch")


class FakeObject:
    def __init__(self, gate=None):
        self.gate = gate
        self.entered = threading.Event()
        self.has_snapshot = True
        self.has_clip = True
        self.obj_data = {"id": "one", "nested": ["original"]}
        self.previous = {"label": "person"}
        self.thumbnail_data = {"frame_time": 1.0}
        self.frame_cache = {1.0: {"frame": b"selected"}, 2.0: {"frame": b"other"}}
        self.writes = []
        self.failures = []

    def write_thumbnail_to_disk(self):
        self.entered.set()
        if self.gate is not None and not self.gate.wait(5):
            raise TimeoutError("Test storage gate timed out")
        if self.failures:
            raise self.failures.pop(0)
        self.writes.append(("thumbnail", self.frame_cache[1.0]["frame"]))

    def write_snapshot_to_disk(self):
        self.writes.append(("snapshot", self.obj_data["nested"][:]))


class SnapshotEndQueueTest(unittest.TestCase):
    def test_slow_storage_keeps_processor_available_and_publishes_after_files(self):
        gate = threading.Event()
        obj = FakeObject(gate)
        writer = storage.SnapshotEndQueue()
        callbacks = []
        owner = threading.get_ident()
        try:
            started = time.monotonic()
            writer.submit("camera", obj, "frame", lambda *args: callbacks.append(
                (threading.get_ident(), args[1].obj_data, list(args[1].frame_cache))))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(obj.entered.wait(1))
            # The camera loop can advance and evict the original image while
            # storage remains blocked. No end event may precede its snapshot.
            obj.frame_cache.clear()
            obj.obj_data["nested"][0] = "changed"
            for _ in range(20):
                writer.drain()
            self.assertEqual([], callbacks)
            self.assertEqual([], obj.writes)
            gate.set()
            writer.flush()
            self.assertEqual([("thumbnail", b"selected"), ("snapshot", ["original"])], obj.writes)
            self.assertEqual([(owner, {"id": "one", "nested": ["original"]}, [1.0])], callbacks)
        finally:
            gate.set()
            writer.close()

    def test_queue_bound_applies_backpressure_without_dropping_events(self):
        writer = storage.SnapshotEndQueue(max_pending=1)
        first, second = FakeObject(), FakeObject()
        completed = []
        try:
            writer.submit("a", first, "one", lambda *args: completed.append(args[2]))
            writer.submit("b", second, "two", lambda *args: completed.append(args[2]))
            self.assertLessEqual(writer.pending, 1)
            writer.close()
            self.assertEqual(["one", "two"], completed)
            self.assertEqual(2, len(first.writes))
            self.assertEqual(2, len(second.writes))
            self.assertFalse(writer.worker.is_alive())
            writer.close()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                writer.submit("a", first, "three", lambda *args: None)
        finally:
            writer.close()

    def test_transient_write_failure_retries_before_publication(self):
        writer = storage.SnapshotEndQueue()
        obj = FakeObject()
        obj.failures.append(OSError("transient storage failure"))
        completed = []
        try:
            with self.assertLogs(storage.logger, level="ERROR"):
                writer.submit("a", obj, "frame", lambda *args: completed.append(True))
                writer.flush()
            self.assertEqual([True], completed)
            self.assertEqual(2, len(obj.writes))
        finally:
            writer.close()

    def test_persistent_write_failure_does_not_publish_success(self):
        writer = storage.SnapshotEndQueue()
        obj = FakeObject()
        obj.failures.extend([OSError("first"), OSError("second")])
        completed = []
        try:
            with self.assertLogs(storage.logger, level="ERROR"):
                writer.submit("a", obj, "frame", lambda *args: completed.append(True))
                with self.assertRaisesRegex(OSError, "second"):
                    writer.flush()
            self.assertEqual([], completed)
        finally:
            writer.close()

    def test_owner_is_processor_thread_not_constructor_thread(self):
        writer = storage.SnapshotEndQueue()
        errors = []

        def processor():
            try:
                writer.drain()
                writer.close()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=processor)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        with self.assertRaisesRegex(RuntimeError, "processor thread"):
            writer.drain()

    def test_pinned_adapter_keeps_native_end_publication_on_processor(self):
        native = Path("/opt/frigate/frigate/track/object_processing.py")
        if not native.exists():
            self.skipTest("Requires the pinned Frigate image")
        source = native.read_text()
        result = adapter.patch_object_processing(source)
        self.assertEqual(result, adapter.patch_object_processing(result))
        tree = ast.parse(result)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TrackedObjectProcessor")
        create = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "create_camera_state")
        functions = {n.name: ast.unparse(n) for n in create.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("snapshot_ends.submit", functions["end"])
        self.assertNotIn(".publish(", functions["end"])
        self.assertIn("event_sender.publish", functions["end_after_snapshot"])
        self.assertNotIn("write_snapshot_to_disk", functions["end_after_snapshot"])
        self.assertIn("self.snapshot_ends.close()\n\n        self.requestor.stop()", result)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            adapter.patch_snapshot_io(result.replace("self.snapshot_ends.close()", "pass"))


if __name__ == "__main__":
    unittest.main()
