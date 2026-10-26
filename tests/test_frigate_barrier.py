import unittest

from ocr.frigate_barrier import CLOSED, OPEN, UNKNOWN, EvidenceGate, camera_healthy, consensus


class EvidenceTests(unittest.TestCase):
    def confirmed(self):
        gate = EvidenceGate()
        for second in range(4):
            state, _ = gate.observe(CLOSED, bytes([second]), second, 100 + second)
        self.assertEqual(state, CLOSED)
        return gate

    def test_failure_never_holds_previous_state(self):
        for healthy, candidate, clock in [(False, CLOSED, b'a'), (True, UNKNOWN, b'b'), (True, CLOSED, None)]:
            gate = self.confirmed()
            self.assertEqual(gate.observe(candidate, clock, 4, 104, healthy)[0], UNKNOWN)

    def test_frozen_and_recovery_require_new_sequence(self):
        gate = self.confirmed()
        self.assertEqual(gate.observe(CLOSED, b'\x03', 6, 106)[0], UNKNOWN)
        self.assertEqual(gate.observe(CLOSED, b'\x04', 7, 107)[0], UNKNOWN)
        self.assertEqual(gate.observe(CLOSED, b'\x05', 8, 108)[0], UNKNOWN)
        self.assertEqual(gate.observe(CLOSED, b'\x06', 9, 109)[0], CLOSED)

    def test_motion_and_state_change_clear_confirmation(self):
        gate = self.confirmed()
        self.assertEqual(gate.observe(OPEN, b'x', 4, 104)[0], UNKNOWN)
        self.assertEqual(gate.observe(UNKNOWN, b'y', 5, 105)[0], UNKNOWN)
        self.assertEqual(gate.observe(OPEN, b'z', 6, 106)[0], UNKNOWN)

    def test_camera_health_is_current_not_just_running(self):
        def stats(updated=100, fps=1):
            return {'service': {'last_updated': updated}, 'cameras': {'gate': {'camera_fps': fps}}}
        self.assertTrue(camera_healthy(stats(), 'gate', 101))
        for data in ({}, stats(50), stats(fps=0), stats(float('nan'))):
            self.assertFalse(camera_healthy(data, 'gate', 101))

    def test_conflict_or_missing_view_is_unknown(self):
        for states in ([], [CLOSED, OPEN], [OPEN, UNKNOWN], [UNKNOWN]):
            self.assertEqual(consensus(states), UNKNOWN)
        self.assertEqual(consensus([OPEN, OPEN]), OPEN)


if __name__ == '__main__':
    unittest.main()
