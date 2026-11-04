"""Create the narrow adapter for the pinned Frigate tracked-object module."""
import argparse
from pathlib import Path


def patch(source):
    marker = "class TrackedObject:"
    addition = "from frigate.track.kugoo_snapshots import is_better_thumbnail\n\n\n"
    if addition in source:
        return source
    if source.count(marker) != 1 or source.count("if self.thumbnail_data is None or is_better_thumbnail(") != 1:
        raise ValueError("Pinned Frigate tracked-object contract changed")
    return source.replace(marker, addition + marker, 1)


def patch_object_processing(source):
    addition = "from frigate.track.kugoo_snapshots import allow_stationary_snapshot\n\n\n"
    marker = "class TrackedObjectProcessor(threading.Thread):"
    original = '        if obj.obj_data["position_changes"] == 0:'
    replacement = ('        if (obj.obj_data["position_changes"] == 0\n'
                   '                and not allow_stationary_snapshot(camera, obj.obj_data["label"])):')
    start = source.find("    def should_save_snapshot(")
    end = source.find("    def should_retain_recording(", start)
    if start < 0 or end < 0 or source.count(marker) != 1:
        raise ValueError("Pinned Frigate snapshot-admission contract changed")
    function = source[start:end]
    if addition in source and replacement in function:
        return patch_snapshot_io(source)
    if addition in source or function.count(original) != 1:
        raise ValueError("Pinned Frigate stationary-snapshot contract changed")
    result = source[:start] + function.replace(original, replacement, 1) + source[end:]
    return patch_snapshot_io(result.replace(marker, addition + marker, 1))


def patch_snapshot_io(source):
    """Defer native end-image writes without moving ZMQ publishers to a worker."""
    addition = ("from frigate.track.snapshot_end_queue import (\n"
                "    SnapshotEndQueue, ProcessorStopEvent, wait_for_end_acks,\n)\n\n\n")
    marker = "class TrackedObjectProcessor(threading.Thread):"
    init = "        self.frame_manager = SharedMemoryFrameManager()"
    init_new = init + "\n        self.snapshot_ends = SnapshotEndQueue()\n        self.pending_snapshot_ends = set()"
    stop = "        self.stop_event: MpEvent = stop_event"
    stop_new = "        self.stop_event: MpEvent = ProcessorStopEvent(stop_event)"
    end_start = "        def end(camera: str, obj: TrackedObject, frame_name: str) -> None:\n"
    writes_start = "            # write thumbnail to disk if it will be saved as an event\n"
    writes_end = "            if not obj.false_positive:\n"
    run = "    def run(self) -> None:\n        while not self.stop_event.is_set():\n"
    run_new = run + "            self.snapshot_ends.drain()\n"
    shutdown = "        self.requestor.stop()"
    shutdown_new = ("        self.snapshot_ends.close()\n"
                    "        wait_for_end_acks(self.pending_snapshot_ends, self.event_end_subscriber)\n\n" + shutdown)
    ack = "                self.camera_states[camera].finished(event_id)"
    ack_new = ("                self.pending_snapshot_ends.discard(event_id)\n"
               "                state = self.camera_states.get(camera)\n"
               "                if state is not None:\n                    state.finished(event_id)")
    ptz = "                self.ptz_autotracker_thread.ptz_autotracker.end_object(camera, obj)"
    ptz_new = ("                if camera in self.config.cameras:\n"
               "    " + ptz)
    publish = "            self.event_sender.publish(\n                (\n                    EventTypeEnum.tracked_object,\n                    EventStateEnum.end,"
    publish_new = "            self.pending_snapshot_ends.add(obj.obj_data[\"id\"])\n" + publish
    deferred = ("            self.snapshot_ends.submit(camera, obj, frame_name, end_after_snapshot)\n\n"
                "        def end_after_snapshot(camera: str, obj: TrackedObject, frame_name: str) -> None:\n")
    if addition in source:
        if not all(s in source for s in (init_new, run_new, shutdown_new, deferred, stop_new, ack_new, ptz_new, publish_new)):
            raise ValueError("Incomplete Frigate snapshot-storage adapter")
        return source
    if any(source.count(s) != 1 for s in (marker, init, end_start, run, shutdown, stop, ack, ptz, publish)):
        raise ValueError("Pinned Frigate snapshot-storage contract changed")
    start = source.index(writes_start, source.index(end_start))
    end = source.index(writes_end, start)
    expected = ("            # write thumbnail to disk if it will be saved as an event\n"
                "            if obj.has_snapshot or obj.has_clip:\n"
                "                obj.write_thumbnail_to_disk()\n\n"
                "            # write the snapshot to disk\n"
                "            if obj.has_snapshot:\n"
                "                obj.write_snapshot_to_disk()\n\n")
    if source[start:end] != expected:
        raise ValueError("Pinned Frigate end-image writes changed")
    result = source[:start] + deferred + source[end:]
    return (result.replace(marker, addition + marker, 1)
            .replace(init, init_new, 1).replace(run, run_new, 1)
            .replace(shutdown, shutdown_new, 1).replace(stop, stop_new, 1)
            .replace(ack, ack_new, 1).replace(ptz, ptz_new, 1)
            .replace(publish, publish_new, 1))


