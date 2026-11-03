"""Record and notify each closed-to-open state-classification transition.

Camera crops, credentials and training images belong in private runtime storage.
Run on the Windows notification host; the recorder never receives a bot token.
"""

import argparse
import base64
from contextlib import nullcontext
import json
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ocr.frigate_caption_worker import exclusive_worker
from ocr.frigate_protected import atomic_json

VERSION = "1.0.0"


class Transitions:
    def __init__(self):
        self.previous = None

    def reset(self):
        self.previous = None

    def observe(self, state, retained=False):
        if state not in {"open", "closed"}:
            return False
        opened = self.previous == "closed" and state == "open" and not retained
        self.previous = state
        return opened


def message(incident, config):
    from datetime import datetime, timedelta, timezone
    stamp = datetime.fromtimestamp(incident["timestamp"], timezone(timedelta(hours=3)))
    title = ("Тест тревоги двери. Реальное открытие не обнаружено."
             if incident.get("test") else "Открыта дверь в ваше помещение.")
    lines = [title, config["label"], stamp.strftime("%d.%m.%Y %H:%M:%S МСК")]
    if incident.get("event_id"):
        event = urllib.parse.quote(incident["event_id"], safe="")
        lines.append(config["frigate_url"] + "/api/events/" + event + "/clip.mp4")
    elif incident.get("recording_url"):
        lines.append(incident["recording_url"])
    else:
        lines.append("Создание события Frigate не подтверждено.")
    return "\n".join(lines)


def record(incident, save, create_event, guard=None):
    # A started POST may have succeeded even if its response was lost.
    # Persist the attempt before sending; never blindly repeat ambiguous writes.
    guard = guard if guard is not None else nullcontext()
    if "event_status" not in incident:
        with guard:
            incident["event_status"] = "started"
            save()
        try:
            result = create_event(incident)
            if not result.get("success") or not (result.get("event_id") or result.get("recording_url")):
                raise ValueError("Event creation unconfirmed")
            with guard:
                incident.update(event_status="created" if result.get("event_id") else "archive_range")
                incident.update({k: result[k] for k in ("event_id", "recording_url") if k in result})
        except Exception as exc:
            with guard:
                incident.update(event_status="unknown", event_error=type(exc).__name__)
        save()


def deliver(incident, config, save, send, guard=None):
    guard = guard if guard is not None else nullcontext()
    if "delivery_status" not in incident:
        with guard:
            incident["delivery_status"] = "started"
            save()
        try:
            result = send(message(incident, config))
            with guard:
                incident["delivery"] = result
            if not result.get("ok") or not result.get("verified"):
                raise ValueError("Telegram delivery unconfirmed")
            with guard:
                incident.update(delivery_status="verified", delivery=result)
        except Exception as exc:
            with guard:
                incident.update(delivery_status="unknown", delivery_error=type(exc).__name__)
        save()


def process(incident, config, save, create_event, send, guard=None):
    record(incident, save, create_event, guard)
    deliver(incident, config, save, send, guard)


class RecordingQueue:
    """Record independently of the potentially slow Telegram consumer."""

    def __init__(self, save, create_event, guard=None):
        self.pending = queue.Queue()
        self.ready = queue.Queue()
        self.save, self.create_event, self.guard = save, create_event, guard

    def run(self):
        while True:
            incident = self.pending.get()
            record(incident, self.save, self.create_event, self.guard)
            self.ready.put(incident)


