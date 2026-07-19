from __future__ import annotations

import unittest

from preflight import (
    CONTAINER_MEMORY_BYTES,
    GIB,
    QUERY_MEMORY_BYTES,
    SERVER_MEMORY_BYTES,
    LocalPreflight,
    evaluate_local_preflight,
)


class PreflightTest(unittest.TestCase):
    def test_passes_only_with_disk_memory_and_zero_starting_volumes(self):
        result = evaluate_local_preflight(
            LocalPreflight(
                31 * GIB,
                79.9,
                8 * GIB,
                CONTAINER_MEMORY_BYTES,
                SERVER_MEMORY_BYTES,
                QUERY_MEMORY_BYTES,
                0,
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["awsCalls"], 0)

    def test_each_guard_can_block(self):
        result = evaluate_local_preflight(
            LocalPreflight(29 * GIB, 80, 5 * GIB, 5 * GIB, 5 * GIB, 5 * GIB, 1)
        )
        self.assertFalse(result["passed"])
        self.assertFalse(all(result["checks"].values()))

    def test_rejects_each_memory_contract_drift(self):
        for container, server, query in [
            (CONTAINER_MEMORY_BYTES - 1, SERVER_MEMORY_BYTES, QUERY_MEMORY_BYTES),
            (CONTAINER_MEMORY_BYTES, SERVER_MEMORY_BYTES - 1, QUERY_MEMORY_BYTES),
            (CONTAINER_MEMORY_BYTES, SERVER_MEMORY_BYTES, QUERY_MEMORY_BYTES + 1),
        ]:
            with self.subTest(container=container, server=server, query=query):
                result = evaluate_local_preflight(
                    LocalPreflight(31 * GIB, 79.9, 8 * GIB, container, server, query, 0)
                )
                self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