def patch_app_shutdown(source):
    addition = "from frigate.track.snapshot_end_queue import stop_snapshot_processor\n"
    marker = "class FrigateApp:"
    stop = "        self.stop_event.set()\n"
    replacement = ("        # Drain snapshot files and native end acknowledgements while consumers run.\n"
                   "        stop_snapshot_processor(self.detected_frames_processor)\n" + stop)
    if addition in source:
        if replacement not in source:
            raise ValueError("Incomplete Frigate snapshot shutdown adapter")
        return source
    before, stop_method = source.split("    def stop(self) -> None:\n", 1)
    if source.count(marker) != 1 or stop_method.count(stop) != 1:
        raise ValueError("Pinned Frigate application shutdown contract changed")
    return (before.replace(marker, addition + "\n\n" + marker, 1)
            + "    def stop(self) -> None:\n" + stop_method.replace(stop, replacement, 1))


def patch_detector_context(source):
    marker = "            detections = object_detector.detect_raw(input_frame)"
    addition = ("            camera_hook = getattr(object_detector.detect_api, \"set_camera\", None)\n"
                "            if camera_hook is not None:\n"
                "                camera_hook(connection_id, region, frame_context)\n")
    if addition + marker in source:
        return source
    queue_marker = "                connection_id = self.detection_queue.get(timeout=1)"
    remote_marker = "class RemoteObjectDetector:"
    if source.count(marker) != 1 or source.count("class ObjectDetectProcess:") != 1 or source.count(remote_marker) != 1 or queue_marker not in source:
        raise ValueError("Pinned Frigate detector-context contract changed")
    before, after = source.split(remote_marker)
    send = "        self.detection_queue.put(self.name)"
    if after.count(send) != 1:
        raise ValueError("Pinned Frigate remote queue contract changed")
    after = after.replace(send, "        self.detection_queue.put((self.name, getattr(self, \"kugoo_region\", None), getattr(self, \"kugoo_frame\", None)))", 1)
    method = ('\n    def set_region(self, region):\n        self.kugoo_region = tuple(region)\n'
              '\n    def set_frame(self, frame_time, regions, tracked_boxes, frame_shape):\n'
              '        if self.name in ("cam1_ds_i202", "common_camera03"):\n'
              '            from frigate.track.kugoo_snapshots import native_context_regions\n'
              '            self.kugoo_turn = getattr(self, "kugoo_turn", -1) + 1\n'
              '            regions[:] = native_context_regions(self.name, self.kugoo_turn, regions, tracked_boxes, frame_shape)\n'
              '        self.kugoo_frame = (frame_time, tuple(regions), tuple(tracked_boxes))\n')
    before = before.replace(queue_marker, queue_marker + '\n                region = None\n                frame_context = None\n                if isinstance(connection_id, tuple):\n                    connection_id, region, frame_context = connection_id', 1)
    before = before.replace(marker, addition + marker, 1)
    return before + remote_marker + method + after


def patch_video_context(source):
    marker = "    region_detections = object_detector.detect(tensor_input)"
    addition = ('    region_hook = getattr(object_detector, "set_region", None)\n'
                '    if region_hook is not None:\n'
                '        region_hook(region)\n')
    frame_marker = '            for region in regions:\n                detections.extend('
    frame_addition = ('            frame_hook = getattr(object_detector, "set_frame", None)\n'
                      '            if frame_hook is not None:\n'
                '                if frame_time - getattr(object_detector, "kugoo_candidates_time", float("-inf")) >= 16:\n'
                      '                    object_detector.kugoo_candidates = []\n'
                      '                frame_hook(frame_time, regions, [obj["box"] for obj in object_tracker.tracked_objects.values() if obj["label"] in ("bicycle", "motorcycle")] + getattr(object_detector, "kugoo_candidates", []), frame_shape)\n')
    candidates_marker = '            consolidated_detections = reduce_detections(frame_shape, detections)'
    candidates_addition = ('\n            if frame_hook is not None:\n'
                          '                fresh_candidates = [d[2] for d in consolidated_detections if d[0] in ("bicycle", "motorcycle") and d[1] >= 0.7]\n'
                          '                if fresh_candidates:\n'
                          '                    object_detector.kugoo_candidates = fresh_candidates\n'
                          '                    object_detector.kugoo_candidates_time = frame_time\n')
    if addition + marker in source and frame_addition + frame_marker in source and candidates_marker + candidates_addition in source:
        return source
    if source.count(marker) != 1 or source.count(frame_marker) != 1 or source.count(candidates_marker) != 1:
        raise ValueError("Pinned Frigate video-region contract changed")
    return source.replace(marker, addition + marker, 1).replace(frame_marker, frame_addition + frame_marker, 1).replace(candidates_marker, candidates_marker + candidates_addition, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--detector-context", action="store_true")
    parser.add_argument("--video-context", action="store_true")
    parser.add_argument("--object-processing", action="store_true")
    parser.add_argument("--app-shutdown", action="store_true")
    args = parser.parse_args()
    transform = patch_app_shutdown if args.app_shutdown else patch_object_processing if args.object_processing else patch_video_context if args.video_context else patch_detector_context if args.detector_context else patch
    args.destination.write_text(transform(args.source.read_text()), encoding="utf-8")
