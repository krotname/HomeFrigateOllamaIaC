#!/usr/bin/env python3
"""Hik-Connect VTM to HEVC pipe; credentials never leave the runtime file."""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import signal
import socket
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CONFIG = Path(os.environ.get("HIK_CONFIG", "/run/hikconnect/runtime.local.json"))
CACHE = Path(os.environ.get("HIK_SESSION_CACHE", "/tmp/hik-session.json"))
PREFIX = b"\0\0\0\1"


class LocalOutputClosed(Exception):
    """The local reader departed; cloud socket errors remain recoverable."""


class CloudError(Exception):
    """An intentionally non-sensitive cloud failure."""


def log(event: str, **fields) -> None:
    # Callers pass only aliases, counts and exception class names, never messages.
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".session-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


class Sessions:
    def __init__(self, config: dict, cache: Path = CACHE, clock=time.time):
        self.config, self.cache, self.clock = config, cache, clock
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, base: str, path: str, data: dict, headers: dict) -> dict:
        url = base.rstrip("/") + path
        if urllib.parse.urlparse(url).scheme != "https":
            raise CloudError("TLS required")
        request = urllib.request.Request(
            url, urllib.parse.urlencode(data).encode(),
            {"Content-Type": "application/x-www-form-urlencoded", **headers},
        )
        with self.http.open(request, timeout=15) as response:
            body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise CloudError("oversized API response")
        return json.loads(body)

    def token(self) -> str:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.cache) + ".lock", "a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.cache.read_text())
            except (FileNotFoundError, ValueError):
                state = {}
            now = self.clock()
            headers = {"clientType": "55", "featureCode": state.get("feature", uuid.uuid4().hex)}
            if state.get("expires", 0) <= now:
                login = self.request(self.config["api_base"], "/v3/users/login/v2", {
                    "account": self.config["account"],
                    "password": hashlib.md5(self.config["password"].encode()).hexdigest(),
                    "featureCode": headers["featureCode"],
                }, headers)
                if str(login.get("meta", {}).get("code")) != "200":
                    raise CloudError("login rejected")
                data = login["loginSession"] if "loginSession" in login else login["data"]["loginSession"]
                state = {"sid": data["sessionId"], "feature": headers["featureCode"],
                         "expires": now + min(int(self.config.get("session_ttl_seconds", 600)), 3600),
                         "tokens": [], "tokens_expire": 0}
                log("session_refreshed")
            headers["sessionId"] = state["sid"]
            if not state.get("tokens") or state.get("tokens_expire", 0) <= now:
                try:
                    reply = self.request(self.config["token_base"], "/api/user/token/get",
                                         {"sessionId": state["sid"], "count": 10}, headers)
                except urllib.error.HTTPError as error:
                    if error.code in (401, 403):
                        atomic_json(self.cache, {})
                    raise
                if str(reply.get("resultCode")) != "0" or not reply.get("tokenArray"):
                    atomic_json(self.cache, {})
                    raise CloudError("token rejected")
                state["tokens"] = reply["tokenArray"]
                state["tokens_expire"] = now + 60  # Consume fresh tokens, never persist long-lived grants.
            token = state["tokens"].pop(0)
            atomic_json(self.cache, state)
            if not isinstance(token, str) or not token:
                raise CloudError("invalid token")
            return token


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number: int, value: bytes | str) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return varint(number * 8 + 2) + varint(len(data)) + data


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        octet = data[offset]
        offset += 1
        value |= (octet & 127) << shift
        if not octet & 128:
            return value, offset
    raise CloudError("invalid protobuf")


def protobuf(data: bytes) -> dict:
    result, offset = {}, 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        wire = tag & 7
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 2:
            size, offset = read_varint(data, offset)
            if offset + size > len(data):
                raise CloudError("truncated protobuf")
            value, offset = data[offset:offset + size], offset + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, offset = data[offset:offset + size], offset + size
        else:
            raise CloudError("unsupported protobuf")
        result[tag >> 3] = value
    return result


def exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            raise EOFError()
        result.extend(part)
    return bytes(result)


