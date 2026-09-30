import asyncio
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from faster_whisper import WhisperModel
from starlette.concurrency import run_in_threadpool


MODEL_NAME = os.getenv("ASR_MODEL", "Systran/faster-whisper-large-v3")
DEVICE = os.getenv("ASR_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8")
HOST = os.getenv("ASR_HOST", "0.0.0.0")
CERT_FILE = os.getenv("ASR_CERT_FILE") or None
KEY_FILE = os.getenv("ASR_KEY_FILE") or None
TMP_DIR = Path(os.getenv("ASR_TMP_DIR", "/tmp/asr"))


def bounded_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be in range {minimum}..{maximum}")
    return value


CPU_THREADS = bounded_int_setting("ASR_CPU_THREADS", 4, 1, 256)
PORT = bounded_int_setting("ASR_PORT", 9443, 1, 65535)
MAX_UPLOAD_BYTES = bounded_int_setting(
    "ASR_MAX_UPLOAD_BYTES", 100 * 1024 * 1024, 1024, 1024 * 1024 * 1024
)
MAX_CONCURRENT_TRANSCRIPTIONS = bounded_int_setting(
    "ASR_MAX_CONCURRENT_TRANSCRIPTIONS", 1, 1, 8
)

app = FastAPI(title="Home ASR", version="1.0")
_model_lock = threading.Lock()
_transcription_slots = threading.BoundedSemaphore(MAX_CONCURRENT_TRANSCRIPTIONS)
_model: Optional[WhisperModel] = None


def get_model() -> WhisperModel:
    global _model
    with _model_lock:
        if _model is None:
            _model = WhisperModel(
                MODEL_NAME,
                device=DEVICE,
                compute_type=COMPUTE_TYPE,
                cpu_threads=CPU_THREADS,
            )
        return _model


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "loaded": _model is not None,
    }


def safe_upload_suffix(filename: Optional[str]) -> str:
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return ".audio"


def validate_request(
    language: str,
    task: str,
    response_format: str,
    prompt: Optional[str],
) -> Optional[str]:
    if task not in {"transcribe", "translate"}:
        raise HTTPException(status_code=400, detail="task must be transcribe or translate")
    if response_format not in {"json", "verbose_json", "text"}:
        raise HTTPException(
            status_code=400,
            detail="response_format must be json, verbose_json, or text",
        )
    normalized_language = language.strip().lower()
    if normalized_language and not re.fullmatch(r"[a-z]{2,8}", normalized_language):
        raise HTTPException(status_code=400, detail="Invalid language code")
    if prompt is not None and len(prompt) > 4000:
        raise HTTPException(status_code=400, detail="prompt must not exceed 4000 characters")
    # An empty form field is indistinguishable from a missing one, which takes the "ru"
    # default, so clients request language detection explicitly with "auto".
    if normalized_language == "auto":
        return None
    return normalized_language or None


def run_transcription(
    temp_path: Path,
    language: Optional[str],
    task: str,
    prompt: Optional[str],
    vad_filter: bool,
    word_timestamps: bool,
) -> tuple[list[dict], object]:
    # The caller holds a transcription slot for the whole call, including the lazy
    # segments iterator: several concurrent runs can exhaust GPU memory.
    model = get_model()
    segments_iter, info = model.transcribe(
        str(temp_path),
        language=language,
        task=task,
        initial_prompt=prompt,
        beam_size=5,
        vad_filter=vad_filter,
        word_timestamps=word_timestamps,
    )
    segments = []
    for index, segment in enumerate(segments_iter):
        item = {
            "id": index,
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
        }
        if word_timestamps:
            item["words"] = [
                {
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                    "word": word.word.strip(),
                    "probability": round(word.probability, 6),
                }
                for word in (segment.words or [])
            ]
        segments.append(item)
    return segments, info


async def acquire_transcription_slot(request: Request) -> bool:
    # Wait on the event loop rather than in a worker thread, and give up once the
    # client has gone. A realtime caller times out while a long batch job holds the
    # GPU; transcribing its abandoned segment afterwards only delays the next one.
    while not _transcription_slots.acquire(blocking=False):
        if await request.is_disconnected():
            return False
        await asyncio.sleep(0.5)
    return True


@app.post("/v1/audio/transcriptions")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("ru"),
    task: str = Form("transcribe"),
    response_format: str = Form("json"),
    prompt: Optional[str] = Form(None),
    vad_filter: bool = Form(True),
    word_timestamps: bool = Form(False),
):
    safe_language = validate_request(language, task, response_format, prompt)
    temp_path: Optional[Path] = None
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=safe_upload_suffix(file.filename),
            dir=TMP_DIR,
        ) as temp:
            temp_path = Path(temp.name)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Audio upload is too large")
                temp.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Audio upload is empty")

        if not await acquire_transcription_slot(request):
            raise HTTPException(status_code=503, detail="Client disconnected while queued")
        try:
            segments, info = await run_in_threadpool(
                run_transcription,
                temp_path,
                safe_language,
                task,
                prompt,
                vad_filter,
                word_timestamps,
            )
        finally:
            _transcription_slots.release()
        text = " ".join(segment["text"] for segment in segments if segment["text"]).strip()
        if response_format == "text":
            return PlainTextResponse(text)
        payload = {
            "text": text,
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration,
        }
        if response_format == "verbose_json":
            payload["segments"] = segments
        return JSONResponse(payload)
    finally:
        try:
            if temp_path is not None:
                temp_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            await file.close()


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        ssl_certfile=CERT_FILE,
        ssl_keyfile=KEY_FILE,
        log_level=os.getenv("ASR_LOG_LEVEL", "info"),
    )
