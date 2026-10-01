import asyncio
import base64
import io
import json
import logging
import math
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool


HOST = os.getenv("OCR_HOST", "0.0.0.0")
# "uid:gid" to switch to once the listening socket is open. Set when the container
# starts as root only to bind a privileged port (443); the service never runs as root.
RUN_AS = os.getenv("OCR_RUN_AS", "").strip()
CERT_FILE = os.getenv("OCR_CERT_FILE") or None
KEY_FILE = os.getenv("OCR_KEY_FILE") or None
TMP_DIR = Path(os.getenv("OCR_TMP_DIR", "/tmp/ocr"))
LLM_URL = os.getenv("OCR_LLM_URL", "http://127.0.0.1:18090").rstrip("/")
MODEL_NAME = os.getenv("OCR_MODEL_NAME", "Qwen3-VL-2B-Instruct-Q8_0")
FONT_FILE = os.getenv("OCR_FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

# Measured on the P40 (2026-09-30): the plain prompt reads a synthetic Russian A4 page
# at CER 0.07 %, the line-spotting prompt at 0.09 % and also returns line boxes.
TEXT_PROMPT = (
    "Extract all text from the image exactly as written, line by line. Output only the text."
)
SPOT_PROMPT = (
    "Spot all the text in the image with line-level, and output in JSON format as "
    '[{"bbox_2d": [x1, y1, x2, y2], "text_content": "..."}].'
)
# Qwen3-VL returns boxes on a 0..1000 grid relative to the image it was given.
BOX_GRID = 1000.0
FORMATS = {"text", "text-json", "json", "pdf"}
PAGE_SPEC = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")
SPOT_ITEM = re.compile(
    r'"bbox_2d"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,'
    r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]\s*,\s*'
    r'"text_content"\s*:\s*"((?:[^"\\]|\\.)*)"'
)
FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


def bounded_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be in range {minimum}..{maximum}")
    return value


PORT = bounded_int_setting("OCR_PORT", 19444, 1, 65535)
MAX_UPLOAD_BYTES = bounded_int_setting(
    "OCR_MAX_UPLOAD_BYTES", 100 * 1024 * 1024, 1024, 1024 * 1024 * 1024
)
MAX_PAGES = bounded_int_setting("OCR_MAX_PAGES", 200, 1, 5000)
MAX_CONCURRENT_JOBS = bounded_int_setting("OCR_MAX_CONCURRENT_JOBS", 1, 1, 8)
DEFAULT_DPI = bounded_int_setting("OCR_DEFAULT_DPI", 200, 72, 400)
# 4096 image tokens of 32x32 pixels; llama-server enforces the same ceiling with
# --image-max-tokens, so a larger picture only costs VRAM, never accuracy we keep.
MAX_PIXELS = bounded_int_setting("OCR_MAX_PIXELS", 4096 * 32 * 32, 65536, 16 * 1024 * 1024)
MAX_TOKENS = bounded_int_setting("OCR_MAX_TOKENS", 6144, 256, 32768)
LLM_TIMEOUT = bounded_int_setting("OCR_LLM_TIMEOUT", 600, 10, 3600)

log = logging.getLogger("home-ocr")
app = FastAPI(title="Home OCR", version="1.0")
_job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
# The model sits on loopback; an inherited HTTP(S)_PROXY must never capture it.
_llm_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class PageImage:
    number: int
    png: bytes
    width: int
    height: int
    unit: str
    unit_scale: float
    lines: list = field(default_factory=list)
    text: str = ""
    truncated: bool = False


def llm_status() -> str:
    try:
        with _llm_opener.open(LLM_URL + "/health", timeout=5) as response:
            status = json.load(response).get("status", "unknown")
    except urllib.error.HTTPError as exc:
        return "loading" if exc.code == 503 else f"http {exc.code}"
    except (OSError, ValueError):
        return "down"
    return "ok" if status == "ok" else str(status)


@app.get("/health")
def health():
    llm = llm_status()
    payload = {"status": "ok" if llm == "ok" else "degraded", "model": MODEL_NAME, "llm": llm}
    return JSONResponse(payload, status_code=200 if llm == "ok" else 503)


def validate_request(response_format: str, pages: Optional[str], dpi: Optional[int]) -> str:
    normalized = (response_format or "").strip().lower()
    if normalized not in FORMATS:
        raise HTTPException(status_code=400, detail="format must be text, text-json, json, or pdf")
    if pages is not None and pages.strip() and not PAGE_SPEC.fullmatch(pages.strip()):
        raise HTTPException(status_code=400, detail="pages must look like 1-3,5")
    if dpi is not None and not 72 <= dpi <= 400:
        raise HTTPException(status_code=400, detail="dpi must be in range 72..400")
    return normalized


