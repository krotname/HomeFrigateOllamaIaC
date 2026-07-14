import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "ollama-audio-transcription"
    / "transcribe_via_ollama.py"
)
SPEC = importlib.util.spec_from_file_location("ollama_audio_test", SCRIPT_PATH)
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


class OllamaTranscriptionTests(unittest.TestCase):
    def test_integer_setting_reports_invalid_environment_value(self):
        with mock.patch.dict("os.environ", {"TEST_INTEGER_SETTING": "not-a-number"}):
            with self.assertRaisesRegex(SystemExit, "must be an integer"):
                SCRIPT.integer_setting("TEST_INTEGER_SETTING", 3)

    def test_ffprobe_rejects_non_finite_or_non_positive_duration(self):
        for raw_value in ("nan", "inf", "0", "-1"):
            result = types.SimpleNamespace(stdout=raw_value)
            with mock.patch.object(SCRIPT, "run", return_value=result):
                with self.assertRaises(ValueError):
                    SCRIPT.ffprobe_duration(Path("sample.wav"))

    def test_rebuild_transcript_is_sorted_and_atomic(self):
        records = {
            2: {"start": 120, "end": 180, "text": "third"},
            0: {"start": 0, "end": 60, "text": "first"},
            1: {"start": 60, "end": 120, "text": "second"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "transcript.txt"
            SCRIPT.rebuild_text_transcript(path, records)
            text = path.read_text(encoding="utf-8")
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

        self.assertLess(text.index("first"), text.index("second"))
        self.assertLess(text.index("second"), text.index("third"))

    def test_atomic_json_round_trip(self):
        payload = {"source": "пример.wav", "model": "demo"}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metadata.json"
            SCRIPT.write_json_atomic(path, payload)
            actual = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
        self.assertEqual(payload, actual)

    def test_jsonl_rebuild_is_sorted_and_atomic(self):
        records = {2: {"index": 2}, 0: {"index": 0}, 1: {"index": 1}}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "records.jsonl"
            SCRIPT.rebuild_jsonl(path, records)
            indexes = [json.loads(line)["index"] for line in path.read_text().splitlines()]
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
        self.assertEqual([0, 1, 2], indexes)

    def test_source_hash_detects_content_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.bin"
            path.write_bytes(b"first")
            first = SCRIPT.sha256_file(path)
            path.write_bytes(b"other")
            second = SCRIPT.sha256_file(path)
        self.assertNotEqual(first, second)

    def test_start_tunnel_refuses_occupied_local_port(self):
        with mock.patch.object(SCRIPT, "local_port_is_open", return_value=True), mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                SCRIPT.start_tunnel()
        popen.assert_not_called()

    def test_start_tunnel_stops_when_forwarded_service_is_not_ollama(self):
        class FakeProcess:
            def __init__(self):
                self.stopped = False
                self.stderr = None

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                return 0

        process = FakeProcess()
        with mock.patch.object(SCRIPT, "local_port_is_open", return_value=False), mock.patch(
            "subprocess.Popen", return_value=process
        ), mock.patch.object(SCRIPT, "request_json", return_value={"status": "ok"}):
            with self.assertRaisesRegex(RuntimeError, "not an Ollama API"):
                SCRIPT.start_tunnel()

        self.assertTrue(process.stopped)

    def test_output_directory_lock_rejects_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            with SCRIPT.output_directory_lock(directory):
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    with SCRIPT.output_directory_lock(directory):
                        pass

    def test_request_json_rejects_oversized_declared_response(self):
        class FakeResponse:
            headers = {"Content-Length": str(SCRIPT.MAX_API_RESPONSE_BYTES + 1)}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(ValueError, "size limit"):
                SCRIPT.request_json("/api/version", None)


if __name__ == "__main__":
    unittest.main()
