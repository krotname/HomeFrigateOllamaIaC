"""A camera-bound protected-object movement alarm; not an accident classifier.

Reference images and the owner-approved profile live outside Git. A possible
impact needs two coherent object landmarks, a stable background, an abrupt
movement observed twice, and a recent nearby vehicle track. Recording gaps,
occlusion alone and initial acquisition never constitute impact evidence.
"""

import argparse
import json
import math
import os
import signal
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

from ocr.frigate_caption_worker import exclusive_worker, request_bytes


VERSION = "1.0.0"


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class Observation:
    timestamp: float
    position: tuple = ()
    confidence: float = 0
    valid: bool = False


class Movement:
    """Confirm movement twice, retaining a known baseline across brief occlusions."""

    def __init__(self, threshold=5, cooldown=300):
        self.threshold = threshold
        self.cooldown = cooldown
        self.baseline = None
        self.previous_time = None
        self.last_valid_time = None
        self.acquired = 0
        self.pending = None
        self.last_alarm = -math.inf

    def observe(self, observation, nearby):
        ts = observation.timestamp
        gap = self.previous_time is None or not 0 < ts - self.previous_time <= 10
        self.previous_time = ts
        if gap or not observation.valid:
            retained = (self.baseline is not None and self.acquired >= 3
                        and self.last_valid_time is not None
                        and 0 < ts - self.last_valid_time <= 30)
            if not retained:
                self.baseline = None
                self.acquired = 0
            self.pending = None
        if not observation.valid:
            return "unknown"
        self.last_valid_time = ts
        pos = observation.position
        if self.baseline is None:
            self.baseline = pos
        if self.acquired < 3:
            if math.dist(self.baseline, pos) > 2:
                self.baseline, self.acquired = pos, 0
            self.acquired += 1
            return "acquiring" if self.acquired < 3 else "present"
        distance = math.dist(pos, self.baseline)
        if distance < self.threshold:
            self.pending = None
            return "present"
        if self.pending is None:
            self.pending = (ts, pos, nearby)
            return "movement_pending"
        first_ts, first_pos, had_vehicle = self.pending
        if ts - first_ts > 10 or math.dist(first_pos, pos) > 2:
            self.pending = (ts, pos, nearby)
            return "movement_pending"
        self.baseline = pos
        self.pending = None
        if (had_vehicle or nearby) and ts - self.last_alarm >= self.cooldown:
            self.last_alarm = ts
            return "possible_impact"
        return "moved"


def vehicle_near(events, timestamp, area):
    x1, y1, x2, y2 = area
    for event in events:
        if event.get("label") not in {"car", "truck", "bus", "motorcycle", "bicycle"}:
            continue
        for point, point_time in event.get("data", {}).get("path_data", []):
            if (timestamp - 20 <= point_time <= timestamp + 4
                    and len(point) == 2 and x1 <= point[0] <= x2
                    and y1 <= point[1] <= y2):
                return True
    return False


