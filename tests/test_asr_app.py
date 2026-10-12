import asyncio
import importlib.util
import sys
import tempfile
import threading
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
    fastapi.Request = object
    fastapi.UploadFile = object

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = DummyResponse
    responses.PlainTextResponse = DummyResponse

    whisper = types.ModuleType("faster_whisper")
    whisper.WhisperModel = object

    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None

    concurrency = types.ModuleType("starlette.concurrency")

    async def direct_call(function, *args):
        return function(*args)

    concurrency.run_in_threadpool = direct_call

    modules = {
        "fastapi": fastapi,
        "fastapi.responses": responses,
        "faster_whisper": whisper,
        "starlette.concurrency": concurrency,
        "uvicorn": uvicorn,
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


class FakeRequest:
    def __init__(self, disconnected=False):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


class AsrAppTests(unittest.TestCase):
    def test_suffix_and_request_validation(self):
        self.assertEqual(".wav", APP.safe_upload_suffix("VOICE.WAV"))
        self.assertEqual(".audio", APP.safe_upload_suffix("bad." + "x" * 500))
        self.assertEqual("ru", APP.validate_request(" RU ", "transcribe", "json", None))
        self.assertIsNone(APP.validate_request("", "translate", "text", ""))
        self.assertIsNone(APP.validate_request(" Auto ", "transcribe", "json", None))
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
            response = asyncio.run(APP.transcribe(FakeRequest(), upload, response_format="json"))
            remaining = list(Path(temp_dir).iterdir())

        self.assertEqual("first second", response.content["text"])
        self.assertTrue(upload.closed)
        self.assertEqual([], remaining)
        self.assertTrue(APP._transcription_slots.acquire(blocking=False))
        APP._transcription_slots.release()

    def test_drops_queued_request_after_client_disconnects(self):
        upload = FakeUpload([b"audio"])
        run = mock.AsyncMock()
        self.assertTrue(APP._transcription_slots.acquire(blocking=False))
        try:
            with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                APP, "TMP_DIR", Path(temp_dir)
            ), mock.patch.object(APP, "run_in_threadpool", run):
                with self.assertRaises(HttpError) as error:
                    asyncio.run(APP.transcribe(FakeRequest(disconnected=True), upload))
                self.assertEqual([], list(Path(temp_dir).iterdir()))
        finally:
            APP._transcription_slots.release()

        self.assertEqual(503, error.exception.status_code)
        run.assert_not_called()
        self.assertTrue(upload.closed)

    def test_rejects_oversized_and_empty_uploads_without_leaks(self):
        for chunks, expected_status in (([b"12345"], 413), ([], 400)):
            upload = FakeUpload(chunks, filename="unsafe." + "z" * 300)
            with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                APP, "TMP_DIR", Path(temp_dir)
            ), mock.patch.object(APP, "MAX_UPLOAD_BYTES", 4):
                with self.assertRaises(HttpError) as error:
                    asyncio.run(APP.transcribe(FakeRequest(), upload))
                self.assertEqual(expected_status, error.exception.status_code)
                self.assertEqual([], list(Path(temp_dir).iterdir()))
                self.assertTrue(upload.closed)


