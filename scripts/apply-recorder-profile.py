#!/usr/bin/env python3
"""Convert the retained Frigate deployment to the low-CPU recorder profile."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path

import yaml


def atomic_write(path: Path, content: str) -> None:
    current_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, current_mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def recorder_config(source: dict, days: int, detect_fps: int) -> dict:
    cameras = source.get("cameras")
    go2rtc = source.get("go2rtc")
    if not isinstance(cameras, dict) or not cameras:
        raise ValueError("Frigate config has no cameras")
    if not isinstance(go2rtc, dict) or not go2rtc.get("streams"):
        raise ValueError("Frigate config has no go2rtc streams")

    converted_cameras = {}
    for name, camera in cameras.items():
        if not isinstance(camera, dict):
            raise ValueError(f"Invalid camera definition: {name}")
        converted = {
            key: value
            for key, value in camera.items()
            if key
            not in {
                "motion",
                "objects",
                "review",
                "snapshots",
                "audio",
                "face_recognition",
                "lpr",
            }
        }
        detect = dict(converted.get("detect") or {})
        detect["enabled"] = False
        detect["fps"] = detect_fps
        converted["detect"] = detect
        converted["motion"] = {"enabled": False}
        converted_cameras[name] = converted

    return {
        "auth": source.get("auth", {"enabled": False}),
        "mqtt": source.get("mqtt", {"enabled": False}),
        "ffmpeg": {
            "output_args": {"record": "preset-record-generic-audio-copy"}
        },
        "detect": {"enabled": False, "fps": detect_fps},
        "motion": {"enabled": False},
        "birdseye": {"enabled": False},
        "review": {
            "alerts": {"enabled": False},
            "detections": {"enabled": False},
        },
        "snapshots": {"enabled": False},
        "record": {
            "enabled": True,
            "continuous": {"days": days},
            "motion": {"days": 0},
            "alerts": {"retain": {"days": 0}},
            "detections": {"retain": {"days": 0}},
        },
        "go2rtc": go2rtc,
        "cameras": converted_cameras,
        "version": source.get("version", "0.17-0"),
    }


def recorder_compose(source: dict, image: str) -> dict:
    service = source.get("services", {}).get("frigate")
    if not isinstance(service, dict):
        raise ValueError("Compose file has no frigate service")
    service = dict(service)
    service["image"] = image
    service.pop("runtime", None)
    service.pop("deploy", None)
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        environment = {
            key: value
            for key, value in environment.items()
            if not key.startswith("NVIDIA_")
        }
    elif isinstance(environment, list):
        environment = [
            value for value in environment if not str(value).startswith("NVIDIA_")
        ]
    service["environment"] = environment
    healthcheck = dict(service.get("healthcheck") or {})
    healthcheck["test"] = [
        "CMD-SHELL",
        "curl -fsSk --max-time 5 https://127.0.0.1:8971/api/version >/dev/null",
    ]
    service["healthcheck"] = healthcheck
    return {"services": {"frigate": service}}


def recorder_nginx(server_names: list[str], backend_port: int, cert_dir: str) -> str:
    names = " ".join(server_names)
    return f"""map $http_upgrade $home_ai_connection_upgrade {{
    default upgrade;
    '' close;
}}

server {{
    listen 8971 ssl;
    server_name {names};

    ssl_certificate {cert_dir}/fullchain.pem;
    ssl_certificate_key {cert_dir}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    auth_basic "Home AI";
    auth_basic_user_file /etc/nginx/.htpasswd-home-ai;
    client_max_body_size 50m;

    location / {{
        proxy_pass https://127.0.0.1:{backend_port};
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $home_ai_connection_upgrade;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frigate-config", type=Path, default=Path("/opt/frigate/config/config.yml"))
    parser.add_argument("--compose", type=Path, default=Path("/opt/frigate/docker-compose.yml"))
    parser.add_argument("--nginx-config", type=Path, default=Path("/etc/nginx/sites-available/home-ai-proxies"))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--detect-fps", type=int, default=1)
    parser.add_argument("--image", default="ghcr.io/blakeblackshear/frigate:stable")
    parser.add_argument("--backend-port", type=int, default=18971)
    parser.add_argument("--cert-dir", default="/opt/frigate/certs")
    parser.add_argument(
        "--server-name",
        action="append",
        dest="server_names",
        default=[],
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.days <= 30:
        raise SystemExit("--days must be between 1 and 30")
    if not 1 <= args.detect_fps <= 5:
        raise SystemExit("--detect-fps must be between 1 and 5")
    names = args.server_names or [
        "192.168.1.138",
        "adler-frigate.lan",
        "adler-asr.lan",
        "frigate-ubuntu",
    ]

    config = recorder_config(
        yaml.safe_load(args.frigate_config.read_text(encoding="utf-8")),
        args.days,
        args.detect_fps,
    )
    compose = recorder_compose(
        yaml.safe_load(args.compose.read_text(encoding="utf-8")), args.image
    )
    nginx = recorder_nginx(names, args.backend_port, args.cert_dir)

    if not args.check:
        atomic_write(args.frigate_config, yaml.safe_dump(config, sort_keys=False))
        atomic_write(args.compose, yaml.safe_dump(compose, sort_keys=False))
        atomic_write(args.nginx_config, nginx)
    print(
        f"profile=recorder cameras={len(config['cameras'])} "
        f"days={args.days} detect_fps={args.detect_fps} write={not args.check}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
