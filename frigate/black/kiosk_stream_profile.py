#!/usr/bin/env python3
"""Add small kiosk restreams without changing recording or detection inputs."""

import argparse
import os
from pathlib import Path

import yaml


STREAMS = {
    "kiosk_gate_camera03": [
        # The gate is x=52.7%..60.7%, y=0%..13.5% of Camera 03.
        # Keep its native pixels; clients upscale this small image for display.
        "ffmpeg:common_camera03_main#video=mjpeg#raw=-vf crop=trunc(iw*0.08/2)*2:trunc(ih*0.135/2)*2:trunc(iw*0.527/2)*2:0,fps=5 -threads 1",
        "ffmpeg:common_camera03_main#video=h264#raw=-vf crop=trunc(iw*0.08/2)*2:trunc(ih*0.135/2)*2:trunc(iw*0.527/2)*2:0,fps=5 -threads 1 -g 10 -tune fastdecode,zerolatency",
    ],
    "kiosk_parking_camera04": [
        # RTP/JPEG carries dimensions in 8-pixel units; keep both divisible by 8.
        "ffmpeg:common_camera04_main#video=mjpeg#width=512#height=288#raw=-r 5 -threads 1",
        "ffmpeg:common_camera04_main#video=h264#width=512#height=288#raw=-r 5 -threads 1 -g 10 -tune fastdecode,zerolatency",
    ],
}


def apply_profile(config):
    streams = config.setdefault("go2rtc", {}).setdefault("streams", {})
    for source in ("common_camera03_main", "common_camera04_main"):
        if source not in streams:
            raise ValueError(f"Required source stream is missing: {source}")
    for name, sources in STREAMS.items():
        if name in streams and streams[name] != sources:
            raise ValueError(f"Refusing to replace a different stream: {name}")
        streams[name] = list(sources)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    config = apply_profile(yaml.safe_load(args.source.read_text()))
    fd = os.open(args.candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        yaml.safe_dump(config, output, sort_keys=False, allow_unicode=True)
    print(f"Candidate created: {args.candidate}")


if __name__ == "__main__":
    main()
