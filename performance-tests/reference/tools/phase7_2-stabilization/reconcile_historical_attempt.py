#!/usr/bin/env python3
"""Build a conservative deterministic charge for the failed historical attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any


EXPECTED_RUN_ID = "run_20260717_225316_phase7_integration"
EXPECTED_SESSION_ID = "phase7-integration-20260717T225316Z"
MONTH_HOURS = Decimal("730")
PUBLIC_IPV4_HOUR_USD = Decimal("0.005")
COST_EXPLORER_QUERY_USD = Decimal("0.01")
FAILURE_PATH_RESERVE_USD = Decimal("0.5")
EVIDENCE_AND_TRANSFER_RESERVE_USD = Decimal("1.5")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def component(
    name: str, quantity: Decimal, unit: str, unit_price: Decimal, note: str
) -> dict[str, str]:
    return {
        "name": name,
        "quantity": format(quantity.normalize(), "f"),
        "unit": unit,
        "unitPriceUsd": format(unit_price.normalize(), "f"),
        "costUsd": money(quantity * unit_price),
        "note": note,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--deploy-log", required=True, type=Path)
    parser.add_argument("--image-manifest", required=True, type=Path)
    parser.add_argument("--cost-explorer", required=True, type=Path)
    parser.add_argument("--cleanup-verification", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    run = read_json(args.run)
    prices_document = read_json(args.prices)
    image_manifest = read_json(args.image_manifest)
    cost_explorer = read_json(args.cost_explorer)
    cleanup = read_json(args.cleanup_verification)
    deploy_log = args.deploy_log.read_text(encoding="utf-8")

    if run.get("runId") != EXPECTED_RUN_ID or run.get("sessionId") != EXPECTED_SESSION_ID:
        raise ValueError("historical run identity mismatch")
    deploy_attempts = [
        item for item in run.get("stageAttempts", []) if item.get("stage") == "deploy"
    ]
    if len(deploy_attempts) != 1 or deploy_attempts[0].get("attempt") != 1:
        raise ValueError("historical run must contain exactly one deploy attempt")
    if run.get("failedStage") != "deploy":
        raise ValueError("historical run did not fail at deploy")
    if "InvalidUserData.Malformed" not in deploy_log or "limited to 16384 bytes" not in deploy_log:
        raise ValueError("historical deploy log does not contain the exact user-data failure")
    if "AWS::AutoScaling::AutoScalingGroup" in deploy_log or "AWS::EC2::Instance" in deploy_log:
        raise ValueError("compute appeared in the failed deploy log; this model would undercharge")
    if image_manifest.get("runId") != EXPECTED_RUN_ID or len(image_manifest.get("images", [])) != 3:
        raise ValueError("historical image manifest mismatch")
    if cleanup.get("allZero") is not True or cleanup.get("taggingApiResiduals") != []:
        raise ValueError("historical cleanup is not authoritatively zero")

    paid_start = parse_utc(str(run["paidStartedAt"]))
    paid_end = parse_utc(str(run["completedAt"]))
    elapsed_seconds = Decimal(str((paid_end - paid_start).total_seconds()))
    if elapsed_seconds <= 0:
        raise ValueError("paid elapsed time must be positive")
    billed_hours = Decimal(math.ceil(elapsed_seconds / Decimal("3600")))
    storage_hours = Decimal("24")

    raw_prices = prices_document.get("prices")
    if prices_document.get("region") != "ap-northeast-2" or not isinstance(raw_prices, dict):
        raise ValueError("historical price document mismatch")
    prices = {key: Decimal(str(value)) for key, value in raw_prices.items()}
    storage_month_fraction = storage_hours / MONTH_HOURS
    components = [
        component(
            "Kinesis provisioned shards",
            Decimal("120") * billed_hours,
            "shard-hour",
            prices["kinesisProvisionedShardHour"],
            "All 120 shards are charged for one full-hour quantum although the stream existed for under three minutes.",
        ),
        component(
            "three Network Load Balancers",
            Decimal("3") * billed_hours,
            "NLB-hour",
            prices["nlbHour"],
            "All three template NLBs are charged for one full-hour quantum.",
        ),
        component(
            "idle NLB capacity allowance",
            Decimal("3") * billed_hours,
            "NLCU-hour",
            prices["nlbLcuHour"],
            "One NLCU-hour per NLB is reserved despite no workload traffic.",
        ),
        component(
            "single NAT gateway",
            billed_hours,
            "gateway-hour",
            prices["natGatewayHour"],
            "The partial NAT gateway hour is rounded up.",
        ),
        component(
            "NAT data allowance",
            Decimal("0.1"),
            "GiB",
            prices["natGatewayDataGb"],
            "Control-plane traffic allowance; no workload ran.",
        ),
        component(
            "public IPv4 address",
            billed_hours,
            "IPv4-hour",
            PUBLIC_IPV4_HOUR_USD,
            "One NAT Elastic IP is charged for one full-hour quantum.",
        ),
        component(
            "Secrets Manager storage",
            storage_month_fraction,
            "secret-month",
            prices["secretsManagerSecretMonth"],
            "One secret is conservatively charged for a full day.",
        ),
        component(
            "ECR image storage",
            Decimal("3") * storage_month_fraction,
            "GiB-month",
            prices["ecrStorageGbMonth"],
            "Three one-GiB image allowances are charged for a full day.",
        ),
        component(
            "DynamoDB storage",
            Decimal("0.3") * storage_month_fraction,
            "GB-month",
            prices["dynamoDbStorageGbMonth"],
            "Three empty control tables receive a full-day storage allowance.",
        ),
        component(
            "DynamoDB control reads",
            Decimal("10000"),
            "read unit",
            prices["dynamoDbReadRequestUnit"],
            "No data workload ran; this is a control-plane allowance.",
        ),
        component(
            "DynamoDB control writes",
            Decimal("10000"),
            "write unit",
            prices["dynamoDbWriteRequestUnit"],
            "No data workload ran; this is a control-plane allowance.",
        ),
        component(
            "S3 storage",
            Decimal("6") * storage_month_fraction,
            "GB-month",
            prices["s3StandardStorageGbMonth"],
            "Two empty buckets receive the full successful-run storage allowance for one day.",
        ),
        component(
            "CloudWatch log ingestion allowance",
            Decimal("0.01"),
            "GiB",
            prices["cloudWatchLogIngestGb"],
            "No ECS hosts started; a small control-plane margin is retained.",
        ),
        component(
            "Cost Explorer query",
            Decimal("1"),
            "query",
            COST_EXPLORER_QUERY_USD,
            "One delayed tag-attribution query was executed.",
        ),
        component(
            "evidence and transfer reserve",
            Decimal("1"),
            "fixed reserve",
            EVIDENCE_AND_TRANSFER_RESERVE_USD,
            "Preserves the original model's unmodeled evidence and transfer reserve.",
        ),
        component(
            "failure-path reserve",
            Decimal("1"),
            "fixed reserve",
            FAILURE_PATH_RESERVE_USD,
            "Covers delayed billing and rollback uncertainty.",
        ),
    ]
    modeled = sum((Decimal(item["costUsd"]) for item in components), Decimal("0"))
    charged = modeled.quantize(Decimal("1"), rounding=ROUND_CEILING)

    observed = (
        cost_explorer.get("ResultsByTime", [{}])[0]
        .get("Total", {})
        .get("UnblendedCost", {})
    )
    result = {
        "schemaVersion": 1,
        "runId": EXPECTED_RUN_ID,
        "sessionId": EXPECTED_SESSION_ID,
        "calculatedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "paidStartedAt": run["paidStartedAt"],
        "paidEndedAt": run["completedAt"],
        "paidElapsedSeconds": format(elapsed_seconds.normalize(), "f"),
        "paidBillingHoursCeiling": format(billed_hours.normalize(), "f"),
        "observedCostUsd": money(Decimal(str(observed.get("Amount", "0")))),
        "observedCostEstimated": bool(
            cost_explorer.get("ResultsByTime", [{}])[0].get("Estimated")
        ),
        "observedCostDisposition": "delayed evidence; not used as a zero-cost assumption",
        "components": components,
        "modeledFailurePathUpperBoundUsd": money(modeled),
        "campaignChargedUpperBoundUsd": money(charged),
        "roundingPolicy": "round the evidence-backed failure-path model up to the next whole USD",
        "computeInstancesStarted": 0,
        "loadRequestsStarted": 0,
        "archiveTasksStarted": 0,
        "cleanupAllZero": True,
        "inputs": {
            "run": {"path": str(args.run), "sha256": sha256(args.run)},
            "prices": {"path": str(args.prices), "sha256": sha256(args.prices)},
            "deployLog": {
                "path": str(args.deploy_log),
                "sha256": sha256(args.deploy_log),
            },
            "imageManifest": {
                "path": str(args.image_manifest),
                "sha256": sha256(args.image_manifest),
            },
            "costExplorer": {
                "path": str(args.cost_explorer),
                "sha256": sha256(args.cost_explorer),
            },
            "cleanupVerification": {
                "path": str(args.cleanup_verification),
                "sha256": sha256(args.cleanup_verification),
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
