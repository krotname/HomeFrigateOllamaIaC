import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from faster_whisper import WhisperModel


MODEL_NAME = os.getenv("ASR_MODEL", "Systran/faster-whisper-large-v3")
DEVICE = os.getenv("ASR_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8")
CPU_THREADS = int(os.getenv("ASR_CPU_THREADS", "4"))
HOST = os.getenv("ASR_HOST", "0.0.0.0")
PORT = int(os.getenv("ASR_PORT", "9443"))
CERT_FILE = os.getenv("ASR_CERT_FILE", "/certs/fullchain.pem")
KEY_FILE = os.getenv("ASR_KEY_FILE", "/certs/privkey.pem")
TMP_DIR = Path(os.getenv("ASR_TMP_DIR", "/tmp/asr"))

app = FastAPI(title="Home ASR", version="1.0")
_model_lock = threading.Lock()
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


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form("ru"),
    task: str = Form("transcribe"),
    response_format: str = Form("json"),
    prompt: Optional[str] = Form(None),
    vad_filter: bool = Form(True),
    word_timestamps: bool = Form(False),
):
    if task not in {"transcribe", "translate"}:
        raise HTTPException(status_code=400, detail="task must be transcribe or translate")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "audio").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR) as temp:
        temp_path = Path(temp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            temp.write(chunk)

    try:
        model = get_model()
        segments_iter, info = model.transcribe(
            str(temp_path),
            language=language or None,
            task=task,
            initial_prompt=prompt,
            beam_size=5,
            vad_filter=vad_filter,
            word_timestamps=word_timestamps,
        )
        segments = [
            {
                "id": index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
            }
            for index, segment in enumerate(segments_iter)
        ]
        text = "".join(segment["text"] for segment in segments).strip()
        if response_format == "text":
            return PlainTextResponse(text)
        if response_format not in {"json", "verbose_json"}:
            raise HTTPException(status_code=400, detail="response_format must be json, verbose_json, or text")
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
            temp_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        ssl_certfile=CERT_FILE,
        ssl_keyfile=KEY_FILE,
        log_level=os.getenv("ASR_LOG_LEVEL", "info"),
    )
