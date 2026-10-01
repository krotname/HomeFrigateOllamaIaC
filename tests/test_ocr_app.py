import asyncio
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
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
    spec = importlib.util.spec_from_file_location("home_ocr_app_test", app_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


APP = load_app_module()


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


def fake_recognize(page, response_format):
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

    def test_health_reports_model_state(self):
        with mock.patch.object(APP, "llm_status", return_value="ok"):
            self.assertEqual(200, APP.health().status_code)
        with mock.patch.object(APP, "llm_status", return_value="loading"):
            response = APP.health()
        self.assertEqual(503, response.status_code)
        self.assertEqual("loading", response.content["llm"])

    def test_text_json_uses_plain_prompt_and_preserves_truncation(self):
        self.assertEqual("text-json", APP.validate_request("text-json", None, None))
        with mock.patch.object(APP, "ask_model", return_value=("Plain text", True)) as ask:
            page = APP.recognize(APP.PageImage(1, b"png", 200, 100, "px", 1.0), "text-json")
        ask.assert_called_once_with(b"png", APP.TEXT_PROMPT)
        self.assertEqual("Plain text", page.text)
        self.assertTrue(page.truncated)
        self.assertEqual([], page.lines)


class OcrEndpointTests(unittest.TestCase):
    def run_ocr(self, upload, request=None, **kwargs):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            APP, "TMP_DIR", Path(temp_dir)
        ), mock.patch.object(APP, "Source", FakeSource), mock.patch.object(
            APP, "recognize", side_effect=fake_recognize
        ):
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