def select_pages(spec: Optional[str], count: int) -> list[int]:
    """Return zero-based page indexes for a 1-based spec such as 1-3,5."""
    if count <= 0:
        raise ValueError("The document has no pages")
    if spec is None or not spec.strip():
        chosen = list(range(count))
    else:
        chosen = []
        for part in spec.strip().split(","):
            first, _, last = part.partition("-")
            start, end = int(first), int(last or first)
            if start < 1 or end < start or end > count:
                raise ValueError(f"Page range {part} is outside 1..{count}")
            chosen.extend(index for index in range(start - 1, end) if index not in chosen)
    if len(chosen) > MAX_PAGES:
        raise ValueError(f"At most {MAX_PAGES} pages per request")
    return chosen


def fit_scale(width: float, height: float, preferred: float, max_pixels: int) -> float:
    """Largest scale up to preferred that keeps the rendered image within max_pixels."""
    if width <= 0 or height <= 0:
        raise ValueError("Empty page")
    limit = math.sqrt(max_pixels / (width * height))
    return min(preferred, limit)


def parse_spotting(content: str) -> list[dict]:
    """Read line boxes from the model; tolerate a truncated or loosely formatted array."""
    items = []
    for match in SPOT_ITEM.finditer(content):
        x1, y1, x2, y2 = (float(value) for value in match.groups()[:4])
        raw_text = match.group(5)
        try:
            text = json.loads('"' + raw_text + '"')
        except ValueError:
            text = raw_text
        items.append({"text": text, "bbox": [x1, y1, x2, y2]})
    return items


def scale_lines(items: list[dict], width: int, height: int, unit_scale: float) -> list[dict]:
    """Convert 0..1000 grid boxes to output units, clamped to the page and ordered."""
    lines = []
    for item in items:
        x1, y1, x2, y2 = item["bbox"]
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        box = [
            min(max(left, 0.0), BOX_GRID) / BOX_GRID * width * unit_scale,
            min(max(top, 0.0), BOX_GRID) / BOX_GRID * height * unit_scale,
            min(max(right, 0.0), BOX_GRID) / BOX_GRID * width * unit_scale,
            min(max(bottom, 0.0), BOX_GRID) / BOX_GRID * height * unit_scale,
        ]
        text = item["text"].strip()
        if text:
            lines.append({"text": text, "bbox": [round(value, 2) for value in box]})
    return lines


def clean_text(content: str) -> str:
    return FENCE.sub("", content.strip()).strip()