class Matcher:
    def __init__(self, profile, directory):
        import cv2
        import numpy as np

        self.cv2, self.np = cv2, np
        self.profile = profile
        self.references = []
        for name in profile["references"]:
            if Path(name).name != name:
                raise ValueError("Reference must be a file in the profile directory")
            raw = (directory / name).read_bytes()
            self.references.append(self.decode(raw))

    def decode(self, raw):
        image = self.cv2.imdecode(self.np.frombuffer(raw, self.np.uint8), 0)
        if image is None or min(image.shape) < 200:
            raise ValueError("Invalid camera image")
        return self.cv2.resize(image, (960, 540))

    def edges(self, image):
        return self.cv2.Canny(self.cv2.GaussianBlur(image, (3, 3), 0), 40, 100)

    def offsets(self, reference, image, rectangles):
        found = []
        for x1, y1, x2, y2 in rectangles:
            margin = self.profile.get("search_pixels", 25)
            left, top = max(0, x1 - margin), max(0, y1 - margin)
            right, bottom = min(960, x2 + margin), min(540, y2 + margin)
            template = self.edges(reference[y1:y2, x1:x2])
            if self.np.count_nonzero(template) < 25:
                continue
            search = self.edges(image[top:bottom, left:right])
            result = self.cv2.matchTemplate(search, template, self.cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = self.cv2.minMaxLoc(result)
            dx, dy = loc[0] + left - x1, loc[1] + top - y1
            if (math.isfinite(score) and score >= self.profile["minimum_match"]
                    and abs(dx) < margin - 2 and abs(dy) < margin - 2):
                found.append((dx, dy, score))
        coherent = []
        for seed in found:
            group = [p for p in found if math.dist(p[:2], seed[:2]) <= 2]
            if len(group) > len(coherent):
                coherent = group
        if len(coherent) < 2:
            return None
        return (float(self.np.median([p[0] for p in coherent])),
                float(self.np.median([p[1] for p in coherent])),
                sum(p[2] for p in coherent) / len(coherent))

    def observe(self, raw, timestamp):
        image = self.decode(raw)
        candidates = []
        for reference in self.references:
            background = self.offsets(reference, image, self.profile["background"])
            target = self.offsets(reference, image, self.profile["landmarks"])
            if background is None or target is None or math.hypot(*background[:2]) > 2:
                continue
            candidates.append((target[2], (target[0] - background[0], target[1] - background[1])))
        if not candidates:
            return Observation(timestamp)
        score, position = max(candidates)
        return Observation(timestamp, position, score, True)


class Api:
    base = "http://127.0.0.1:15000/api"

    def data(self, path, payload=None, *, bypass_cache=False):
        body = None if payload is None else json.dumps(payload).encode()
        raw = request_bytes(self.base + path, method="GET" if body is None else "POST",
                            body=body, timeout=15, limit=4 * 1024 * 1024,
                            bypass_cache=bypass_cache)
        return raw

    def json(self, path, payload=None, *, bypass_cache=False):
        return json.loads(self.data(path, payload, bypass_cache=bypass_cache))

    def candidates(self, camera, timestamp):
        filters = {"cameras": camera, "limit": 100, "include_thumbnails": 0}
        recent = self.json("/events?" + urllib.parse.urlencode(
            {**filters, "after": timestamp - 120}))
        # Event start_time does not bound the age of its latest trajectory points.
        active = self.json("/events?" + urllib.parse.urlencode({**filters, "in_progress": 1}))
        return list({event["id"]: event for event in recent + active}.values())

    def frame(self, camera, timestamp):
        query = urllib.parse.urlencode({"after": timestamp - 2, "before": timestamp + 2})
        def coverage():
            segments = self.json(f"/{camera}/recordings?{query}", bypass_cache=True)
            return {row["id"] for row in segments
                    if row["start_time"] <= timestamp <= row["end_time"]}
        before = coverage()
        if not before:
            raise ValueError("No recording at exact timestamp")
        raw = self.data(f"/{camera}/recordings/{timestamp}/snapshot.jpg?height=1080")
        if not before.intersection(coverage()):
            raise ValueError("Recording disappeared during extraction")
        return raw


def publish(api, profile, directory, state, timestamp, confidence, test=False):
    key = str(uuid.uuid4())
    incident = {"id": key, "timestamp": timestamp, "test": test,
                "label": profile["label"], "camera": profile["camera"],
                "frigate_status": "started", "event_id": None}
    state["incidents"].append(incident)
    atomic_json(directory / "state.json", state)
    try:
        result = api.json(f'/events/{profile["camera"]}/'
                          + ("protected_object_test" if test else "possible_trailer_impact") + '/create',
                          {"sub_label": ("Тест · " if test else "Возможный удар · ") + profile["label"],
                           "score": round(confidence, 3), "duration": 1 if test else 30,
                           "include_recording": not test, "pre_capture": 60})
        if not result.get("success") or not result.get("event_id"):
            raise ValueError("Frigate did not confirm event creation")
        incident.update(event_id=result["event_id"], frigate_status="created")
    except Exception:
        # A timed-out POST is ambiguous. Persist it and never blindly repeat it.
        incident["frigate_status"] = "unknown"
    atomic_json(directory / "state.json", state)
    return incident


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/state"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-alert", action="store_true")
    options = parser.parse_args()
    directory = options.directory
    profile = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    if (not profile["camera"].replace("_", "").isalnum()
            or not 1 <= len(profile["label"]) <= 65):
        raise ValueError("Invalid camera or label")
    api, matcher = Api(), Matcher(profile, directory)
    movement = Movement(profile.get("movement_pixels", 5))
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"incidents": []}
    state.update(version=VERSION, label=profile["label"], camera=profile["camera"])
    alarms = [row["timestamp"] for row in state["incidents"] if not row.get("test")]
    movement.last_alarm = max(alarms, default=-math.inf)
    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    with exclusive_worker(directory):
        if options.test_alert:
            print(json.dumps(publish(api, profile, directory, state, time.time(), 1, True)))
            return
        while not stop:
            timestamp = math.floor(time.time() - 15)
            try:
                stats = api.json("/stats")["cameras"][profile["camera"]]
                if stats.get("camera_fps", 0) <= 0:
                    raise ValueError("Camera has no live frames")
                raw = api.frame(profile["camera"], timestamp)
                observation = matcher.observe(raw, timestamp)
                nearby = vehicle_near(api.candidates(profile["camera"], timestamp),
                                      timestamp, profile["vehicle_area"])
                status = movement.observe(observation, nearby)
                state.update(observed_at=timestamp, status=status, position=observation.position,
                             confidence=round(observation.confidence, 4), nearby_vehicle=nearby,
                             last_success=time.time())
                if observation.valid:
                    state["last_recognized"] = time.time()
                state.pop("error", None)
                if status == "possible_impact" and profile.get("alerts_enabled", False):
                    publish(api, profile, directory, state, timestamp, observation.confidence)
            except Exception as exc:
                movement.observe(Observation(timestamp), False)
                state.update(status="unknown", error=type(exc).__name__, observed_at=timestamp)
            state["heartbeat"] = time.time()
            state["incidents"] = state["incidents"][-100:]
            atomic_json(state_path, state)
            if options.once:
                print(json.dumps(state, ensure_ascii=False))
                return
            for _ in range(40):
                if stop:
                    break
                time.sleep(0.1)


if __name__ == "__main__":
    main()
