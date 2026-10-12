import asyncio
import base64
import dataclasses
import importlib.util
import io
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest import mock


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DummyApp:
    def __init__(self, **_kwargs):
        pass

    def get(self, _path):
        return lambda function: function

    def post(self, _path):
        return lambda function: function


class DummyResponse:
    def __init__(self, content, status_code=200, media_type=None):
        self.content = content
        self.status_code = status_code
        self.media_type = media_type


def load_app_module():
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = DummyApp
    fastapi.File = lambda default, **_kwargs: default
    fastapi.Form = lambda default=None, **_kwargs: default
    fastapi.HTTPException = HttpError
    fastapi.Request = object
    fastapi.UploadFile = object

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = DummyResponse
    responses.PlainTextResponse = DummyResponse
    responses.Response = DummyResponse

    pymupdf = types.ModuleType("pymupdf")
    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace(DecompressionBombError=type("Bomb", (Exception,), {}))
    pil.ImageOps = types.SimpleNamespace()

    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None

    concurrency = types.ModuleType("starlette.concurrency")

    async def direct_call(function, *args):
        return function(*args)

    concurrency.run_in_threadpool = direct_call

    modules = {
        "fastapi": fastapi,
        "fastapi.responses": responses,
        "pymupdf": pymupdf,
        "PIL": pil,
        "starlette.concurrency": concurrency,
        "uvicorn": uvicorn,
    }
    app_path = Path(__file__).parents[1] / "ocr" / "app.py"
    with mock.patch.object(sys, "path", [str(app_path.parent), *sys.path]):
        importlib.import_module("frigate_caption_worker")
    spec = importlib.util.spec_from_file_location("home_ocr_app_test", app_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


APP = load_app_module()
WORKER = sys.modules["frigate_caption_worker"]


class FakeUpload:
    def __init__(self, chunks, filename="scan.png"):
        self.filename = filename
        self._chunks = iter(chunks)
        self.closed = False

    async def read(self, _size):
        return next(self._chunks, b"")

    async def close(self):
        self.closed = True


class FakeRequest:
    def __init__(self, disconnected=False):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


class FakeSource:
    kind = "image"
    count = 2
    closed = False

    def __init__(self, path, dpi):
        assert Path(path).exists()
        self.dpi = dpi

    def render(self, index):
        return APP.PageImage(index + 1, b"png", 100, 200, "px", 2.0)

    def close(self):
        FakeSource.closed = True


def fake_recognize(page, response_format, max_tokens=None):
    page.text = f"page {page.number} {response_format}"
    page.lines = [{"text": page.text, "bbox": [0, 0, 10, 10]}]
    page.truncated = response_format == "text-json"
    return page


class FakeHttpResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class OcrHelpersTests(unittest.TestCase):
    def test_request_validation(self):
        self.assertEqual("json", APP.validate_request(" JSON ", "1-3,5", 150))
        self.assertEqual("text", APP.validate_request("text", None, None))
        for bad in (("xml", None, None), ("pdf", "1;rm", None), ("pdf", None, 1000)):
            with self.assertRaises(HttpError) as error:
                APP.validate_request(*bad)
            self.assertEqual(400, error.exception.status_code)

    def test_page_selection(self):
        self.assertEqual([0, 1, 2], APP.select_pages(None, 3))
        self.assertEqual([0, 1, 2, 4], APP.select_pages("1-3,5", 6))
        self.assertEqual([1, 0, 2], APP.select_pages("2,1-3", 3))
        for spec in ("0", "4", "3-2"):
            with self.assertRaises(ValueError):
                APP.select_pages(spec, 3)
        with mock.patch.object(APP, "MAX_PAGES", 2), self.assertRaises(ValueError):
            APP.select_pages(None, 3)

    def test_fit_scale_keeps_a4_resolution_and_shrinks_photos(self):
        self.assertAlmostEqual(200 / 72, APP.fit_scale(595, 842, 200 / 72, APP.MAX_PIXELS))
        scale = APP.fit_scale(6000, 4000, 1.0, APP.MAX_PIXELS)
        self.assertLess(scale, 1.0)
        self.assertLessEqual(6000 * scale * 4000 * scale, APP.MAX_PIXELS + 1)

    def test_parse_spotting_tolerates_fences_escapes_and_truncation(self):
        content = (
            '```json\n[{"bbox_2d": [85, 62, 702, 80], "text_content": "Адрес: \\"Сочи\\""},\n'
            '{"bbox_2d": [85, 84, 684, 100], "text_content": "info@example.ru"},\n'
            '{"bbox_2d": [85, 104, 6'
        )
        items = APP.parse_spotting(content)
        self.assertEqual(['Адрес: "Сочи"', "info@example.ru"], [item["text"] for item in items])
        self.assertEqual([85.0, 62.0, 702.0, 80.0], items[0]["bbox"])

    def test_scale_lines_maps_grid_to_units(self):
        items = [
            {"text": " line ", "bbox": [500, 100, 0, 200]},
            {"text": "clamped", "bbox": [-5, 900, 1200, 1100]},
            {"text": "  ", "bbox": [0, 0, 10, 10]},
        ]
        lines = APP.scale_lines(items, 1000, 2000, 0.5)
        self.assertEqual({"text": "line", "bbox": [0.0, 100.0, 250.0, 200.0]}, lines[0])
        self.assertEqual([0.0, 900.0, 500.0, 1000.0], lines[1]["bbox"])
        self.assertEqual(2, len(lines))

    def test_clean_text_strips_markdown_fences(self):
        self.assertEqual("Строка 1\nLine 2", APP.clean_text("```text\nСтрока 1\nLine 2\n```"))
        self.assertEqual("plain", APP.clean_text("  plain  "))

    def test_recognize_uses_the_right_prompt_and_reports_truncation(self):
        replies = [
            {"choices": [{"message": {"content": "```\nТекст\n```"}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": '[{"bbox_2d": [0, 0, 500, 100], '
                                                 '"text_content": "Слово"}]'},
                          "finish_reason": "length"}]},
        ]
        opener = mock.Mock()
        opener.open.side_effect = [FakeHttpResponse(json.dumps(reply).encode()) for reply in replies]
        with mock.patch.object(APP, "_llm_opener", opener):
            text_page = APP.recognize(APP.PageImage(1, b"x", 200, 100, "px", 1.0), "text")
            spot_page = APP.recognize(APP.PageImage(2, b"x", 200, 100, "pt", 0.5), "json")

        self.assertEqual("Текст", text_page.text)
        self.assertFalse(text_page.truncated)
        self.assertEqual([{"text": "Слово", "bbox": [0.0, 0.0, 50.0, 5.0]}], spot_page.lines)
        self.assertTrue(spot_page.truncated)
        first_body = json.loads(opener.open.call_args_list[0].args[0].data)
        self.assertEqual(APP.TEXT_PROMPT, first_body["messages"][0]["content"][1]["text"])
        self.assertEqual(0, first_body["temperature"])
        self.assertEqual(APP.MAX_TOKENS, first_body["max_tokens"])

    def test_health_reports_model_state(self):
        with mock.patch.object(APP, "llm_status", return_value="ok"):
            self.assertEqual(200, APP.health().status_code)
        with mock.patch.object(APP, "llm_status", return_value="loading"):
            response = APP.health()
        self.assertEqual(503, response.status_code)
        self.assertEqual("loading", response.content["llm"])

    def test_text_json_retries_length_stop_with_repetition_penalty(self):
        replies = [
            {"choices": [{"message": {"content": "loop"}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "Repeated line\nRepeated line"}, "finish_reason": "stop"}]},
        ]
        opener = mock.Mock()
        opener.open.side_effect = [FakeHttpResponse(json.dumps(reply).encode()) for reply in replies]
        with mock.patch.object(APP, "_llm_opener", opener):
            page = APP.recognize(APP.PageImage(1, b"x", 200, 100, "px", 1.0), "text-json")
        self.assertFalse(page.truncated)
        self.assertEqual("Repeated line\nRepeated line", page.text)
        initial = json.loads(opener.open.call_args_list[0].args[0].data)
        retry = json.loads(opener.open.call_args_list[1].args[0].data)
        self.assertNotIn("repeat_penalty", initial)
        self.assertEqual(1.1, retry["repeat_penalty"])
        self.assertEqual(256, retry["repeat_last_n"])
        self.assertEqual(initial["messages"], retry["messages"])

    def test_complete_text_json_does_not_retry(self):
        with mock.patch.object(APP, "ask_model", return_value=("complete", False)) as ask:
            page = APP.recognize(APP.PageImage(1, b"x", 200, 100, "px", 1.0), "text-json")
        self.assertEqual("complete", page.text)
        ask.assert_called_once_with(b"x", APP.TEXT_PROMPT)

    def test_fragment_budget_bounds_both_attempts_and_preserves_length_stop(self):
        reply = {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
        opener = mock.Mock()
        opener.open.side_effect = [FakeHttpResponse(json.dumps(reply).encode()) for _ in range(2)]
        with mock.patch.object(APP, "_llm_opener", opener):
            page = APP.recognize(APP.PageImage(1, b"x", 904, 480, "px", 1.0),
                                 "text-json", max_tokens=512)
        self.assertTrue(page.truncated)
        self.assertEqual("partial", page.text)
        for call in opener.open.call_args_list:
            self.assertEqual(512, json.loads(call.args[0].data)["max_tokens"])

    def test_text_json_uses_plain_prompt_and_preserves_truncation(self):
        self.assertEqual("text-json", APP.validate_request("text-json", None, None))
        with mock.patch.object(APP, "ask_model", return_value=("Plain text", True)) as ask:
            page = APP.recognize(APP.PageImage(1, b"png", 200, 100, "px", 1.0), "text-json")
        self.assertEqual([
            mock.call(b"png", APP.TEXT_PROMPT),
            mock.call(b"png", APP.TEXT_PROMPT, repeat_penalty=1.1),
        ], ask.call_args_list)
        self.assertEqual("Plain text", page.text)
        self.assertTrue(page.truncated)
        self.assertEqual([], page.lines)


class OcrEndpointTests(unittest.TestCase):
    def run_ocr(self, upload, request=None, **kwargs):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            APP, "TMP_DIR", Path(temp_dir)
        ), mock.patch.object(APP, "Source", FakeSource), mock.patch.object(
            APP, "recognize", side_effect=fake_recognize
        ) as recognition:
            self.last_recognition = recognition
            try:
                return asyncio.run(APP.ocr(request or FakeRequest(), upload, **kwargs))
            finally:
                self.assertEqual([], list(Path(temp_dir).iterdir()))
                self.assertTrue(upload.closed)

    def assert_slot_free(self):
        self.assertTrue(APP._job_slots.acquire(blocking=False))
        APP._job_slots.release()

    def test_text_joins_pages_with_form_feed(self):
        FakeSource.closed = False
        response = self.run_ocr(FakeUpload([b"image"]), response_format="text")
        self.assertEqual("page 1 text\fpage 2 text", response.content)
        self.assertTrue(FakeSource.closed)
        self.assert_slot_free()

    def test_json_reports_pages_in_source_units(self):
        response = self.run_ocr(FakeUpload([b"image"]), response_format="json", pages="2")
        payload = response.content
        self.assertEqual("page 2 json", payload["text"])
        self.assertEqual(1, len(payload["pages"]))
        page = payload["pages"][0]
        self.assertEqual((2, "px", 200.0, 400.0), (page["page"], page["unit"], page["width"],
                                                   page["height"]))
        self.assertEqual("page 2 json", page["lines"][0]["text"])

    def test_text_json_returns_metadata_instead_of_plain_response(self):
        response = self.run_ocr(FakeUpload([b"image"]), response_format="text-json")
        self.assertEqual("page 1 text-json\fpage 2 text-json", response.content["text"])
        self.assertEqual([1, 2], [page["page"] for page in response.content["pages"]])
        self.assertTrue(all(page["truncated"] for page in response.content["pages"]))
        self.assert_slot_free()

    def test_fragment_budget_reaches_every_selected_page(self):
        self.run_ocr(FakeUpload([b"image"]), response_format="text-json", max_tokens=512)
        self.assertEqual(2, self.last_recognition.call_count)
        for call in self.last_recognition.call_args_list:
            self.assertEqual(512, call.args[2])
        self.assert_slot_free()

    def test_invalid_fragment_budgets_rejected_before_rendering(self):
        for budget in (0, 255, APP.MAX_TOKENS + 1, True, 512.0):
            upload = FakeUpload([b"image"])
            with mock.patch.object(APP, "Source") as source, self.assertRaises(HttpError) as error:
                asyncio.run(APP.ocr(FakeRequest(), upload, max_tokens=budget))
            self.assertEqual(400, error.exception.status_code)
            source.assert_not_called()
            self.assertTrue(upload.closed)
        self.assert_slot_free()

    def test_rejects_bad_uploads_without_leaks(self):
        for chunks, expected_status in (([b"12345"], 413), ([], 400)):
            with mock.patch.object(APP, "MAX_UPLOAD_BYTES", 4), self.assertRaises(HttpError) as error:
                self.run_ocr(FakeUpload(chunks, filename="bad." + "z" * 300))
            self.assertEqual(expected_status, error.exception.status_code)
        with self.assertRaises(HttpError) as error:
            self.run_ocr(FakeUpload([b"image"]), pages="3")
        self.assertEqual(400, error.exception.status_code)
        self.assert_slot_free()

    def test_drops_queued_request_after_client_disconnects(self):
        self.assertTrue(APP._job_slots.acquire(blocking=False))
        try:
            with self.assertRaises(HttpError) as error:
                self.run_ocr(FakeUpload([b"image"]), request=FakeRequest(disconnected=True))
        finally:
            APP._job_slots.release()
        self.assertEqual(503, error.exception.status_code)

    def test_model_failure_is_503_and_releases_the_slot(self):
        with mock.patch.object(APP, "Source", FakeSource), mock.patch.object(
            APP, "recognize", side_effect=urllib.error.URLError("refused")
        ), tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            APP, "TMP_DIR", Path(temp_dir)
        ):
            with self.assertRaises(HttpError) as error:
                asyncio.run(APP.ocr(FakeRequest(), FakeUpload([b"image"])))
        self.assertEqual(503, error.exception.status_code)
        self.assert_slot_free()


