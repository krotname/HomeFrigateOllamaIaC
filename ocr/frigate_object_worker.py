"""GPU-only reference matching for cats and cars in Frigate events.

Private profiles/vectors stay in STATE_DIR. No person embeddings, plate OCR,
continuous video inference, external inference service, or notification channel.
"""

import argparse
import gc
import io
import json
import logging
import math
import os
import re
import signal
import sqlite3
import subprocess
import time
import urllib.parse
from pathlib import Path

from ocr.frigate_caption_worker import request_bytes


LOG = logging.getLogger("frigate-objects")
KINDS = {"cat", "car"}
CAMERAS = {"cam1_ds_i202", "cam2_ds_i551", "cam3_ds_i200", "common_camera01",
           "common_camera02", "common_camera03"}
MODEL_REVISION = "mobilenetv2-12-gap-v1"
MAX_REFERENCES = 500
MAX_EVENTS = 512
EVENT_TTL = 3 * 86400
POLL_SECONDS = 15
MIN_INTERVAL = 0.5
MODEL_BUDGET_MIB = 1536
HEADROOM_MIB = 2048


def unit(vector):
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    if not values or not math.isfinite(norm) or norm < 1e-9:
        raise ValueError("Invalid feature vector")
    return [x / norm for x in values]


def similarity(left, right):
    if len(left) != len(right):
        raise ValueError("Feature dimensions differ")
    return sum(x * y for x, y in zip(left, right))


def reference_score(vector, references):
    scores = sorted((similarity(vector, x) for x in references), reverse=True)
    return sum(scores[:2]) / min(2, len(scores)) if scores else -1.0


def calibration(positive_train, positive_test, negative_test):
    """Threshold selection on separate events; these are small-sample checks."""
    counts = {"train": len(positive_train), "positive_test": len(positive_test),
              "negative_test": len(negative_test)}
    if counts["train"] < 3 or counts["positive_test"] < 2 or counts["negative_test"] < 3:
        return {"enabled": False, "reason": "insufficient_separate_examples", **counts}
    low_positive = min(reference_score(x, positive_train) for x in positive_test)
    high_negative = max(reference_score(x, positive_train) for x in negative_test)
    threshold = max(0.85, high_negative + 0.04)
    enabled = threshold <= min(0.995, low_positive)
    return {"enabled": enabled, "reason": "validated" if enabled else "overlapping_examples",
            "threshold": round(threshold, 6), "margin": 0.06,
            "lowest_positive": low_positive, "highest_negative": high_negative, **counts}


