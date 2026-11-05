import unittest
import threading
from unittest.mock import patch

from ocr.frigate_state_notify import RecordingQueue, Transitions, event_creator, message, process


class DoorTests(unittest.TestCase):
    def test_every_cycle_without_cooldown_or_repeats(self):
        gate = Transitions()
        states = ["closed", "open", "open", "closed", "open", "closed", "open"]
        self.assertEqual([gate.observe(s) for s in states],
                         [False, True, False, False, True, False, True])

    def test_initial_open_unknown_and_disconnect_do_not_invent_opening(self):
        gate = Transitions()
        self.assertFalse(gate.observe("open"))
        self.assertFalse(gate.observe("unknown"))
        self.assertFalse(gate.observe("open"))
        gate.observe("closed")
        gate.reset()
        self.assertFalse(gate.observe("open"))

    def test_retained_open_never_triggers_and_close_primes(self):
        gate = Transitions()
        gate.observe("closed")
        self.assertFalse(gate.observe("open", retained=True))
        self.assertFalse(gate.observe("open"))
        gate.observe("closed", retained=True)
        self.assertTrue(gate.observe("open"))

    def test_ambiguous_posts_are_not_retried(self):
        row = {"timestamp": 0}
        calls = []
        config = {"label": "Room", "frigate_url": "https://recorder.example"}
        def failure(value):
            calls.append(value)
            raise TimeoutError()
        process(row, config, lambda: None, failure, failure)
        process(row, config, lambda: None, failure, failure)
        self.assertEqual(len(calls), 2)
        self.assertEqual(row["delivery_status"], "unknown")
        self.assertEqual(row["event_status"], "unknown")

    def test_test_message_is_explicit_and_has_exact_clip(self):
        text = message({"timestamp": 0, "test": True, "event_id": "123-abc"},
                       {"label": "Room", "frigate_url": "https://recorder.example"})
        self.assertIn("Реальное открытие не обнаружено", text)
        self.assertIn("/api/events/123-abc/clip.mp4", text)


    def test_blocked_delivery_does_not_delay_next_recording(self):
        recorder = RecordingQueue(lambda: None, lambda row: {"success": True, "event_id": row["id"]})
        threading.Thread(target=recorder.run, daemon=True).start()
        recorder.pending.put({"id": "first", "timestamp": 1})
        recorder.ready.get(timeout=2)  # Delivery remains queued/blocked.
        recorder.pending.put({"id": "second", "timestamp": 2})
        self.assertEqual(recorder.ready.get(timeout=2)["event_id"], "second")

    def test_recovered_incident_keeps_original_recording_time_without_post(self):
        config = {"frigate_url": "https://recorder.example", "api_user": "u", "api_password": "p", "camera": "hall"}
        with patch("ocr.frigate_state_notify.time.time", return_value=1000):
            result = event_creator(config)({"timestamp": 100})
        self.assertNotIn("event_id", result)
        self.assertEqual(result["recording_url"], "https://recorder.example/api/hall/start/90/end/130/clip.mp4")

    def test_unverified_delivery_keeps_receipt_for_reconciliation(self):
        row = {"timestamp": 0, "event_status": "unknown"}
        receipt = {"ok": True, "verified": False, "sent": [{"message_id": 42}]}
        process(row, {"label": "Room"}, lambda: None, None, lambda text: receipt)
        self.assertEqual(row["delivery_status"], "unknown")
        self.assertEqual(row["delivery"], receipt)


if __name__ == "__main__":
    unittest.main()