class AsrIdleTests(unittest.TestCase):
    def setUp(self):
        self.model = mock.Mock()
        patch = mock.patch.multiple(APP, _model=self.model, _model_loaded=True,
                                    _active_transcriptions=0, _last_activity=100.0, IDLE_TIMEOUT=300)
        patch.start()
        self.addCleanup(patch.stop)

    def test_idle_offload_and_reload_keep_the_whisper_wrapper(self):
        self.assertFalse(APP.unload_idle_model(399.9))
        self.assertTrue(APP.unload_idle_model(400.0))
        self.model.model.unload_model.assert_called_once_with(to_cpu=True)
        self.assertFalse(APP.unload_idle_model(900.0))
        with mock.patch.object(APP, "WhisperModel") as constructor:
            self.assertFalse(APP.health()["loaded"])
            self.assertTrue(APP.health()["idle"])
            constructor.assert_not_called()
            self.model.model.load_model.assert_not_called()
            self.assertIs(self.model, APP.get_model())
            self.model.model.load_model.assert_called_once_with()
            self.assertTrue(APP.health()["loaded"])

    def test_never_offloads_an_active_lazy_iterator(self):
        entered = threading.Event()
        finish = threading.Event()
        failures = []

        def iterator():
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("Test iterator did not finish")
            yield types.SimpleNamespace(start=0.0, end=1.0, text="test")

        self.model.transcribe.return_value = (iterator(), object())

        def transcribe():
            try:
                APP.run_transcription(Path("unused.wav"), "ru", "transcribe", None, True, False)
            except BaseException as exc:
                failures.append(exc)

        runner = threading.Thread(target=transcribe)
        runner.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(APP.health()["idle"])
            # Far beyond the timeout; last_activity alone must not permit unload.
            self.assertFalse(APP.unload_idle_model(100000000000.0))
            self.model.model.unload_model.assert_not_called()
        finally:
            finish.set()
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertEqual([], failures)
        self.assertTrue(APP.health()["idle"])
        self.assertFalse(APP.unload_idle_model(APP._last_activity + 299))
        self.assertTrue(APP.unload_idle_model(APP._last_activity + 300))

    def test_transcription_admission_and_reload_wait_for_offload_lock(self):
        unloading = threading.Event()
        finish_unload = threading.Event()
        admitted = threading.Event()
        failures = []

        def unload(**_kwargs):
            unloading.set()
            if not finish_unload.wait(3):
                raise RuntimeError("Offload did not finish")

        def transcribe(*_args, **_kwargs):
            self.model.model.load_model.assert_called_once_with()
            admitted.set()
            return iter([]), object()

        self.model.model.unload_model.side_effect = unload
        self.model.transcribe.side_effect = transcribe

        def run(function, *args):
            try:
                function(*args)
            except BaseException as exc:
                failures.append(exc)

        offloader = threading.Thread(target=run, args=(APP.unload_idle_model, 400.0))
        caller = threading.Thread(target=run, args=(APP.run_transcription,
                                  Path("unused.wav"), "ru", "transcribe", None, True, False))
        offloader.start()
        try:
            self.assertTrue(unloading.wait(2))
            caller.start()
            self.assertFalse(admitted.wait(0.05))
        finally:
            finish_unload.set()
            offloader.join(3)
            if caller.ident is not None:
                caller.join(3)
        self.assertTrue(admitted.is_set())
        self.assertEqual([], failures)
        self.assertEqual(0, APP._active_transcriptions)

    def test_failed_iterator_is_closed_before_activity_is_released(self):
        closed = []

        def iterator():
            try:
                raise ValueError("Decode error")
                yield
            finally:
                closed.append(APP._active_transcriptions)

        self.model.transcribe.return_value = (iterator(), object())
        with self.assertRaises(ValueError):
            APP.run_transcription(Path("bad.wav"), None, "transcribe", None, True, False)
        self.assertEqual([1], closed)
        self.assertEqual(0, APP._active_transcriptions)

    def test_cancelled_http_request_retains_slot_until_transcription_thread_finishes(self):
        entered, finish = threading.Event(), threading.Event()
        info = types.SimpleNamespace(language="ru", language_probability=1.0, duration=1.0)

        def iterator():
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("ASR iterator did not finish")
            yield types.SimpleNamespace(start=0.0, end=1.0, text="test")

        self.model.transcribe.return_value = (iterator(), info)

        async def scenario():
            upload = FakeUpload([b"audio"])
            task = asyncio.create_task(APP.transcribe(FakeRequest(), upload))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(APP._transcription_slots.acquire(blocking=False))
                self.assertFalse(APP.unload_idle_model(100000000000.0))
                self.assertFalse(upload.closed)
            finally:
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(upload.closed)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(APP, "TMP_DIR", Path(directory)), mock.patch.object(
            APP, "run_in_threadpool", side_effect=asyncio.to_thread
        ):
            asyncio.run(scenario())
            self.assertEqual([], list(Path(directory).iterdir()))
        self.assertTrue(APP._transcription_slots.acquire(blocking=False))
        APP._transcription_slots.release()


if __name__ == "__main__":
    unittest.main()
