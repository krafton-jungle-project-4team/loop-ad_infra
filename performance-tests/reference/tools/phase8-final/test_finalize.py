#!/usr/bin/env python3
"""Focused tests for the unpaid Phase 8 post-promotion verifier."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


FINAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(FINAL_DIR))

import finalize  # noqa: E402


class Phase8PostPromotionVerificationTest(unittest.TestCase):
    def promoted_ledger(self) -> dict:
        return finalize.read_json(finalize.ROOT / finalize.LEDGER)

    def test_promoted_campaign_revalidates_without_aws(self) -> None:
        pre_promotion, promotion = finalize.validate_promoted_campaign(
            self.promoted_ledger()
        )
        context = finalize.validate_inputs(pre_promotion)
        result = finalize.verify_created_outputs(promotion)

        self.assertEqual("promoted", self.promoted_ledger()["status"])
        self.assertEqual(
            "0.000000",
            self.promoted_ledger()["budget"]["activeEpochAccruedUpperBoundUsd"],
        )
        self.assertEqual(
            "60.000000",
            self.promoted_ledger()["budget"]["remainingBeforeHardCapUsd"],
        )
        self.assertEqual("11.227766", context["accrued"])
        self.assertEqual("phase8-post-promotion-verification", result["recordType"])
        self.assertEqual(0, result["awsRequests"])
        self.assertFalse(result["paidAwsExperiment"])
        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))

    def test_attempt_rewrite_is_rejected(self) -> None:
        ledger = copy.deepcopy(self.promoted_ledger())
        ledger["attempts"][16]["verdict"] = "passed"

        with self.assertRaisesRegex(RuntimeError, "immutable campaign data"):
            finalize.validate_promoted_campaign(ledger)

    def test_paid_work_reenable_is_rejected(self) -> None:
        ledger = copy.deepcopy(self.promoted_ledger())
        ledger["budget"]["newPaidWorkAuthorized"] = True

        with self.assertRaisesRegex(RuntimeError, "fail-closed"):
            finalize.validate_promoted_campaign(ledger)

    def test_promotion_artifact_hash_rewrite_is_rejected(self) -> None:
        ledger = copy.deepcopy(self.promoted_ledger())
        ledger["promotion"]["manifest"]["fileSha256"] = "0" * 64

        with self.assertRaisesRegex(RuntimeError, "manifest binding"):
            finalize.validate_promoted_campaign(ledger)

    def test_cost_reset_authorization_hash_rewrite_is_rejected(self) -> None:
        ledger = copy.deepcopy(self.promoted_ledger())
        ledger["budgetEpochs"][-1]["authorizationRecord"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(RuntimeError, "cost reset authorization hash"):
            finalize.validate_promoted_campaign(ledger)


if __name__ == "__main__":
    unittest.main()