def ask_model(png: bytes, prompt: str, *, repeat_penalty: Optional[float] = None) -> tuple[str, bool]:
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "cache_prompt": False,
    }
    if repeat_penalty is not None:
        body.update({"repeat_penalty": repeat_penalty, "repeat_last_n": 256})
    request = urllib.request.Request(
        LLM_URL + "/v1/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with _llm_opener.open(request, timeout=LLM_TIMEOUT) as response:
        choice = json.load(response)["choices"][0]
    return choice["message"].get("content") or "", choice.get("finish_reason") == "length"


def recognize(page: PageImage, response_format: str) -> PageImage:
    if response_format in {"text", "text-json"}:
        content, page.truncated = ask_model(page.png, TEXT_PROMPT)
        if response_format == "text-json" and page.truncated:
            # Greedy decoding can loop even on a small screenshot fragment.
            # Retry only incomplete text; keep the calibrated first pass intact.
            content, page.truncated = ask_model(page.png, TEXT_PROMPT, repeat_penalty=1.1)
        page.text = clean_text(content)
        return page
    content, page.truncated = ask_model(page.png, SPOT_PROMPT)
    page.lines = scale_lines(parse_spotting(content), page.width, page.height, page.unit_scale)
    page.text = "\n".join(line["text"] for line in page.lines)
    return page


def png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Source:
    """A PDF or a (possibly multi-frame) image, rendered one page at a time."""

    def __init__(self, path: Path, dpi: int):
        self.path = path
        self.dpi = dpi
        self.document = None
        self.image = None
        with path.open("rb") as stream:
            self.kind = "pdf" if stream.read(5) == b"%PDF-" else "image"
        if self.kind == "pdf":
            try:
                self.document = pymupdf.open(str(path))
            except RuntimeError as exc:
                raise ValueError("The PDF cannot be opened") from exc
            if self.document.needs_pass:
                raise ValueError("Encrypted PDFs are not supported")
            self.count = self.document.page_count
            return
        try:
            # Frames are decoded one at a time: a long multi-page TIFF must not sit in RAM.
            self.image = Image.open(path)
            self.count = getattr(self.image, "n_frames", 1)
            self.frame(0)
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("Upload is neither a PDF nor a readable image") from exc
        self.image_format = self.image.format or "PNG"
        declared = (self.image.info.get("dpi") or (0,))[0]
        self.image_dpi = float(declared) if 72 <= float(declared or 0) <= 1200 else float(DEFAULT_DPI)

    def frame(self, index: int):
        self.image.seek(index)
        return ImageOps.exif_transpose(self.image).convert("RGB")

    def render(self, index: int) -> PageImage:
        if self.kind == "pdf":
            page = self.document[index]
            rect = page.rect
            zoom = fit_scale(rect.width, rect.height, self.dpi / 72.0, MAX_PIXELS)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return PageImage(index + 1, pixmap.tobytes("png"), pixmap.width, pixmap.height,
                             "pt", 1.0 / zoom)
        frame = self.frame(index)
        scale = fit_scale(frame.width, frame.height, 1.0, MAX_PIXELS)
        model_image = frame
        if scale < 1.0:
            size = (max(1, int(frame.width * scale)), max(1, int(frame.height * scale)))
            model_image = frame.resize(size, Image.LANCZOS)
        return PageImage(index + 1, png_bytes(model_image), model_image.width,
                         model_image.height, "px", frame.width / model_image.width)

    def close(self) -> None:
        if self.document is not None:
            self.document.close()
        if self.image is not None:
            self.image.close()


def has_text_layer(page) -> bool:
    return len(page.get_text("text").strip()) >= 20


def add_text_layer(page, lines: list[dict], font, points_per_unit: float) -> None:
    """Write invisible text into each line box so the page becomes searchable."""
    for line in lines:
        x0, y0, x1, y1 = (value * points_per_unit for value in line["bbox"])
        width, height = x1 - x0, y1 - y0
        if width <= 1 or height <= 1:
            continue
        fontsize = height * 0.85
        natural = font.text_length(line["text"], fontsize=fontsize)
        if natural <= 0:
            continue
        origin = pymupdf.Point(x0, y1 - height * 0.2)
        writer = pymupdf.TextWriter(page.rect)
        writer.append(origin, line["text"], font=font, fontsize=fontsize)
        writer.write_text(page, render_mode=3, morph=(origin, pymupdf.Matrix(width / natural, 1)))


def build_pdf(source: Source, indexes: list[int], pages: dict[int, PageImage]) -> bytes:
    font = pymupdf.Font(fontfile=FONT_FILE)
    if source.kind == "pdf":
        output = pymupdf.open(str(source.path))
        output.select(indexes)
        for position, index in enumerate(indexes):
            page = output[position]
            if index in pages:
                # Boxes are in displayed coordinates, which is what PyMuPDF expects on a page
                # with /Rotate. Do not call remove_rotation(): text written afterwards becomes
                # unextractable (measured on PyMuPDF 1.28.2: 1 of 12 lines survived).
                add_text_layer(page, pages[index].lines, font, 1.0)
    else:
        output = pymupdf.open()
        points_per_pixel = 72.0 / source.image_dpi
        for index in indexes:
            frame = source.frame(index)
            page = output.new_page(width=frame.width * points_per_pixel,
                                   height=frame.height * points_per_pixel)
            buffer = io.BytesIO()
            if source.image_format == "JPEG":
                frame.save(buffer, format="JPEG", quality=92)
            else:
                frame.save(buffer, format="PNG")
            page.insert_image(page.rect, stream=buffer.getvalue())
            add_text_layer(page, pages[index].lines, font, points_per_pixel)
    output.subset_fonts()
    data = output.tobytes(garbage=3, deflate=True)
    output.close()
    return data


def pages_needing_ocr(source: Source, indexes: list[int], response_format: str) -> list[int]:
    # A searchable-PDF request leaves pages that already carry text as they are,
    # like ocrmypdf --skip-text; text and JSON requests read every page.
    if response_format == "pdf" and source.kind == "pdf":
        return [index for index in indexes if not has_text_layer(source.document[index])]
    return list(indexes)


async def acquire_job_slot(request: Request) -> bool:
    # Same policy as ASR: wait on the event loop and give up once the client has gone.
    while not _job_slots.acquire(blocking=False):
        if await request.is_disconnected():
            return False
        await asyncio.sleep(0.5)
    return True


def safe_upload_suffix(filename: Optional[str]) -> str:
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return ".upload"


@app.post("/v1/ocr")
async def ocr(
    request: Request,
    file: UploadFile = File(...),
    response_format: str = Form("text", alias="format"),
    pages: Optional[str] = Form(None),
    dpi: Optional[int] = Form(None),
):
    normalized_format = validate_request(response_format, pages, dpi)
    temp_path: Optional[Path] = None
    source: Optional[Source] = None
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=safe_upload_suffix(file.filename), dir=TMP_DIR
        ) as temp:
            temp_path = Path(temp.name)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload is too large")
                temp.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Upload is empty")

        try:
            source = await run_in_threadpool(Source, temp_path, dpi or DEFAULT_DPI)
            indexes = select_pages(pages, source.count)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not await acquire_job_slot(request):
            raise HTTPException(status_code=503, detail="Client disconnected while queued")
        started = time.monotonic()
        results: dict[int, PageImage] = {}
        try:
            for index in pages_needing_ocr(source, indexes, normalized_format):
                if await request.is_disconnected():
                    raise HTTPException(status_code=503, detail="Client disconnected")
                try:
                    page = await run_in_threadpool(source.render, index)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"Page {index + 1} cannot be rendered"
                    ) from exc
                try:
                    results[index] = await run_in_threadpool(recognize, page, normalized_format)
                except (OSError, ValueError, KeyError, IndexError) as exc:
                    # URLError and timeouts are OSError; a malformed reply is ValueError/KeyError.
                    raise HTTPException(status_code=503, detail="OCR model is unavailable") from exc
            if normalized_format == "pdf":
                pdf = await run_in_threadpool(build_pdf, source, indexes, results)
        finally:
            _job_slots.release()
        seconds = round(time.monotonic() - started, 2)
        log.info("ocr format=%s pages=%d recognized=%d seconds=%s",
                 normalized_format, len(indexes), len(results), seconds)

        if normalized_format == "pdf":
            return Response(pdf, media_type="application/pdf")
        ordered = [results[index] for index in indexes]
        text = "\f".join(page.text for page in ordered)
        if normalized_format == "text":
            return PlainTextResponse(text)
        return JSONResponse({
            "model": MODEL_NAME,
            "seconds": seconds,
            "text": text,
            "pages": [
                {
                    "page": page.number,
                    "unit": page.unit,
                    "width": round(page.width * page.unit_scale, 2),
                    "height": round(page.height * page.unit_scale, 2),
                    "truncated": page.truncated,
                    "text": page.text,
                    "lines": page.lines,
                }
                for page in ordered
            ],
        })
    finally:
        if source is not None:
            source.close()
        try:
            if temp_path is not None:
                temp_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            await file.close()


