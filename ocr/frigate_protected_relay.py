"""Deliver protected-object incidents on a Windows host with its own bot token.

The existing tg_bot helper checks bot identity and reads back the owner's chat.
No bot token is copied to the recorder. Ambiguous sends are never retried.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path

from ocr.frigate_caption_worker import exclusive_worker
from ocr.frigate_protected import atomic_json


def render(incident):
    stamp = datetime.datetime.fromtimestamp(
        incident["timestamp"], datetime.timezone(datetime.timedelta(hours=3)))
    title = ("Тест оповещения: прицеп под наблюдением. ДТП не обнаружено."
             if incident.get("test") else "Возможный удар по моему прицепу. Проверьте запись.")
    lines = [title, str(incident["label"]), stamp.strftime("%d.%m.%Y %H:%M:%S МСК")]
    event = incident.get("event_id")
    if event and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", event):
        lines.append("https://frigate.adler-black-u2.lan/review")
        if not incident.get("test"):
            lines.append(f"https://frigate.adler-black-u2.lan/api/events/{event}/clip.mp4")
    else:
        lines.append("Событие Frigate не подтверждено. Камера: common_camera04.")
    return "\n".join(lines)


def deliver(key, text, journal, path, sender):
    if key in journal["sent"]:
        return
    journal["sent"][key] = {"status": "started", "at": time.time()}
    atomic_json(path, journal)
    try:
        result = sender(text)
        if not result.get("ok") or not result.get("verified"):
            raise RuntimeError("Telegram readback did not confirm delivery")
        journal["sent"][key].update(status="verified", result=result)
    except Exception as exc:
        journal["sent"][key].update(status="unknown", error=type(exc).__name__)
    atomic_json(path, journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    directory = args.directory
    config = json.loads((directory / "relay.json").read_text(encoding="utf-8"))
    path = directory / "delivery.json"
    with exclusive_worker(directory):
        journal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sent": {}}

        def sender(text):
            message = directory / "message.txt"
            message.write_text(text, encoding="utf-8")
            try:
                result = subprocess.run(
                    [config["python"], "-X", "utf8", config["helper"], "--agent", "codex",
                     "--chat", str(config["owner_chat"]), "--file", str(message)],
                    capture_output=True, text=True, encoding="utf-8", timeout=150,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                # Helper output is metadata, never token or runtime credentials.
                value = json.loads(result.stdout)
                if result.returncode:
                    raise RuntimeError("Telegram helper failed")
                return value
            finally:
                message.unlink(missing_ok=True)

        try:
            result = subprocess.run(
                [config["ssh"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 "krt@adler-black-u2.lan", "sudo -n cat /var/lib/frigate-protected/state.json"],
                capture_output=True, timeout=40)
            if result.returncode or len(result.stdout) > 256 * 1024:
                raise RuntimeError("Recorder state unavailable")
            state = json.loads(result.stdout)
            if state.get("camera") != "common_camera04":
                raise ValueError("Unexpected camera")
            for incident in state.get("incidents", []):
                if incident.get("camera") != "common_camera04":
                    raise ValueError("Unexpected incident camera")
                # Ignore historical events predating this installation.
                if incident["timestamp"] >= config["enabled_at"]:
                    deliver(incident["id"], render(incident), journal, path, sender)
            healthy = (time.time() - state.get("heartbeat", 0) < 180
                       and time.time() - state.get("last_recognized", 0) < 180)
            journal["last_state"] = {k: state.get(k) for k in
                                     ("status", "heartbeat", "last_recognized", "confidence")}
            journal.pop("error", None)
        except Exception as exc:
            healthy = False
            journal["error"] = type(exc).__name__
        if not healthy and not journal.get("outage"):
            journal["outage"] = str(int(time.time()))
            atomic_json(path, journal)
            deliver("offline-" + journal["outage"],
                    "Наблюдение за прицепом временно недоступно: нет свежего распознавания "
                    "с камеры 04. Автоматическая тревога об ударе сейчас не гарантируется.",
                    journal, path, sender)
        elif healthy and journal.get("outage"):
            deliver("restored-" + journal["outage"],
                    "Наблюдение за прицепом на камере 04 восстановлено.", journal, path, sender)
            journal.pop("outage", None)
        journal["heartbeat"] = time.time()
        if len(journal["sent"]) > 1000:
            recent = sorted(journal["sent"], key=lambda key: journal["sent"][key]["at"])[-1000:]
            journal["sent"] = {key: journal["sent"][key] for key in recent}
        atomic_json(path, journal)
        print(json.dumps({"healthy": healthy, "deliveries": {
            key: value["status"] for key, value in journal["sent"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
