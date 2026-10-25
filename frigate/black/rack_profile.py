"""Add the rack view without rebuilding the deployed recorder configuration."""

import argparse
import copy
import os
from pathlib import Path

import yaml

CAMERA = "cam2_ds_i551"
RACK_FILTER = (
    "fps=3,crop=1536:1536:512:192,"
    "v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:"
    "h_fov=90:v_fov=75:yaw=35:pitch=45:roll=30:w=1280:h=960"
)
RACK_SOURCE = (
    "ffmpeg:cam2_main#video=h264#raw=-an -vf "
    + RACK_FILTER
    + " -threads 2 -filter_threads 2"
)


def rack_profile(source):
    """Preserve recording, other cameras and unrelated live configuration."""
    result = copy.deepcopy(source)
    camera = result["cameras"][CAMERA]
    streams = result["go2rtc"]["streams"]
    if "cam2_main" not in streams or "cam2_sub" not in streams:
        raise ValueError("Both existing cam2 streams are required")
    detect_inputs = [
        item for item in camera["ffmpeg"]["inputs"]
        if "detect" in item.get("roles", [])
    ]
    if len(detect_inputs) != 1 or detect_inputs[0]["roles"] != ["detect"]:
        raise ValueError("Expected one separate detect input; recording must stay unchanged")

    streams["cam2_rack"] = [RACK_SOURCE]
    live = camera.setdefault("live", {})
    previous = live.get("streams", {})
    live["streams"] = {
        "Стойка": "cam2_rack",
        "Рыбий глаз — полный": "cam2_main",
        "Экономичный": "cam2_sub",
        **{label: stream for label, stream in previous.items()
           if stream not in {"cam2_rack", "cam2_main", "cam2_sub"}},
    }
    detect_inputs[0]["path"] = "rtsp://127.0.0.1:8554/cam2_main"
    camera.setdefault("detect", {}).update(width=1280, height=960, fps=3)
    camera.setdefault("snapshots", {}).update(quality=90)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("Generate a candidate file; do not overwrite the active config")
    source = yaml.safe_load(args.source.read_text(encoding="utf-8"))
    result = rack_profile(source)
    candidate = yaml.safe_dump(result, sort_keys=False, allow_unicode=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(candidate)


if __name__ == "__main__":
    main()
