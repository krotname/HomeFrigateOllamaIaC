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
              '\n    def set_frame(self, frame_time, regions, tracked_boxes):\n        self.kugoo_frame = (frame_time, tuple(regions), tuple(tracked_boxes))\n')
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
                      '                frame_hook(frame_time, regions, object_tracker.untracked_object_boxes + [obj["box"] for obj in object_tracker.tracked_objects.values() if obj["label"] in ("bicycle", "motorcycle")])\n')
    if addition + marker in source and frame_addition + frame_marker in source:
        return source
    if source.count(marker) != 1 or source.count(frame_marker) != 1:
        raise ValueError("Pinned Frigate video-region contract changed")
    return source.replace(marker, addition + marker, 1).replace(frame_marker, frame_addition + frame_marker, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--detector-context", action="store_true")
    parser.add_argument("--video-context", action="store_true")
    args = parser.parse_args()
    transform = patch_video_context if args.video_context else patch_detector_context if args.detector_context else patch
    args.destination.write_text(transform(args.source.read_text()), encoding="utf-8")
