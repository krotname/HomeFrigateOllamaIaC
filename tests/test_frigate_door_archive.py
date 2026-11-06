import copy
import threading
import unittest

from ocr.frigate_door_archive import APIError, ArchiveQueue, archive_once, needs_archive, trim_history
from ocr.frigate_state_notify import RecordingQueue


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.config = {"camera": "hall", "label": "My room"}
        self.row = {"id": "real-opening", "timestamp": 100, "archive_required": True,
                    "delivery_status": "verified"}
        self.calls = []
        self.exports = []
        self.saved = []

    def save(self):
        self.saved.append(copy.deepcopy(self.row))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.exports[0] if path != "/api/exports" else self.exports
        self.assertEqual(self.saved[-1]["archive"]["status"], "started")
        self.assertEqual(path, "/api/export/hall/start/90/end/130")
        self.exports.append({"id": "hall_export", "camera": "hall", "name": body["name"],
                             "in_progress": True, "video_path": "/media/frigate/exports/clip.mp4"})
        return {"success": True, "export_id": "hall_export"}

    def tick(self, now=200, request=None):
        archive_once(self.row, self.config, request or self.request, self.save, now=now)

    def test_postroll_queued_then_native_completion(self):
        self.tick(149)
        self.assertEqual(self.calls, [])
        self.tick(150)
        self.assertEqual(self.row["archive"]["status"], "queued")
        self.assertTrue(needs_archive(self.row))
        self.tick()
        self.assertEqual(self.row["archive"]["status"], "queued")
        self.exports[0]["in_progress"] = False
        self.tick()
        self.assertFalse(needs_archive(self.row))
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_restart_after_lost_post_response_reconciles_without_duplicate(self):
        def lost_response(method, path, body=None):
            result = self.request(method, path, body)
            if method == "POST":
                raise TimeoutError()
            return result
        self.tick(request=lost_response)
        self.row = copy.deepcopy(self.saved[-1])  # Reload persisted state.
        self.exports[0]["in_progress"] = False
        self.tick()
        self.assertEqual(self.row["archive"]["status"], "complete")
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_ambiguous_missing_export_is_not_reposted(self):
        def unavailable(method, path, body=None):
            self.calls.append((method, path, body))
            if method == "POST":
                raise TimeoutError()
            return []
        self.tick(request=unavailable)
        self.tick(request=unavailable)
        self.tick(request=unavailable)
        self.assertEqual(self.row["archive"]["status"], "unknown")
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_explicit_queue_full_rejection_can_retry_after_delay(self):
        def full(method, path, body=None):
            if method == "POST":
                raise APIError(503)
            return []
        self.tick(request=full)
        self.assertEqual(self.row["archive"]["status"], "pending")
        self.tick(259)
        self.assertEqual(self.calls, [])
        self.tick(260)
        self.assertEqual(self.row["archive"]["status"], "queued")

    def test_lookup_failure_never_makes_an_ambiguous_post_retryable(self):
        self.tick()
        self.row["archive"].pop("export_id")
        self.row["archive"]["status"] = "started"
        def unavailable(*args):
            raise APIError(503)
        self.tick(request=unavailable)
        self.assertEqual(self.row["archive"]["status"], "started")

    def test_test_alarm_is_excluded_by_default(self):
        self.row.update(test=True, archive_required=False)
        self.tick()
        self.assertEqual(self.calls, [])
        self.row["archive_required"] = True  # Explicitly requested technical export.
        self.tick()
        self.assertTrue(self.row["archive"]["name"].startswith("TEST |"))

    def test_unfinished_exports_survive_bounded_notification_history(self):
        self.tick(100)
        finished = [{"id": n, "delivery_status": "verified"} for n in range(1100)]
        rows = trim_history([self.row] + finished)
        self.assertIn(self.row, rows)
        self.assertEqual(len(rows), 1001)

    def test_export_identity_mismatch_is_never_accepted(self):
        self.tick()
        self.exports[0].update(camera="other", in_progress=False)
        self.tick()
        self.assertNotEqual(self.row["archive"]["status"], "complete")
        self.assertEqual(self.row["archive"]["error"], "ValueError")

    def test_blocked_export_does_not_block_recording_queue(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(3)
            return []
        row = dict(self.row, timestamp=0)
        queue = ArchiveQueue({"incidents": [row]}, self.config, lambda: None,
                             threading.RLock(), request=blocked)
        thread = threading.Thread(target=queue.run, args=(stop,), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            recorder = RecordingQueue(lambda: None, lambda row: {"success": True, "event_id": "next"})
            threading.Thread(target=recorder.run, daemon=True).start()
            recorder.pending.put({"timestamp": 0})
            self.assertEqual(recorder.ready.get(timeout=2)["event_id"], "next")
        finally:
            stop.set()
            release.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
