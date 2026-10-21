"""Bounded, resumable archive sampling. Produces private proposals; never writes labels."""

import argparse
import bisect
import hashlib
import io
import json
import math
import os
import time
import urllib.parse
from collections import Counter
from pathlib import Path

from ocr.frigate_caption_worker import RemoteError, request_bytes
from ocr.frigate_object_worker import (
    CAMERAS, DEFAULT_CAR_MODEL, DEFAULT_MODEL, EVENT_KINDS, MODEL_REVISION, Encoder, Frigate,
)


def get_json(path):
    return json.loads(request_bytes(Frigate.base + path, limit=24 * 1024 * 1024,
                                   timeout=30, bypass_cache=True))


def event_pages(after, before, fetch=get_json):
    """Split saturated time ranges so equal timestamps cannot be lost at a cursor."""
    filters = {"labels": ",".join(sorted(EVENT_KINDS)), "cameras": ",".join(sorted(CAMERAS)),
               "include_thumbnails": 0, "limit": 200}
    pending, found = [(after, before)], {}
    requests = 0
    while pending:
        requests += 1
        if requests > 128:
            raise RuntimeError("Archive pagination request budget")
        low, high = pending.pop()
        rows = fetch("/events?" + urllib.parse.urlencode({**filters, "after": low, "before": high}))
        if len(rows) >= 200:
            if high - low < .001:
                raise RuntimeError("Archive event range saturated; coverage is incomplete")
            middle = (low + high) / 2
            pending.extend([(low, middle + .000001), (middle - .000001, high)])
            if len(pending) > 64:
                raise RuntimeError("Archive pagination limit")
        else:
            found.update((row["id"], row) for row in rows)
    return sorted(found.values(), key=lambda row: (row["start_time"], row["id"]))


def recording_spans(recordings, after, before):
    """Union overlapping segments before counting coverage or locating a frame."""
    spans = []
    for row in sorted(recordings, key=lambda item: item["start_time"]):
        low, high = max(after, row["start_time"]), min(before, row["end_time"])
        if high <= low:
            continue
        if spans and low <= spans[-1][1]:
            spans[-1][1] = max(high, spans[-1][1])
        else:
            spans.append([low, high])
    return spans


def sample_plan(recordings, after, before, interval, max_samples=None):
    ordered = recording_spans(recordings, after, before)
    starts = [row[0] for row in ordered]
    jobs, gaps = [], 0
    for target in range(math.ceil(after / interval) * interval, math.ceil(before), interval):
        index = bisect.bisect_right(starts, target) - 1
        if index < 0 or ordered[index][1] <= target:
            gaps += 1
            continue
        jobs.append(float(target))
        if max_samples is not None and len(jobs) > max_samples:
            raise ValueError("Planned frames exceed the explicit budget")
    return jobs, gaps


def overlap(left, right):
    x = max(0, min(left[0] + left[2], right[0] + right[2]) - max(left[0], right[0]))
    y = max(0, min(left[1] + left[3], right[1] + right[3]) - max(left[1], right[1]))
    intersection = x * y
    return intersection / max(1e-9, left[2] * left[3] + right[2] * right[3] - intersection)


def detections(output, threshold=.4):
    """Decode the deployed YOLOv9 COCO output, with class-specific NMS."""
    import numpy as np

    rows = np.squeeze(output)
    if rows.ndim != 2:
        raise ValueError("Unexpected YOLO output shape")
    if rows.shape[0] < rows.shape[1]:
        rows = rows.T
    if rows.shape[1] != 84:
        raise ValueError("Expected deployed 80-class YOLO model")
    result = []
    # Ignore all other classes, including people. This is not a person classifier.
    classes = np.argmax(rows[:, 4:], axis=1)
    scores = np.max(rows[:, 4:], axis=1)
    for class_ids, kind in (((2,), "car"), ((15,), "cat"), ((1, 3), "two_wheeler")):
        selected = rows[np.isin(classes, class_ids) & (scores >= threshold)]
        selected = selected[np.argsort(-np.max(selected[:, 4:], axis=1))]
        kept = []
        for row in selected:
            x, y, width, height = (float(value) / 320 for value in row[:4])
            box = [max(0., x - width / 2), max(0., y - height / 2), 0., 0.]
            box[2:] = [min(1., x + width / 2) - box[0], min(1., y + height / 2) - box[1]]
            if min(box[2:]) <= 0 or any(overlap(box, item["box"]) > .4 for item in kept):
                continue
            kept.append({"kind": kind, "score": float(max(row[4:])), "box": box})
            if len(kept) >= 8:
                break
        result.extend(kept)
    return result


