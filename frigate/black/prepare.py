#!/usr/bin/env python3
"""Prepare a Black profile from live camera configuration, without logging secrets."""
from __future__ import annotations

import argparse
import copy
import os
import tempfile
from pathlib import Path

import yaml

COURTYARD_PRIMARY = "common_camera02"
COURTYARD_SECONDARY = ("cam1_ds_i202", "common_camera01", "common_camera03")


def configure_courtyard_review(source: dict) -> dict:
    """Keep one person alert and detection review items on overlapping angles."""
    config = copy.deepcopy(source)
    cameras = config.get("cameras", {})
    required = (COURTYARD_PRIMARY, *COURTYARD_SECONDARY)
    missing = [name for name in required if name not in cameras]
    if missing:
        raise ValueError(f"Courtyard cameras are missing: {', '.join(missing)}")

    primary_alerts = cameras[COURTYARD_PRIMARY].setdefault("review", {}).setdefault("alerts", {})
    primary_alerts["enabled"] = True
    primary_labels = primary_alerts.get("labels", config.get("review", {}).get("alerts", {}).get("labels"))
    if primary_labels is not None:
        primary_alerts["labels"] = list(dict.fromkeys(["person", *primary_labels]))

    default_detections = config.get("review", {}).get("detections", {}).get("labels")
    for name in COURTYARD_SECONDARY:
        review = cameras[name].setdefault("review", {})
        review.setdefault("alerts", {})["enabled"] = False
        detections = review.setdefault("detections", {})
        labels = detections.get("labels", default_detections)
        detections["enabled"] = True
        if labels is not None:
            detections["labels"] = list(dict.fromkeys(["person", *labels]))
    return config


def black_config(source: dict, *, staging: bool = False, cpu: bool = False) -> dict:
    config = copy.deepcopy(source)
    if not config.get("cameras") or not config.get("go2rtc", {}).get("streams"):
        raise ValueError("A live camera configuration is required")
    # Auto discovery can advertise only the public NAT address from Docker.
    # Keep the White relay and the Black LAN listener reachable by LAN clients.
    webrtc = config["go2rtc"].setdefault("webrtc", {})
    candidates = webrtc.setdefault("candidates", [])
    for candidate in ("192.168.1.138:8555", "192.168.1.242:8555"):
        if candidate not in candidates:
            candidates.append(candidate)
    config["detectors"] = {"objects": {"type": "onnx", "device": "CPU" if cpu else "GPU"}}
    config["model"] = {
        "model_type": "yolo-generic", "width": 320, "height": 320,
        "input_tensor": "nchw", "input_dtype": "float", "input_pixel_format": "rgb",
        "path": "/config/model_cache/yolov9-t-320.onnx",
        "labelmap_path": "/config/model_cache/coco-yolo-80.txt",
    }
    config.setdefault("ffmpeg", {}).setdefault("output_args", {})["record"] = "preset-record-generic-audio-copy"
    if cpu:
        config["ffmpeg"].pop("hwaccel_args", None)
    else:
        config["ffmpeg"]["hwaccel_args"] = "preset-nvidia"
    config["detect"] = {"enabled": True, "fps": 3}
    config["motion"] = {"enabled": True}
    config["objects"] = {"track": ["person", "cat", "dog", "car", "bicycle", "motorcycle"]}
    config["review"] = {"alerts": {"enabled": True, "labels": ["person"]}, "detections": {"enabled": True, "labels": ["cat", "dog"]}}
    config["snapshots"] = {"enabled": True, "retain": {"default": 3}}
    config["record"] = {
        "enabled": not staging, "continuous": {"days": 3}, "motion": {"days": 0},
        "alerts": {"retain": {"days": 3, "mode": "active_objects"}},
        "detections": {"retain": {"days": 3, "mode": "active_objects"}},
    }
    # Russian text search uses quantized multilingual Jina v2 on CPU. This keeps
    # GPU memory available for interactive OCR, Whisper and the existing Qwen.
    config["semantic_search"] = {"enabled": True, "model": "jinav2", "model_size": "small", "device": "CPU"}
    config["face_recognition"] = {"enabled": True, "model_size": "small"}
    # Finished events are described by the durable foreground-priority OCR queue.
    config.pop("genai", None)
    for camera in config["cameras"].values():
        camera.setdefault("detect", {}).update({"enabled": True, "fps": 3})
        camera["motion"] = {"enabled": True}
        for key in ("record", "objects", "review", "snapshots", "genai"):
            camera.pop(key, None)
        camera["face_recognition"] = {"enabled": True}
    config["version"] = "0.18-0"
    if all(name in config["cameras"] for name in (COURTYARD_PRIMARY, *COURTYARD_SECONDARY)):
        config = configure_courtyard_review(config)
    return config


