import unittest

from ocr.frigate_state_notify import Transitions, message, process


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


if __name__ == "__main__":
    unittest.main()
