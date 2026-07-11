import base64
import contextlib
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def integer_setting(name, default):
    raw_value = os.getenv(name, str(default)).strip()
    if not raw_value:
        raw_value = str(default)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw_value!r}.") from exc


SOURCE_VALUE = os.getenv("OLLAMA_TRANSCRIBE_SOURCE", "").strip()
SOURCE = Path(SOURCE_VALUE) if SOURCE_VALUE else None
OUT_DIR = Path(os.getenv("OLLAMA_TRANSCRIBE_OUT_DIR", r"C:\develop\CODEX\ollama-audio-transcription"))
SSH_KEY = Path(os.getenv("OLLAMA_TRANSCRIBE_SSH_KEY", Path.home() / ".ssh" / "win-home-codex_ed25519"))
SSH_KNOWN_HOSTS = Path(
    os.getenv("OLLAMA_TRANSCRIBE_KNOWN_HOSTS", Path.home() / ".ssh" / "known_hosts")
)
SSH_TARGET = os.getenv("OLLAMA_TRANSCRIBE_SSH_TARGET", "krt@192.168.1.138")
LOCAL_PORT = integer_setting("OLLAMA_TRANSCRIBE_LOCAL_PORT", 11435)
REMOTE_PORT = integer_setting("OLLAMA_TRANSCRIBE_REMOTE_PORT", 11435)
MODEL = os.getenv("OLLAMA_TRANSCRIBE_MODEL", "").strip()
CHUNK_SECONDS = integer_setting("OLLAMA_TRANSCRIBE_CHUNK_SECONDS", 60)
API = f"http://127.0.0.1:{LOCAL_PORT}"
MAX_CHUNKS = integer_setting("OLLAMA_TRANSCRIBE_MAX_CHUNKS", 0)
MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TRANSCRIPT_TEXT_CHARS = 1_000_000


def run(args, timeout=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)


@contextlib.contextmanager
def output_directory_lock(directory):
    lock_path = directory / ".transcription.lock"
    with lock_path.open("a+b") as lock_file:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Output directory is already in use: {directory}") from exc

        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ffprobe_duration(path):
    result = run([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid media duration: {duration}")
    return duration


def stop_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def local_port_is_open():
    try:
        with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.25):
            return True
    except OSError:
        return False


def wait_for_tunnel(proc):
    for _ in range(60):
        if proc.poll() is not None:
            error = (proc.stderr.read() if proc.stderr else "").strip()
            raise RuntimeError(f"SSH tunnel exited before API became reachable: {error}")
        try:
            version_info = request_json("/api/version", None, timeout=3)
            if not isinstance(version_info, dict) or not isinstance(
                version_info.get("version"), str
            ):
                raise RuntimeError("Forwarded service is not an Ollama API")
            if proc.poll() is not None:
                error = (proc.stderr.read() if proc.stderr else "").strip()
                raise RuntimeError(f"SSH tunnel exited during startup: {error}")
            return proc
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
        ):
            if proc.poll() is not None:
                error = (proc.stderr.read() if proc.stderr else "").strip()
                raise RuntimeError(f"SSH tunnel exited before API became reachable: {error}")
            time.sleep(1)
    raise TimeoutError("Ollama API did not become reachable through SSH tunnel")


def start_tunnel():
    if local_port_is_open():
        raise RuntimeError(f"Local port {LOCAL_PORT} is already in use")
    args = [
        "ssh",
        "-i",
        str(SSH_KEY),
        "-o",
        "LogLevel=ERROR",
        "-o",
        "BatchMode=yes",
        "-o",
        "CertificateFile=none",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-L",
        f"127.0.0.1:{LOCAL_PORT}:127.0.0.1:{REMOTE_PORT}",
        "-N",
        SSH_TARGET,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        return wait_for_tunnel(proc)
    except BaseException:
        stop_process(proc)
        raise


def request_json(path, payload, timeout=900):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise ValueError("Ollama returned an invalid Content-Length header") from exc
            if declared_length > MAX_API_RESPONSE_BYTES:
                raise ValueError("Ollama response exceeds the configured size limit")
        raw_response = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(raw_response) > MAX_API_RESPONSE_BYTES:
            raise ValueError("Ollama response exceeds the configured size limit")
        return json.loads(raw_response.decode("utf-8"))


def warm_model():
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply OK only."}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_predict": 8},
    }
    response = request_json("/api/chat", payload, timeout=900)
    if not isinstance(response, dict) or response.get("done") is not True:
        raise RuntimeError("Ollama did not complete the model warm-up request")
    return response


