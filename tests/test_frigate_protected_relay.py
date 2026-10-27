import tempfile
import unittest
from pathlib import Path
from ocr.frigate_protected_relay import deliver, prune_journal, render


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

    def test_retained_incidents_survive_notification_pruning_without_resend(self):
        journal = {"sent": {
            "confirmed": {"at": 1, "status": "verified"},
            "ambiguous": {"at": 2, "status": "unknown"},
            **{f"offline-{i}": {"at": i + 10, "status": "verified"} for i in range(1001)}
        }}
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.json"
            for _ in range(2):
                prune_journal(journal, {"confirmed", "ambiguous"})
                for key in ("confirmed", "ambiguous"):
                    deliver(key, "old incident", journal, path, calls.append)
        self.assertEqual(calls, [])
        self.assertEqual(journal["sent"]["confirmed"]["status"], "verified")
        self.assertEqual(journal["sent"]["ambiguous"]["status"], "unknown")
        self.assertNotIn("offline-0", journal["sent"])
        self.assertEqual(len(journal["sent"]), 1002)


if __name__ == "__main__":
    unittest.main()