def event_creator(config):
    base = config["frigate_url"].rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise ValueError("Frigate must use verified HTTPS")
    passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    passwords.add_password(None, base, config["api_user"], config["api_password"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPBasicAuthHandler(passwords))

    def create(incident):
        label = "door_open_test" if incident.get("test") else "door_open"
        camera = urllib.parse.quote(config["camera"], safe="")
        if time.time() - incident["timestamp"] > 5:
            # Frigate 0.18 timestamps manual events at POST receipt. Recovered
            # incidents must link their original archive range, never a new clip.
            start, end = int(incident["timestamp"] - 10), int(incident["timestamp"] + 30)
            return {"success": True, "recording_url": base + f"/api/{camera}/start/{start}/end/{end}/clip.mp4"}
        body = {"sub_label": ("Тест · " if incident.get("test") else "Открытие · ") + config["label"],
                "duration": 30, "include_recording": True, "pre_capture": 10}
        if config.get("recorder_ssh"):
            code = ("import json,socket,sys,urllib.request; p=json.load(sys.stdin); "
                    "assert socket.gethostname()==p['hostname']; "
                    "r=urllib.request.Request('http://127.0.0.1:15000/api/events/'+p['camera']+'/'+p['label']+'/create',"
                    "data=json.dumps(p['body']).encode(),headers={'Content-Type':'application/json'}); "
                    "print(urllib.request.urlopen(r,timeout=10).read().decode())")
            payload = {"hostname": config["recorder_hostname"], "camera": camera, "label": label, "body": body}
            result = subprocess.run([config["ssh"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                     config["recorder_ssh"], "python3 -c " + "'" + code.replace("'", "'\"'\"'") + "'"],
                                    input=json.dumps(payload), text=True, capture_output=True, timeout=20)
            if result.returncode:
                raise RuntimeError("Recorder SSH request failed")
            return json.loads(result.stdout)
        request = urllib.request.Request(base + f"/api/events/{camera}/{label}/create",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=20) as response:
            return json.load(response)
    return create


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    directory = args.directory
    config = json.loads((directory / "runtime.local.json").read_text(encoding="utf-8-sig"))
    path = directory / "state.json"
    with exclusive_worker(directory):
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"incidents": []}
        lock = threading.RLock()
        create = event_creator(config)
        recordings = RecordingQueue(save=lambda: None, create_event=create, guard=lock)
        pending = recordings.pending
        gate = Transitions()
        gate.previous = state.get("door_state") if state.get("door_state") in {"open", "closed"} else None

        def save():
            with lock:
                atomic_json(path, state)
        recordings.save = save

        def enqueue(test=False):
            incident = {"id": str(uuid.uuid4()), "timestamp": time.time(), "test": test}
            state["incidents"].append(incident)
            save()
            pending.put(incident)

        def send(text):
            message_path = directory / "message.txt"
            message_path.write_text(text, encoding="utf-8")
            try:
                result = subprocess.run([config["python"], "-X", "utf8", config["helper"],
                                         "--agent", "codex", "--chat", str(config["owner_chat"]),
                                         "--file", str(message_path)],
                                        capture_output=True, text=True, encoding="utf-8", timeout=150)
                if result.returncode:
                    raise RuntimeError("Telegram helper failed")
                return json.loads(result.stdout)
            finally:
                message_path.unlink(missing_ok=True)

        if args.test:
            enqueue(test=True)
            process(pending.get_nowait(), config, save, create, send, lock)
            print(json.dumps(state["incidents"][-1], ensure_ascii=False))
            return

        for incident in state["incidents"]:
            if "delivery_status" not in incident:
                pending.put(incident)
        threading.Thread(target=recordings.run, daemon=True).start()

        import websocket
        topic = config["camera"] + "/classification/" + config["model"]

        def connected(client):
            with lock:
                state["stream_connected"] = True
                save()

        def disconnected(client, code, reason):
            with lock:
                state["stream_connected"] = False
                save()

        def received(client, raw):
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict) or packet.get("topic") != topic:
                    return
                value = packet.get("payload")
            except (ValueError, UnicodeError):
                return
            if not isinstance(value, str):
                return
            with lock:
                if gate.observe(value):
                    enqueue()
                if value in {"open", "closed"}:
                    state.update(door_state=value, observed_at=time.time())
                    save()

        auth = base64.b64encode((config["api_user"] + ":" + config["api_password"]).encode()).decode()
        client = websocket.WebSocketApp(
            "wss://" + config["frigate_url"].split("://", 1)[1].rstrip("/") + "/ws",
            header=["Authorization: Basic " + auth],
            on_open=connected, on_close=disconnected, on_message=received)

        def listen():
            while True:
                try:
                    client.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    with lock:
                        state["stream_error"] = type(exc).__name__
                disconnected(client, None, None)
                time.sleep(5)

        threading.Thread(target=listen, daemon=True).start()
        try:
            while True:
                try:
                    incident = recordings.ready.get(timeout=2)
                except queue.Empty:
                    incident = None
                # Telegram may take longer than the stream keepalive. Keep its
                # network callback free to journal further openings meanwhile.
                if incident:
                    deliver(incident, config, save, send, lock)
                with lock:
                    state.update(version=VERSION, heartbeat=time.time())
                    # Keep unfinished attempts and a bounded notification history.
                    finished = [r for r in state["incidents"] if "delivery_status" in r][-1000:]
                    state["incidents"] = [r for r in state["incidents"] if "delivery_status" not in r] + finished
                    save()
        finally:
            client.close()


if __name__ == "__main__":
    main()