class CaptionRequest(FakeRequest):
    def __init__(self, payload=None, host="127.0.0.1", chunks=None):
        super().__init__()
        self.client = types.SimpleNamespace(host=host)
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


class CaptionEndpointTests(unittest.TestCase):
    def assert_slot_free(self):
        self.assertFalse(APP._caption_active)
        self.assertEqual(0, APP._foreground_jobs)
        self.assertEqual(0, APP._foreground_waiters)
        self.assertTrue(APP._job_slots.acquire(blocking=False))
        APP._job_slots.release()

    def test_caption_uses_same_model_with_fixed_prompt_and_token_budget(self):
        reply = {"choices": [{"message": {"content": "Человек стоит у двери."}, "finish_reason": "stop"}]}
        with mock.patch.object(APP, "request_bytes", return_value=json.dumps(reply).encode()) as send:
            self.assertEqual("Человек стоит у двери.", APP.ask_caption([b"one", b"two"], 12))
        self.assertEqual(APP.LLM_URL + "/v1/chat/completions", send.call_args.args[0])
        body = json.loads(send.call_args.kwargs["body"])
        self.assertEqual(256, body["max_tokens"])
        content = body["messages"][0]["content"]
        self.assertEqual(2, len([item for item in content if item["type"] == "image_url"]))
        self.assertEqual(APP.CAPTION_PROMPT, content[-1]["text"])
        self.assertLessEqual(send.call_args.kwargs["timeout"], 90)

    def test_internal_endpoint_rejects_non_loopback_before_generation(self):
        with mock.patch.object(APP, "ask_caption") as ask, self.assertRaises(HttpError) as error:
            asyncio.run(APP.frigate_caption(CaptionRequest({"images": ["eA=="]}, host="192.168.1.5")))
        self.assertEqual(403, error.exception.status_code)
        ask.assert_not_called()

    def test_caption_never_waits_or_claims_a_slot_if_foreground_is_queued(self):
        with mock.patch.object(APP, "_foreground_waiters", 1), mock.patch.object(
            APP, "_job_slots", threading.BoundedSemaphore(2)
        ), mock.patch.object(APP, "ask_caption") as ask:
            with self.assertRaises(HttpError) as error:
                asyncio.run(APP.frigate_caption(CaptionRequest({"images": ["eA=="]})))
            self.assertEqual(503, error.exception.status_code)
            self.assertFalse(APP.acquire_caption_slot())
            self.assertTrue(APP._job_slots.acquire(blocking=False))
            self.assertTrue(APP._job_slots.acquire(blocking=False))
            APP._job_slots.release()
            APP._job_slots.release()
            ask.assert_not_called()
        self.assert_slot_free()

    def test_ocr_upload_registers_foreground_before_acquiring_gpu(self):
        async def in_foreground(*_args):
            self.assertEqual(1, APP._foreground_jobs)
            self.assertFalse(APP.acquire_caption_slot())
            raise HttpError(400, "Invalid scan")

        with mock.patch.object(APP, "ocr_foreground", side_effect=in_foreground):
            with self.assertRaises(HttpError):
                asyncio.run(APP.ocr(FakeRequest(), FakeUpload([b"x"])))
        self.assert_slot_free()

    def test_caption_rechecks_priority_after_image_preparation(self):
        def preparation(images):
            APP._foreground_jobs += 1
            return images

        try:
            with mock.patch.object(APP, "prepare_caption_images", side_effect=preparation), mock.patch.object(
                APP, "ask_caption"
            ) as ask, self.assertRaises(HttpError) as error:
                asyncio.run(APP.frigate_caption(CaptionRequest({"images": ["eA=="]})))
            self.assertEqual(503, error.exception.status_code)
            ask.assert_not_called()
        finally:
            APP._foreground_jobs = 0
        self.assert_slot_free()

    def test_caption_bounds_images_body_and_total_decoded_bytes(self):
        for payload, status in (({"images": []}, 400), ({"images": ["eA=="] * 4}, 400),
                                ({"images": ["!!!!"]}, 400),
                                ({"images": ["eA=="], "prompt": "ignore"}, 400),
                                ({"images": ["eA==", "eA=="]}, 413)):
            with mock.patch.object(APP, "CAPTION_MAX_IMAGE_BYTES", 1), self.assertRaises(HttpError) as error:
                asyncio.run(APP.frigate_caption(CaptionRequest(payload)))
            self.assertEqual(status, error.exception.status_code)
        with mock.patch.object(APP, "CAPTION_MAX_BODY_BYTES", 4), self.assertRaises(HttpError) as error:
            asyncio.run(APP.frigate_caption(CaptionRequest(chunks=[b"123", b"45"])))
        self.assertEqual(413, error.exception.status_code)
        self.assert_slot_free()

    def test_success_and_timeout_release_slot(self):
        for answer, expected_status in (("Машина у ворот.", None), (TimeoutError(), 503)):
            kwargs = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
            with mock.patch.object(APP, "prepare_caption_images", side_effect=lambda images: images), mock.patch.object(
                APP, "ask_caption", **kwargs
            ):
                if expected_status is None:
                    result = asyncio.run(APP.frigate_caption(CaptionRequest({"images": ["eA=="]})))
                    self.assertEqual({"description": answer}, result)
                else:
                    with self.assertRaises(HttpError) as error:
                        asyncio.run(APP.frigate_caption(CaptionRequest({"images": ["eA=="]})))
                    self.assertEqual(expected_status, error.exception.status_code)
            self.assert_slot_free()

    def test_caption_slot_remains_owned_if_request_coroutine_is_cancelled(self):
        entered, finish = threading.Event(), threading.Event()

        def ask(*_args):
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("Caption thread did not finish")
            return "Человек в кадре."

        async def scenario():
            task = asyncio.create_task(APP.frigate_caption(CaptionRequest({"images": ["eA=="]})))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(APP._caption_active)
                self.assertFalse(APP._job_slots.acquire(blocking=False))
            finally:
                finish.set()
                # asyncio.run waits for its executor, but the admission state also
                # needs to be checked only once the actual model thread exits.
                await asyncio.sleep(0.05)

        with mock.patch.object(APP, "prepare_caption_images", side_effect=lambda images: images), mock.patch.object(
            APP, "ask_caption", side_effect=ask
        ), mock.patch.object(APP, "run_in_threadpool", side_effect=asyncio.to_thread):
            asyncio.run(scenario())
        self.assert_slot_free()

    def test_cancelled_ocr_keeps_foreground_priority_until_native_call_finishes(self):
        entered, finish = threading.Event(), threading.Event()

        def recognition(page, _format):
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("OCR thread did not finish")
            return page

        async def scenario():
            upload = FakeUpload([b"scan"])
            task = asyncio.create_task(APP.ocr(FakeRequest(), upload))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(APP.acquire_caption_slot())
                self.assertEqual(1, APP._foreground_jobs)
                self.assertFalse(upload.closed)
            finally:
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(upload.closed)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(APP, "TMP_DIR", Path(directory)), mock.patch.object(
            APP, "Source", FakeSource
        ), mock.patch.object(APP, "recognize", side_effect=recognition), mock.patch.object(
            APP, "run_in_threadpool", side_effect=asyncio.to_thread
        ):
            asyncio.run(scenario())
            self.assertEqual([], list(Path(directory).iterdir()))
        self.assert_slot_free()