def make_chunk(start_seconds, duration, out_path):
    run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(SOURCE),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_path),
    ], timeout=120)


def transcribe_chunk(wav_path):
    audio = base64.b64encode(wav_path.read_bytes()).decode("ascii")
    prompt = (
        "Transcribe the Russian speech in this audio verbatim. "
        "Do not translate, summarize, correct meaning, or add comments. "
        "If a word is unclear, write [неразборчиво]. "
        "Output only the spoken words."
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [audio]}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            "num_predict": 900,
            "stop": [
                "Transcribe",
                "Output only",
                "Расшифруй",
                "расшифруй",
                "Выведи",
                "выведи",
            ],
        },
    }
    return request_json("/api/chat", payload, timeout=900)


def timestamp(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_json_atomic(path, payload):
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp_path, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebuild_jsonl(path, records):
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as jsonl:
        for index in sorted(records):
            jsonl.write(json.dumps(records[index], ensure_ascii=False) + "\n")
        jsonl.flush()
        os.fsync(jsonl.fileno())
    os.replace(temp_path, path)


def rebuild_text_transcript(path, records):
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as transcript:
        for index in sorted(records):
            record = records[index]
            transcript.write(
                f"[{timestamp(record['start'])}-{timestamp(record['end'])}]\n"
                f"{record['text']}\n\n"
            )
        transcript.flush()
        os.fsync(transcript.fileno())
    os.replace(temp_path, path)


def main():
    if not MODEL:
        raise SystemExit("Set OLLAMA_TRANSCRIBE_MODEL to an audio-capable Ollama model.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?", MODEL):
        raise SystemExit("OLLAMA_TRANSCRIBE_MODEL contains unsupported characters.")

    if SOURCE is None:
        raise SystemExit("Set OLLAMA_TRANSCRIBE_SOURCE to the audio file path.")
    if not SOURCE.is_file():
        raise SystemExit(f"Source audio file not found: {SOURCE}")
    if not 1 <= LOCAL_PORT <= 65535:
        raise SystemExit("OLLAMA_TRANSCRIBE_LOCAL_PORT must be in range 1..65535.")
    if not 1 <= REMOTE_PORT <= 65535:
        raise SystemExit("OLLAMA_TRANSCRIBE_REMOTE_PORT must be in range 1..65535.")
    if not 1 <= CHUNK_SECONDS <= 600:
        raise SystemExit("OLLAMA_TRANSCRIBE_CHUNK_SECONDS must be in range 1..600.")
    if MAX_CHUNKS < 0:
        raise SystemExit("OLLAMA_TRANSCRIBE_MAX_CHUNKS must not be negative.")
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]{0,63}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}",
        SSH_TARGET,
    ):
        raise SystemExit(
            "OLLAMA_TRANSCRIBE_SSH_TARGET must be a safe user@IPv4-or-DNS target."
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with output_directory_lock(OUT_DIR):
        return transcribe_locked()


def transcribe_locked():
    progress_path = OUT_DIR / "progress.log"
    jsonl_path = OUT_DIR / "transcript_chunks.jsonl"
    txt_path = OUT_DIR / "transcript_ollama_audio.txt"

    duration = ffprobe_duration(SOURCE)
    total_chunks = math.ceil(duration / CHUNK_SECONDS)

    metadata_path = OUT_DIR / "run-metadata.json"
    source_stat = SOURCE.stat()
    expected_metadata = {
        "source": str(SOURCE.resolve()),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "source_sha256": sha256_file(SOURCE),
        "model": MODEL,
        "chunk_seconds": CHUNK_SECONDS,
        "duration": round(duration, 6),
    }
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid run metadata: {exc}") from exc
        if metadata != expected_metadata:
            raise SystemExit(
                "Output directory contains a transcript for a different source, model, or chunk size."
            )
    elif jsonl_path.exists() and jsonl_path.stat().st_size:
        raise SystemExit(
            "Existing transcript has no run-metadata.json; use a new output directory to avoid mixing runs."
        )
    else:
        write_json_atomic(metadata_path, expected_metadata)

    records = {}
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                index = record["index"]
                if type(index) is not int or not 0 <= index < total_chunks:
                    raise ValueError("invalid chunk index")
                if index in records:
                    raise ValueError(f"duplicate chunk index {index}")
                start = record["start"]
                end = record["end"]
                expected_start = index * CHUNK_SECONDS
                expected_end = min(duration, expected_start + CHUNK_SECONDS)
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, (int, float))
                    or not isinstance(end, (int, float))
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                    or start < 0
                    or end <= start
                    or not isinstance(record["text"], str)
                    or len(record["text"]) > MAX_TRANSCRIPT_TEXT_CHARS
                    or not math.isclose(start, expected_start, abs_tol=0.001)
                    or not math.isclose(end, expected_end, abs_tol=0.001)
                ):
                    raise ValueError("invalid chunk payload")
                records[index] = record
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise SystemExit(f"Invalid transcript record: {exc}") from exc
    rebuild_text_transcript(txt_path, records)

    if len(records) == total_chunks:
        with progress_path.open("a", encoding="utf-8") as progress:
            progress.write(f"ALREADY_COMPLETE chunks={total_chunks}\n")
        return
    if not SSH_KEY.is_file():
        raise SystemExit(f"SSH key not found: {SSH_KEY}")
    if not SSH_KNOWN_HOSTS.is_file():
        raise SystemExit(f"SSH known_hosts file not found: {SSH_KNOWN_HOSTS}")

    tunnel = start_tunnel()
    try:
        with progress_path.open("a", encoding="utf-8") as progress:
            progress.write(f"START duration={duration:.2f} chunks={total_chunks} model={MODEL}\n")
            progress.flush()
            warm = warm_model()
            progress.write(f"WARM done={warm.get('done')} duration_ns={warm.get('total_duration')}\n")
            progress.flush()

            newly_done = 0
            for index in range(total_chunks):
                if index in records:
                    continue
                if MAX_CHUNKS and newly_done >= MAX_CHUNKS:
                    progress.write(f"STOP max_chunks={MAX_CHUNKS}\n")
                    progress.flush()
                    break
                start = index * CHUNK_SECONDS
                chunk_duration = min(CHUNK_SECONDS, max(0, duration - start))
                wav_path = OUT_DIR / f"chunk_{index:03d}.wav"
                make_chunk(start, chunk_duration, wav_path)
                attempt = 0
                while True:
                    attempt += 1
                    t0 = time.time()
                    try:
                        response = transcribe_chunk(wav_path)
                        elapsed = time.time() - t0
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                        if attempt >= 3:
                            raise
                        progress.write(f"RETRY index={index} attempt={attempt} error={exc}\n")
                        progress.flush()
                        warm_model()

                if response.get("done") is not True:
                    raise RuntimeError(f"Ollama returned an incomplete response for chunk {index}")
                content = response.get("message", {}).get("content")
                if not isinstance(content, str):
                    raise RuntimeError(f"Ollama returned no text content for chunk {index}")
                text = content.strip()
                record = {
                    "index": index,
                    "start": start,
                    "end": start + chunk_duration,
                    "elapsed": round(elapsed, 2),
                    "text": text,
                    "raw": {k: response.get(k) for k in ("done", "done_reason", "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")},
                }
                records[index] = record
                rebuild_jsonl(jsonl_path, records)
                rebuild_text_transcript(txt_path, records)
                try:
                    wav_path.unlink()
                except FileNotFoundError:
                    pass
                progress.write(f"DONE {index + 1}/{total_chunks} elapsed={elapsed:.2f}s\n")
                progress.flush()
                newly_done += 1

            if len(records) == total_chunks:
                progress.write("COMPLETE\n")
                progress.flush()
    finally:
        stop_process(tunnel)
        for wav_path in OUT_DIR.glob("chunk_*.wav"):
            try:
                wav_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    if SOURCE is None:
        print("missing source: set OLLAMA_TRANSCRIBE_SOURCE", file=sys.stderr)
        raise SystemExit(2)
    if not SOURCE.exists():
        print(f"missing source: {SOURCE}", file=sys.stderr)
        raise SystemExit(2)
    main()
