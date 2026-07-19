#!/usr/bin/env python3
"""Build the deterministic Phase 4 paid-wall-clock cost gate."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PAID_WALL_CLOCK_HOURS = Decimal("2")
PRODUCER_HOURS = Decimal("1")
MONTH_HOURS = Decimal("730")
FULL_LOAD_RECORDS = 15_000_000
VALIDATION_RECORD_ALLOWANCE = 2_000
KINESIS_SHARDS = 120
LAMBDA_MEMORY_GB = Decimal("2")
LAMBDA_TIMEOUT_SECONDS = Decimal("30")
SAFE_RECORDS_PER_INVOCATION = 2_400
ESM_INVOCATION_BOUNDARY_ALLOWANCE = KINESIS_SHARDS * 2
EBS_GB = Decimal("500")
EBS_EXTRA_THROUGHPUT_MIBPS = Decimal("375")
UNPRICED_SERVICE_RESERVE_USD = Decimal("0.25")
CLEANUP_RESERVE_USD = Decimal("3")
NEW_LOAD_STOP_USD = Decimal("12")
HARD_CAP_USD = Decimal("15")


@dataclass(frozen=True)
class Component:
    name: str
    quantity: Decimal
    unit: str
    unit_price_usd: Decimal
    note: str

    @property
    def cost_usd(self) -> Decimal:
        return self.quantity * self.unit_price_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quantity": decimal_text(self.quantity),
            "unit": self.unit,
            "unitPriceUsd": decimal_text(self.unit_price_usd),
            "costUsd": money_text(self.cost_usd),
            "note": self.note,
        }


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def build_cost_model(price_document: dict[str, Any]) -> dict[str, Any]:
    if price_document.get("region") != "ap-northeast-2":
        raise ValueError("price document must be for ap-northeast-2")
    price_values = price_document.get("prices")
    if not isinstance(price_values, dict):
        raise ValueError("price document has no prices object")

    required_prices = {
        "clickHouseR7g2xlargeHour",
        "producerC7g2xlargeHour",
        "kinesisProvisionedShardHour",
        "kinesisPutPayloadUnit",
        "ebsGp3GbMonth",
        "ebsGp3ThroughputGibpsMonth",
        "lambdaArmGbSecond",
        "lambdaArmRequest",
        "secretsManagerSecretMonth",
        "secretsManagerApiRequest",
        "vpcEndpointHour",
        "publicIpv4Hour",
    }
    missing = sorted(required_prices.difference(price_values))
    if missing:
        raise ValueError(f"price document is missing: {', '.join(missing)}")
    prices = {name: Decimal(str(price_values[name])) for name in required_prices}
    if any(value < 0 for value in prices.values()):
        raise ValueError("prices must be non-negative")

    total_records = FULL_LOAD_RECORDS + VALIDATION_RECORD_ALLOWANCE
    lambda_invocations = (
        ceil_div(total_records, SAFE_RECORDS_PER_INVOCATION)
        + ESM_INVOCATION_BOUNDARY_ALLOWANCE
    )
    lambda_gb_seconds = (
        Decimal(lambda_invocations) * LAMBDA_MEMORY_GB * LAMBDA_TIMEOUT_SECONDS
    )
    ebs_month_fraction = PAID_WALL_CLOCK_HOURS / MONTH_HOURS
    secret_month_fraction = PAID_WALL_CLOCK_HOURS / MONTH_HOURS

    components = [
        Component(
            "ClickHouse r7g.2xlarge",
            PAID_WALL_CLOCK_HOURS,
            "instance-hour",
            prices["clickHouseR7g2xlargeHour"],
            "Full 120-minute paid-wall-clock allowance.",
        ),
        Component(
            "qualified producer c7g.2xlarge",
            PRODUCER_HOURS,
            "instance-hour",
            prices["producerC7g2xlargeHour"],
            "One hour covers bootstrap, the exact 300-second load, and evidence upload.",
        ),
        Component(
            "Kinesis provisioned shards",
            Decimal(KINESIS_SHARDS) * PAID_WALL_CLOCK_HOURS,
            "shard-hour",
            prices["kinesisProvisionedShardHour"],
            "120 shards for the complete two-hour maximum.",
        ),
        Component(
            "Kinesis PUT payload units",
            Decimal(total_records),
            "25-KiB payload unit",
            prices["kinesisPutPayloadUnit"],
            "Every qualified payload is below 25 KiB; includes 2,000 validation records.",
        ),
        Component(
            "Lambda ARM duration upper bound",
            lambda_gb_seconds,
            "GB-second",
            prices["lambdaArmGbSecond"],
            "Every bounded invocation is charged the full 30-second timeout at 2 GiB.",
        ),
        Component(
            "Lambda ARM requests",
            Decimal(lambda_invocations),
            "request",
            prices["lambdaArmRequest"],
            "Payload-size bound plus two shard-boundary allowances.",
        ),
        Component(
            "ClickHouse gp3 capacity",
            EBS_GB * ebs_month_fraction,
            "GB-month",
            prices["ebsGp3GbMonth"],
            "500 GiB prorated for two hours.",
        ),
        Component(
            "ClickHouse gp3 extra throughput",
            (EBS_EXTRA_THROUGHPUT_MIBPS / Decimal(1024)) * ebs_month_fraction,
            "GiB/s-month",
            prices["ebsGp3ThroughputGibpsMonth"],
            "500 MiB/s requested minus the included 125 MiB/s, prorated for two hours.",
        ),
        Component(
            "Secrets Manager interface endpoint",
            PAID_WALL_CLOCK_HOURS,
            "endpoint-hour",
            prices["vpcEndpointHour"],
            "One same-AZ endpoint for two hours.",
        ),
        Component(
            "Secrets Manager generated secret",
            secret_month_fraction,
            "secret-month",
            prices["secretsManagerSecretMonth"],
            "One run-owned secret prorated for two hours.",
        ),
        Component(
            "Secrets Manager API allowance",
            Decimal(1_000),
            "API request",
            prices["secretsManagerApiRequest"],
            "One thousand requests exceeds the expected number of execution-environment cold starts.",
        ),
        Component(
            "public IPv4 addresses",
            PAID_WALL_CLOCK_HOURS + PRODUCER_HOURS,
            "address-hour",
            prices["publicIpv4Hour"],
            "ClickHouse for two hours and producer for one hour.",
        ),
        Component(
            "S3, CloudWatch, endpoint data, and rounding reserve",
            Decimal("1"),
            "fixed allowance",
            UNPRICED_SERVICE_RESERVE_USD,
            "Conservative allowance for low-volume services not individually priced here.",
        ),
    ]

    operational_max = sum((component.cost_usd for component in components), Decimal(0))
    hard_cap_max = operational_max + CLEANUP_RESERVE_USD
    gates = {
        "operationalMaximumBelowNewLoadStop": operational_max < NEW_LOAD_STOP_USD,
        "cleanupReserveAtLeastThreeUsd": CLEANUP_RESERVE_USD >= Decimal("3"),
        "maximumIncludingCleanupAtMostHardCap": hard_cap_max <= HARD_CAP_USD,
    }
    gates["pass"] = all(gates.values())

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "region": "ap-northeast-2",
        "currency": "USD",
        "priceAsOf": price_document.get("asOf"),
        "source": price_document.get("source"),
        "assumptions": {
            "paidWallClockHours": decimal_text(PAID_WALL_CLOCK_HOURS),
            "producerHours": decimal_text(PRODUCER_HOURS),
            "fullLoadRecords": FULL_LOAD_RECORDS,
            "validationRecordAllowance": VALIDATION_RECORD_ALLOWANCE,
            "kinesisShards": KINESIS_SHARDS,
            "lambdaMemoryGiB": decimal_text(LAMBDA_MEMORY_GB),
            "lambdaTimeoutSeconds": decimal_text(LAMBDA_TIMEOUT_SECONDS),
            "safeRecordsPerInvocation": SAFE_RECORDS_PER_INVOCATION,
            "esmInvocationBoundaryAllowance": ESM_INVOCATION_BOUNDARY_ALLOWANCE,
            "lambdaInvocationUpperBound": lambda_invocations,
            "cleanupStartsAtMinute": 100,
            "unconditionalCleanupByMinute": 120,
            "errorPolicy": "Any Lambda/ESM/ClickHouse failure stops new load and starts cleanup; retry cost is covered by reserves.",
        },
        "components": [component.as_dict() for component in components],
        "operationalMaximumUsd": money_text(operational_max),
        "newLoadStopThresholdUsd": money_text(NEW_LOAD_STOP_USD),
        "cleanupReserveUsd": money_text(CLEANUP_RESERVE_USD),
        "maximumIncludingCleanupUsd": money_text(hard_cap_max),
        "hardCapUsd": money_text(HARD_CAP_USD),
        "headroomUsd": money_text(HARD_CAP_USD - hard_cap_max),
        "gates": gates,
    }


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model = build_cost_model(json.loads(args.prices.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(model, indent=2)}\n", encoding="utf-8")
    print(json.dumps({
        "operationalMaximumUsd": model["operationalMaximumUsd"],
        "maximumIncludingCleanupUsd": model["maximumIncludingCleanupUsd"],
        "headroomUsd": model["headroomUsd"],
        "gates": model["gates"],
    }, indent=2))
    return 0 if model["gates"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
