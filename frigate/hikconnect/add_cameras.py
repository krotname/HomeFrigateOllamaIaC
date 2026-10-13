#!/usr/bin/env python3
"""Add only communal cameras to the existing Frigate 0.18 configuration."""
from __future__ import annotations

import argparse
import copy
import os
import tempfile
from pathlib import Path

LABELS = ["person", "car", "truck", "bus", "bicycle", "motorcycle", "cat", "dog"]
ALIASES = [f"common_camera{i:02d}" for i in range(1, 5)]
COURTYARD_SECONDARY = ("cam1_ds_i202", "common_camera01", "common_camera03")


def _detection_only(camera: dict, default_labels: list[str]) -> None:
    review = camera.setdefault("review", {})
    review.setdefault("alerts", {})["enabled"] = False
    detections = review.setdefault("detections", {})
    labels = detections.get("labels", default_labels)
    detections["enabled"] = True
    detections["labels"] = list(dict.fromkeys(["person", *labels]))


def add_cameras(source: dict, *, substream: bool = True) -> dict:
    result = copy.deepcopy(source)
    cameras = result.setdefault("cameras", {})
    if not cameras or result.get("version") != "0.18-0":
        raise ValueError("An existing Frigate 0.18 configuration is required")
    streams = result.setdefault("go2rtc", {}).setdefault("streams", {})
    for alias in ALIASES:
        main, sub = alias + "_main", alias + "_sub"
        for name in (main, sub) if substream else (main,):
            uri = f"rtsp://hikconnect-relay:8554/{name}"
            if name == main:
                # Preserve the recorder's frame rate and make the archive playable in Chrome.
                uri = f"ffmpeg:{uri}#video=h264#hardware=cuda#raw=-r 15 -g 30 -flags +global_header"
            if name in streams and streams[name] != [uri]:
                raise ValueError("An unrelated stream uses a communal alias")
            streams[name] = [uri]
        inputs = [{"path": f"rtsp://127.0.0.1:8554/{main}",
                   "input_args": ["-rtsp_transport", "tcp", "-timeout", "15000000",
                                  "-analyzeduration", "10000000", "-probesize", "10000000"],
                   "roles": ["record"] if substream else ["record", "detect"]}]
        if substream:
            inputs.append({"path": f"rtsp://127.0.0.1:8554/{sub}", "input_args": "preset-rtsp-restream", "roles": ["detect"]})
        camera = {
            "enabled": True,
            "ffmpeg": {"inputs": inputs, "output_args": {"record": "preset-record-generic"}},
            "detect": {"enabled": True, "fps": 3, "width": 352 if substream else 1920,
                       "height": 288 if substream else 1080},
            "motion": {"enabled": True},
            "objects": {"track": LABELS},
            "review": {"alerts": {"enabled": True, "labels": ["person"]},
                       "detections": {"enabled": True, "labels": LABELS[1:]}},
            "record": {"enabled": True, "continuous": {"days": 3}, "motion": {"days": 0},
                       "alerts": {"retain": {"days": 3, "mode": "active_objects"}},
                       "detections": {"retain": {"days": 3, "mode": "active_objects"}}},
            "snapshots": {"enabled": True, "retain": {"default": 3}},
            "face_recognition": {"enabled": False},
            "live": {"streams": {alias: main}},
        }
        previous_camera = copy.deepcopy(camera)
        if alias in COURTYARD_SECONDARY:
            _detection_only(camera, LABELS[1:])
        if alias in cameras and cameras[alias] not in (previous_camera, camera):
            raise ValueError("An unrelated camera uses a communal alias")
        cameras[alias] = camera
    if "cam1_ds_i202" in cameras:
        defaults = result.get("review", {}).get("detections", {}).get("labels", ["cat", "dog"])
        _detection_only(cameras["cam1_ds_i202"], defaults)
    return result


def main() -> None:
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--main-only", action="store_true")
    options = parser.parse_args()
    config = yaml.safe_load(options.source.read_text())
    result = add_cameras(config, substream=not options.main_only)
    fd, temporary = tempfile.mkstemp(dir=options.destination.parent, prefix=".hik-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(result, sort_keys=False, allow_unicode=True))
        os.chmod(temporary, 0o600)
        os.replace(temporary, options.destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Added four communal cameras; existing camera configuration preserved.")


if __name__ == "__main__":
    main()
