import unittest

from ocr.frigate_protected import Movement, Observation, vehicle_near


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

    def test_occlusion_cannot_be_impact(self):
        detector = self.armed()
        self.assertEqual(detector.observe(Observation(112), True), "unknown")
        for ts in (116, 120, 124):
            self.assertNotEqual(detector.observe(Observation(ts, (9, 0), .9, True), True), "possible_impact")

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


if __name__ == "__main__":
    unittest.main()
