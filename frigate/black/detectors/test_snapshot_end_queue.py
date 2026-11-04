import ast
import json
import importlib.util
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
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
            self.assertEqual([(owner, {"id": "one", "nested": ["changed"]}, [])], callbacks)
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

    def test_shutdown_keeps_consumer_running_until_slow_write_is_acknowledged(self):
        shared = threading.Event()
        gate = threading.Event()
        obj = FakeObject(gate)
        messages, acks = queue.Queue(), queue.Queue()
        order = []
        errors = []

        class Subscriber:
            def check_for_update(self, timeout):
                try:
                    return acks.get(timeout=timeout)
                except queue.Empty:
                    return None

        class Processor(threading.Thread):
            stop_event = storage.ProcessorStopEvent(shared)

            def run(self):
                writer = storage.SnapshotEndQueue()
                try:
                    writer.submit("camera", obj, "frame", lambda *args: messages.put(("one",)))
                    while not self.stop_event.is_set():
                        time.sleep(0.001)
                    writer.close()
                    self.acknowledged = storage.wait_for_end_acks({"one"}, Subscriber())
                    order.append("processor_stopped")
                except Exception as exc:
                    errors.append(exc)

        def consume():
            while not shared.is_set():
                try:
                    item = messages.get(timeout=0.02)
                except queue.Empty:
                    continue
                self.assertFalse(shared.is_set())
                self.assertEqual(2, len(obj.writes))
                order.append("database_end_written")
                acks.put(item)

        processor, consumer = Processor(), threading.Thread(target=consume)
        consumer.start()
        processor.start()
        self.assertTrue(obj.entered.wait(1))
        timer = threading.Timer(0.05, gate.set)
        timer.start()
        try:
            storage.stop_snapshot_processor(processor)
            self.assertEqual([], errors)
            self.assertTrue(processor.acknowledged)
            self.assertEqual(["database_end_written", "processor_stopped"], order)
            self.assertTrue(consumer.is_alive())
        finally:
            gate.set()
            shared.set()
            processor.join(2)
            consumer.join(2)
            timer.join(2)

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
        self.assertIn("self.snapshot_ends.close()", result)
        self.assertIn("wait_for_end_acks(self.pending_snapshot_ends", result)
        self.assertIn("state = self.camera_states.get(camera)", result)
        # A camera can disappear while storage is blocked. Its end event still
        # reaches the database, but no removed PTZ configuration is accessed.
        end = next(n for n in create.body if isinstance(n, ast.FunctionDef) and n.name == "end_after_snapshot")
        for arg in end.args.args:
            arg.annotation = None
        end.returns = None
        sent = []
        publisher = SimpleNamespace(publish=lambda *a, **k: sent.append(a))
        fake_self = SimpleNamespace(config=SimpleNamespace(cameras={}), dispatcher=publisher,
            event_sender=publisher, pending_snapshot_ends=set(),
            ptz_autotracker_thread=SimpleNamespace())
        obj = SimpleNamespace(false_positive=False, previous={}, obj_data={"id": "one"},
            camera_config=SimpleNamespace(detect=SimpleNamespace(width=1920,height=1080)),
            to_dict=lambda: {"id": "one", "sub_label": "latest recognition"})
        namespace = {"self": fake_self, "json": json,
            "snapshot_end_data": storage.snapshot_end_data,
            "EventTypeEnum": SimpleNamespace(tracked_object="tracked"),
            "EventStateEnum": SimpleNamespace(end="end")}
        exec(compile(ast.Module(body=[end], type_ignores=[]), "native_end", "exec"), namespace)
        namespace["end_after_snapshot"]("removed_camera", obj, "frame")
        self.assertEqual("latest recognition", sent[-1][0][-1]["sub_label"])
        self.assertEqual([1920,1080], sent[-1][0][-1]["_snapshot_dimensions"])
        self.assertNotIn("_snapshot_dimensions", sent[0][1])
        self.assertEqual({"one"}, fake_self.pending_snapshot_ends)
        app = Path("/opt/frigate/frigate/app.py").read_text()
        app_result = adapter.patch_app_shutdown(app)
        self.assertEqual(app_result, adapter.patch_app_shutdown(app_result))
        stop = app_result.split("    def stop(self) -> None:", 1)[1]
        self.assertLess(stop.index("stop_snapshot_processor("), stop.index("self.stop_event.set()"))
        ast.parse(app_result)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            adapter.patch_snapshot_io(result.replace("self.snapshot_ends.close()", "pass"))

    def test_native_consumer_finalizes_removed_camera_with_original_dimensions(self):
        native = Path("/opt/frigate/frigate/events/maintainer.py")
        if not native.exists():
            self.skipTest("Requires the pinned Frigate image")
        result = adapter.patch_event_maintainer(native.read_text())
        self.assertEqual(result, adapter.patch_event_maintainer(result))
        tree = ast.parse(result)
        cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="EventProcessor")
        function = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="handle_object_detection")
        for arg in function.args.args:
            arg.annotation = None
        function.returns = None
        inserted, acknowledged = [], []

        class Query:
            def on_conflict(self, **kwargs):
                return self
            def execute(self):
                return 1

        fields={n.attr:n.attr for n in ast.walk(function)
            if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=="Event"}
        event_model=SimpleNamespace(**fields)
        event_model.insert=lambda event: inserted.append(event) or Query()
        cfg=SimpleNamespace(cameras={},detectors={"one":SimpleNamespace(model=None,type="test")},
            classification=SimpleNamespace(custom={}))
        previous={"has_clip":True,"has_snapshot":True}
        obj=SimpleNamespace(config=cfg,events_in_process={"one":previous},
            event_end_publisher=SimpleNamespace(publish=acknowledged.append))
        event={"id":"one","camera":"removed_camera","start_time":1,"end_time":10,
            "snapshot":None,"has_clip":True,"has_snapshot":True,"entered_zones":[],
            "label":"person","top_score":.9,"average_estimated_speed":0,
            "velocity_angle":0,"sub_label":["latest",.8],"_snapshot_dimensions":[1920,1080]}
        namespace={"Event":event_model,"EventStateEnum":SimpleNamespace(end="end"),
            "event_camera_dimensions":storage.event_camera_dimensions,
            "should_update_db":lambda *args:True,"should_update_state":lambda *args:False}
        exec(compile(ast.Module(body=[function],type_ignores=[]),"native_event_consumer","exec"),namespace)
        namespace['handle_object_detection'](obj,"end","removed_camera",event)
        self.assertEqual(10,inserted[0]['end_time'])
        self.assertEqual("latest",inserted[0]['sub_label'])
        self.assertNotIn('_snapshot_dimensions',inserted[0])
        self.assertEqual([("one","removed_camera",True)],acknowledged)
        self.assertEqual({},obj.events_in_process)
        self.assertEqual((None,None),storage.event_camera_dimensions(cfg,"another_camera",event))


if __name__ == "__main__":
    unittest.main()
