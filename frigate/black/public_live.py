#!/usr/bin/env python3
"""Resolve public live sources on demand; add them as optional live streams."""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    """Return a fresh public HLS URL, never storing a signed URL."""
    camera_id, _ = SOURCES[name]
    if camera_id is None:
        return MOSCOW_URL
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
    return source


def configure_public_live(source: dict, resolver_base: str) -> dict:
    """Add only go2rtc sources and optional live-menu entries, without inputs."""
    base = urlsplit(resolver_base)
    address = ipaddress.ip_address(base.hostname or "")
    if not (
        base.scheme == "http" and address.version == 4 and address.is_private
        and not address.is_unspecified and not address.is_multicast
        and base.port is not None and 1024 <= base.port <= 65535
        and base.path in ("", "/") and not base.query and not base.fragment
        and base.username is None and base.password is None
    ):
        raise ValueError("The resolver must use a private IPv4 address and an explicit port")
    resolver_base = resolver_base.rstrip("/")
    config = copy.deepcopy(source)
    camera = config.get("cameras", {}).get(ANCHOR)
    if camera is None:
        raise ValueError("The live menu anchor camera is missing")
    streams = config.setdefault("go2rtc", {}).setdefault("streams", {})
    choices = camera.setdefault("live", {}).setdefault("streams", {})
    for name, (_, label) in SOURCES.items():
        expected = f"ffmpeg:{resolver_base}/{name}.m3u8#video=copy"
        if name in streams and streams[name] != expected:
            raise ValueError("Public stream name is already in use")
        if label in choices and choices[label] != name:
            raise ValueError("Public live menu label is already in use")
        streams[name] = expected
        choices[label] = name
    return config


class ResolverHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def do_GET(self):
        name = {f"/{name}.m3u8": name for name in SOURCES}.get(self.path)
        if name is None:
            self.send_error(404)
            return
        try:
            target = resolve_source(name)
        except Exception:
            self.send_error(503, "Public camera is temporarily unavailable")
            return
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        # No request bodies, temporary URLs, headers or tokens in service logs.
        pass


class ResolverServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def handle_error(self, request, client_address):
        print("Public live request failed", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    resolver = commands.add_parser("resolve")
    resolver.add_argument("source", choices=SOURCES)
    profile = commands.add_parser("profile")
    profile.add_argument("source", type=Path)
    profile.add_argument("candidate", type=Path)
    profile.add_argument("--resolver-base", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--bind", required=True)
    server.add_argument("--port", type=int, default=18786)
    args = parser.parse_args(argv)
    try:
        if args.action == "resolve":
            print(resolve_source(args.source))
        elif args.action == "profile":
            import yaml

            config = configure_public_live(yaml.safe_load(args.source.read_text(encoding="utf-8")), args.resolver_base)
            args.candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        else:
            # Reuse profile validation to reject wildcard, public and IPv6 binds.
            configure_public_live({"cameras": {ANCHOR: {}}}, f"http://{args.bind}:{args.port}")
            with ResolverServer((args.bind, args.port), ResolverHandler) as httpd:
                httpd.serve_forever()
    except Exception:
        # Exceptions can contain signed URLs. Do not copy them into go2rtc logs.
        print("Public live source could not be prepared", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
