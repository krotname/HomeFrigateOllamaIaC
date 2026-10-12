#!/usr/bin/env python3
"""Bounded eight-stream RTSP acceptance; output contains no cloud credentials."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import threading
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--allow-reconnect", action="store_true")
    parser.add_argument("--record-main", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path("/tmp/acceptance"))
    parser.add_argument("--host", default="hikconnect-relay")
    options = parser.parse_args()
    if not 15 <= options.seconds <= 3600:
        parser.error("seconds must be between 15 and 3600")
    options.directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = "/usr/lib/ffmpeg/8.0/bin/ffmpeg"
    ffprobe = "/usr/lib/ffmpeg/8.0/bin/ffprobe"
    started = time.monotonic()
    state, guard = {}, threading.Lock()

    def check(alias: str, quality: str) -> dict:
        name, attempts, errors, frames, duration, size = alias + "_" + quality, 0, 0, 0, 0.0, 0
        deadline = started + options.seconds
        while time.monotonic() < deadline:
            attempts += 1
            remaining = options.seconds if attempts == 1 else max(1, deadline - time.monotonic())
            destination = options.directory / f"{name}-{attempts}.mkv"
            command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                       "-timeout", "15000000", "-threads", "1", "-i", f"rtsp://{options.host}:8554/{name}",
                       "-t", str(remaining), "-map", "0:v:0", "-threads", "1", "-an", "-progress", "pipe:1"]
            # Every stream is decoded. Main recordings are a separate copy output.
            command += ["-f", "null", "-"]
            if quality == "main" and options.record_main:
                command += ["-t", str(remaining), "-map", "0:v:0", "-c:v", "copy", "-an",
                            "-f", "matroska", str(destination)]
            error_path = options.directory / f"{name}-{attempts}-errors.txt"
            with error_path.open("wb") as error_file:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file, text=True)
                watchdog = threading.Timer(remaining + 45, child.kill)
                watchdog.start()
                progress = {}
                try:
                    for line in child.stdout:
                        key, _, value = line.strip().partition("=")
                        progress[key] = value
                        if key == "progress":
                            with guard:
                                state[name] = {"attempts": attempts, "frames": frames + int(progress.get("frame", 0)),
                                               "media_seconds": round(duration + int(progress.get("out_time_us", 0)) / 1e6, 2)}
                    exit_code = child.wait(timeout=5)
                finally:
                    watchdog.cancel()
                    if child.poll() is None:
                        child.kill()
                    child.wait()
                attempt_frames = int(progress.get("frame", 0))
                attempt_duration = max(0, int(progress.get("out_time_us", 0))) / 1e6
                frames += attempt_frames
                duration += attempt_duration
            attempt_errors = sum(1 for line in error_path.read_bytes().splitlines() if line.strip())
            errors += attempt_errors
            if destination.exists() and destination.stat().st_size > 0:
                size += destination.stat().st_size
                if exit_code == 0:
                    metadata = json.loads(subprocess.check_output(
                        [ffprobe, "-v", "error", "-show_entries", "format=duration,size",
                         "-of", "json", str(destination)], timeout=15))["format"]
                    with guard:
                        state[name]["recording_seconds"] = round(float(metadata["duration"]), 2)
            if not options.allow_reconnect or time.monotonic() >= deadline:
                break
            time.sleep(1)
        return {"stream": name, "exit": exit_code, "attempts": attempts, "error_lines": errors,
                "frames": frames, "media_seconds": round(duration, 2), "bytes": size,
                "last_attempt_frames": attempt_frames, "last_attempt_media_seconds": round(attempt_duration, 2),
                "last_attempt_error_lines": attempt_errors,
                "three_day_gib": round(size / duration * 259200 / 2**30, 2) if size and duration > 0 else None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(check, f"common_camera{i:02d}", quality)
                   for i in range(1, 5) for quality in ("main", "sub")]
        while not all(future.done() for future in futures):
            with guard:
                snapshot = {"wall_seconds": round(time.monotonic() - started), "streams": dict(state)}
            (options.directory / "progress.json").write_text(json.dumps(snapshot))
            time.sleep(5)
        results = [future.result() for future in futures]
    report = {"wall_seconds": round(time.monotonic() - started, 2), "results": results}
    (options.directory / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    if any(row["exit"] or (options.allow_reconnect and
           (row["last_attempt_frames"] <= 0 or row["last_attempt_media_seconds"] < min(15, options.seconds / 3)
            or row["last_attempt_error_lines"] or row["media_seconds"] < options.seconds / 2))
           or (not options.allow_reconnect and
           (row["attempts"] != 1 or row["error_lines"] or row["media_seconds"] < options.seconds - 5)) for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
