from __future__ import annotations

import unittest

from integration_runner import LOCAL_QUERY_MEMORY_BYTES, make_config, wait_for_quiescence


class FakeClickHouse:
    def __init__(self, activity):
        self.activity = list(activity)
        self.index = 0

    def one(self, _query):
        value = self.activity[min(self.index, len(self.activity) - 1)]
        self.index += 1
        return {"merges": value[0], "mutations": value[1]}


class FakeTime:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class QuiescenceTest(unittest.TestCase):
    def test_local_query_limit_is_between_server_and_container_caps(self):
        config = make_config(
            bucket="phase6-unit-memory",
            run_id="run_unit_memory",
            rows=3000,
            rows_per_part=1000,
            part_count=3,
            image_digest="sha256:test",
            code_sha256="test",
            production=False,
        )
        self.assertEqual(config.clickhouse_memory_bytes, LOCAL_QUERY_MEMORY_BYTES)
        self.assertEqual(LOCAL_QUERY_MEMORY_BYTES, 4831838208)
        self.assertLess(LOCAL_QUERY_MEMORY_BYTES, 5261334937)

    def test_waits_until_two_consecutive_quiet_observations(self):
        clock = FakeTime()
        result = wait_for_quiescence(
            FakeClickHouse([(1, 0), (0, 0), (0, 0)]),
            timeout_seconds=10,
            poll_seconds=1,
            consecutive_observations=2,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(result["samples"][-1]["consecutiveQuietObservations"], 2)

    def test_activity_resets_the_quiet_streak(self):
        clock = FakeTime()
        result = wait_for_quiescence(
            FakeClickHouse([(0, 0), (1, 0), (0, 0), (0, 0)]),
            timeout_seconds=10,
            poll_seconds=1,
            consecutive_observations=2,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual([sample["consecutiveQuietObservations"] for sample in result["samples"]], [1, 0, 1, 2])

    def test_timeout_is_evidence_not_an_exception(self):
        clock = FakeTime()
        result = wait_for_quiescence(
            FakeClickHouse([(1, 0)]),
            timeout_seconds=2,
            poll_seconds=1,
            consecutive_observations=2,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["waitedSeconds"], 2)
        self.assertEqual(len(result["samples"]), 3)


if __name__ == "__main__":
    unittest.main()
