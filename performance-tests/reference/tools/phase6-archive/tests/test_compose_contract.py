from __future__ import annotations

import unittest
from pathlib import Path


class ComposeContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_clickhouse_container_and_server_memory_caps_are_explicit(self):
        compose = (self.root / "docker-compose.yml").read_text(encoding="utf-8")
        memory = (self.root / "clickhouse-config/memory.xml").read_text(encoding="utf-8")
        self.assertIn("mem_limit: 5g", compose)
        self.assertIn("phase6-memory.xml:ro", compose)
        self.assertIn("<max_server_memory_usage>5261334937</max_server_memory_usage>", memory)
        self.assertIn("<max_server_memory_usage_to_ram_ratio>0.98</max_server_memory_usage_to_ram_ratio>", memory)


if __name__ == "__main__":
    unittest.main()
