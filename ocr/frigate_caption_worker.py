"""Low-priority, single-event caption worker; run separately from the OCR API.

Configuration is FRIGATE_CAPTION_* environment variables (see Config.from_env).
State must be on a persistent local volume, outside the application/Git tree.
Only text and event metadata are persisted; images remain in memory.
"""

import base64
import http.client
import ipaddress
import json
import logging
import math
import os
import signal
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger("frigate-caption")
MAX_DESCRIPTION_CHARS = 1000
MAX_STATE_EVENTS = 10000
LOOKBACK_SECONDS = 300


class RemoteError(Exception):
    def __init__(self, status=0, message="Remote request failed"):
        super().__init__(message)
        self.status = status


def loopback_url(url, *, priority=False):
    parsed = urllib.parse.urlsplit(url)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL must use a literal loopback address") from exc
    own_llm = priority and parsed.scheme == "http" and str(address) == "192.168.1.242" and port == 8000 and parsed.path == "/slots" and not parsed.query
    if (parsed.scheme not in {"http", "https"} or not (address.is_loopback or own_llm)
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError("Only credential-free loopback HTTP(S) URLs are allowed")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    return parsed


def request_bytes(url, *, method="GET", body=None, limit=1024 * 1024,
                  timeout=10, allow_self_signed=False, priority_key=None,
                  return_headers=False, bypass_cache=False):
    """Bound bytes AND wall time, including trickled headers/body; no proxy/redirect.

    Closing only HTTPConnection is insufficient once getresponse() hands its socket
    to a Connection: close response. Keep the connected socket for the deadline.
    """
    parsed = loopback_url(url, priority=priority_key is not None)
    started = time.monotonic()
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        if allow_self_signed:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port, timeout=timeout, context=context
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    timer = None
    expired = threading.Event()
    try:
        connection.connect()
        connected_socket = connection.sock

        def expire():
            expired.set()
            try:
                connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Request deadline exceeded")
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        headers = {"Accept": "application/json", "Connection": "close"}
        if bypass_cache:
            headers["X-Cache-Bypass"] = "1"
        if priority_key is not None:
            if method != "GET" or parsed.path != "/slots":
                raise ValueError("Priority key is restricted to the slots probe")
            headers["Authorization"] = "Bearer " + priority_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        declared = response.getheader("Content-Length")
        if declared is not None and (int(declared) < 0 or int(declared) > limit):
            raise RemoteError(413, "Remote response too large")
        data = response.read(limit + 1)
        if expired.is_set() or time.monotonic() - started >= timeout:
            raise TimeoutError("Request deadline exceeded")
        if len(data) > limit:
            raise RemoteError(413, "Remote response too large")
        if declared is not None and len(data) != int(declared):
            raise RemoteError(0, "Incomplete remote response")
        if not 200 <= response.status < 300:
            raise RemoteError(response.status)
        if return_headers:
            return data, {key.lower(): value for key, value in response.getheaders()}
        return data
    except (OSError, http.client.HTTPException) as exc:
        raise RemoteError(0) from exc
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def env_int(name, default, minimum, maximum):
    value = int(os.getenv("FRIGATE_CAPTION_" + name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"FRIGATE_CAPTION_{name} must be {minimum}..{maximum}")
    return value


@dataclass(frozen=True)
class Config:
    state_dir: Path = Path("/var/lib/frigate-caption")
    frigate_url: str = "http://127.0.0.1:15000"
    caption_url: str = "https://127.0.0.1/v1/frigate-caption"
    priority_url: str = ""
    priority_key_file: str = ""
    asr_health_url: str = ""
    poll_seconds: int = 30
    queue_limit: int = 500
    retention_seconds: int = 3 * 86400
    event_ttl: int = 6 * 3600
    max_attempts: int = 10
    backoff_max: int = 1800
    http_timeout: int = 10
    caption_timeout: int = 90
    image_bytes: int = 8 * 1024 * 1024
    json_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls):
        return cls(
            state_dir=Path(os.getenv("FRIGATE_CAPTION_STATE_DIR", "/var/lib/frigate-caption")),
            frigate_url=os.getenv("FRIGATE_CAPTION_FRIGATE_URL", cls.frigate_url).rstrip("/"),
            caption_url=os.getenv("FRIGATE_CAPTION_URL", cls.caption_url),
            # Opt-in protected loopback /slots; failure/unknown schema defers work.
            priority_url=os.getenv("FRIGATE_CAPTION_PRIORITY_URL", ""),
            priority_key_file=os.getenv("FRIGATE_CAPTION_PRIORITY_KEY_FILE", ""),
            asr_health_url=os.getenv("FRIGATE_CAPTION_ASR_HEALTH_URL", ""),
            poll_seconds=env_int("POLL_SECONDS", 30, 30, 3600),
            queue_limit=env_int("QUEUE_LIMIT", 500, 1, 500),
            retention_seconds=env_int("RETENTION_SECONDS", 3 * 86400, 300, 3 * 86400),
            event_ttl=env_int("EVENT_TTL", 6 * 3600, 30, 3 * 86400),
            max_attempts=env_int("MAX_ATTEMPTS", 10, 1, 100),
            backoff_max=env_int("BACKOFF_MAX", 1800, 30, 3600),
            http_timeout=env_int("HTTP_TIMEOUT", 10, 1, 30),
            caption_timeout=env_int("TIMEOUT", 90, 1, 90),
            image_bytes=env_int("IMAGE_BYTES", 8 * 1024 * 1024, 1024, 8 * 1024 * 1024),
            json_bytes=env_int("JSON_BYTES", 2 * 1024 * 1024, 1024, 2 * 1024 * 1024),
        )

    def __post_init__(self):
        loopback_url(self.frigate_url)
        loopback_url(self.caption_url)
        if self.priority_url:
            loopback_url(self.priority_url, priority=bool(self.priority_key_file))
        if self.asr_health_url:
            loopback_url(self.asr_health_url)
        if not (30 <= self.poll_seconds <= 3600 and 1 <= self.queue_limit <= 500
                and 300 <= self.retention_seconds <= 3 * 86400
                and 30 <= self.event_ttl <= 3 * 86400 and 1 <= self.max_attempts <= 100
                and 30 <= self.backoff_max <= 3600 and 1 <= self.http_timeout <= 30
                and 1 <= self.caption_timeout <= 90
                and 1024 <= self.image_bytes <= 8 * 1024 * 1024
                and 1024 <= self.json_bytes <= 2 * 1024 * 1024):
            raise ValueError("Caption worker configuration is outside hard bounds")


def description(event):
    data = event.get("data")
    value = data.get("description") if isinstance(data, dict) else None
    if value is None:
        value = event.get("description")
    if value is not None and not isinstance(value, str):
        raise ValueError("Invalid event description")
    # Whitespace is still an existing edit: never silently replace it.
    return value or ""


def event_identity(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid event")
    event_id, camera = event.get("id"), event.get("camera")
    if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 200
            or not isinstance(camera, str) or not 1 <= len(camera) <= 200
            or any(ord(char) < 32 for char in event_id + camera)):
        raise ValueError("Invalid event identity")
    for key in ("start_time", "end_time"):
        value = event.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Only finished events with valid timestamps are accepted")
    if event["end_time"] < event["start_time"]:
        raise ValueError("Invalid event times")
    return event_id, camera


class State:
    def __init__(self, path, config):
        self.config = config
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, seen REAL NOT NULL,
                deadline REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL, caption TEXT, write_started INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS due_events ON events(status, next_try);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def prune(self, now):
        with self.db:
            self.db.execute("UPDATE events SET status='expired' WHERE status='queued' AND deadline<=?", (now,))
            self.db.execute("DELETE FROM events WHERE seen<=?", (now - self.config.retention_seconds,))
            # Keep even terminal dedup records bounded. Never discard IDs still in
            # the discovery window; refuse intake instead of repeatedly captioning.
            self.db.execute("""DELETE FROM events WHERE id IN (
                SELECT id FROM events WHERE status!='queued' AND seen<? ORDER BY seen
                LIMIT MAX(0, (SELECT COUNT(*) FROM events)-?)
            )""", (now - LOOKBACK_SECONDS, MAX_STATE_EVENTS - self.config.queue_limit))

    def enqueue(self, event, now):
        event_id, camera = event_identity(event)
        if self.db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            return False
        status = "skipped" if description(event) else "queued"
        total = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        queued = self.db.execute("SELECT COUNT(*) FROM events WHERE status='queued'").fetchone()[0]
        if total >= MAX_STATE_EVENTS or (status == "queued" and queued >= self.config.queue_limit):
            return False
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO events
                (id,camera,seen,deadline,status,next_try) VALUES (?,?,?,?,?,?)""",
                (event_id, camera, now, now + min(self.config.event_ttl, self.config.retention_seconds), status, now))
        return True

    def due(self, now):
        return self.db.execute("""SELECT * FROM events WHERE status='queued'
            AND next_try<=? AND deadline>? ORDER BY next_try,seen,id LIMIT 1""", (now, now)).fetchone()

    def finish(self, event_id, status):
        with self.db:
            self.db.execute("UPDATE events SET status=?,error=NULL WHERE id=?", (status, event_id))

    def attempt(self, row, now):
        attempts = row["attempts"] + 1
        with self.db:
            # Schedule before doing I/O, so restart cannot create a rapid retry loop.
            self.db.execute("UPDATE events SET attempts=?,next_try=? WHERE id=?",
                            (attempts, now + self.config.poll_seconds, row["id"]))
        return attempts

    def retry(self, row, attempts, now, error):
        delay = min(self.config.backoff_max, self.config.poll_seconds * 2 ** min(attempts - 1, 16))
        status = "expired" if attempts >= self.config.max_attempts or now + delay >= row["deadline"] else "queued"
        with self.db:
            self.db.execute("UPDATE events SET status=?,next_try=?,error=? WHERE id=?",
                            (status, now + delay, error[:120], row["id"]))

    def save_caption(self, event_id, caption):
        with self.db:
            self.db.execute("UPDATE events SET caption=? WHERE id=?", (caption, event_id))

    def begin_write(self, event_id):
        with self.db:
            self.db.execute("UPDATE events SET write_started=1 WHERE id=?", (event_id,))


class Worker:
    def __init__(self, config, state, *, transport=request_bytes, clock=time.time, stop=None):
        self.config, self.state, self.transport, self.clock = config, state, transport, clock
        self.stop = stop if stop is not None else threading.Event()
        self.start_floor = clock() - LOOKBACK_SECONDS

    def fetch(self, url, *, body=None, image=False, caption=False):
        extra = {}
        if url == self.config.priority_url and self.config.priority_key_file:
            key = Path(self.config.priority_key_file).read_text().strip()
            if not key or len(key) > 4096 or any(char.isspace() for char in key):
                raise ValueError("Invalid priority key file")
            extra["priority_key"] = key
        return self.transport(
            url, method="POST" if body is not None else "GET",
            body=None if body is None else json.dumps(body).encode(),
            limit=self.config.image_bytes if image else self.config.json_bytes,
            timeout=self.config.caption_timeout if caption else self.config.http_timeout,
            allow_self_signed=True,
            **extra,
        )

    def json(self, url, **kwargs):
        return json.loads(self.fetch(url, **kwargs))

    def poll(self):
        now = self.clock()
        after = max(self.start_floor, now - LOOKBACK_SECONDS)
        # Frigate's after/before filter start_time, even for finished events.
        # Scan the retained range, then admit by completion time, including events
        # that remained active longer than the five-minute discovery window.
        before = now
        for _page in range(MAX_STATE_EVENTS // 500):
            query = urllib.parse.urlencode({"after": max(0, now - self.config.retention_seconds),
                                           "before": before, "in_progress": 0, "sort": "date_desc",
                                           "include_thumbnails": 0, "limit": 500})
            events = self.json(self.config.frigate_url + "/api/events?" + query)
            if not isinstance(events, list) or len(events) > 500:
                raise ValueError("Invalid events response")
            starts = []
            for event in events:
                if self.stop.is_set():
                    return
                try:
                    event_identity(event)
                    starts.append(event["start_time"])
                    if after <= event["end_time"] <= now:
                        self.state.enqueue(event, now)
                except ValueError:
                    log.warning("Ignoring invalid finished event")
            if len(events) < 500 or not starts or min(starts) >= before:
                break
            before = min(starts)

    def event(self, row):
        path = "/api/events/" + urllib.parse.quote(row["id"], safe="")
        event = self.json(self.config.frigate_url + path)
        if event_identity(event) != (row["id"], row["camera"]):
            raise ValueError("Event identity changed")
        return event, path

    def priority_idle(self):
        if self.config.asr_health_url:
            health = self.json(self.config.asr_health_url)
            if not isinstance(health, dict) or type(health.get("idle")) is not bool:
                raise ValueError("Unknown ASR priority state")
            if not health["idle"]:
                return False
        if not self.config.priority_url:
            return True
        slots = self.json(self.config.priority_url)
        if isinstance(slots, dict):
            slots = slots.get("slots")
        if not isinstance(slots, list) or not slots:
            raise ValueError("Unknown priority probe schema")
        if any(not isinstance(slot, dict) or type(slot.get("is_processing")) is not bool for slot in slots):
            raise ValueError("Unknown priority slot state")
        return not any(slot["is_processing"] for slot in slots)

    def process_one(self):
        row = self.state.due(self.clock())
        if row is None or self.stop.is_set():
            return
        if row["attempts"] >= self.config.max_attempts:
            self.state.finish(row["id"], "expired")
            return
        attempts = self.state.attempt(row, self.clock())
        try:
            event, path = self.event(row)
            existing = description(event)
            if existing:
                self.state.finish(row["id"], "done" if row["write_started"] and existing == row["caption"] else "skipped")
                return
            if row["write_started"]:
                # POST outcome is uncertain, including a crash between commit and
                # send. Verify only; reposting can overwrite a later manual clear.
                raise RemoteError(0, "Description not yet read back")
            if not self.priority_idle():
                raise RemoteError(429, "General LLM busy")
            caption = row["caption"]
            if not caption:
                try:
                    image = self.fetch(self.config.frigate_url + path + "/snapshot.jpg", image=True)
                except RemoteError as exc:
                    if exc.status not in {404, 400}:
                        raise
                    image = self.fetch(self.config.frigate_url + path + "/thumbnail.jpg", image=True)
                if not image:
                    raise ValueError("Empty event image")
                if self.stop.is_set():
                    return
                reply = self.json(self.config.caption_url,
                                  body={"images": [base64.b64encode(image).decode("ascii")]}, caption=True)
                caption = reply.get("description") if isinstance(reply, dict) else None
                if not isinstance(caption, str) or not caption.strip() or len(caption) > MAX_DESCRIPTION_CHARS:
                    raise ValueError("Invalid caption response")
                caption = caption.strip()
                self.state.save_caption(row["id"], caption)
            if self.stop.is_set():
                return
            # Generation may take 90s: fetch again immediately before the write.
            event, path = self.event(row)
            if description(event):
                self.state.finish(row["id"], "skipped")
                return
            if self.clock() >= row["deadline"]:
                self.state.finish(row["id"], "expired")
                return
            # Frigate 0.18 has no conditional description update/CAS. This final
            # read protects edits during generation, not a simultaneous GET/POST
            # race. Persist intent first and never retry this unconditional write.
            self.state.begin_write(row["id"])
            self.fetch(self.config.frigate_url + path + "/description", body={"description": caption})
            actual = description(self.event(row)[0])
            if actual == caption:
                self.state.finish(row["id"], "done")
            elif actual:
                self.state.finish(row["id"], "skipped")
            else:
                raise RemoteError(0, "Description readback mismatch")
        except RemoteError as exc:
            if exc.status == 404:
                self.state.finish(row["id"], "skipped")
            else:
                self.state.retry(row, attempts, self.clock(), f"remote status={exc.status}")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.state.retry(row, attempts, self.clock(), type(exc).__name__)

    def tick(self):
        self.state.prune(self.clock())
        try:
            self.poll()
        except (RemoteError, OSError, ValueError, TypeError):
            log.warning("Event poll deferred")
        self.process_one()

    def run(self):
        while not self.stop.is_set():
            self.tick()
            # No catch-up bursts, even when a previous tick took longer than 30s.
            self.stop.wait(self.config.poll_seconds)


@contextmanager
def exclusive_worker(state_dir):
    # Advisory OS lock is released on crash; a stale PID file never blocks restart.
    stream = (state_dir / "worker.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def main():
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env()
    directory = config.state_dir.resolve()
    if (directory == Path(__file__).resolve().parent
            or Path(__file__).resolve().parent in directory.parents
            or any((parent / ".git").exists() for parent in (directory, *directory.parents))):
        raise ValueError("State directory must be outside Git and the application tree")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    with exclusive_worker(directory):
        state = State(directory / "state.sqlite3", config)
        try:
            Worker(config, state, stop=stop).run()
        finally:
            state.close()


if __name__ == "__main__":
    main()
