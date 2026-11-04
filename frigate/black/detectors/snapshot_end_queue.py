"""Keep slow snapshot storage outside the shared camera processing loop."""
import copy
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class SnapshotEndQueue:
    """Write frozen event images in order, then publish on the owning thread."""

    def __init__(self, max_pending=16):
        if max_pending < 1:
            raise ValueError("Snapshot queue must have a positive bound")
        self.max_pending = max_pending
        self.pending = 0
        # Frigate constructs its processor in the parent thread, then starts it.
        self.owner = None
        self.jobs = queue.Queue()
        self.completed = queue.Queue()
        self.closed = False
        self.worker = threading.Thread(
            target=self._run, name="snapshot_storage", daemon=True
        )
        self.worker.start()

    def _assert_owner(self):
        if self.owner is None:
            self.owner = threading.get_ident()
        if threading.get_ident() != self.owner:
            raise RuntimeError("Snapshot completions must run on the processor thread")

    @staticmethod
    def _freeze(obj):
        frozen = copy.copy(obj)
        frozen.obj_data = copy.deepcopy(obj.obj_data)
        frozen.previous = copy.deepcopy(obj.previous)
        frozen.thumbnail_data = copy.deepcopy(obj.thumbnail_data)
        # CameraState evicts its cache after the end callback. Retain just the
        # selected, immutable frame array, not the entire camera frame history.
        frame_time = (frozen.thumbnail_data or {}).get("frame_time")
        frozen.frame_cache = (
            {frame_time: dict(obj.frame_cache[frame_time])}
            if frame_time in obj.frame_cache else {}
        )
        return frozen

    @staticmethod
    def _write(obj):
        if obj.has_snapshot or obj.has_clip:
            obj.write_thumbnail_to_disk()
        if obj.has_snapshot:
            obj.write_snapshot_to_disk()

    def submit(self, camera, obj, frame_name, callback):
        self._assert_owner()
        if self.closed:
            raise RuntimeError("Snapshot queue is closed")
        self.drain()
        if self.pending >= self.max_pending:
            # Preserve files and bound retained frames during a storage outage.
            # Persistent saturation deliberately retains native backpressure.
            logger.warning("Snapshot storage queue full; waiting for pending writes")
            self.flush()
        if not (obj.has_snapshot or obj.has_clip):
            callback(camera, obj, frame_name)
            return
        frozen = self._freeze(obj)
        self.pending += 1
        self.jobs.put((camera, frozen, frame_name, callback))

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            error = None
            try:
                self._write(job[1])
            except Exception as exc:
                error = exc
                logger.exception("Snapshot storage write failed; native retry required")
            self.completed.put((job, error))

    def _finish(self, completion):
        (camera, obj, frame_name, callback), error = completion
        self.pending -= 1
        if error is not None:
            # A transient failure gets the native synchronous retry. A repeated
            # failure propagates instead of publishing a nonexistent snapshot.
            self._write(obj)
        callback(camera, obj, frame_name)

    def drain(self):
        self._assert_owner()
        while True:
            try:
                completion = self.completed.get_nowait()
            except queue.Empty:
                return
            self._finish(completion)

    def flush(self):
        self._assert_owner()
        while self.pending:
            self._finish(self.completed.get())

    def close(self):
        self._assert_owner()
        if self.closed:
            return
        try:
            self.flush()
        finally:
            self.closed = True
            self.jobs.put(None)
            self.worker.join(timeout=5)