def choose(vector, profiles, references, kind):
    ranked = []
    for p in profiles:
        if p["kind"] != kind:
            continue
        train = [r["vector"] for r in references
                 if r["profile_id"] == p["id"] and r["role"] == "train"]
        ranked.append((reference_score(vector, train), p))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked:
        return None
    score, profile = ranked[0]
    policy = profile["calibration"]
    runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
    unknowns = [r["vector"] for r in references
                if r["kind"] == kind and not r["profile_id"]]
    if unknowns:
        runner_up = max(runner_up, reference_score(vector, unknowns))
    if (not policy.get("enabled") or score < policy["threshold"]
            or score - runner_up < policy["margin"]):
        return None
    return {"id": profile["id"], "label": profile["label"], "similarity": score}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "objects.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
                calibration TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS refs (
                source TEXT PRIMARY KEY, profile_id TEXT, kind TEXT NOT NULL,
                role TEXT NOT NULL, vector TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, updated REAL NOT NULL, frame TEXT NOT NULL,
                votes TEXT NOT NULL, status TEXT NOT NULL);
        """)
        self.db.commit()

    def profiles(self):
        return [{**dict(x), "calibration": json.loads(x["calibration"])}
                for x in self.db.execute("SELECT * FROM profiles ORDER BY id")]

    def refs(self):
        return [{**dict(x), "vector": json.loads(x["vector"])} for x in self.db.execute(
            "SELECT * FROM refs WHERE model=?", (MODEL_REVISION,))]

    def profile(self, identity, kind, label):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity) or kind not in KINDS
                or not 1 <= len(label.strip()) <= 80 or any(ord(x) < 32 for x in label)):
            raise ValueError("Invalid cat/car profile")
        existing = self.db.execute("SELECT * FROM profiles WHERE id=?", (identity,)).fetchone()
        if existing and existing["kind"] != kind:
            raise ValueError("Cannot change profile object kind")
        self.db.execute("INSERT INTO profiles(id,kind,label) VALUES(?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET label=excluded.label", (identity, kind, label))
        self.db.commit()

    def add_reference(self, source, profile_id, kind, role, vector):
        if kind not in KINDS or role not in {"train", "validation"}:
            raise ValueError("Only cat/car train/validation references are accepted")
        if self.db.execute("SELECT 1 FROM refs WHERE source=?", (source,)).fetchone():
            raise ValueError("Source already enrolled; train/test reuse is forbidden")
        if self.db.execute("SELECT count(*) FROM refs").fetchone()[0] >= MAX_REFERENCES:
            raise ValueError("Reference gallery is full")
        if profile_id and not self.db.execute(
                "SELECT 1 FROM profiles WHERE id=? AND kind=?", (profile_id, kind)).fetchone():
            raise ValueError("Unknown profile or wrong object kind")
        self.db.execute("INSERT INTO refs VALUES(?,?,?,?,?,?)",
                        (source, profile_id, kind, role, json.dumps(unit(vector)), MODEL_REVISION))
        # Changing any gallery entry invalidates thresholds of the same object kind.
        self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (kind,))
        self.db.commit()

    def calibrate(self, identity):
        profiles, refs = self.profiles(), self.refs()
        profile = next(p for p in profiles if p["id"] == identity)
        train = [r["vector"] for r in refs if r["profile_id"] == identity and r["role"] == "train"]
        positive = [r["vector"] for r in refs if r["profile_id"] == identity and r["role"] == "validation"]
        negative = [r["vector"] for r in refs if r["profile_id"] != identity
                    and r["kind"] == profile["kind"] and r["role"] == "validation"]
        policy = calibration(train, positive, negative)
        self.db.execute("UPDATE profiles SET calibration=? WHERE id=?", (json.dumps(policy), identity))
        self.db.commit()
        return policy

    def remember(self, event_id, frame, votes, status):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "updated=excluded.updated,frame=excluded.frame,votes=excluded.votes,status=excluded.status",
                        (event_id, time.time(), frame, json.dumps(votes), status))
        self.db.execute("DELETE FROM events WHERE updated<?", (time.time() - EVENT_TTL,))
        self.db.execute("DELETE FROM events WHERE id IN (SELECT id FROM events "
                        "ORDER BY updated DESC LIMIT -1 OFFSET ?)", (MAX_EVENTS,))
        self.db.commit()

    def health(self, **value):
        value.update(updated=time.time(), model=MODEL_REVISION,
                     events=dict(self.db.execute("SELECT status,count(*) FROM events GROUP BY status")))
        pending = self.directory / "health.tmp"
        pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        pending.replace(self.directory / "health.json")


class Frigate:
    base = "http://127.0.0.1:15000/api"

    def json(self, path, body=None):
        return json.loads(request_bytes(self.base + path, method="GET" if body is None else "POST",
                                       body=None if body is None else json.dumps(body).encode(),
                                       limit=2 * 1024 * 1024, timeout=8, bypass_cache=True))

    def event(self, event_id):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", event_id):
            raise ValueError("Invalid event ID")
        return self.json("/events/" + event_id)

    def candidates(self):
        filters = {"labels": ",".join(sorted(KINDS)), "cameras": ",".join(sorted(CAMERAS)),
                   "limit": 100, "has_snapshot": 1, "include_thumbnails": 0}
        recent = self.json("/events?" + urllib.parse.urlencode(
            {**filters, "after": int(time.time() - 300)}))
        # A parked object's event can remain active for hours; start_time is not recency.
        active = self.json("/events?" + urllib.parse.urlencode({**filters, "in_progress": 1}))
        merged = {event["id"]: event for event in recent + active}
        return sorted(merged.values(), key=lambda event: event["start_time"], reverse=True)

    def snapshot(self, event):
        from PIL import Image

        if event["label"] not in KINDS or event["camera"] not in CAMERAS:
            raise ValueError("Only cats/cars on the configured property cameras")
        expected = float(event.get("data", {}).get("snapshot_frame_time", 0))
        box = event.get("data", {}).get("box")
        if not expected or not isinstance(box, list) or len(box) != 4:
            raise ValueError("No verified snapshot frame/box")
        if (any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                or min(box) < 0 or max(box) > 1 or min(box[2:]) <= 0):
            raise ValueError("Invalid relative object box")
        data, headers = request_bytes(
            self.base + "/events/" + urllib.parse.quote(event["id"], safe="")
            + "/snapshot.jpg?crop=0&bbox=0&timestamp=0&height=512&quality=90",
            limit=4 * 1024 * 1024, timeout=8, return_headers=True, bypass_cache=True)
        if abs(float(headers.get("x-frame-time", 0)) - expected) > 0.001:
            raise ValueError("Snapshot advanced; wait for matching event metadata")
        Image.MAX_IMAGE_PIXELS = 8_000_000
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Snapshot exceeds pixel limit")
            x, y, width, height = box
            margin = .04 * max(width, height)
            crop = image.crop((max(0, int((x - margin) * image.width)),
                               max(0, int((y - margin) * image.height)),
                               min(image.width, math.ceil((x + width + margin) * image.width)),
                               min(image.height, math.ceil((y + height + margin) * image.height))))
            if min(crop.size) < 40:
                raise ValueError("Object box is too small")
            output = io.BytesIO()
            crop.convert("RGB").save(output, format="JPEG", quality=90)
            return output.getvalue()

    def apply(self, event_id, label, *, manual=False):
        current = self.event(event_id)
        allowed = KINDS | ({"person"} if manual else set())
        if current["label"] not in allowed or current["camera"] not in CAMERAS:
            raise ValueError("Event kind/camera outside allowed scope")
        if not 1 <= len(label.strip()) <= 80 or any(ord(x) < 32 for x in label):
            raise ValueError("Invalid label")
        existing = current.get("sub_label")
        if isinstance(existing, list):
            existing = existing[0] if existing else None
        if existing and existing != label:
            return False  # Preserve native and human labels.
        if not existing:
            self.json("/events/" + event_id + "/sub_label", {"subLabel": label})
        # Native API publishes an asynchronous metadata update; poll GET, never POST.
        for delay in (0, 0.1, 0.2, 0.4, 0.8, 1.6):
            if delay:
                time.sleep(delay)
            verified = self.event(event_id).get("sub_label")
            if isinstance(verified, list):
                verified = verified[0] if verified else None
            if verified == label:
                return True
        raise RuntimeError("Frigate label readback failed; do not retry blindly")


def gpu_stats(uuid):
    result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,memory.free,utilization.gpu",
                             "--format=csv,noheader,nounits"], check=True, capture_output=True,
                            text=True, timeout=5)
    rows = [x.split(",") for x in result.stdout.strip().splitlines()]
    matching = [x for x in rows if x[0].strip() == uuid]
    if len(matching) != 1:
        raise RuntimeError("Exact GPU UUID not found")
    return {"free_mib": int(matching[0][1]), "util_percent": int(matching[0][2])}


def gpu_available(stats, loaded=False):
    return stats["free_mib"] >= HEADROOM_MIB + (0 if loaded else MODEL_BUDGET_MIB) and stats["util_percent"] < 25


class Encoder:
    def __init__(self, model, gpu_uuid, audit=False):
        self.model, self.gpu_uuid = model, gpu_uuid
        self.audit = audit
        self.session = None
        self.last_run = 0.0
        self.last_used = 0.0
        self.load_mib = 0
        self.baseline_free = None
        self.inference_ms = 0.0

    def unload(self):
        self.session = None
        gc.collect()

    def encode(self, data):
        import numpy as np
        import onnxruntime as ort
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 8_000_000
        before = gpu_stats(self.gpu_uuid)
        if not gpu_available(before, self.session is not None):
            self.unload()
            raise RuntimeError("GPU priority/headroom guard")
        if self.session is None:
            self.baseline_free = before["free_mib"]
            ort.preload_dlls()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
            options.log_severity_level = 3
            if self.audit:
                options.enable_profiling = True
                options.profile_file_prefix = "/tmp/frigate-objects-ort"
            self.session = ort.InferenceSession(str(self.model), sess_options=options,
                providers=[("CUDAExecutionProvider", {"device_id": 0,
                    "gpu_mem_limit": MODEL_BUDGET_MIB * 1024 * 1024,
                    "arena_extend_strategy": "kSameAsRequested", "cudnn_conv_algo_search": "HEURISTIC",
                    "cudnn_conv_use_max_workspace": "0"})])
            self.session.disable_fallback()
            # ORT can register CPU EP even when assigning nodes to it is disabled.
            if "CUDAExecutionProvider" not in self.session.get_providers():
                self.unload()
                raise RuntimeError("CUDA-only execution was not established")
            self.load_mib = max(0, before["free_mib"] - gpu_stats(self.gpu_uuid)["free_mib"])
            if self.load_mib > MODEL_BUDGET_MIB:
                self.unload()
                raise RuntimeError("GPU model memory budget exceeded")
        with Image.open(io.BytesIO(data)) as image:
            if min(image.size) < 40 or image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Object crop dimensions outside limits")
            image = image.convert("RGB")
            image.thumbnail((224, 224), Image.Resampling.BILINEAR)
            canvas = Image.new("RGB", (224, 224), (124, 116, 104))
            canvas.paste(image, ((224 - image.width) // 2, (224 - image.height) // 2))
            array = np.asarray(canvas, dtype=np.float32) / 255.0
        array = (array - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
        tensor = np.ascontiguousarray(array.transpose(2, 0, 1)[None])
        time.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - self.last_run)))
        started = time.monotonic()
        output = self.session.run(None, {self.session.get_inputs()[0].name: tensor})[0]
        self.inference_ms = (time.monotonic() - started) * 1000
        self.last_run = self.last_used = time.monotonic()
        after = gpu_stats(self.gpu_uuid)
        self.load_mib = max(self.load_mib, self.baseline_free - after["free_mib"])
        if not gpu_available(after, True) or self.load_mib > MODEL_BUDGET_MIB:
            self.unload()
            raise RuntimeError("GPU headroom lost during inference")
        return unit(output.reshape(-1).tolist())

    def audit_providers(self):
        if not self.audit or self.session is None:
            raise RuntimeError("No active execution profile")
        path = Path(self.session.end_profiling())
        try:
            events = json.loads(path.read_text())
            providers = sorted({x.get("args", {}).get("provider") for x in events
                                if x.get("cat") == "Node" and x.get("args", {}).get("provider")})
            if providers != ["CUDAExecutionProvider"]:
                raise RuntimeError("Non-CUDA kernel execution detected")
            return providers
        finally:
            path.unlink(missing_ok=True)


def run(store, encoder, api, once=False):
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            profiles, refs = store.profiles(), store.refs()
            active = [p for p in profiles if p["calibration"].get("enabled")]
            if not active:
                encoder.unload()
                store.health(state="waiting_for_references", gpu_loaded=False)
            else:
                for event in reversed(api.candidates()):
                    if stopping:
                        break
                    if (event.get("label") not in KINDS or event.get("camera") not in CAMERAS
                            or not any(p["kind"] == event["label"] for p in active)):
                        continue
                    frame = str(event.get("data", {}).get("snapshot_frame_time", ""))
                    previous = store.db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()
                    if previous and previous["status"] == "pending":
                        # A timeout may follow an accepted POST. Only observe its result.
                        if event.get("sub_label"):
                            store.remember(event["id"], frame, [], "protected")
                        continue
                    if previous and (previous["status"] in {"applied", "protected", "complete_unknown"}
                                     or previous["frame"] == frame):
                        continue
                    if event.get("sub_label"):
                        store.remember(event["id"], frame, [], "protected")
                        continue
                    try:
                        image = api.snapshot(event)
                    except ValueError:
                        continue  # Unaligned/low-quality samples remain unknown.
                    vector = encoder.encode(image)
                    match = choose(vector, profiles, refs, event["label"])
                    votes = (json.loads(previous["votes"]) if previous else [])[-2:]
                    votes.append(match["id"] if match else None)
                    # Require two distinct best-frame timestamps, never repeated polls of one JPG.
                    accepted = match and bool(frame) and votes.count(match["id"]) >= 2
                    if accepted:
                        store.remember(event["id"], frame, votes, "pending")
                        success = api.apply(event["id"], match["label"])
                        status = "applied" if success else "protected"
                    else:
                        status = "complete_unknown" if event.get("end_time") else "observing"
                    store.remember(event["id"], frame, votes, status)
                store.health(state="watching", gpu_loaded=encoder.session is not None,
                             gpu_uuid=encoder.gpu_uuid, load_mib=encoder.load_mib,
                             last_inference_ms=encoder.inference_ms)
        except Exception as exc:
            encoder.unload()
            LOG.warning("Paused: %s", type(exc).__name__)
            store.health(state="paused", reason=str(exc)[:200], gpu_loaded=False)
        if once:
            break
        if encoder.session and time.monotonic() - encoder.last_used > 60:
            encoder.unload()
        for _ in range(POLL_SECONDS):
            if stopping:
                break
            time.sleep(1)
    encoder.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=os.getenv("FRIGATE_OBJECT_STATE", "/state"))
    parser.add_argument("--model", default=os.getenv("FRIGATE_OBJECT_MODEL", "/models/mobilenetv2-gap.onnx"))
    parser.add_argument("--gpu", default=os.getenv("FRIGATE_OBJECT_GPU", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    worker = commands.add_parser("run")
    worker.add_argument("--once", action="store_true")
    profile = commands.add_parser("profile")
    profile.add_argument("--id", required=True)
    profile.add_argument("--kind", choices=sorted(KINDS), required=True)
    profile.add_argument("--label", required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--event", required=True)
    enroll.add_argument("--profile")
    enroll.add_argument("--role", choices=["train", "validation"], required=True)
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--profile", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--event", required=True)
    manual = commands.add_parser("manual-label")
    manual.add_argument("--event", required=True)
    manual.add_argument("--label", required=True)
    args = parser.parse_args()
    store, api = Store(args.state), Frigate()
    encoder = Encoder(args.model, args.gpu, audit=args.command == "probe")
    try:
        if args.command == "status":
            health = store.directory / "health.json"
            result = {"profiles": store.profiles(), "references": len(store.refs()),
                      "health": json.loads(health.read_text()) if health.exists() else None}
        elif args.command == "profile":
            store.profile(args.id, args.kind, args.label)
            result = {"profiles": store.profiles()}
        elif args.command == "calibrate":
            result = store.calibrate(args.profile)
        elif args.command == "manual-label":
            result = {"verified": api.apply(args.event, args.label, manual=True), "event": args.event}
        elif args.command in {"enroll", "probe"}:
            event = api.event(args.event)
            image = api.snapshot(event)
            vector = encoder.encode(image)
            if args.command == "enroll":
                store.add_reference(args.event, args.profile, event["label"], args.role, vector)
            result = {"event": args.event, "dimensions": len(vector), "provider": "CUDAExecutionProvider",
                      "load_mib": encoder.load_mib, "inference_ms": encoder.inference_ms,
                      "match": choose(vector, store.profiles(), store.refs(), event["label"])}
            if args.command == "probe":
                warm = []
                for _ in range(3):
                    encoder.encode(image)
                    warm.append(encoder.inference_ms)
                result.update(executed_providers=encoder.audit_providers(), warm_inference_ms=warm)
        else:
            run(store, encoder, api, args.once)
            result = {"stopped": True}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        encoder.unload()
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
