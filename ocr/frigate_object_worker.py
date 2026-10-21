"""GPU-only reference matching for cats, cars and two-wheelers in Frigate events.

Private profiles/vectors stay in STATE_DIR. No person embeddings, plate OCR,
continuous video inference, external inference service, or notification channel.
"""

import argparse
import gc
import hashlib
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
from functools import wraps
from pathlib import Path

from ocr.frigate_caption_worker import RemoteError, request_bytes


LOG = logging.getLogger("frigate-objects")
KINDS = {"cat", "car", "two_wheeler"}
# COCO has no scooter class. Both detector labels compete in one identity gallery.
EVENT_KINDS = {"cat": "cat", "car": "car", "bicycle": "two_wheeler",
               "motorcycle": "two_wheeler"}
CAMERAS = {"cam1_ds_i202", "cam2_ds_i551", "cam3_ds_i200", "common_camera01",
           "common_camera02", "common_camera03"}
MODEL_REVISION = "dinov2-cat-vehicle-reid-car-v1"
DEFAULT_MODEL = "/models/dinov2-small-cls.onnx"
DEFAULT_CAR_MODEL = "/models/vehicle-reid-0001-static208.onnx"
FEATURE_DIMENSIONS = 384
CAR_FEATURE_DIMENSIONS = 512
MAX_REFERENCES = 500
MAX_EVENTS = 512
MAX_PENDING = 512
EVENT_TTL = 3 * 86400
POLL_SECONDS = 15
MIN_INTERVAL = 0.5
MODEL_BUDGET_MIB = 1536
HEADROOM_MIB = 2048
MIN_VALIDATION_COVERAGE = .75