def crop_image(image, box):
    from PIL import ImageStat, ImageFilter

    x, y, width, height = box
    crop = image.crop((int(x * image.width), int(y * image.height),
                       math.ceil((x + width) * image.width), math.ceil((y + height) * image.height)))
    if min(crop.size) < 40:
        raise ValueError("Small object crop")
    edges = ImageStat.Stat(crop.convert("L").filter(ImageFilter.FIND_EDGES)).stddev[0]
    if edges < 5:
        raise ValueError("Low-detail crop")
    pixels = crop.convert("L").resize((9, 8)).load()
    fingerprint = sum((pixels[x, y] > pixels[x + 1, y]) << (y * 8 + x)
                      for y in range(8) for x in range(8))
    return crop, fingerprint, edges


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def build_plan(after, before, interval, event_after, max_frames):
    jobs, coverage = [], {}
    for camera in sorted(CAMERAS):
        rows = get_json("/" + camera + "/recordings?" + urllib.parse.urlencode(
            {"after": after, "before": before}))
        times, gaps = sample_plan(rows, after, before, interval, max_frames - len(jobs))
        coverage[camera] = {"segments": len(rows), "grid_samples": len(times), "grid_gaps": gaps,
                            "recorded_hours": sum(high - low for low, high in
                                recording_spans(rows, after, before)) / 3600}
        jobs.extend({"camera": camera, "time": stamp, "event": None} for stamp in times)
    events = event_pages(event_after, before)
    minimal = []
    for event in events:
        if (not event.get("has_clip") or event["camera"] not in CAMERAS
                or event["label"] not in EVENT_KINDS):
            continue
        start = event["start_time"]
        end = min(event.get("end_time") or before, before)
        frame_time = event.get("data", {}).get("snapshot_frame_time")
        if frame_time is None:
            frame_time = start + .5
        record = {"id": event["id"], "camera": event["camera"], "kind": EVENT_KINDS[event["label"]],
                  "start": start, "end": end, "snapshot_time": frame_time,
                  "box": event.get("data", {}).get("box")}
        minimal.append(record)
        for stamp in sorted({start + .5, frame_time, min(start + 30, end - .1)}):
            if start <= stamp <= end:
                jobs.append({"camera": event["camera"], "time": stamp, "event": record})
                if len(jobs) > max_frames:
                    raise ValueError("Planned frames exceed the explicit budget")
    # Event examples first; stable IDs make resuming independent of wall time.
    jobs.sort(key=lambda job: (job["event"] is None, job["camera"], job["time"]))
    for index, job in enumerate(jobs):
        job["id"] = f"frame-{index:05d}"
    if len(jobs) > max_frames:
        raise ValueError("Planned frames exceed the explicit budget")
    return {"after": after, "before": before, "event_after": event_after, "interval": interval,
            "coverage": coverage, "events": minimal, "jobs": jobs}


def read_results(path):
    """Keep complete JSONL rows; only discard an interrupted trailing record."""
    rows, recovered = [], 0
    if not path.exists():
        return rows, recovered
    with path.open("r+b") as stream:
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if line.endswith(b"\n"):
                    raise ValueError("Corrupt complete archive result record") from None
                recovered = len(line)
                stream.truncate(start)
                break
            if not isinstance(row, dict):
                raise ValueError("Archive result record must be an object")
            rows.append(row)
            if not line.endswith(b"\n"):
                stream.write(b"\n")
                break
    return rows, recovered


