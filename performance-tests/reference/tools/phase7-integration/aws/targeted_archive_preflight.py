#!/usr/bin/env python3
"""Read-only absent-state admission gate before a targeted archive image build."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from targeted_archive_cleanup import describe_stack, inventory
from targeted_archive_common import (
    EXPECTED_OPERATOR_ARN,
    EXPECTED_REGION,
    SDK_CONFIG,
    git_identity,
    locked_session,
    source_closure,
    utc_now,
    validate_identifiers,
    write_json,
)


INSTANCE_TYPE = "r7g.2xlarge"


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def decimal_field(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"targeted cost model has invalid {name}") from error


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    validate_identifiers(args.run_id, args.session_id)
    args.infra_root = args.infra_root.resolve()
    cost = read_object(args.cost_model)
    session = locked_session()
    identity = session.client("sts", config=SDK_CONFIG).get_caller_identity()
    cloudformation = session.client("cloudformation", config=SDK_CONFIG)
    bootstrap = describe_stack(cloudformation, "CDKToolkit")
    absent = inventory(session, args.run_id, args.session_id)
    ec2 = session.client("ec2", config=SDK_CONFIG)
    images = ec2.describe_images(ImageIds=[args.arm_ami]).get("Images", [])
    offerings = ec2.describe_instance_type_offerings(
        LocationType="region",
        Filters=[{"Name": "instance-type", "Values": [INSTANCE_TYPE]}],
    ).get("InstanceTypeOfferings", [])
    quota = session.client("service-quotas", config=SDK_CONFIG).get_service_quota(
        ServiceCode="ec2", QuotaCode="L-1216C47A"
    ).get("Quota", {})
    price_age = datetime.now(UTC) - parse_timestamp(str(cost.get("priceAsOf")))
    implementation = git_identity(args.infra_root)
    closure = source_closure(args.infra_root)
    active_prior = decimal_field(
        cost.get("activeEpochPriorUpperBoundUsd"),
        "activeEpochPriorUpperBoundUsd",
    )
    charge = decimal_field(
        cost.get("chargedOperationalUpperBoundUsd"),
        "chargedOperationalUpperBoundUsd",
    )
    modeled = decimal_field(
        cost.get("modeledOperationalUpperBoundUsd"),
        "modeledOperationalUpperBoundUsd",
    )
    strict = decimal_field(
        cost.get("strictAttemptReservedUpperBoundUsd"),
        "strictAttemptReservedUpperBoundUsd",
    )
    cleanup_reserve = decimal_field(
        cost.get("cleanupReserveUsd"),
        "cleanupReserveUsd",
    )
    hard_cap = decimal_field(cost.get("hardCapUsd"), "hardCapUsd")
    maximum = decimal_field(
        cost.get("maximumIncludingTargetedStrictAndCleanupUsd"),
        "maximumIncludingTargetedStrictAndCleanupUsd",
    )
    checks = {
        "identityExact": identity.get("Arn") == EXPECTED_OPERATOR_ARN,
        "regionExact": session.region_name == EXPECTED_REGION,
        "freshIdentityInventoryZero": absent.get("allZero") is True,
        "bootstrapReady": bootstrap is not None and str(bootstrap.get("StackStatus", "")).endswith("COMPLETE"),
        "armEcsAmiExact": (
            len(images) == 1
            and images[0].get("State") == "available"
            and images[0].get("Architecture") == "arm64"
            and images[0].get("OwnerId") == "591542846629"
        ),
        "instanceOfferingPresent": any(item.get("InstanceType") == INSTANCE_TYPE for item in offerings),
        "standardVpcQuotaSufficient": float(quota.get("Value", 0)) >= 8,
        "costModelPassed": cost.get("passed") is True,
        "targetedChargeCoversModeledUpperBound": charge >= modeled,
        "activeEpochPriorExact": active_prior == Decimal("0"),
        "costIdentityExact": maximum == active_prior + charge + strict + cleanup_reserve,
        "strictRetryAndCleanupFit": maximum <= hard_cap,
        "pricesFreshWithinTwoHours": timedelta(0) <= price_age < timedelta(hours=2),
        "implementationCommitExact": len(implementation.get("commit", "")) == 40,
        "imageSourceClosureExact": len(closure.get("sha256", "")) == 64,
    }
    return {
        "schemaVersion": 1,
        "workload": "phase7-targeted-archive-diagnostic",
        "checkedAt": utc_now(),
        "runId": args.run_id,
        "sessionId": args.session_id,
        "identity": {"account": identity.get("Account"), "arn": identity.get("Arn")},
        "region": session.region_name,
        "implementation": implementation,
        "sourceClosure": closure,
        "inventory": absent,
        "bootstrap": {
            "stackName": bootstrap.get("StackName") if bootstrap else None,
            "status": bootstrap.get("StackStatus") if bootstrap else None,
        },
        "ami": images,
        "offerings": offerings,
        "quota": {"code": "L-1216C47A", "value": quota.get("Value")},
        "cost": cost,
        "checks": checks,
        "newAwsWorkAuthorized": all(checks.values()),
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--arm-ami", required=True)
    parser.add_argument("--cost-model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = preflight(args)
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
