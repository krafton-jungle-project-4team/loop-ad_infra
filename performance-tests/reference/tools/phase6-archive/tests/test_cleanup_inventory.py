from __future__ import annotations

import unittest

from cleanup_inventory import cleanup_result, select_owned


class CleanupInventoryTest(unittest.TestCase):
    def test_requires_both_exact_labels(self):
        records = [
            {"Name": "owned", "Labels": {"loopad.local_session_id": "session", "com.docker.compose.project": "project"}},
            {"Name": "wrong-session", "Labels": {"loopad.local_session_id": "other", "com.docker.compose.project": "project"}},
            {"Name": "wrong-project", "Labels": {"loopad.local_session_id": "session", "com.docker.compose.project": "other"}},
        ]
        self.assertEqual(
            [item["Name"] for item in select_owned(records, local_session_id="session", compose_project="project")],
            ["owned"],
        )

    def test_zero_inventory_is_required(self):
        passed = cleanup_result([], [], local_session_id="session", compose_project="project")
        self.assertTrue(passed["passed"])
        blocked = cleanup_result(
            [],
            [{"Name": "v", "Labels": {"loopad.local_session_id": "session", "com.docker.compose.project": "project"}}],
            local_session_id="session",
            compose_project="project",
        )
        self.assertFalse(blocked["passed"])


if __name__ == "__main__":
    unittest.main()
