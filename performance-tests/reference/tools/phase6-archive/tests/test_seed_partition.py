from __future__ import annotations

import unittest
from datetime import date

from seed_partition import (
    DEFAULT_SEED,
    FULL_SCALE_ROWS,
    GENERATOR_VERSION,
    GeneratorContract,
    generator_select_sql,
    is_eligible,
    seed_insert_sql,
    utc_source_partition,
)


class GeneratorTest(unittest.TestCase):
    def contract(self) -> GeneratorContract:
        return GeneratorContract(
            version=GENERATOR_VERSION,
            seed=DEFAULT_SEED,
            partition="2026-07-09",
            rows=FULL_SCALE_ROWS,
            run_id="run_phase6",
        )

    def test_partition_and_eligibility_are_utc_date_rules(self):
        today = date(2026, 7, 17)
        self.assertEqual(utc_source_partition(today), date(2026, 7, 9))
        self.assertTrue(is_eligible(date(2026, 7, 9), today))
        self.assertFalse(is_eligible(date(2026, 7, 10), today))

    def test_reference_hash_is_deterministic_and_contract_sensitive(self):
        contract = self.contract()
        self.assertEqual(contract.reference_sha256(), contract.reference_sha256())
        changed = GeneratorContract(**{**contract.__dict__, "seed": contract.seed + 1})
        self.assertNotEqual(contract.reference_sha256(), changed.reference_sha256())

    def test_generator_uses_numbers_without_python_materialization(self):
        sql = seed_insert_sql(self.contract())
        self.assertIn("FROM numbers(0, 15000000)", sql)
        self.assertNotIn("VALUES", sql)
        self.assertIn("toUInt256", sql)

    def test_reference_ranges_are_bounded(self):
        sql = generator_select_sql(
            self.contract(), offset=5_000_000, rows=5_000_000, include_event_date=True
        )
        self.assertIn("FROM numbers(5000000, 5000000)", sql)
        with self.assertRaises(ValueError):
            generator_select_sql(
                self.contract(), offset=14_000_000, rows=2_000_000, include_event_date=True
            )


if __name__ == "__main__":
    unittest.main()