def finished_event(event_id="event-1", now=10000.0, text=""):
    return {"id": event_id, "camera": "camera-from-api", "start_time": now - 20,
            "end_time": now - 10, "data": {"description": text}}


class CaptionTransport:
    def __init__(self):
        self.events = {"event-1": finished_event()}
        self.calls = []
        self.busy = False
        self.on_caption = None
        self.store_description = True
        self.fail_after_save = False
        self.snapshot_missing = False
        self.slots = [{"is_processing": False}]

    def __call__(self, url, **options):
        self.calls.append((url, options))
        parsed = urlsplit(url)
        if parsed.path == "/api/events":
            return json.dumps(list(self.events.values())).encode()
        if parsed.path == "/slots":
            return json.dumps(self.slots).encode()
        if parsed.path == "/v1/frigate-caption":
            if self.busy:
                raise WORKER.RemoteError(503)
            if self.on_caption:
                self.on_caption()
            return json.dumps({"description": "Человек у двери."}).encode()
        event_id = parsed.path.split("/")[3]
        if event_id not in self.events:
            raise WORKER.RemoteError(404)
        if parsed.path.endswith("/snapshot.jpg"):
            if self.snapshot_missing:
                raise WORKER.RemoteError(404)
            return b"snapshot"
        if parsed.path.endswith("/thumbnail.jpg"):
            return b"thumbnail"
        if parsed.path.endswith("/description"):
            if self.store_description:
                self.events[event_id]["data"]["description"] = json.loads(options["body"])["description"]
            if self.fail_after_save:
                raise WORKER.RemoteError(0)
            return b'{"success":true}'
        return json.dumps(self.events[event_id]).encode()

    def count(self, suffix):
        return len([url for url, _options in self.calls if urlsplit(url).path.endswith(suffix)])


