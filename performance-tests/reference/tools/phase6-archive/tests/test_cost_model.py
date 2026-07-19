from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from cost_model import calculate, load_prices


class CostModelTest(unittest.TestCase):
    def test_checked_in_fixture_stays_below_hard_cap(self):
        fixture = Path(__file__).parents[1] / "price-fixtures" / "ap-northeast-2-20260716.json"
        result = calculate(load_prices(fixture))
        self.assertTrue(result["passed"])
        self.assertLessEqual(Decimal(result["deterministicMaximumUsd"]), Decimal("15"))
        self.assertFalse(result["livePriceLookup"])


if __name__ == "__main__":
    unittest.main()
