from __future__ import annotations

import unittest
from pathlib import Path


class SystemdContractTest(unittest.TestCase):
    def test_service_is_oneshot_and_uses_nonblocking_flock(self):
        root = Path(__file__).parents[1] / "systemd"
        service = (root / "loopad-phase6-archive.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("/usr/bin/flock -n -E 75", service)
        self.assertIn("TimeoutStartSec=30min", service)

    def test_production_timer_is_daily_persistent_0115_utc(self):
        root = Path(__file__).parents[1] / "systemd"
        timer = (root / "loopad-phase6-archive.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 01:15:00 UTC", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