class CaptionPriorityTests(unittest.TestCase):
    def test_key_is_sent_only_to_the_exact_local_llm_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "key"
            key_path.write_text("test-key")
            config = WORKER.Config(priority_url="http://192.168.1.242:8000/slots",
                                   priority_key_file=str(key_path))
            transport = mock.Mock(return_value=b"[]")
            worker = WORKER.Worker(config, None, transport=transport)
            worker.fetch(config.priority_url)
            self.assertEqual("test-key", transport.call_args.kwargs["priority_key"])
            worker.fetch(config.frigate_url + "/api/events")
            self.assertNotIn("priority_key", transport.call_args.kwargs)
            with self.assertRaises(ValueError):
                WORKER.loopback_url(config.priority_url)
            with self.assertRaises(ValueError):
                dataclasses.replace(config, priority_url="http://192.168.1.243:8000/slots")

    def test_asr_busy_or_unknown_health_prevents_background_caption(self):
        worker = WORKER.Worker(WORKER.Config(asr_health_url="https://127.0.0.1:19443/health"), None)
        worker.json = mock.Mock(return_value={"idle": False})
        self.assertFalse(worker.priority_idle())
        worker.json.return_value = {"idle": True}
        self.assertTrue(worker.priority_idle())
        worker.json.return_value = {"status": "ok"}
        with self.assertRaises(ValueError):
            worker.priority_idle()


class CaptionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = WORKER.Config(state_dir=Path(self.directory.name))
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.state = WORKER.State(self.path, self.config)
        self.addCleanup(lambda: self.state.close())
        self.now = 10000.0
        self.transport = CaptionTransport()
        self.worker = WORKER.Worker(self.config, self.state, transport=self.transport, clock=lambda: self.now)

    def row(self, event_id="event-1"):
        return self.state.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def restart(self):
        self.state.close()
        self.state = WORKER.State(self.path, self.config)
        self.worker = WORKER.Worker(self.config, self.state, transport=self.transport, clock=lambda: self.now)

    def test_finished_only_recent_window_one_event_per_tick_and_dedup_after_restart(self):
        self.transport.events.update({
            "old": finished_event("old", self.now - 400),
            "live": {**finished_event("live"), "end_time": None},
            "event-2": finished_event("event-2"),
        })
        self.worker.tick()
        self.assertEqual(1, self.transport.count("/v1/frigate-caption"))
        self.assertEqual(1, self.transport.count("/description"))
        self.assertEqual("done", self.row()["status"])
        self.assertIsNone(self.row("old"))
        self.assertIsNone(self.row("live"))
        poll = next(url for url, _ in self.transport.calls if urlsplit(url).path == "/api/events")
        query = parse_qs(urlsplit(poll).query)
        self.assertEqual(["0"], query["in_progress"])
        self.assertEqual(self.now - 300, float(query["after"][0]))
        self.restart()
        self.now += 30
        self.worker.tick()
        self.assertEqual(2, self.transport.count("/v1/frigate-caption"))
        self.worker.tick()
        self.assertEqual(2, self.transport.count("/description"))

    def test_busy_is_durable_and_backoff_capped(self):
        self.transport.busy = True
        self.worker.tick()
        self.assertEqual("queued", self.row()["status"])
        self.assertEqual(1, self.row()["attempts"])
        first_due = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual(1, self.transport.count("/v1/frigate-caption"))
        self.restart()
        self.assertEqual(first_due, self.row()["next_try"])
        self.now = first_due
        self.transport.busy = False
        self.worker.tick()
        self.assertEqual("done", self.row()["status"])
        row = self.row()
        self.state.retry(row, 9, self.now, "busy")
        self.assertLessEqual(self.row()["next_try"] - self.now, self.config.backoff_max)

    def test_existing_description_and_manual_edit_during_generation_are_preserved(self):
        self.transport.events["event-1"]["data"]["description"] = "Ручное описание"
        self.worker.tick()
        self.assertEqual("skipped", self.row()["status"])
        self.assertEqual(0, self.transport.count("/v1/frigate-caption"))
        self.transport.events["event-2"] = finished_event("event-2")
        self.transport.on_caption = lambda: self.transport.events["event-2"]["data"].update(description="Правка во время генерации")
        self.now += 30
        self.worker.tick()
        self.assertEqual("skipped", self.row("event-2")["status"])
        self.assertEqual("Правка во время генерации", self.transport.events["event-2"]["data"]["description"])
        self.assertEqual(0, self.transport.count("/description"))

    def test_description_post_200_is_not_success_without_exact_readback(self):
        self.transport.store_description = False
        self.worker.tick()
        self.assertEqual("queued", self.row()["status"])
        self.assertEqual(1, self.row()["write_started"])
        self.assertEqual("Человек у двери.", self.row()["caption"])
        self.now = self.row()["next_try"]
        self.restart()
        self.worker.tick()
        self.assertEqual("queued", self.row()["status"])
        self.assertEqual(1, self.transport.count("/description"))
        self.transport.events["event-1"]["data"]["description"] = "Человек у двери. "
        self.now = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual("skipped", self.row()["status"])

    def test_post_timeout_after_save_is_reconciled_without_duplicate_write(self):
        self.transport.fail_after_save = True
        self.worker.tick()
        self.assertEqual("queued", self.row()["status"])
        self.restart()
        self.now = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual("done", self.row()["status"])
        self.assertEqual(1, self.transport.count("/description"))
        self.assertEqual(1, self.transport.count("/v1/frigate-caption"))

    def test_manual_clear_after_unconfirmed_write_is_not_overwritten(self):
        self.transport.fail_after_save = True
        self.worker.tick()
        self.transport.events["event-1"]["data"]["description"] = ""
        self.now = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual("queued", self.row()["status"])
        self.assertEqual(1, self.transport.count("/description"))
        self.transport.events["event-1"]["data"]["description"] = "Новая ручная правка"
        self.now = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual("skipped", self.row()["status"])

    def test_queue_limit_deadline_attempts_and_retention(self):
        for index in range(510):
            self.state.enqueue(finished_event(f"bounded-{index}"), self.now)
        self.assertEqual(500, self.state.db.execute("SELECT COUNT(*) FROM events WHERE status='queued'").fetchone()[0])
        self.assertFalse(self.state.enqueue(finished_event("bounded-0"), self.now))
        self.now += self.config.event_ttl
        self.state.prune(self.now)
        self.assertIsNone(self.state.due(self.now))
        self.assertTrue(self.state.enqueue(finished_event("attempt-limited", self.now), self.now))
        row = self.row("attempt-limited")
        self.state.retry(row, self.config.max_attempts, self.now, "busy")
        self.assertEqual("expired", self.row("attempt-limited")["status"])
        self.now += self.config.retention_seconds
        self.state.prune(self.now)
        self.assertEqual(0, self.state.db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_priority_probe_busy_or_unknown_defers_without_model_request(self):
        self.worker.config = dataclasses.replace(self.config, priority_url="http://127.0.0.1:18091/slots")
        for slots in ([{"is_processing": True}], [{"unexpected": False}], []):
            self.transport.slots = slots
            self.worker.tick()
            self.assertEqual("queued", self.row()["status"])
            self.assertEqual(0, self.transport.count("/v1/frigate-caption"))
            self.now = self.row()["next_try"]
        self.transport.slots = [{"is_processing": False}]
        self.worker.tick()
        self.assertEqual("done", self.row()["status"])

    def test_snapshot_fallback_bounds_remote_requests_and_shutdown_saves_candidate(self):
        self.transport.snapshot_missing = True
        self.transport.on_caption = self.worker.stop.set
        self.worker.tick()
        self.assertEqual(1, self.transport.count("/thumbnail.jpg"))
        self.assertEqual("Человек у двери.", self.row()["caption"])
        self.assertEqual(0, self.transport.count("/description"))
        for _url, options in self.transport.calls:
            self.assertLessEqual(options["timeout"], 90)
            self.assertLessEqual(options["limit"], 8 * 1024 * 1024)
        self.restart()
        self.now = self.row()["next_try"]
        self.worker.tick()
        self.assertEqual("done", self.row()["status"])
        self.assertEqual(1, self.transport.count("/v1/frigate-caption"))

    def test_worker_config_rejects_remote_targets_credentials_and_excess_bounds(self):
        for url in ("https://example.org/slots", "http://127.0.0.1.evil/", "http://user:pass@127.0.0.1/"):
            with self.assertRaises(ValueError):
                dataclasses.replace(self.config, priority_url=url)
        for settings in ({"queue_limit": 501}, {"retention_seconds": 4 * 86400},
                         {"poll_seconds": 1}, {"caption_timeout": 91}):
            with self.assertRaises(ValueError):
                dataclasses.replace(self.config, **settings)

    def test_transport_rejects_oversized_reply_before_reading_and_never_redirects(self):
        response = mock.Mock(status=200)
        response.getheader.return_value = "1001"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(WORKER.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(WORKER.RemoteError) as error:
                WORKER.request_bytes("http://127.0.0.1:15000/api/events", limit=1000)
        self.assertEqual(413, error.exception.status)
        response.read.assert_not_called()
        connection.close.assert_called_once_with()

        response.status = 302
        response.getheader.return_value = None
        response.read.return_value = b""
        connection.reset_mock()
        with mock.patch.object(WORKER.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(WORKER.RemoteError) as error:
                WORKER.request_bytes("http://127.0.0.1:15000/api/events", limit=1000)
        self.assertEqual(302, error.exception.status)
        connection.request.assert_called_once()

    def test_transport_wall_deadline_shuts_down_socket_during_read(self):
        response = mock.Mock(status=200)
        response.getheader.return_value = None
        connection = mock.Mock()
        connection.getresponse.return_value = response
        callback = []

        def make_timer(_seconds, expire):
            callback.append(expire)
            return mock.Mock()

        def read(_limit):
            callback[0]()
            return b'{}'

        response.read.side_effect = read
        with mock.patch.object(WORKER.http.client, "HTTPConnection", return_value=connection), mock.patch.object(
            WORKER.threading, "Timer", side_effect=make_timer
        ):
            with self.assertRaises(WORKER.RemoteError):
                WORKER.request_bytes("http://127.0.0.1:15000/api/events", timeout=1)
        connection.sock.shutdown.assert_called_once_with(WORKER.socket.SHUT_RDWR)


class PrivilegeDropTest(unittest.TestCase):
    def patch_ids(self, uid):
        ids = {"uid": uid, "gid": uid}
        calls = []

        def setuid(value):
            calls.append(("setuid", value))
            ids["uid"] = value

        def setgid(value):
            calls.append(("setgid", value))
            ids["gid"] = value

        patches = [
            mock.patch.object(APP.os, "getuid", lambda: ids["uid"], create=True),
            mock.patch.object(APP.os, "geteuid", lambda: ids["uid"], create=True),
            mock.patch.object(APP.os, "getgid", lambda: ids["gid"], create=True),
            mock.patch.object(APP.os, "getegid", lambda: ids["gid"], create=True),
            mock.patch.object(APP.os, "setuid", setuid, create=True),
            mock.patch.object(APP.os, "setgid", setgid, create=True),
            mock.patch.object(
                APP.os, "setgroups", lambda groups: calls.append(("setgroups", groups)), create=True
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_root_switches_to_the_service_uid_group_first(self):
        calls = self.patch_ids(0)
        APP.drop_privileges("10001:10001")
        self.assertEqual(
            [("setgroups", []), ("setgid", 10001), ("setuid", 10001)], calls
        )

    def test_root_without_run_as_refuses_to_serve(self):
        self.patch_ids(0)
        with self.assertRaises(RuntimeError):
            APP.drop_privileges("")

    def test_unprivileged_start_keeps_its_uid(self):
        calls = self.patch_ids(10001)
        APP.drop_privileges("10001:10001")
        self.assertEqual([], calls)

    def test_run_as_rejects_root_and_garbage(self):
        for spec in ("0:0", "10001:0", "ocr", "10001:x"):
            with self.subTest(spec=spec), self.assertRaises(RuntimeError):
                APP.parse_run_as(spec)
        self.assertEqual((10001, 10001), APP.parse_run_as("10001"))


if __name__ == "__main__":
    unittest.main()
