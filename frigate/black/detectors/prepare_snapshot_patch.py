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
                "                camera_hook(connection_id)\n")
    if addition + marker in source:
        return source
    if source.count(marker) != 1 or source.count("class ObjectDetectProcess:") != 1:
        raise ValueError("Pinned Frigate detector-context contract changed")
    return source.replace(marker, addition + marker, 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--detector-context", action="store_true")
    args = parser.parse_args()
    transform = patch_detector_context if args.detector_context else patch
    args.destination.write_text(transform(args.source.read_text()), encoding="utf-8")
