import unittest

from local_gate import nearest_rank_percentile


class LocalGateTest(unittest.TestCase):
    def test_nearest_rank_p95_is_deterministic(self) -> None:
        self.assertEqual(nearest_rank_percentile([], 0.95), 0.0)
        self.assertEqual(nearest_rank_percentile([7.0], 0.95), 7.0)
        self.assertEqual(nearest_rank_percentile([float(value) for value in range(1, 101)], 0.95), 95.0)
