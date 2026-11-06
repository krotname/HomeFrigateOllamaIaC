#!/usr/bin/env python3
"""Resolve public live sources on demand; add them as optional live streams."""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ANCHOR = "cam1_ds_i202"
SOURCES = {
    "public_adler_station": (445, "Город: вокзал Адлера"),
    "public_adler_sea": (170, "Море и ж/д: Фрегат"),
    "public_moscow_ryazansky": (None, "Москва: Рязанский / Фрезер"),
}
MOSCOW_URL = "https://cameras.inetcom.ru/hls/camera12_10.m3u8"
RESPONSE_LIMIT = 262144


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "Public camera redirects are disabled", headers, response)


def resolve_source(name: str, opener=None) -> str:
    """Return a fresh go2rtc FFmpeg source, never storing a signed URL."""
    camera_id, _ = SOURCES[name]
    if camera_id is None:
        return "ffmpeg:" + MOSCOW_URL + "#video=copy"
    api = f"https://sochi.camera/vse-kamery/cam-{camera_id}/?format=json"
    request = Request(api, headers={"User-Agent": "Frigate-Public-Live/1.0", "Accept": "application/json"})
    with (opener or build_opener(NoRedirect()).open)(request, timeout=12) as response:
        api_url = urlsplit(response.geturl())
        if api_url.scheme != "https" or api_url.hostname != "sochi.camera":
            raise ValueError("Unexpected public camera API host")
        payload = response.read(RESPONSE_LIMIT + 1)
    if len(payload) > RESPONSE_LIMIT:
        raise ValueError("Public camera response is too large")
    data = json.loads(payload.decode("utf-8-sig"))
    source = data.get("video_url") if isinstance(data, dict) else None
    if not isinstance(source, str) or re.search(r"[\s\\\"'<>]", source):
        raise ValueError("Invalid public camera source")
    parsed = urlsplit(source)
    if not (
        parsed.scheme == "https"
        and parsed.hostname == "sochi.camera"
        and parsed.port == 8443
        and parsed.username is None
        and parsed.password is None
        and parsed.path == f"/cam_{camera_id}/index.m3u8"
        and not parsed.fragment
        and parse_qs(parsed.query).get("token")
    ):
        raise ValueError("Unexpected public camera source")
    return "ffmpeg:" + source + "#video=copy"


def configure_public_live(source: dict) -> dict:
    """Add only go2rtc sources and optional live-menu entries, without inputs."""
    config = copy.deepcopy(source)
    camera = config.get("cameras", {}).get(ANCHOR)
    if camera is None:
        raise ValueError("The live menu anchor camera is missing")
    streams = config.setdefault("go2rtc", {}).setdefault("streams", {})
    choices = camera.setdefault("live", {}).setdefault("streams", {})
    for name, (_, label) in SOURCES.items():
        expected = f"echo:python3 /config/public_live.py resolve {name}"
        if name in streams and streams[name] != expected:
            raise ValueError("Public stream name is already in use")
        if label in choices and choices[label] != name:
            raise ValueError("Public live menu label is already in use")
        streams[name] = expected
        choices[label] = name
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    resolver = commands.add_parser("resolve")
    resolver.add_argument("source", choices=SOURCES)
    profile = commands.add_parser("profile")
    profile.add_argument("source", type=Path)
    profile.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "resolve":
            print(resolve_source(args.source))
        else:
            import yaml

            config = configure_public_live(yaml.safe_load(args.source.read_text(encoding="utf-8")))
            args.candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        # Exceptions can contain signed URLs. Do not copy them into go2rtc logs.
        print("Public live source could not be prepared", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
