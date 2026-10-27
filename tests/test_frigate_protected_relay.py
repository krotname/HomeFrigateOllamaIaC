import tempfile
import unittest
from pathlib import Path
from ocr.frigate_protected_relay import deliver, render


class RelayTests(unittest.TestCase):
    def test_uncertain_send_is_not_repeated(self):
        calls = []
        def fail(text):
            calls.append(text)
            raise TimeoutError()
        with tempfile.TemporaryDirectory() as tmp:
            journal = {"sent": {}}
            path = Path(tmp) / "delivery.json"
            deliver("one", "test", journal, path, fail)
            deliver("one", "test", journal, path, fail)
            self.assertEqual(calls, ["test"])
            self.assertEqual(journal["sent"]["one"]["status"], "unknown")

    def test_test_message_does_not_claim_accident(self):
        text = render({"timestamp": 1793123892, "test": True,
                       "label": "Прицеп", "event_id": "test-abc"})
        self.assertIn("ДТП не обнаружено", text)
        self.assertNotIn("clip.mp4", text)


if __name__ == "__main__":
    unittest.main()
