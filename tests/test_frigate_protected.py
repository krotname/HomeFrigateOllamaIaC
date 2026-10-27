import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ocr.frigate_protected import Api, Movement, Observation, vehicle_near


class MovementTests(unittest.TestCase):
    def armed(self):
        detector = Movement()
        for ts in (100, 104, 108):
            detector.observe(Observation(ts, (0, 0), .9, True), False)
        return detector

    def test_passing_vehicle_without_object_movement(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(112, (0, 0), .9, True), True), "present")

    def test_persistent_shift_with_vehicle(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(112, (7, 0), .9, True), True), "movement_pending")
        self.assertEqual(detector.observe(Observation(116, (7, 1), .9, True), False), "possible_impact")
        self.assertEqual(detector.observe(Observation(120, (7, 1), .9, True), True), "present")

    def test_occlusion_without_movement_cannot_be_impact(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(112), True), "unknown")
        for ts in (116, 120, 124):
            self.assertEqual(detector.observe(Observation(ts, (0, 0), .9, True), True), "present")

    def test_movement_during_brief_occlusion_is_confirmed_on_reappearance(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(112), True), "unknown")
        self.assertEqual(detector.observe(Observation(116, (9, 0), .9, True), True),
                         "movement_pending")
        self.assertEqual(detector.observe(Observation(120, (9, 0), .9, True), True),
                         "possible_impact")

    def test_long_occlusion_requires_a_new_baseline(self):
        detector = self.armed()
        for ts in range(112, 149, 4):
            self.assertEqual(detector.observe(Observation(ts), True), "unknown")
        for ts in (152, 156, 160):
            self.assertNotEqual(detector.observe(Observation(ts, (9, 0), .9, True), True),
                                "possible_impact")

    def test_gap_cannot_be_impact(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(160, (10, 0), .9, True), True), "acquiring")

    def test_shift_without_vehicle_does_not_report_collision(self):
        detector = self.armed()
        detector.observe(Observation(112, (7, 0), .9, True), False)
        self.assertEqual(detector.observe(Observation(116, (7, 0), .9, True), False), "moved")

    def test_one_bad_match_cannot_alarm(self):
        detector = self.armed()
        detector.observe(Observation(112, (8, 0), .9, True), True)
        self.assertEqual(detector.observe(Observation(116, (0, 0), .9, True), True), "present")

    def test_vehicle_tracks_are_time_and_position_bound(self):
        event = {"label": "car", "data": {"path_data": [[[.6, .2], 100]]}}
        area = [.5, .1, .7, .3]
        self.assertTrue(vehicle_near([event], 110, area))
        self.assertFalse(vehicle_near([event], 130, area))
        self.assertFalse(vehicle_near([event], 110, [.1, .1, .2, .3]))
        event["label"] = "person"
        self.assertFalse(vehicle_near([event], 110, area))


class ApiTests(unittest.TestCase):
    def test_parked_vehicle_with_fresh_track_is_included_once(self):
        parked = {"id": "parked", "start_time": 1, "label": "car",
                  "data": {"path_data": [[[.6, .2], 300]]}}
        recent = {"id": "recent", "start_time": 290, "label": "person"}
        api = Api()
        with patch.object(api, "json", side_effect=[[recent], [recent, parked]]) as fetch:
            candidates = api.candidates("common_camera04", 310)
        self.assertEqual({row["id"] for row in candidates}, {"recent", "parked"})
        self.assertEqual(len(candidates), 2)
        active_query = parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)
        self.assertEqual(active_query["in_progress"], ["1"])
        self.assertNotIn("after", active_query)
        self.assertTrue(vehicle_near(candidates, 310, [.5, .1, .7, .3]))

    def test_recording_disappearance_is_rejected_with_fresh_coverage(self):
        with patch("ocr.frigate_protected.request_bytes", side_effect=[
                b'[{"id":"segment","start_time":90,"end_time":110}]', b"image", b"[]"
        ]) as fetch:
            with self.assertRaisesRegex(ValueError, "disappeared"):
                Api().frame("common_camera04", 100)
        self.assertTrue(fetch.call_args_list[0].kwargs["bypass_cache"])
        self.assertTrue(fetch.call_args_list[2].kwargs["bypass_cache"])


if __name__ == "__main__":
    unittest.main()