def parse_run_as(spec: str) -> tuple[int, int]:
    uid_text, _, gid_text = spec.partition(":")
    try:
        uid = int(uid_text)
        gid = int(gid_text or uid_text)
    except ValueError as exc:
        raise RuntimeError("OCR_RUN_AS must be uid:gid") from exc
    if uid <= 0 or gid <= 0:
        raise RuntimeError("OCR_RUN_AS must name a non-root uid:gid")
    return uid, gid


def open_listener(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def drop_privileges(spec: str) -> None:
    if os.getuid() != 0:
        if spec:
            log.info("already running as uid %s, OCR_RUN_AS ignored", os.getuid())
        return
    if not spec:
        raise RuntimeError("refusing to serve as root: set OCR_RUN_AS")
    uid, gid = parse_run_as(spec)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    # setuid() from root to another uid clears every capability; prove it stuck.
    if 0 in (os.getuid(), os.geteuid(), os.getgid(), os.getegid()):
        raise RuntimeError("failed to drop root privileges")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    listener = open_listener(HOST, PORT)
    drop_privileges(RUN_AS)
    # TLS material is loaded by Server.run, after the switch: the key stays readable
    # only to the service uid.
    server = uvicorn.Server(uvicorn.Config(
        "app:app",
        ssl_certfile=CERT_FILE,
        ssl_keyfile=KEY_FILE,
        log_level=os.getenv("OCR_LOG_LEVEL", "info"),
    ))
    server.run(sockets=[listener])