def encounter_group(value, *, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("Invalid encounter group")
    value = value.strip()
    if len(value) > 160 or (required and not value):
        raise ValueError("A valid curated encounter group is required")
    return value


def unit(vector):
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    if not values or not math.isfinite(norm) or norm < 1e-9:
        raise ValueError("Invalid feature vector")
    return [x / norm for x in values]


def model_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def similarity(left, right):
    if len(left) != len(right):
        raise ValueError("Feature dimensions differ")
    return sum(x * y for x, y in zip(left, right))


def reference_score(vector, references, groups=None):
    scores = [similarity(vector, x) for x in references]
    if groups is not None:
        encounters = {}
        for group, score in zip(groups, scores):
            encounters[group] = max(encounters.get(group, -1.0), score)
        scores = list(encounters.values())
    scores.sort(reverse=True)
    return sum(scores[:2]) / min(2, len(scores)) if scores else -1.0


def calibration(positive_train, positive_test, negative_test, train_groups=None,
                *, minimum_similarity=.85, negative_train=()):
    """Threshold selection on separate events; these are small-sample checks."""
    counts = {"train": len(positive_train), "positive_test": len(positive_test),
              "negative_test": len(negative_test)}
    if counts["train"] < 3 or counts["positive_test"] < 2 or counts["negative_test"] < 3:
        return {"enabled": False, "reason": "insufficient_separate_examples", **counts}
    low_positive = min(reference_score(x, positive_train, train_groups) for x in positive_test)
    high_negative = max(reference_score(x, positive_train, train_groups) for x in negative_test)
    high_training_negative = max((reference_score(x, positive_train, train_groups)
                                  for x in negative_train), default=-1.0)
    threshold = max(minimum_similarity, high_negative + 0.04, high_training_negative + 0.08)
    accepted = sum(reference_score(x, positive_train, train_groups) >= threshold for x in positive_test)
    coverage = accepted / len(positive_test)
    enabled = threshold <= .995 and accepted >= 2 and coverage >= MIN_VALIDATION_COVERAGE
    return {"enabled": enabled, "reason": "validated" if enabled else "overlapping_examples",
            "threshold": round(threshold, 6), "margin": 0.06,
            "lowest_positive": low_positive, "highest_negative": high_negative,
            "highest_training_negative": high_training_negative,
            "accepted_positive": accepted, "positive_coverage": coverage, **counts}


def choose(vector, profiles, references, kind):
    ranked = []
    for p in profiles:
        if p["kind"] != kind:
            continue
        train = [r for r in references if r["profile_id"] == p["id"] and r["role"] == "train"]
        ranked.append((reference_score(vector, [r["vector"] for r in train],
                                       [r.get("group_id", str(i)) for i, r in enumerate(train)]), p))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked:
        return None
    score, profile = ranked[0]
    policy = profile["calibration"]
    runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
    unknowns = [r for r in references if r["kind"] == kind and not r["profile_id"]]
    if unknowns:
        runner_up = max(runner_up, reference_score(vector, [r["vector"] for r in unknowns],
                                                  [r.get("group_id", str(i)) for i, r in enumerate(unknowns)]))
    if (not policy.get("enabled") or score < policy["threshold"]
            or score - runner_up < policy["margin"]):
        return None
    return {"id": profile["id"], "label": profile["label"], "similarity": score}


def transaction(method):
    @wraps(method)
    def atomic(self, *args, **kwargs):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = method(self, *args, **kwargs)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise
    return atomic


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
            CREATE TABLE IF NOT EXISTS feature_models (
                kind TEXT PRIMARY KEY, digest TEXT NOT NULL);
        """)
        self.db.execute("BEGIN IMMEDIATE")
        if "group_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(refs)")}:
            self.db.execute("ALTER TABLE refs ADD COLUMN group_id TEXT NOT NULL DEFAULT ''")
            # Old thresholds did not check encounter-level independence.
            self.db.execute("UPDATE profiles SET calibration='{}'")
        # Previously padded IDs could inflate independence or cross dataset roles.
        # Preserve the vectors, but require explicit re-curation of those entries.
        dirty = [row for row in self.db.execute("SELECT source,kind,group_id FROM refs")
                 if row["group_id"] != encounter_group(row["group_id"])]
        for row in dirty:
            self.db.execute("UPDATE refs SET group_id='' WHERE source=?", (row["source"],))
            self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (row["kind"],))
        # A new feature space cannot reuse thresholds or temporal votes from
        # older vectors. Keep the sources for explicit re-encoding.
        stale_kinds = {row[0] for row in self.db.execute(
            "SELECT DISTINCT kind FROM refs WHERE model!=?", (MODEL_REVISION,))}
        for kind in stale_kinds:
            self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (kind,))
        if stale_kinds:
            self.db.execute("UPDATE events SET status='observing' WHERE status='complete_unknown'")
        if dirty or stale_kinds:
            self.reset_votes()
        self.db.commit()

    def profiles(self):
        return [{**dict(x), "calibration": json.loads(x["calibration"])}
                for x in self.db.execute("SELECT * FROM profiles ORDER BY id")]

    def refs(self):
        return [{**dict(x), "vector": json.loads(x["vector"])} for x in self.db.execute(
            "SELECT * FROM refs WHERE model=?", (MODEL_REVISION,))]

    def data_version(self):
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def reset_votes(self):
        self.db.execute("UPDATE events SET votes='[]',frame='' WHERE status='observing'")

    @transaction
    def bind_model(self, kind, digest):
        if kind not in KINDS or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid feature model binding")
        previous = self.db.execute("SELECT digest FROM feature_models WHERE kind=?", (kind,)).fetchone()
        if previous and previous[0] == digest:
            return False
        # An unbound gallery cannot establish which weights produced its vectors.
        self.db.execute("UPDATE refs SET model='invalidated:'||model WHERE kind=? AND model=?",
                        (kind, MODEL_REVISION))
        self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (kind,))
        self.db.execute("UPDATE events SET status='observing' WHERE status='complete_unknown'")
        self.reset_votes()
        self.db.execute("INSERT INTO feature_models(kind,digest) VALUES(?,?) "
                        "ON CONFLICT(kind) DO UPDATE SET digest=excluded.digest", (kind, digest))
        return True

    @transaction
    def profile(self, identity, kind, label):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity) or kind not in KINDS
                or not 1 <= len(label.strip()) <= 80 or any(ord(x) < 32 for x in label)):
            raise ValueError("Invalid object profile")
        existing = self.db.execute("SELECT * FROM profiles WHERE id=?", (identity,)).fetchone()
        if existing and existing["kind"] != kind:
            raise ValueError("Cannot change profile object kind")
        self.db.execute("INSERT INTO profiles(id,kind,label) VALUES(?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET label=excluded.label", (identity, kind, label))
        self.reset_votes()

    @transaction
    def add_reference(self, source, profile_id, kind, role, vector, *, group_id=None):
        if kind not in KINDS or role not in {"train", "validation"}:
            raise ValueError("Only cat/car train/validation references are accepted")
        if not profile_id and role != "validation":
            raise ValueError("Negative references require validation")
        group_id = encounter_group(group_id)
        group = self.db.execute("SELECT profile_id,kind,role FROM refs WHERE group_id=?",
                                (group_id,)).fetchall() if group_id else []
        if any(tuple(row) != (profile_id, kind, role) for row in group):
            raise ValueError("An encounter cannot cross identities or train/validation")
        existing = self.db.execute("SELECT * FROM refs WHERE source=?", (source,)).fetchone()
        if existing:
            if existing["model"] == MODEL_REVISION:
                raise ValueError("Source already enrolled; train/test reuse is forbidden")
            if tuple(existing[key] for key in ("profile_id", "kind", "role", "group_id")) != (
                    profile_id, kind, role, group_id):
                raise ValueError("Re-encoding must preserve identity, kind, role and encounter")
        if not existing and self.db.execute("SELECT count(*) FROM refs").fetchone()[0] >= MAX_REFERENCES:
            raise ValueError("Reference gallery is full")
        if profile_id and not self.db.execute(
                "SELECT 1 FROM profiles WHERE id=? AND kind=?", (profile_id, kind)).fetchone():
            raise ValueError("Unknown profile or wrong object kind")
        self.db.execute("INSERT INTO refs(source,profile_id,kind,role,vector,model,group_id) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                        "vector=excluded.vector,model=excluded.model", (source, profile_id, kind, role,
                        json.dumps(unit(vector)), MODEL_REVISION, group_id))
        # Changing any gallery entry invalidates thresholds of the same object kind.
        self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (kind,))
        self.reset_votes()

    @transaction
    def curate_reference(self, source, group_id, role=None):
        group_id = encounter_group(group_id, required=True)
        row = self.db.execute("SELECT * FROM refs WHERE source=?", (source,)).fetchone()
        if row is None:
            raise ValueError("Reference not found")
        role = role or row["role"]
        if role not in {"train", "validation"}:
            raise ValueError("Invalid reference role")
        if not row["profile_id"] and role != "validation":
            raise ValueError("Negative references require validation")
        others = self.db.execute("SELECT profile_id,kind,role FROM refs WHERE group_id=? AND source!=?",
                                 (group_id, source)).fetchall()
        if any(tuple(other) != (row["profile_id"], row["kind"], role) for other in others):
            raise ValueError("An encounter cannot cross identities or train/validation")
        self.db.execute("UPDATE refs SET group_id=?,role=? WHERE source=?", (group_id, role, source))
        self.db.execute("UPDATE profiles SET calibration='{}' WHERE kind=?", (row["kind"],))
        self.reset_votes()

    @transaction
    def calibrate(self, identity):
        profiles, refs = self.profiles(), self.refs()
        profile = next(p for p in profiles if p["id"] == identity)
        train = [r["vector"] for r in refs if r["profile_id"] == identity and r["role"] == "train"]
        positive = [r["vector"] for r in refs if r["profile_id"] == identity and r["role"] == "validation"]
        negative = [r["vector"] for r in refs if r["profile_id"] != identity
                    and r["kind"] == profile["kind"] and r["role"] == "validation"]
        train_groups = [r["group_id"] for r in refs if r["profile_id"] == identity and r["role"] == "train"]
        if profile["kind"] == "car":
            other_train = [r["vector"] for r in refs if r["profile_id"] != identity
                           and r["kind"] == "car" and r["role"] == "train"]
            policy = calibration(train, positive, negative, train_groups,
                                 minimum_similarity=.60, negative_train=other_train)
        else:
            policy = calibration(train, positive, negative, train_groups)
        groups = {
            "train": {r["group_id"] for r in refs if r["group_id"] and r["profile_id"] == identity and r["role"] == "train"},
            "positive_test": {r["group_id"] for r in refs if r["group_id"] and r["profile_id"] == identity and r["role"] == "validation"},
            "negative_test": {r["group_id"] for r in refs if r["profile_id"] != identity
                              and r["group_id"] and r["kind"] == profile["kind"] and r["role"] == "validation"},
        }
        policy["independent_groups"] = {key: len(value) for key, value in groups.items()}
        if any(len(groups[key]) < required for key, required in
               (("train", 3), ("positive_test", 2), ("negative_test", 3))):
            policy.update(enabled=False, reason="insufficient_independent_encounters")
        if any(not r["group_id"] for r in refs if r["kind"] == profile["kind"]):
            policy.update(enabled=False, reason="uncurated_encounters")
        if policy["enabled"]:
            profile["calibration"] = policy
            held_out = [r for r in refs if r["profile_id"] == identity and r["role"] == "validation"]
            predictions = [choose(r["vector"], profiles, refs, profile["kind"]) for r in held_out]
            accepted_groups = {r["group_id"] for r, match in zip(held_out, predictions)
                               if match and match["id"] == identity}
            accepted = sum(bool(match) and match["id"] == identity for match in predictions)
            encounter_results = {}
            for reference, match in zip(held_out, predictions):
                results = encounter_results.setdefault(reference["group_id"], [])
                results.append(bool(match) and match["id"] == identity)
            # Each encounter has equal weight regardless of its frame count.
            coverages = [sum(results) / len(results) for results in encounter_results.values()]
            policy.update(accepted_positive=accepted, positive_coverage=accepted / len(held_out),
                          accepted_positive_groups=len(accepted_groups),
                          positive_group_coverage=sum(coverages) / len(coverages))
            if (any(match and match["id"] != identity for match in predictions)
                    or len(accepted_groups) < 2
                    or policy["positive_coverage"] < MIN_VALIDATION_COVERAGE
                    or policy["positive_group_coverage"] < MIN_VALIDATION_COVERAGE):
                policy.update(enabled=False, reason="validation_margin_failed")
        self.db.execute("UPDATE profiles SET calibration=? WHERE id=?", (json.dumps(policy), identity))
        self.reset_votes()
        return policy

    @transaction
    def remember(self, event_id, frame, votes, status):
        self._remember(event_id, frame, votes, status)

    @transaction
    def remember_current(self, version, event_id, frame, votes, status):
        if self.data_version() != version:
            return False
        self._remember(event_id, frame, votes, status)
        return True

    def _remember(self, event_id, frame, votes, status):
        if status == "pending" and self.db.execute(
                "SELECT count(*) FROM events WHERE status='pending' AND id!=?", (event_id,)
        ).fetchone()[0] >= MAX_PENDING:
            raise RuntimeError("Unresolved write limit reached; reconcile pending events")
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "updated=excluded.updated,frame=excluded.frame,votes=excluded.votes,status=excluded.status",
                        (event_id, time.time(), frame, json.dumps(votes), status))
        self.db.execute("DELETE FROM events WHERE status!='pending' AND updated<?", (time.time() - EVENT_TTL,))
        self.db.execute("DELETE FROM events WHERE id IN (SELECT id FROM events WHERE status!='pending' "
                        "ORDER BY updated DESC LIMIT -1 OFFSET ?)", (MAX_EVENTS,))

    def health(self, **value):
        value.update(updated=time.time(), model=MODEL_REVISION,
                     feature_models=dict(self.db.execute("SELECT kind,digest FROM feature_models")),
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
        filters = {"labels": ",".join(sorted(EVENT_KINDS)), "cameras": ",".join(sorted(CAMERAS)),
                   "limit": 100, "has_snapshot": 1, "include_thumbnails": 0}
        recent = self.json("/events?" + urllib.parse.urlencode(
            {**filters, "after": int(time.time() - 300)}))
        # A parked object's event can remain active for hours; start_time is not recency.
        active = self.json("/events?" + urllib.parse.urlencode({**filters, "in_progress": 1}))
        merged = {event["id"]: event for event in recent + active}
        return sorted(merged.values(), key=lambda event: event["start_time"], reverse=True)

    def recording_coverage(self, camera, timestamp):
        query = urllib.parse.urlencode({"after": timestamp, "before": timestamp})
        segments = self.json("/" + camera + "/recordings?" + query)
        if not isinstance(segments, list):
            raise ValueError("Invalid recording coverage")
        return {(row["id"], row["start_time"], row["end_time"]) for row in segments
                if isinstance(row, dict) and isinstance(row.get("id"), str)
                and all(isinstance(row.get(key), (int, float)) and math.isfinite(row[key])
                        for key in ("start_time", "end_time"))
                and row["start_time"] <= timestamp <= row["end_time"]}

    def snapshot(self, event):
        from PIL import Image

        if event["label"] not in EVENT_KINDS or event["camera"] not in CAMERAS:
            raise ValueError("Unsupported object or property camera")
        expected = float(event.get("data", {}).get("snapshot_frame_time", 0))
        box = event.get("data", {}).get("box")
        if not math.isfinite(expected) or expected <= 0 or not isinstance(box, list) or len(box) != 4:
            raise ValueError("No verified snapshot frame/box")
        if (any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                or min(box) < 0 or max(box) > 1 or min(box[2:]) <= 0):
            raise ValueError("Invalid relative object box")
        data, headers = request_bytes(
            self.base + "/events/" + urllib.parse.quote(event["id"], safe="")
            + "/snapshot.jpg?crop=0&bbox=0&timestamp=0&height=512&quality=90",
            limit=4 * 1024 * 1024, timeout=8, return_headers=True, bypass_cache=True)
        actual = float(headers.get("x-frame-time", 0))
        if not math.isfinite(actual) or abs(actual - expected) > 0.001:
            raise ValueError("Snapshot advanced; wait for matching event metadata")
        if EVENT_KINDS[event["label"]] in {"car", "two_wheeler"}:
            # Event JPEGs come from the small detection stream. Use the native
            # recording at its verified best-frame time, preserving the same
            # normalized box. An unfinished/missing recording remains retryable.
            coverage = self.recording_coverage(event["camera"], expected)
            if not coverage:
                raise ValueError("No recording at the exact best-frame timestamp")
            data = request_bytes(self.base + "/" + event["camera"] + "/recordings/"
                + str(expected) + "/snapshot.jpg?height=1080",
                limit=8 * 1024 * 1024, timeout=20, bypass_cache=True)
            # Frigate rounds up across recording gaps. Verify the exact interval
            # before and after extraction, including retention/deletion races.
            if not coverage.intersection(self.recording_coverage(event["camera"], expected)):
                raise ValueError("Recording coverage changed during extraction")
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

    def apply(self, event_id, label, *, manual=False, expected_kind=None, before_write=None):
        current = self.event(event_id)
        allowed = set(EVENT_KINDS) | ({"person"} if manual else set())
        if current["label"] not in allowed or current["camera"] not in CAMERAS:
            raise ValueError("Event kind/camera outside allowed scope")
        if not manual and (expected_kind not in KINDS
                           or EVENT_KINDS[current["label"]] != expected_kind):
            raise ValueError("Event gallery changed before label write")
        if not 1 <= len(label.strip()) <= 80 or any(ord(x) < 32 for x in label):
            raise ValueError("Invalid label")
        existing = current.get("sub_label")
        if isinstance(existing, list):
            existing = existing[0] if existing else None
        if existing and existing != label:
            return False  # Preserve native and human labels.
        if not existing:
            if before_write is not None:
                before_write()
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


def gpu_available(stats, loaded=False, *, check_utilization=True):
    return (stats["free_mib"] >= HEADROOM_MIB + (0 if loaded else MODEL_BUDGET_MIB)
            and (not check_utilization or stats["util_percent"] < 25))


def wait_for_gpu(uuid, loaded=False, *, attempts=1):
    for attempt in range(attempts):
        stats = gpu_stats(uuid)
        if gpu_available(stats, loaded):
            return stats
        if (not gpu_available(stats, loaded, check_utilization=False)
                or attempt + 1 == attempts):
            raise RuntimeError("GPU priority/headroom guard")
        # nvidia-smi may still include a preceding sampler kernel in its window.
        time.sleep(.25)


class Encoder:
    def __init__(self, model, gpu_uuid, audit=False, wait_for_idle=False,
                 car_model=DEFAULT_CAR_MODEL):
        self.model, self.gpu_uuid = model, gpu_uuid
        self.base_model, self.car_model = model, car_model
        self.audit = audit
        self.wait_for_idle = wait_for_idle
        self.session = None
        self.last_run = 0.0
        self.last_used = 0.0
        self.load_mib = 0
        self.current_load_mib = 0
        self.inference_ms = 0.0
        self.bound_car_digest = None
        self.bound_base_digest = None
        self._base_stamp = self._base_digest = None
        self._car_stamp = self._car_digest = None

    def base_model_digest(self):
        info = Path(self.base_model).stat()
        stamp = (self.base_model, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp != self._base_stamp:
            self._base_digest = model_digest(self.base_model)
            self._base_stamp = stamp
        return self._base_digest

    def car_model_digest(self):
        info = Path(self.car_model).stat()
        stamp = (self.car_model, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp != self._car_stamp:
            self._car_digest = model_digest(self.car_model)
            self._car_stamp = stamp
        return self._car_digest

    def unload(self):
        self.session = None
        gc.collect()

    def infer(self, tensor):
        """Run a prepared tensor under the same CUDA priority and memory guards."""
        import onnxruntime as ort
        try:
            before = wait_for_gpu(self.gpu_uuid, self.session is not None,
                                  attempts=9 if self.wait_for_idle else 1)
        except RuntimeError:
            self.unload()
            raise
        if self.session is None:
            self.current_load_mib = 0
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
            loaded = gpu_stats(self.gpu_uuid)
            self.current_load_mib = max(0, before["free_mib"] - loaded["free_mib"])
            self.load_mib = max(self.load_mib, self.current_load_mib)
            if self.load_mib > MODEL_BUDGET_MIB:
                self.unload()
                raise RuntimeError("GPU model memory budget exceeded")
            before = loaded
        time.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - self.last_run)))
        started = time.monotonic()
        output = self.session.run(None, {self.session.get_inputs()[0].name: tensor})[0]
        self.inference_ms = (time.monotonic() - started) * 1000
        self.last_run = self.last_used = time.monotonic()
        after = gpu_stats(self.gpu_uuid)
        # Only account for changes during this session's own call. Another
        # session may allocate between calls; a lifetime baseline includes it.
        self.current_load_mib = max(0, self.current_load_mib + before["free_mib"] - after["free_mib"])
        self.load_mib = max(self.load_mib, self.current_load_mib)
        # This sample includes our own kernels; contention is checked before inference.
        if not gpu_available(after, True, check_utilization=False) or self.load_mib > MODEL_BUDGET_MIB:
            self.unload()
            raise RuntimeError("GPU headroom lost during inference")
        return output

    def encode(self, data, kind="cat"):
        import numpy as np
        from PIL import Image

        if kind not in KINDS:
            raise ValueError("Unsupported object feature model")
        if (kind == "two_wheeler" and self.bound_base_digest is not None
                and self.base_model_digest() != self.bound_base_digest):
            self.unload()
            raise RuntimeError("Two-wheeler model changed; gallery must be rebound")
        if (kind == "car" and self.bound_car_digest is not None
                and self.car_model_digest() != self.bound_car_digest):
            self.unload()
            raise RuntimeError("Car model changed before inference; gallery must be rebound")
        selected_model = self.car_model if kind == "car" else self.base_model
        if self.model != selected_model:
            self.unload()
            self.model = selected_model
        Image.MAX_IMAGE_PIXELS = 8_000_000
        with Image.open(io.BytesIO(data)) as image:
            if min(image.size) < 40 or image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Object crop dimensions outside limits")
            image = image.convert("RGB")
            if kind == "car":
                # The original RGB OSNet graph includes input InstanceNormalization.
                image = image.resize((208, 208), Image.Resampling.BILINEAR)
                array = np.asarray(image, dtype=np.float32)
            else:
                image.thumbnail((224, 224), Image.Resampling.BILINEAR)
                canvas = Image.new("RGB", (224, 224), (124, 116, 104))
                canvas.paste(image, ((224 - image.width) // 2, (224 - image.height) // 2))
                array = np.asarray(canvas, dtype=np.float32) / 255.0
                array = (array - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
        tensor = np.ascontiguousarray(array.transpose(2, 0, 1)[None])
        vector = self.infer(tensor).reshape(-1)
        if vector.size != (CAR_FEATURE_DIMENSIONS if kind == "car" else FEATURE_DIMENSIONS):
            self.unload()
            raise ValueError("Feature model revision/dimensions do not match")
        return unit(vector.tolist())

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


def bind_car_model(store, encoder):
    digest = encoder.car_model_digest()
    if encoder.bound_car_digest != digest:
        encoder.unload()
    store.bind_model("car", digest)
    encoder.bound_car_digest = digest


def bind_feature_models(store, encoder):
    bind_car_model(store, encoder)
    digest = encoder.base_model_digest()
    if encoder.bound_base_digest != digest:
        encoder.unload()
    store.bind_model("two_wheeler", digest)
    encoder.bound_base_digest = digest


def run(store, encoder, api, once=False, refresh_car_model=None):
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            if refresh_car_model is not None:
                refresh_car_model()
            version = store.data_version()
            profiles, refs = store.profiles(), store.refs()
            active = [p for p in profiles if p["calibration"].get("enabled")]
            if not active:
                encoder.unload()
                store.health(state="waiting_for_references", gpu_loaded=False)
            else:
                for event in reversed(api.candidates()):
                    if stopping or store.data_version() != version:
                        break
                    kind = EVENT_KINDS.get(event.get("label"))
                    if (kind is None or event.get("camera") not in CAMERAS
                            or not any(p["kind"] == kind for p in active)):
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
                    try:
                        timestamp = float(frame)
                        if not math.isfinite(timestamp) or timestamp <= 0:
                            continue
                        if previous and previous["frame"] and timestamp <= float(previous["frame"]):
                            continue  # An older best frame cannot supply another confirmation.
                    except (TypeError, ValueError):
                        continue
                    if event.get("sub_label"):
                        store.remember(event["id"], frame, [], "protected")
                        continue
                    try:
                        image = api.snapshot(event)
                    except (ValueError, OSError, RemoteError):
                        continue  # Unaligned/low-quality samples remain unknown.
                    vector = encoder.encode(image, kind)
                    match = choose(vector, profiles, refs, kind)
                    votes = (json.loads(previous["votes"]) if previous else [])[-2:]
                    votes.append(match["id"] if match else None)
                    # Require consecutive matches on strictly increasing best-frame timestamps.
                    accepted = match and bool(frame) and votes[-2:] == [match["id"], match["id"]]
                    if accepted:
                        if store.data_version() != version:
                            break

                        def before_write():
                            # The authoritative GET may have taken seconds; check again here.
                            if not store.remember_current(version, event["id"], frame, votes, "pending"):
                                raise RuntimeError("Gallery changed before label POST")

                        success = api.apply(event["id"], match["label"], expected_kind=kind,
                                            before_write=before_write)
                        status = "applied" if success else "protected"
                        store.remember(event["id"], frame, votes, status)
                    else:
                        status = "complete_unknown" if event.get("end_time") else "observing"
                        if not store.remember_current(version, event["id"], frame, votes, status):
                            break
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


def import_archive(store, directory, image, profile_id, role, group_id, model,
                   car_model=DEFAULT_CAR_MODEL):
    """Import one explicitly confirmed proposal without inference or label writes."""
    group_id = encounter_group(group_id, required=True)
    directory = Path(directory).resolve(strict=True)
    if Path(image).name != image or not re.fullmatch(r"frame-[0-9]+-[0-9]+-(cat|car|two_wheeler)\.jpg", image):
        raise ValueError("Select one archive crop filename")
    crop = (directory / image).resolve(strict=True)
    if crop.parent != directory or not crop.is_file():
        raise ValueError("Archive crop must remain inside its directory")
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    with Path(model).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    models = plan.get("models", {})
    if models.get("feature_revision") != MODEL_REVISION or models.get("model") != digest.hexdigest():
        raise ValueError("Import requires the original feature model identity")
    digest = hashlib.sha256()
    with Path(car_model).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if models.get("car_model") != digest.hexdigest():
        raise ValueError("Import requires the original feature model identity")
    selected = None
    with (directory / "results.jsonl").open(encoding="utf-8") as stream:
        for index, line in enumerate(iter(lambda: stream.readline(1024 * 1024), "")):
            if index >= 20000 or not line.endswith("\n"):
                raise ValueError("Archive results are incomplete or exceed the import budget")
            row = json.loads(line)
            for item in row.get("crops", []):
                if item.get("image") == image:
                    if selected is not None:
                        raise ValueError("Archive crop occurs more than once")
                    selected = (row, item)
    if selected is None:
        raise ValueError("Archive crop not found in results")
    row, item = selected
    if (row.get("status") != "sampled" or row.get("camera") not in CAMERAS
            or item.get("kind") not in KINDS or not math.isfinite(float(row["time"]))
            or len(item["vector"]) != (CAR_FEATURE_DIMENSIONS if item.get("kind") == "car" else FEATURE_DIMENSIONS)
            or len(item["box"]) != 4
            or not all(math.isfinite(float(x)) and 0 <= float(x) <= 1 for x in item["box"])):
        raise ValueError("Invalid archive proposal")
    # Stable across renamed directories and sampler frame numbering.
    identity = json.dumps([row["camera"], float(row["time"]), item["kind"],
                           [float(x) for x in item["box"]]], separators=(",", ":"))
    source = "archive:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    store.add_reference(source, profile_id, item["kind"], role, item["vector"], group_id=group_id)
    return {"source": source, "image": image, "group_id": group_id, "imported": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=os.getenv("FRIGATE_OBJECT_STATE", "/state"))
    parser.add_argument("--model", default=os.getenv("FRIGATE_OBJECT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--car-model", default=os.getenv("FRIGATE_OBJECT_CAR_MODEL", DEFAULT_CAR_MODEL))
    parser.add_argument("--gpu", default=os.getenv("FRIGATE_OBJECT_GPU", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--references", action="store_true", help="List reference metadata without vectors")
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
    enroll.add_argument("--group", help="Shared encounter ID for all cameras/frames of one visit")
    curate = commands.add_parser("curate")
    curate.add_argument("--source", required=True)
    curate.add_argument("--group", required=True)
    curate.add_argument("--role", choices=["train", "validation"])
    archive = commands.add_parser("import-archive")
    archive.add_argument("--archive", required=True)
    archive.add_argument("--image", required=True)
    archive.add_argument("--profile")
    archive.add_argument("--role", choices=["train", "validation"], required=True)
    archive.add_argument("--group", required=True)
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--profile", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--event", required=True)
    manual = commands.add_parser("manual-label")
    manual.add_argument("--event", required=True)
    manual.add_argument("--label", required=True)
    args = parser.parse_args()
    store, api = Store(args.state), Frigate()
    encoder = Encoder(args.model, args.gpu, audit=args.command == "probe", car_model=args.car_model)
    try:
        if args.command in {"enroll", "probe", "import-archive", "calibrate"}:
            bind_feature_models(store, encoder)
        if args.command == "status":
            health = store.directory / "health.json"
            result = {"profiles": store.profiles(), "references": len(store.refs()),
                      "health": json.loads(health.read_text()) if health.exists() else None}
            if args.references:
                result["reference_metadata"] = [{key: row[key] for key in
                    ("source", "profile_id", "kind", "role", "group_id")} for row in store.refs()]
        elif args.command == "import-archive":
            result = import_archive(store, args.archive, args.image, args.profile,
                                    args.role, args.group, args.model, args.car_model)
        elif args.command == "curate":
            store.curate_reference(args.source, args.group, args.role)
            result = {"source": args.source, "group_id": encounter_group(args.group), "curated": True}
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
            kind = EVENT_KINDS[event["label"]]
            vector = encoder.encode(image, kind)
            if args.command == "enroll":
                store.add_reference(args.event, args.profile, kind, args.role, vector,
                                    group_id=args.group)
            result = {"event": args.event, "dimensions": len(vector), "provider": "CUDAExecutionProvider",
                      "load_mib": encoder.load_mib, "inference_ms": encoder.inference_ms,
                      "match": choose(vector, store.profiles(), store.refs(), kind)}
            if args.command == "probe":
                warm = []
                for _ in range(3):
                    encoder.encode(image, kind)
                    warm.append(encoder.inference_ms)
                result.update(executed_providers=encoder.audit_providers(), warm_inference_ms=warm)
        else:
            run(store, encoder, api, args.once, refresh_car_model=lambda: bind_feature_models(store, encoder))
            result = {"stopped": True}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        encoder.unload()
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