def write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_two_wheelers(source):
    """Add tracking/review only on property cameras; preserve runtime overrides."""
    config = copy.deepcopy(source)
    names = {"cam1_ds_i202", "cam2_ds_i551", "cam3_ds_i200",
             "common_camera01", "common_camera02", "common_camera03"}
    for name, camera in config.get("cameras", {}).items():
        if name not in names:
            continue
        objects = camera.setdefault("objects", {})
        track = objects.get("track", config.get("objects", {}).get("track", ["person"]))
        objects["track"] = list(dict.fromkeys([*track, "bicycle", "motorcycle"]))
        review = camera.setdefault("review", {}).setdefault("detections", {})
        labels = review.get("labels", config.get("review", {}).get("detections", {}).get("labels"))
        if labels is not None:
            review["labels"] = list(dict.fromkeys([*labels, "bicycle", "motorcycle"]))
    return config


def configure_kugoo_detector(source: dict) -> dict:
    """Enable measured native proposals without changing identity/filter thresholds."""
    config = configure_two_wheelers(source)
    detectors = config.get("detectors", {})
    if len(detectors) != 1:
        raise ValueError("Kugoo supplement requires exactly one existing ONNX detector")
    name, detector = next(iter(detectors.items()))
    if detector.get("type") not in {"onnx", "kugoo_onnx"}:
        raise ValueError("Kugoo supplement requires an existing ONNX detector")
    model = config.setdefault("model", {})
    if not str(model.get("path", "")).endswith("yolov9-t-320.onnx"):
        raise ValueError("The measured base detector must remain yolov9-t-320.onnx")
    model.update(width=960, height=960)
    detectors[name] = {**detector, "type": "kugoo_onnx", "device": "GPU",
                       "supplement_model": "/config/model_cache/kugoo-yolo11n-pyramid.onnx",
                       "supplement_threshold": .7, "supplement_interval": .55,
                       "supplement_cameras": ["cam1_ds_i202", "common_camera03"]}
    measured = {"cam1_ds_i202": (1280, 720), "common_camera03": (1920, 1080)}
    for camera, values in config.get("cameras", {}).items():
        if camera not in measured:
            for zone, settings in values.get("zones", {}).items():
                if {"bicycle", "motorcycle"}.intersection(settings.get("objects", [])):
                    raise ValueError(
                        f"Kugoo conversion cannot remove two-wheeler tracking from {camera}: "
                        f"zone {zone} requires it")
            objects = values.get("objects", {})
            tracked = objects.get("track", config.get("objects", {}).get("track", ["person"]))
            retained = [label for label in tracked if label not in {"bicycle", "motorcycle"}]
            if retained != tracked:
                values.setdefault("objects", {})["track"] = retained
    for camera, (width, height) in measured.items():
        if camera not in config.get("cameras", {}):
            raise ValueError(f"Measured Kugoo camera is missing: {camera}")
        inputs = config["cameras"][camera].get("ffmpeg", {}).get("inputs", [])
        sub_path = f"rtsp://127.0.0.1:8554/{camera}_sub"
        if any(item.get("path") == sub_path and "detect" in item.get("roles", [])
               for item in inputs):
            main_path = f"rtsp://127.0.0.1:8554/{camera}_main"
            main = next((item for item in inputs if item.get("path") == main_path), None)
            if main is None:
                raise ValueError(f"Measured full-resolution stream is missing: {camera}")
            for item in inputs:
                item["roles"] = [role for role in item.get("roles", []) if role != "detect"]
            main["roles"].append("detect")
            inputs[:] = [item for item in inputs if item["roles"]]
        config["cameras"][camera].setdefault("detect", {}).update(
            width=width, height=height, fps=1)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--staging", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--courtyard-only", action="store_true",
                        help="Change only courtyard review alerts/detections in an existing config")
    parser.add_argument("--two-wheelers-only", action="store_true",
                        help="Add two-wheeler tracking/review without regenerating runtime config")
    parser.add_argument("--kugoo-only", action="store_true",
                        help="Enable the native CUDA two-wheeler supplement in an existing config")
    options = parser.parse_args()
    source = yaml.safe_load(options.source.read_text())
    modes = sum((options.courtyard_only, options.two_wheelers_only, options.kugoo_only))
    if modes > 1 or modes and (options.cpu or options.staging):
        parser.error("Choose one configuration mode")
    if options.kugoo_only:
        converted = configure_kugoo_detector(source)
    elif options.two_wheelers_only:
        converted = configure_two_wheelers(source)
    elif options.courtyard_only:
        converted = configure_courtyard_review(source)
    else:
        converted = configure_two_wheelers(black_config(source, staging=options.staging, cpu=options.cpu))
    write_atomic(options.destination, yaml.safe_dump(converted, sort_keys=False, allow_unicode=True))
    if options.kugoo_only:
        print("Configured native CUDA two-wheeler supplement; identity thresholds retained")
    elif options.two_wheelers_only:
        print("Configured property two-wheeler tracking/review")
    elif options.courtyard_only:
        print("Configured one courtyard alert camera; recording and other cameras unchanged")
    else:
        print(f"Prepared {len(converted['cameras'])} cameras; archive={'disabled' if options.staging else 'White NFS'}")


if __name__ == "__main__":
    main()

