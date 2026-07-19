#!/usr/bin/env python3
"""Conservative admission model for the isolated 15M archive diagnostic."""

from __future__ import annotations

import argparse
import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from targeted_archive_common import utc_now, write_json


PAID_HOURS = Decimal("3")
MONTH_HOURS = Decimal("730")
ACTIVE_PRIOR_USD = Decimal("0")
STRICT_ATTEMPT_UPPER_BOUND_USD = Decimal("33.870718")
CLEANUP_RESERVE_USD = Decimal("5")
HARD_CAP_USD = Decimal("60")
NEW_WORK_STOP_USD = Decimal("55")
TARGETED_AVAILABLE_USD = (
    HARD_CAP_USD
    - ACTIVE_PRIOR_USD
    - STRICT_ATTEMPT_UPPER_BOUND_USD
    - CLEANUP_RESERVE_USD
)
REQUIRED_PRICES = {
    "clickHouseR7g2xlargeHour",
    "publicIpv4Hour",
    "ebsGp3GbMonth",
    "ebsGp3ThroughputGibpsMonth",
    "secretsManagerSecretMonth",
    "secretsManagerApiRequest",
    "cloudWatchLogIngestGb",
    "cloudWatchLogStorageGbMonth",
    "ecrStorageGbMonth",
    "s3StandardStorageGbMonth",
    "s3Tier1Request",
    "s3Tier2Request",
}


def build(price_document: dict[str, Any]) -> dict[str, Any]:
    raw = price_document.get("prices", {})
    missing = sorted(REQUIRED_PRICES.difference(raw))
    if missing:
        raise ValueError(f"targeted price document is missing: {', '.join(missing)}")
    prices = {key: Decimal(str(raw[key])) for key in REQUIRED_PRICES}
    if any(value < 0 for value in prices.values()):
        raise ValueError("targeted prices must be non-negative")
    month_fraction = PAID_HOURS / MONTH_HOURS
    quantities = {
        "clickHouseR7g2xlargeHour": PAID_HOURS,
        "publicIpv4Hour": PAID_HOURS,
        "ebsGp3GbMonth": Decimal("500") * month_fraction,
        "ebsGp3ThroughputGibpsMonth": (Decimal("375") / Decimal("1024")) * month_fraction,
        "secretsManagerSecretMonth": month_fraction,
        "secretsManagerApiRequest": Decimal("500"),
        "cloudWatchLogIngestGb": Decimal("2"),
        "cloudWatchLogStorageGbMonth": Decimal("2") * month_fraction,
        "ecrStorageGbMonth": month_fraction,
        "s3StandardStorageGbMonth": Decimal("6") * month_fraction,
        "s3Tier1Request": Decimal("10000"),
        "s3Tier2Request": Decimal("30000"),
    }
    components = [
        {
            "name": name,
            "quantity": format(quantities[name].normalize(), "f"),
            "unitPriceUsd": format(prices[name].normalize(), "f"),
            "upperBoundUsd": format((quantities[name] * prices[name]).quantize(Decimal("0.000001")), "f"),
        }
        for name in sorted(REQUIRED_PRICES)
    ]
    modeled = sum((quantities[name] * prices[name] for name in REQUIRED_PRICES), Decimal("0"))
    modeled += Decimal("0.75")  # control-plane, transfer, rollback, and delayed-metering reserve
    charge = modeled.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    after_target = ACTIVE_PRIOR_USD + charge
    strict_maximum = after_target + STRICT_ATTEMPT_UPPER_BOUND_USD + CLEANUP_RESERVE_USD
    checks = {
        "freshPublicPrices": price_document.get("source") == "AWS Price List GetProducts, public On-Demand USD",
        "modeledOperationalFitsAvailableBudget": modeled <= TARGETED_AVAILABLE_USD,
        "chargedUpperBoundEqualsCeilingRoundedModel": charge >= modeled,
        "newWorkStopPreserved": after_target < NEW_WORK_STOP_USD,
        "strictRetryAndCleanupStillFit": strict_maximum <= HARD_CAP_USD,
        "cleanupReservePreserved": CLEANUP_RESERVE_USD >= Decimal("5"),
    }
    return {
        "schemaVersion": 1,
        "workload": "phase7-targeted-archive-diagnostic",
        "calculatedAt": utc_now(),
        "priceAsOf": price_document.get("asOf"),
        "region": price_document.get("region"),
        "paidWallClockHours": format(PAID_HOURS, "f"),
        "components": components,
        "unpricedReserveUsd": "0.75",
        "targetedAvailableBeforeHardCapUsd": format(TARGETED_AVAILABLE_USD, "f"),
        "modeledOperationalUpperBoundUsd": format(modeled.quantize(Decimal("0.000001")), "f"),
        "chargedOperationalUpperBoundUsd": format(charge, "f"),
        "activeEpochPriorUpperBoundUsd": format(ACTIVE_PRIOR_USD, "f"),
        "activeEpochAfterTargetedUpperBoundUsd": format(after_target, "f"),
        "strictAttemptReservedUpperBoundUsd": format(STRICT_ATTEMPT_UPPER_BOUND_USD, "f"),
        "cleanupReserveUsd": format(CLEANUP_RESERVE_USD, "f"),
        "maximumIncludingTargetedStrictAndCleanupUsd": format(strict_maximum, "f"),
        "hardCapUsd": format(HARD_CAP_USD, "f"),
        "newWorkStopUsd": format(NEW_WORK_STOP_USD, "f"),
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = build(json.loads(args.prices.read_text(encoding="utf-8")))
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