def model_identity(args):
    identity = {"feature_revision": MODEL_REVISION}
    for name in ("model", "car_model", "detector"):
        digest = hashlib.sha256()
        with Path(getattr(args, name)).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        identity[name] = digest.hexdigest()
    return identity


def scan(args):
    import fcntl

    os.umask(0o077)
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the inode after closing: unlinking a lock file permits a second lock
    # while another process still holds the old inode. Kernel releases on exit.
    with (directory / ".scan.lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Archive output is already being scanned") from None
        return _scan(args)


def _scan(args):
    import numpy as np
    from PIL import Image

    os.umask(0o077)
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    models = model_identity(args)
    plan_path = directory / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (plan["after"], plan["before"], plan["interval"], plan["event_after"]) != (
                args.after, args.before, args.interval, args.event_after):
            raise ValueError("Resume requires the original time range and interval")
        if plan.get("models") != models:
            raise ValueError("Resume requires the original model identities")
    else:
        plan = build_plan(args.after, args.before, args.interval, args.event_after, args.max_frames)
        plan["models"] = models
        atomic_json(plan_path, plan)
    if len(plan["jobs"]) > args.max_frames:
        raise ValueError("Resume plan exceeds the explicit frame budget")
    results_path = directory / "results.jsonl"
    previous, recovered_tail_bytes = read_results(results_path)
    done = {row["id"] for row in previous}
    if len(done) != len(previous) or not done.issubset({job["id"] for job in plan["jobs"]}):
        raise ValueError("Results contain duplicate or foreign frame IDs")
    crop_counts = Counter(item["kind"] for row in previous for item in row.get("crops", []))
    if any(count > args.max_crops_per_kind for count in crop_counts.values()):
        raise ValueError("Resume crop count exceeds the explicit per-kind budget")
    fingerprints = {}
    for row in previous:
        for item in row.get("crops", []):
            fingerprints.setdefault((row["camera"], item["kind"]), []).append(item["fingerprint"])
    detector = Encoder(args.detector, args.gpu, audit=True, wait_for_idle=True)
    encoder = Encoder(args.model, args.gpu, audit=True, wait_for_idle=True, car_model=args.car_model)
    encoder.bound_car_digest = models["car_model"]
    encoder.bound_base_digest = models["model"]
    proof_path = directory / "execution.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.exists() else {}
    # A legacy generic result cannot establish which feature model ran.
    proof.pop("encoder", None)
    deadline = time.monotonic() + args.max_seconds
    pause = None
    try:
        with results_path.open("a", encoding="utf-8") as output:
            for job in plan["jobs"]:
                if job["id"] in done:
                    continue
                if time.monotonic() >= deadline:
                    pause = "time_budget"
                    break
                row = {**job, "crops": [], "budget_skips": {}}
                try:
                    data = request_bytes(Frigate.base + "/" + job["camera"] + "/recordings/"
                        + str(job["time"]) + "/snapshot.jpg?height=640", limit=4 * 1024 * 1024,
                        timeout=20, bypass_cache=True)
                    Image.MAX_IMAGE_PIXELS = 8_000_000
                    with Image.open(io.BytesIO(data)) as source:
                        if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                            raise ValueError("Oversized archive frame")
                        image = source.convert("RGB")
                    tensor = np.asarray(image.resize((320, 320)), dtype=np.float32) / 255.
                    tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
                    objects = detections(detector.infer(tensor))
                    if detector.audit:
                        proof["detector"] = detector.audit_providers()
                        atomic_json(proof_path, proof)
                        detector.audit = False
                    for index, item in enumerate(objects):
                        if crop_counts[item["kind"]] >= args.max_crops_per_kind:
                            row["budget_skips"][item["kind"]] = row["budget_skips"].get(item["kind"], 0) + 1
                            continue
                        try:
                            crop, fingerprint, detail = crop_image(image, item["box"])
                        except ValueError:
                            continue
                        key = (job["camera"], item["kind"])
                        seen = fingerprints.setdefault(key, [])
                        if job["event"] is None and any((fingerprint ^ old).bit_count() < 7 for old in seen):
                            continue
                        buffer = io.BytesIO()
                        crop.save(buffer, format="JPEG", quality=92)
                        encoders = proof.setdefault("encoders", {})
                        shared_kind = {"cat": "two_wheeler", "two_wheeler": "cat"}.get(item["kind"])
                        if item["kind"] not in encoders and shared_kind in encoders:
                            # Both kinds execute the same digest-bound DINO model.
                            encoders[item["kind"]] = encoders[shared_kind]
                        encoder.audit = item["kind"] not in encoders
                        vector = encoder.encode(buffer.getvalue(), item["kind"])
                        if encoder.audit:
                            proof["encoders"][item["kind"]] = encoder.audit_providers()
                            atomic_json(proof_path, proof)
                            encoder.audit = False
                        filename = f'{job["id"]}-{index}-{item["kind"]}.jpg'
                        (directory / filename).write_bytes(buffer.getvalue())
                        row["crops"].append({**item, "image": filename, "vector": vector,
                            "fingerprint": fingerprint, "detail": detail,
                            # Different events/cameras do not prove different visits.
                            "group_id": None,
                            "confirmed_profile": None})
                        seen.append(fingerprint)
                        crop_counts[item["kind"]] += 1
                    if row["crops"]:
                        (directory / (job["id"] + ".jpg")).write_bytes(data)
                    row["status"] = "sampled"
                except RemoteError as exc:
                    if exc.status == 404:
                        row.update(status="error", error="Recording frame unavailable (404)")
                    else:
                        pause = "RemoteError: " + str(exc)[:160]
                        break
                except RuntimeError as exc:
                    # Priority guards and uncertain infrastructure failures are resumable.
                    pause = type(exc).__name__ + ": " + str(exc)[:160]
                    break
                except (ValueError, OSError) as exc:
                    row.update(status="error", error=type(exc).__name__ + ": " + str(exc)[:160])
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                previous.append(row)
                done.add(job["id"])
                if len(done) % 50 == 0:
                    print(json.dumps({"done": len(done), "planned": len(plan["jobs"]),
                                      "crops": sum(len(x["crops"]) for x in previous)}), flush=True)
    finally:
        detector.unload()
        encoder.unload()
    report = {"planned_frames": len(plan["jobs"]), "finished_frames": len(done), "pause": pause,
        "complete": len(done) == len(plan["jobs"]), "after": plan["after"], "before": plan["before"],
        "interval_seconds": plan["interval"], "event_count": len(plan["events"]),
        "status_counts": dict(Counter(row["status"] for row in previous)),
        "crops": dict(Counter(item["kind"] for row in previous for item in row["crops"])),
        "executed_providers": proof, "coverage": plan["coverage"],
        "max_crops_per_kind": args.max_crops_per_kind,
        "recovered_tail_bytes": recovered_tail_bytes,
        "budget_skips": dict(sum((Counter(row.get("budget_skips", {})) for row in previous), Counter())),
        "detector_vram_mib": detector.load_mib, "encoder_vram_mib": encoder.load_mib,
        "mode": "sparse_archive_proposals_no_label_writes"}
    atomic_json(directory / "report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--after", type=float, required=True)
    parser.add_argument("--before", type=float, required=True)
    parser.add_argument("--event-after", type=float, required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--max-frames", type=int, default=2400)
    parser.add_argument("--max-seconds", type=int, default=2400)
    parser.add_argument("--max-crops-per-kind", type=int, default=600)
    parser.add_argument("--detector", default="/models/yolov9-t-320.onnx")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--car-model", default=DEFAULT_CAR_MODEL)
    parser.add_argument("--gpu", default=os.getenv("FRIGATE_OBJECT_GPU", ""))
    args = parser.parse_args()
    if not (all(math.isfinite(value) for value in (args.after, args.before, args.event_after))
            and 1 <= args.interval <= 3600 and 1 <= args.max_frames <= 20000
            and 1 <= args.max_seconds <= 7200 and 1 <= args.max_crops_per_kind <= 5000
            and args.after < args.before
            and args.before - args.after <= 7 * 86400 and args.event_after <= args.after):
        parser.error("Invalid bounded archive interval/budget")
    scan(args)


if __name__ == "__main__":
    main()
