#!/usr/bin/env python3
"""Prepare a Black profile from live camera configuration, without logging secrets."""
from __future__ import annotations

import argparse
import copy
import os
import tempfile
from pathlib import Path

import yaml


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
    config["objects"] = {"track": ["person", "cat", "dog"]}
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--staging", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    options = parser.parse_args()
    source = yaml.safe_load(options.source.read_text())
    converted = black_config(source, staging=options.staging, cpu=options.cpu)
    write_atomic(options.destination, yaml.safe_dump(converted, sort_keys=False, allow_unicode=True))
    print(f"Prepared {len(converted['cameras'])} cameras; archive={'disabled' if options.staging else 'White NFS'}")


if __name__ == "__main__":
    main()

