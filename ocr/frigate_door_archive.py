"""Durable native Frigate exports, independent of the notification queue."""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone


class APIError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"Frigate HTTP {status}")
        self.status = status


def recorder_api(config):
    base = config["frigate_url"].rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise ValueError("Frigate must use verified HTTPS")
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, base, config["api_user"], config["api_password"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPBasicAuthHandler(manager))

    def request(method, path, body=None):
        if config.get("recorder_ssh"):
            code = """import json,socket,sys,urllib.request,urllib.error
p=json.load(sys.stdin)
assert socket.gethostname()==p['hostname']
assert p['path'].startswith('/api/')
r=urllib.request.Request('http://127.0.0.1:15000'+p['path'],method=p['method'],
    data=json.dumps(p['body']).encode() if p['body'] is not None else None,
    headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(r,timeout=15) as response:
        result={'status':response.status,'body':json.load(response)}
except urllib.error.HTTPError as exc:
    result={'status':exc.code,'body':None}
print(json.dumps(result))
"""
            payload = {"hostname": config["recorder_hostname"], "method": method,
                       "path": path, "body": body}
            result = subprocess.run(
                [config["ssh"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 config["recorder_ssh"], "python3 -c '" + code.replace("'", "'\"'\"'") + "'"],
                input=json.dumps(payload), text=True, capture_output=True, timeout=25)
            if result.returncode:
                raise RuntimeError("Recorder SSH request failed")
            response = json.loads(result.stdout)
            if response["status"] >= 400:
                raise APIError(response["status"])
            return response["body"]
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from exc
    return request


def needs_archive(incident):
    return incident.get("archive_required", False) and incident.get("archive", {}).get("status") != "complete"


def trim_history(incidents, limit=1000):
    unfinished = [row for row in incidents if "delivery_status" not in row or needs_archive(row)]
    finished = [row for row in incidents if "delivery_status" in row and not needs_archive(row)]
    return unfinished + finished[-limit:]


def archive_once(incident, config, request, save, guard=None, now=None):
    """Reconcile before writing. An ambiguous POST is never blindly repeated."""
    if not needs_archive(incident):
        return
    guard = guard if guard is not None else nullcontext()
    now = time.time() if now is None else now

    def update(**fields):
        with guard:
            incident.setdefault("archive", {}).update(fields)
            save()

    if "archive" not in incident:
        stamp = datetime.fromtimestamp(incident["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        prefix = "TEST" if incident.get("test") else "Door opening"
        update(status="pending", name=f"{prefix} | {config['label'][:100]} | {stamp} | {incident['id']}",
               start=int(incident["timestamp"] - 10), end=int(incident["timestamp"] + 30))
    job = incident["archive"]
    # Leave time for the post-roll and the recorder's segment finalization.
    if now < max(job["end"] + 20, job.get("retry_at", 0)):
        return
    posting = False
    try:
        if job.get("export_id"):
            found = request("GET", "/api/exports/" + urllib.parse.quote(job["export_id"], safe=""))
        else:
            rows = request("GET", "/api/exports")
            found = next((row for row in rows if row.get("name") == job["name"]
                          and row.get("camera") == config["camera"]), None)
        if found:
            if found.get("camera") != config["camera"] or found.get("name") != job["name"]:
                raise ValueError("Export identity mismatch")
            complete = found.get("in_progress") is False and bool(found.get("video_path"))
            update(status="complete" if complete else "queued", export_id=found["id"],
                   video_path=found.get("video_path"), checked_at=now, error=None)
            return
        if job["status"] != "pending":
            update(status="unknown", error="POST outcome requires reconciliation", checked_at=now)
            return
        update(status="started", attempted_at=now)
        camera = urllib.parse.quote(config["camera"], safe="")
        posting = True
        result = request("POST", f"/api/export/{camera}/start/{job['start']}/end/{job['end']}",
                         {"source": "recordings", "name": job["name"]})
        if not result.get("success") or not result.get("export_id"):
            raise ValueError("Export creation unconfirmed")
        update(status="queued", export_id=result["export_id"], error=None)
    except APIError as exc:
        # Only these explicit rejection responses prove that no job was queued.
        if posting and exc.status in {429, 503}:
            update(status="pending", retry_at=now + 60, error=str(exc))
        else:
            update(error=str(exc), checked_at=now)
    except Exception as exc:
        update(error=type(exc).__name__, checked_at=now)


class ArchiveQueue:
    def __init__(self, state, config, save, guard, request=None):
        self.state, self.config, self.save, self.guard = state, config, save, guard
        self.request = request if request is not None else recorder_api(config)

    def run(self, stop=None):
        stop = stop if stop is not None else threading.Event()
        while not stop.is_set():
            with self.guard:
                pending = [row for row in self.state["incidents"] if needs_archive(row)]
            for row in pending:
                archive_once(row, self.config, self.request, self.save, self.guard)
            with self.guard:
                self.state["archive_heartbeat"] = time.time()
                self.state["archive_pending"] = sum(needs_archive(row) for row in self.state["incidents"])
                self.state["archive_issues"] = sum(bool(row.get("archive", {}).get("error")) for row in pending)
                self.save()
            stop.wait(5)
