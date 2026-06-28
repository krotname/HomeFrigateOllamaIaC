import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


SOURCE_VALUE = os.getenv("OLLAMA_TRANSCRIBE_SOURCE", "").strip()
SOURCE = Path(SOURCE_VALUE) if SOURCE_VALUE else None
OUT_DIR = Path(os.getenv("OLLAMA_TRANSCRIBE_OUT_DIR", r"C:\develop\CODEX\ollama-audio-transcription"))
SSH_KEY = Path.home() / ".ssh" / "win-home-codex_ed25519"
SSH_TARGET = os.getenv("OLLAMA_TRANSCRIBE_SSH_TARGET", "krt@192.168.1.138")
LOCAL_PORT = int(os.getenv("OLLAMA_TRANSCRIBE_LOCAL_PORT", "11435"))
MODEL = os.getenv("OLLAMA_TRANSCRIBE_MODEL", "").strip()
CHUNK_SECONDS = int(os.getenv("OLLAMA_TRANSCRIBE_CHUNK_SECONDS", "60"))
API = f"http://127.0.0.1:{LOCAL_PORT}"
MAX_CHUNKS = int(os.getenv("OLLAMA_TRANSCRIBE_MAX_CHUNKS", "0") or "0")


def run(args, timeout=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)


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
    return float(result.stdout.strip())


def start_tunnel():
    args = [
        "ssh",
        "-i",
        str(SSH_KEY),
        "-o",
        "LogLevel=ERROR",
        "-o",
        "CertificateFile=none",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=NUL",
        "-L",
        f"127.0.0.1:{LOCAL_PORT}:127.0.0.1:11434",
        "-N",
        SSH_TARGET,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            request_json("/api/version", None, timeout=3)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("SSH tunnel exited before API became reachable")
            time.sleep(1)
    proc.terminate()
    raise TimeoutError("Ollama API did not become reachable through SSH tunnel")


def request_json(path, payload, timeout=900):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def warm_model():
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply OK only."}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_predict": 8},
    }
    return request_json("/api/chat", payload, timeout=900)


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


def main():
    if not MODEL:
        raise SystemExit("Set OLLAMA_TRANSCRIBE_MODEL to an audio-capable Ollama model.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    progress_path = OUT_DIR / "progress.log"
    jsonl_path = OUT_DIR / "transcript_chunks.jsonl"
    txt_path = OUT_DIR / "transcript_ollama_audio.txt"
    if SOURCE is None:
        raise SystemExit("Set OLLAMA_TRANSCRIBE_SOURCE to the audio file path.")

    duration = ffprobe_duration(SOURCE)
    total_chunks = int((duration + CHUNK_SECONDS - 0.001) // CHUNK_SECONDS)

    completed = set()
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                completed.add(json.loads(line)["index"])
            except Exception:
                pass

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
                if index in completed:
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
                    except (urllib.error.URLError, TimeoutError, TimeoutError) as exc:
                        if attempt >= 3:
                            raise
                        progress.write(f"RETRY index={index} attempt={attempt} error={exc}\n")
                        progress.flush()
                        warm_model()

                text = response.get("message", {}).get("content", "").strip()
                record = {
                    "index": index,
                    "start": start,
                    "end": start + chunk_duration,
                    "elapsed": round(elapsed, 2),
                    "text": text,
                    "raw": {k: response.get(k) for k in ("done", "done_reason", "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")},
                }
                with jsonl_path.open("a", encoding="utf-8") as jsonl:
                    jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                with txt_path.open("a", encoding="utf-8") as txt:
                    txt.write(f"[{timestamp(start)}-{timestamp(start + chunk_duration)}]\n{text}\n\n")
                try:
                    wav_path.unlink()
                except FileNotFoundError:
                    pass
                progress.write(f"DONE {index + 1}/{total_chunks} elapsed={elapsed:.2f}s\n")
                progress.flush()
                newly_done += 1

            if not MAX_CHUNKS or newly_done < MAX_CHUNKS:
                progress.write("COMPLETE\n")
                progress.flush()
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()


if __name__ == "__main__":
    if SOURCE is None:
        print("missing source: set OLLAMA_TRANSCRIBE_SOURCE", file=sys.stderr)
        raise SystemExit(2)
    if not SOURCE.exists():
        print(f"missing source: {SOURCE}", file=sys.stderr)
        raise SystemExit(2)
    main()
