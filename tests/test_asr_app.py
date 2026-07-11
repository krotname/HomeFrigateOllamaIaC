import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
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
    def __init__(self, content):
        self.content = content


def load_app_module():
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = DummyApp
    fastapi.File = lambda default, **_kwargs: default
    fastapi.Form = lambda default=None, **_kwargs: default
    fastapi.HTTPException = HttpError
    fastapi.UploadFile = object

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = DummyResponse
    responses.PlainTextResponse = DummyResponse

    whisper = types.ModuleType("faster_whisper")
    whisper.WhisperModel = object

    concurrency = types.ModuleType("starlette.concurrency")

    async def direct_call(function, *args):
        return function(*args)

    concurrency.run_in_threadpool = direct_call

    modules = {
        "fastapi": fastapi,
        "fastapi.responses": responses,
        "faster_whisper": whisper,
        "starlette.concurrency": concurrency,
    }
    app_path = Path(__file__).parents[1] / "asr" / "app.py"
    spec = importlib.util.spec_from_file_location("home_asr_app_test", app_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


APP = load_app_module()


class FakeUpload:
    def __init__(self, chunks, filename="sample.wav"):
        self.filename = filename
        self._chunks = iter(chunks)
        self.closed = False

    async def read(self, _size):
        return next(self._chunks, b"")

    async def close(self):
        self.closed = True


class AsrAppTests(unittest.TestCase):
    def test_suffix_and_request_validation(self):
        self.assertEqual(".wav", APP.safe_upload_suffix("VOICE.WAV"))
        self.assertEqual(".audio", APP.safe_upload_suffix("bad." + "x" * 500))
        self.assertEqual("ru", APP.validate_request(" RU ", "transcribe", "json", None))
        self.assertIsNone(APP.validate_request("", "translate", "text", ""))
        with self.assertRaises(HttpError) as invalid_language:
            APP.validate_request("../../etc", "transcribe", "json", None)
        self.assertEqual(400, invalid_language.exception.status_code)
        with self.assertRaises(HttpError):
            APP.validate_request("ru", "invalid", "json", None)
        with self.assertRaises(HttpError):
            APP.validate_request("ru", "transcribe", "xml", None)

    def test_transcription_serializes_word_timestamps(self):
        word = types.SimpleNamespace(start=0.1, end=0.4, word=" hello ", probability=0.98765432)
        segment = types.SimpleNamespace(start=0.0, end=0.5, text=" hello ", words=[word])
        info = types.SimpleNamespace(language="en")
        model = mock.Mock()
        model.transcribe.return_value = (iter([segment]), info)

        with mock.patch.object(APP, "get_model", return_value=model):
            segments, actual_info = APP.run_transcription(
                Path("unused.wav"), "en", "transcribe", None, True, True
            )

        self.assertIs(info, actual_info)
        self.assertEqual("hello", segments[0]["text"])
        self.assertEqual("hello", segments[0]["words"][0]["word"])
        self.assertEqual(0.987654, segments[0]["words"][0]["probability"])

    def test_transcribe_joins_segments_and_removes_temporary_file(self):
        upload = FakeUpload([b"audio"])
        info = types.SimpleNamespace(language="ru", language_probability=0.9, duration=1.0)

        async def direct_call(function, *args):
            self.assertTrue(args[0].exists())
            return ([{"text": "first"}, {"text": "second"}], info)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            APP, "TMP_DIR", Path(temp_dir)
        ), mock.patch.object(APP, "run_in_threadpool", side_effect=direct_call):
            response = asyncio.run(APP.transcribe(upload, response_format="json"))
            remaining = list(Path(temp_dir).iterdir())

        self.assertEqual("first second", response.content["text"])
        self.assertTrue(upload.closed)
        self.assertEqual([], remaining)

    def test_rejects_oversized_and_empty_uploads_without_leaks(self):
        for chunks, expected_status in (([b"12345"], 413), ([], 400)):
            upload = FakeUpload(chunks, filename="unsafe." + "z" * 300)
            with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                APP, "TMP_DIR", Path(temp_dir)
            ), mock.patch.object(APP, "MAX_UPLOAD_BYTES", 4):
                with self.assertRaises(HttpError) as error:
                    asyncio.run(APP.transcribe(upload))
                self.assertEqual(expected_status, error.exception.status_code)
                self.assertEqual([], list(Path(temp_dir).iterdir()))
                self.assertTrue(upload.closed)


if __name__ == "__main__":
    unittest.main()
