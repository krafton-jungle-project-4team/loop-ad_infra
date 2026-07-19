#!/usr/bin/env python3
"""Durably seal one fresh scoped attempt before the paid image boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    file_sha256,
    read_json,
    scoped_diagnostic_source_checks,
    utc_now,
    validate_identifiers,
    write_json,
)
from full_stack_scoped_cost_model import (
    canonical_sha256,
    validate_campaign_ledger,
    validate_cost_model,
)


ATTEMPT_TYPE = "aws-full-stack-scoped-diagnostic"


def seal(args: argparse.Namespace) -> dict[str, Any]:
    root = args.infra_root.resolve()
    ledger_path = args.attempt_ledger.resolve()
    expected_ledger = (
        root / "performance-tests/phase7_2-stabilization/attempt-ledger.json"
    ).resolve()
    if ledger_path != expected_ledger:
        raise RuntimeError("attempt seal requires the exact campaign ledger")
    validate_identifiers(args.run_id, args.session_id)
    if args.output.exists():
        raise FileExistsError("scoped attempt admission seal is immutable")
    source_checks, source = scoped_diagnostic_source_checks(
        root, args.scoped_diagnostic_source
    )
    if not all(check.passed for check in source_checks):
        raise RuntimeError("scoped source seal failed admission revalidation")
    prices = read_json(args.prices)
    cost = read_json(args.cost_model)
    absent = read_json(args.absent_preflight)
    ledger = read_json(ledger_path)
    prior, epoch_id, admission_ledger_sha256 = validate_campaign_ledger(ledger)
    if not validate_cost_model(
        prices,
        ledger,
        cost,
        expected_run_id=args.run_id,
        expected_session_id=args.session_id,
    ):
        raise RuntimeError("scoped cost model failed admission revalidation")
    if cost.get("campaignLedgerSha256") != admission_ledger_sha256:
        raise RuntimeError("cost model is not bound to the admission ledger")
    source_tree = source.get("implementationTreeSha256")
    expected_cost_authorization = {
        "campaignLedgerSha256": cost.get("campaignLedgerSha256"),
        "priceDocumentSha256": cost.get("priceDocumentSha256"),
        "phase8PromotionPolicySha256": cost.get(
            "phase8PromotionPolicySha256"
        ),
    }
    if (
        absent.get("passed") is not True
        or absent.get("runId") != args.run_id
        or absent.get("sessionId") != args.session_id
        or absent.get("imageState") != "absent"
        or absent.get("attemptType") != ATTEMPT_TYPE
        or absent.get("promotionEligible") is not False
        or absent.get("sourceAuthorization", {}).get("implementationTreeSha256")
        != source_tree
        or absent.get("costAuthorization") != expected_cost_authorization
        or absent.get("imageAuthorization") is not None
    ):
        raise RuntimeError("absent preflight is not the exact unpaid admission gate")
    attempts = ledger.get("attempts", [])
    if any(
        item.get("runId") == args.run_id or item.get("sessionId") == args.session_id
        for item in attempts
        if isinstance(item, dict)
    ):
        raise RuntimeError("scoped attempt identity is not fresh")

    ordinal = len(attempts) + 1
    sealed_at = utc_now()
    active = {
        "ordinal": ordinal,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "state": "sealed-unpaid",
        "sealedAt": sealed_at,
        "paidStartedAt": None,
        "activeEpochId": epoch_id,
        "activeEpochPriorUpperBoundUsd": str(cost["activeEpochPriorUpperBoundUsd"]),
        "chargedOperationalUpperBoundUsd": str(
            cost["chargedOperationalUpperBoundUsd"]
        ),
        "maximumIncludingCleanupUsd": str(cost["maximumIncludingCleanupUsd"]),
        "admissionLedgerSha256": admission_ledger_sha256,
        "costAuthorization": expected_cost_authorization,
        "immutableInputs": {
            "sourcePath": str(args.scoped_diagnostic_source.resolve().relative_to(root)),
            "sourceSha256": file_sha256(args.scoped_diagnostic_source),
            "implementationTreeSha256": source_tree,
            "pricesPath": str(args.prices.resolve().relative_to(root)),
            "pricesSha256": file_sha256(args.prices),
            "costModelPath": str(args.cost_model.resolve().relative_to(root)),
            "costModelSha256": file_sha256(args.cost_model),
            "absentPreflightPath": str(args.absent_preflight.resolve().relative_to(root)),
            "absentPreflightSha256": file_sha256(args.absent_preflight),
        },
        "stageMaximumAttempts": {
            "imagePreparation": 1,
            "imageStackDeploy": 1,
            "deploy": 1,
            "verify": 1,
            "seed": 1,
            "archive": 1,
            "collect": 1,
            "cleanup": 1,
            "inventory": 1,
        },
    }
    next_ledger = dict(ledger)
    next_ledger["activeAttempt"] = active
    budget = dict(ledger["budget"])
    budget["currentAttemptOrdinal"] = ordinal
    budget["currentAttemptPaidStartAt"] = None
    budget["currentAttemptReservedOperationalUpperBoundUsd"] = str(
        cost["chargedOperationalUpperBoundUsd"]
    )
    budget["currentAttemptMaximumIncludingCleanupUsd"] = str(
        cost["maximumIncludingCleanupUsd"]
    )
    next_ledger["budget"] = budget
    next_ledger["updatedAt"] = sealed_at
    write_json(ledger_path, next_ledger)
    result = {
        "schemaVersion": 1,
        "recordType": "phase7-full-stack-scoped-attempt-admission-seal",
        "sealedAt": sealed_at,
        "ordinal": ordinal,
        "runId": args.run_id,
        "sessionId": args.session_id,
        "attemptType": ATTEMPT_TYPE,
        "promotionEligible": False,
        "paidBoundaryCrossed": False,
        "activeEpochPriorUpperBoundUsd": str(prior),
        "admissionLedgerSha256": admission_ledger_sha256,
        "sealedLedgerSha256": canonical_sha256(next_ledger),
        "activeAttempt": active,
    }
    write_json(args.output, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--scoped-diagnostic-source", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--cost-model", required=True, type=Path)
    parser.add_argument("--absent-preflight", required=True, type=Path)
    parser.add_argument("--attempt-ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = seal(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
