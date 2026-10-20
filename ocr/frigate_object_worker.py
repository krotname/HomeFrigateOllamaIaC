"""GPU-only reference matching for cats and cars in Frigate events.

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
KINDS = {"cat", "car"}
CAMERAS = {"cam1_ds_i202", "cam2_ds_i551", "cam3_ds_i200", "common_camera01",
           "common_camera02", "common_camera03"}
MODEL_REVISION = "dinov2-small-cls-v1"
DEFAULT_MODEL = "/models/dinov2-small-cls.onnx"
FEATURE_DIMENSIONS = 384
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


def calibration(positive_train, positive_test, negative_test, train_groups=None):
    """Threshold selection on separate events; these are small-sample checks."""
    counts = {"train": len(positive_train), "positive_test": len(positive_test),
              "negative_test": len(negative_test)}
    if counts["train"] < 3 or counts["positive_test"] < 2 or counts["negative_test"] < 3:
        return {"enabled": False, "reason": "insufficient_separate_examples", **counts}
    low_positive = min(reference_score(x, positive_train, train_groups) for x in positive_test)
    high_negative = max(reference_score(x, positive_train, train_groups) for x in negative_test)
    threshold = max(0.85, high_negative + 0.04)
    accepted = sum(reference_score(x, positive_train, train_groups) >= threshold for x in positive_test)
    coverage = accepted / len(positive_test)
    enabled = threshold <= .995 and accepted >= 2 and coverage >= MIN_VALIDATION_COVERAGE
    return {"enabled": enabled, "reason": "validated" if enabled else "overlapping_examples",
            "threshold": round(threshold, 6), "margin": 0.06,
            "lowest_positive": low_positive, "highest_negative": high_negative,
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
    def profile(self, identity, kind, label):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity) or kind not in KINDS
                or not 1 <= len(label.strip()) <= 80 or any(ord(x) < 32 for x in label)):
            raise ValueError("Invalid cat/car profile")
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
        if self.db.execute("SELECT 1 FROM refs WHERE source=?", (source,)).fetchone():
            raise ValueError("Source already enrolled; train/test reuse is forbidden")
        if self.db.execute("SELECT count(*) FROM refs").fetchone()[0] >= MAX_REFERENCES:
            raise ValueError("Reference gallery is full")
        if profile_id and not self.db.execute(
                "SELECT 1 FROM profiles WHERE id=? AND kind=?", (profile_id, kind)).fetchone():
            raise ValueError("Unknown profile or wrong object kind")
        self.db.execute("INSERT INTO refs(source,profile_id,kind,role,vector,model,group_id) "
                        "VALUES(?,?,?,?,?,?,?)", (source, profile_id, kind, role,
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
            policy.update(accepted_positive=accepted, positive_coverage=accepted / len(held_out),
                          accepted_positive_groups=len(accepted_groups),
                          positive_group_coverage=len(accepted_groups) / len(groups["positive_test"]))
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

    def apply(self, event_id, label, *, manual=False, before_write=None):
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
    def __init__(self, model, gpu_uuid, audit=False, wait_for_idle=False):
        self.model, self.gpu_uuid = model, gpu_uuid
        self.audit = audit
        self.wait_for_idle = wait_for_idle
        self.session = None
        self.last_run = 0.0
        self.last_used = 0.0
        self.load_mib = 0
        self.current_load_mib = 0
        self.inference_ms = 0.0

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
            self.current_load_mib = self.load_mib = 0
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
            self.current_load_mib = self.load_mib = max(0, before["free_mib"] - loaded["free_mib"])
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

    def encode(self, data):
        import numpy as np
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 8_000_000
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
        vector = self.infer(tensor).reshape(-1)
        if vector.size != FEATURE_DIMENSIONS:
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


def run(store, encoder, api, once=False):
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
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
                    vector = encoder.encode(image)
                    match = choose(vector, profiles, refs, event["label"])
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

                        success = api.apply(event["id"], match["label"], before_write=before_write)
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


def import_archive(store, directory, image, profile_id, role, group_id, model):
    """Import one explicitly confirmed proposal without inference or label writes."""
    group_id = encounter_group(group_id, required=True)
    directory = Path(directory).resolve(strict=True)
    if Path(image).name != image or not re.fullmatch(r"frame-[0-9]+-[0-9]+-(cat|car)\.jpg", image):
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
            or len(item["vector"]) != 1280 or len(item["box"]) != 4
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
    encoder = Encoder(args.model, args.gpu, audit=args.command == "probe")
    try:
        if args.command == "status":
            health = store.directory / "health.json"
            result = {"profiles": store.profiles(), "references": len(store.refs()),
                      "health": json.loads(health.read_text()) if health.exists() else None}
            if args.references:
                result["reference_metadata"] = [{key: row[key] for key in
                    ("source", "profile_id", "kind", "role", "group_id")} for row in store.refs()]
        elif args.command == "import-archive":
            result = import_archive(store, args.archive, args.image, args.profile,
                                    args.role, args.group, args.model)
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
            vector = encoder.encode(image)
            if args.command == "enroll":
                store.add_reference(args.event, args.profile, event["label"], args.role, vector,
                                    group_id=args.group)
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
