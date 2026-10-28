#!/usr/bin/env python3
"""Apply motion-only retention and preserve original HEVC archive streams."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path


def storage_profile(source: dict) -> dict:
    result = copy.deepcopy(source)
    cameras = result.get("cameras")
    if not isinstance(cameras, dict) or not cameras:
        raise ValueError("An existing camera configuration is required")

    records = [result.setdefault("record", {})]
    records.extend(camera["record"] for camera in cameras.values() if "record" in camera)
    for record in records:
        record.setdefault("continuous", {})["days"] = 0
        record.setdefault("motion", {})["days"] = 7
        for category in ("alerts", "detections"):
            record.setdefault(category, {}).setdefault("retain", {})["days"] = 7

    streams = result.get("go2rtc", {}).get("streams", {})
    for name, camera in cameras.items():
        if name not in {f"common_camera{i:02d}" for i in range(1, 5)}:
            continue
        main_name = f"{name}_main"
        main_source = streams.get(main_name)
        if isinstance(main_source, list) and len(main_source) == 1:
            main_source = main_source[0]
        if not isinstance(main_source, str) or not main_source.startswith("ffmpeg:rtsp://"):
            raise ValueError(f"Unexpected source for {main_name}")
        if "#video=h264" not in main_source:
            raise ValueError(f"Expected the existing H.264 live stream for {name}")
        archive_name = f"{name}_archive"
        archive_source = main_source.removeprefix("ffmpeg:").split("#", 1)[0]
        if archive_name in streams and streams[archive_name] != archive_source:
            raise ValueError(f"A different archive stream already exists for {name}")
        streams[archive_name] = archive_source
        archive_path = f"rtsp://127.0.0.1:8554/{archive_name}"
        inputs = camera["ffmpeg"]["inputs"]
        if any(item.get("path") == archive_path and "record" in item.get("roles", []) for item in inputs):
            continue
        updated_inputs = []
        recording_inputs = 0
        for item in inputs:
            if "record" not in item.get("roles", []):
                updated_inputs.append(item)
                continue
            if item.get("path") != f"rtsp://127.0.0.1:8554/{main_name}":
                raise ValueError(f"Unexpected recording input for {name}")
            recording_inputs += 1
            archive_input = copy.deepcopy(item)
            archive_input["path"] = archive_path
            archive_input["roles"] = ["record"]
            other_roles = [role for role in item["roles"] if role != "record"]
            if other_roles:
                item["roles"] = other_roles
                updated_inputs.append(item)
            updated_inputs.append(archive_input)
        if recording_inputs != 1:
            raise ValueError(f"Expected one recording input for {name}")
        camera["ffmpeg"]["inputs"] = updated_inputs
    return result


def main() -> None:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve():
        parser.error("Write a candidate file and validate it before deployment")
    source = yaml.safe_load(args.source.read_text(encoding="utf-8"))
    candidate = storage_profile(source)
    import os
    fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        yaml.safe_dump(candidate, target, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