def packet(connection: socket.socket) -> tuple[int, int, bytes]:
    magic, channel, length, _, code = struct.unpack(">BBHHH", exact(connection, 8))
    if magic != 36:
        raise CloudError("invalid framing")
    return channel, code, exact(connection, length)


def send(connection: socket.socket, body: bytes, code=0x13B, seq=0) -> None:
    connection.sendall(struct.pack(">BBHHH", 36, 0, len(body), seq & 65535, code) + body)


def relay_address(url: str, config: dict) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "ysproto" or not parsed.hostname or not parsed.port:
        raise CloudError("invalid relay address")
    name = parsed.hostname.lower()
    try:
        if not ipaddress.ip_address(name).is_global:
            raise CloudError("non-public redirect")
    except ValueError:
        suffixes = tuple(config.get("relay_domain_suffixes", [".ezvizru.com", ".hik-connectru.com"]))
        if name != config["vtm_host"] and not name.endswith(suffixes):
            raise CloudError("unexpected relay domain")
        addresses = socket.getaddrinfo(name, parsed.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise CloudError("non-public relay DNS")
        # Connect to the checked address; do not perform a second DNS lookup.
        name = addresses[0][4][0]
    return name, parsed.port


def connect(config: dict, channel: int, stream: int, token: str):
    query = {"biz": 1, "dev": config["device_serial"], "chn": channel, "stream": stream,
             "cln": 55, "isp": 0, "auth": 1, "ssn": token, "vip": 0,
             "timestamp": int(time.time() * 1000)}
    url = f'ysproto://{config["vtm_host"]}:{config.get("vtm_port", 8554)}/live?' + urllib.parse.urlencode(query)
    stream_key = None
    for redirect in range(4):
        connection = socket.create_connection(relay_address(url, config), timeout=10)
        try:
            body = field(1, url) + (field(2, stream_key) if stream_key else b"")
            send(connection, body + field(3, "v3.6.3.20221124") + b"\x20\x00" + field(6, "v3.6.3.20221124"))
            info = None
            for _ in range(20):
                _, code, response = packet(connection)
                if code == 0x13C:
                    info = protobuf(response)
                    break
            if info and info.get(1) == 0:
                return connection, info
            if info and info.get(7) and info.get(5) and redirect < 3:
                url, stream_key = info[7].decode(), info[5]
            else:
                raise CloudError("VTM rejected")
        except BaseException:
            connection.close()
            raise
        connection.close()
    raise CloudError("too many redirects")


class Hevc:
    """Bounded RFC 7798 depacketizer for this Hikvision HEVC profile."""
    def __init__(self, verification: str):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        self.cipher = lambda: Cipher(algorithms.AES(verification.encode().ljust(16, b"\0")[:16]), modes.ECB()).decryptor()
        self.fragment, self.seq, self.parameters, self.ready = None, None, {}, False

    def nal(self, data: bytes) -> bytes:
        kind = (data[0] >> 1) & 63
        # This recorder encrypts parameter sets and IRAPs; P slices are clear.
        if kind in (32, 33, 34) or 16 <= kind <= 23:
            size = min(4096, len(data) - 2) // 16 * 16
            if size:
                decoder = self.cipher()
                data = data[:2] + decoder.update(data[2:2 + size]) + decoder.finalize() + data[2 + size:]
        if kind in (32, 33, 34):
            self.parameters[kind] = data
            return b""
        if 16 <= kind <= 23 and len(self.parameters) == 3:
            self.ready = True
            return b"".join(PREFIX + self.parameters[k] for k in (32, 33, 34)) + PREFIX + data
        return PREFIX + data if self.ready and kind < 32 else b""

    def feed(self, rtp: bytes) -> bytes:
        if len(rtp) < 12 or rtp[0] >> 6 != 2 or (rtp[1] & 127) != 96:
            return b""
        offset = 12 + 4 * (rtp[0] & 15)
        if rtp[0] & 16:
            if len(rtp) < offset + 4:
                raise CloudError("truncated RTP extension")
            offset += 4 + 4 * int.from_bytes(rtp[offset + 2:offset + 4], "big")
        end = len(rtp) - (rtp[-1] if rtp[0] & 32 else 0)
        data = rtp[offset:end]
        if len(data) >= 13 and data[0] == 13 and data[1] & 240 in (128, 144, 160, 208):
            data = data[13:]
        if len(data) < 2:
            return b""
        kind, seq = (data[0] >> 1) & 63, int.from_bytes(rtp[2:4], "big")
        if self.seq is not None and seq != ((self.seq + 1) & 65535):
            self.fragment, self.ready = None, False
        self.seq = seq
        if kind == 49 and len(data) > 3:
            if data[2] & 128:
                self.fragment = bytearray([(data[0] & 129) | ((data[2] & 63) << 1), data[1]])
            elif self.fragment is None:
                return b""
            self.fragment.extend(data[3:])
            if len(self.fragment) > 2_097_152:
                self.fragment, self.ready = None, False
                raise CloudError("oversized NAL")
            if data[2] & 64:
                result, self.fragment = bytes(self.fragment), None
                return self.nal(result)
            return b""
        if kind == 48:
            result, offset = bytearray(), 2
            while offset + 2 <= len(data):
                size = int.from_bytes(data[offset:offset + 2], "big")
                offset += 2
                if size < 2 or offset + size > len(data):
                    raise CloudError("truncated aggregation")
                result.extend(self.nal(data[offset:offset + size]))
                offset += size
            return bytes(result)
        return self.nal(data) if kind <= 40 else b""


class RtpClock:
    """Keep the recorder's 90 kHz clock, including reconnect gaps and wraps."""
    def __init__(self):
        self.source, self.timestamp, self.wall = None, 0, None

    def advance(self, source: int, wall: float) -> int:
        if self.source is not None:
            delta = (source - self.source) & 0xFFFFFFFF
            if delta > 900000:  # A new recorder clock, rather than a normal wrap.
                delta = max(1, round((wall - self.wall) * 90000))
            self.timestamp = (self.timestamp + delta) & 0xFFFFFFFF
        self.source, self.wall = source, wall
        return self.timestamp


class Publisher:
    """Publish plaintext RTP only to go2rtc's local exec receiver."""
    def __init__(self, output: str, parameters: dict):
        parsed = urllib.parse.urlparse(output)
        if parsed.scheme != "rtsp" or parsed.hostname != "127.0.0.1" or parsed.port != 8554:
            raise CloudError("non-local publisher")
        self.url, self.sequence, self.cseq, self.session = output, 0, 0, None
        self.ssrc = uuid.uuid4().int & 0xFFFFFFFF
        self.clock = RtpClock()
        self.connection = socket.create_connection(("127.0.0.1", 8554), timeout=10)
        self.reader = self.connection.makefile("rb")
        try:
            values = [base64.b64encode(parameters[k]).decode() for k in (32, 33, 34)]
            sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Camera\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                   "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"
                   f"a=fmtp:96 sprop-vps={values[0]};sprop-sps={values[1]};sprop-pps={values[2]}\r\n"
                   "a=control:trackID=0\r\n").encode()
            self.request("ANNOUNCE", output, {"Content-Type": "application/sdp"}, sdp)
            headers = self.request("SETUP", output + "/trackID=0",
                                   {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
            self.session = headers["session"].split(";", 1)[0]
            self.request("RECORD", output, {"Range": "npt=0.000-"})
        except BaseException:
            self.close()
            raise

    def request(self, method: str, url: str, headers: dict, body=b"") -> dict:
        self.cseq += 1
        headers = {"CSeq": str(self.cseq), "Content-Length": str(len(body)), **headers}
        if self.session:
            headers["Session"] = self.session
        message = f"{method} {url} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.connection.sendall(message.encode() + body)
        status = self.reader.readline(4096)
        if not status.startswith(b"RTSP/1.0 200 "):
            raise CloudError("local RTSP rejected")
        response = {}
        for _ in range(64):
            line = self.reader.readline(4096)
            if line == b"\r\n":
                break
            if not line or len(line) >= 4096:
                raise CloudError("invalid local RTSP response")
            key, value = line.decode().split(":", 1)
            response[key.lower()] = value.strip()
        else:
            raise CloudError("oversized local RTSP headers")
        size = int(response.get("content-length", 0))
        if not 0 <= size <= 65536:
            raise CloudError("oversized local RTSP body")
        self.reader.read(size)
        return response

    def write(self, annexb: bytes, source_timestamp: int, source_marker: bool) -> None:
        nals = [nal for nal in annexb.split(PREFIX) if nal]
        timestamp = self.clock.advance(source_timestamp, time.monotonic())
        for index, nal in enumerate(nals):
            if len(nal) <= 1200:
                chunks = [nal]
            else:
                head, kind, tail = bytes([(nal[0] & 129) | (49 << 1), nal[1]]), (nal[0] >> 1) & 63, nal[2:]
                chunks = [head + bytes([kind | (128 if pos == 0 else 0) |
                                       (64 if pos + 1197 >= len(tail) else 0)]) + tail[pos:pos + 1197]
                          for pos in range(0, len(tail), 1197)]
            for fragment, payload in enumerate(chunks):
                marker = source_marker and index == len(nals) - 1 and fragment == len(chunks) - 1
                header = struct.pack(">BBHII", 128, 96 | (128 if marker else 0), self.sequence, timestamp, self.ssrc)
                self.sequence = (self.sequence + 1) & 65535
                self.connection.sendall(struct.pack(">BBH", 36, 0, len(header) + len(payload)) + header + payload)

    def close(self) -> None:
        self.reader.close()
        self.connection.close()


def stream(config: dict, alias: str, quality: str, output: str | None = None) -> None:
    channel = int(config["cameras"][alias])
    selector = 1 if quality == "main" else 2
    sessions, connection, publisher, running, reconnect = Sessions(config), None, None, True, False

    def stop(signum, _frame):
        nonlocal running, reconnect
        if signum == signal.SIGUSR1:
            reconnect = True
        else:
            running = False
        if connection:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
        signal.signal(signum, stop)
    delay = 1
    while running:
        try:
            connection, info = connect(config, channel, selector, sessions.token())
            video, last, sequence = Hevc(config["verification_code"]), time.monotonic(), 1
            log("connected", camera=alias, stream=quality)
            delay, reconnect = 1, False
            while running and not reconnect:
                if time.monotonic() - last >= 4:
                    send(connection, field(1, info[4]), 0x132, sequence)
                    sequence, last = sequence + 1, time.monotonic()
                channel_id, code, body = packet(connection)
                if channel_id == 1 and code == info[2]:
                    out = video.feed(body)
                    if out:
                        try:
                            if output:
                                if publisher is None:
                                    publisher = Publisher(output, video.parameters)
                                publisher.write(out, int.from_bytes(body[4:8], "big"), bool(body[1] & 128))
                            else:
                                sys.stdout.buffer.write(out)
                                sys.stdout.buffer.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            raise LocalOutputClosed() from None
                elif channel_id == 11:
                    raise CloudError("unsupported encrypted transport")
                elif code == 0x132:
                    send(connection, field(1, info[4]), 0x133, sequence)
                    sequence += 1
        except LocalOutputClosed:
            break  # Last RTSP reader left; go2rtc owns worker lifetime.
        except Exception as error:
            if running:
                log("reconnecting", camera=alias, stream=quality, error=type(error).__name__)
        finally:
            if connection:
                connection.close()
                connection = None
        if running:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    if publisher:
        publisher.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera", nargs="?")
    parser.add_argument("quality", nargs="?", choices=("main", "sub"))
    parser.add_argument("output", nargs="?")
    parser.add_argument("--expire-session", action="store_true")
    options = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    if options.expire_session:
        with open(str(CACHE) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            atomic_json(CACHE, {})
        print("Session expired locally; next connection will authenticate again.")
    elif options.camera and options.quality:
        stream(config, options.camera, options.quality, options.output)
    else:
        parser.error("camera and quality required")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log("worker_failed", error=type(error).__name__)
        sys.exit(1)
