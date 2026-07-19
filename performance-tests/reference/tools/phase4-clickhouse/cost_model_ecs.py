#!/usr/bin/env python3
"""Build the deterministic Phase 4 ECS-on-EC2 paid-wall-clock cost gate."""

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
CONSUMER_HOSTS = 2
INTERFACE_ENDPOINTS = 9
EBS_GB = Decimal("500")
EBS_EXTRA_THROUGHPUT_MIBPS = Decimal("375")
KCL_PER_SHARD_METRICS = 11
KCL_UNIQUE_WORKERS = 3
KCL_NON_SHARD_METRIC_SERIES_RESERVE = 240
KCL_CUSTOM_METRIC_SERIES = (
    KINESIS_SHARDS * KCL_UNIQUE_WORKERS * KCL_PER_SHARD_METRICS
    + KCL_NON_SHARD_METRIC_SERIES_RESERVE
)
CONTAINER_INSIGHTS_CUSTOM_METRIC_SERIES = 100
EC2_DETAILED_MONITORING_METRIC_SERIES = 20
HOST_MEMORY_CUSTOM_METRIC_SERIES = 3
CLOUDWATCH_LOG_INGEST_GIB = Decimal("0.25")
CLOUDWATCH_GET_METRIC_DATA_REQUESTED_METRICS = 20_000
ECR_IMAGE_STORAGE_GIB = Decimal("0.5")
VPC_ENDPOINT_DATA_GIB = Decimal("25")
DYNAMODB_READ_REQUEST_UNITS = 1_000_000
DYNAMODB_WRITE_REQUEST_UNITS = 500_000
DYNAMODB_STORAGE_GIB = Decimal("0.1")
S3_STORAGE_GIB = Decimal("1")
S3_TIER1_REQUESTS = 1_000
S3_TIER2_REQUESTS = 5_000
UNPRICED_SERVICE_RESERVE_USD = Decimal("0.10")
CLEANUP_RESERVE_USD = Decimal("3")
NEW_LOAD_STOP_USD = Decimal("17")
HARD_CAP_USD = Decimal("20")


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


def build_cost_model(price_document: dict[str, Any]) -> dict[str, Any]:
    if price_document.get("region") != "ap-northeast-2":
        raise ValueError("price document must be for ap-northeast-2")
    price_values = price_document.get("prices")
    if not isinstance(price_values, dict):
        raise ValueError("price document has no prices object")

    required_prices = {
        "clickHouseR7g2xlargeHour",
        "consumerHostC7gLargeHour",
        "producerC7g2xlargeHour",
        "kinesisProvisionedShardHour",
        "kinesisPutPayloadUnit",
        "ebsGp3GbMonth",
        "ebsGp3ThroughputGibpsMonth",
        "secretsManagerSecretMonth",
        "secretsManagerApiRequest",
        "vpcEndpointHour",
        "vpcEndpointDataGb",
        "publicIpv4Hour",
        "cloudWatchMetricMonth",
        "cloudWatchLogIngestGb",
        "cloudWatchLogStorageGbMonth",
        "cloudWatchGetMetricDataMetric",
        "ecrStorageGbMonth",
        "dynamoDbReadRequestUnit",
        "dynamoDbWriteRequestUnit",
        "dynamoDbStorageGbMonth",
        "s3StandardStorageGbMonth",
        "s3Tier1Request",
        "s3Tier2Request",
    }
    missing = sorted(required_prices.difference(price_values))
    if missing:
        raise ValueError(f"price document is missing: {', '.join(missing)}")
    prices = {name: Decimal(str(price_values[name])) for name in required_prices}
    if any(value < 0 for value in prices.values()):
        raise ValueError("prices must be non-negative")

    total_records = FULL_LOAD_RECORDS + VALIDATION_RECORD_ALLOWANCE
    month_fraction = PAID_WALL_CLOCK_HOURS / MONTH_HOURS
    custom_metric_series = (
        KCL_CUSTOM_METRIC_SERIES
        + CONTAINER_INSIGHTS_CUSTOM_METRIC_SERIES
        + EC2_DETAILED_MONITORING_METRIC_SERIES
        + HOST_MEMORY_CUSTOM_METRIC_SERIES
    )
    components = [
        Component(
            "ClickHouse r7g.2xlarge",
            PAID_WALL_CLOCK_HOURS,
            "instance-hour",
            prices["clickHouseR7g2xlargeHour"],
            "Full 120-minute paid-wall-clock allowance.",
        ),
        Component(
            "ECS consumer hosts c7g.large",
            Decimal(CONSUMER_HOSTS) * PAID_WALL_CLOCK_HOURS,
            "instance-hour",
            prices["consumerHostC7gLargeHour"],
            "Two fixed On-Demand hosts for the complete two-hour maximum.",
        ),
        Component(
            "qualified producer c7g.2xlarge",
            PRODUCER_HOURS,
            "instance-hour",
            prices["producerC7g2xlargeHour"],
            "One hour covers bootstrap, exact 300-second load, and evidence transfer.",
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
            "ClickHouse gp3 capacity",
            EBS_GB * month_fraction,
            "GB-month",
            prices["ebsGp3GbMonth"],
            "500 GiB prorated for two hours.",
        ),
        Component(
            "ClickHouse gp3 extra throughput",
            (EBS_EXTRA_THROUGHPUT_MIBPS / Decimal(1024)) * month_fraction,
            "GiB/s-month",
            prices["ebsGp3ThroughputGibpsMonth"],
            "500 MiB/s requested minus the included 125 MiB/s, prorated for two hours.",
        ),
        Component(
            "private interface endpoints",
            Decimal(INTERFACE_ENDPOINTS) * PAID_WALL_CLOCK_HOURS,
            "endpoint-AZ-hour",
            prices["vpcEndpointHour"],
            "Nine single-AZ endpoints for Kinesis, ECR, Logs, Secrets, ECS, and CloudWatch.",
        ),
        Component(
            "private interface endpoint data",
            VPC_ENDPOINT_DATA_GIB,
            "GiB processed",
            prices["vpcEndpointDataGb"],
            "Twenty-five GiB covers Kinesis reads plus image, logs, control-plane, and response overhead.",
        ),
        Component(
            "Secrets Manager generated secret",
            month_fraction,
            "secret-month",
            prices["secretsManagerSecretMonth"],
            "One run-owned secret prorated for two hours.",
        ),
        Component(
            "Secrets Manager API allowance",
            Decimal(1_000),
            "API request",
            prices["secretsManagerApiRequest"],
            "One thousand requests covers ClickHouse bootstrap and consumer task replacement.",
        ),
        Component(
            "public IPv4 addresses",
            PAID_WALL_CLOCK_HOURS + PRODUCER_HOURS,
            "address-hour",
            prices["publicIpv4Hour"],
            "ClickHouse for two hours and the qualified producer for one hour.",
        ),
        Component(
            "CloudWatch custom metrics",
            Decimal(custom_metric_series) * month_fraction,
            "metric-month",
            prices["cloudWatchMetricMonth"],
            (
                "Upper bound of 4,200 KCL DETAILED, 100 Container Insights, 20 EC2 "
                "detailed-monitoring, and three host-memory EMF metric series for two hours. KCL allows "
                "11 documented per-shard metrics across 120 shards and three unique "
                "workers (two initial plus one planned replacement), plus 240 "
                "application/worker-series reserve."
            ),
        ),
        Component(
            "CloudWatch Logs ingestion",
            CLOUDWATCH_LOG_INGEST_GIB,
            "GiB",
            prices["cloudWatchLogIngestGb"],
            "A 0.25 GiB upper bound for consumer, ECS agent, and bootstrap logs; records are not logged.",
        ),
        Component(
            "CloudWatch Logs storage",
            CLOUDWATCH_LOG_INGEST_GIB * month_fraction,
            "GiB-month",
            prices["cloudWatchLogStorageGbMonth"],
            "The 0.25 GiB ingestion upper bound retained for the complete two-hour maximum.",
        ),
        Component(
            "CloudWatch GetMetricData",
            Decimal(CLOUDWATCH_GET_METRIC_DATA_REQUESTED_METRICS),
            "requested metric",
            prices["cloudWatchGetMetricDataMetric"],
            "Twenty thousand requested metrics covers the full KCL series catalog and run metrics.",
        ),
        Component(
            "ECR image storage",
            ECR_IMAGE_STORAGE_GIB * month_fraction,
            "GiB-month",
            prices["ecrStorageGbMonth"],
            "A 0.5 GiB consumer image prorated for the complete two-hour maximum.",
        ),
        Component(
            "DynamoDB on-demand reads",
            Decimal(DYNAMODB_READ_REQUEST_UNITS),
            "read request unit",
            prices["dynamoDbReadRequestUnit"],
            "One million read request units across the three KCL metadata tables.",
        ),
        Component(
            "DynamoDB on-demand writes",
            Decimal(DYNAMODB_WRITE_REQUEST_UNITS),
            "write request unit",
            prices["dynamoDbWriteRequestUnit"],
            "Five hundred thousand write request units cover leases, checkpoints, and worker state.",
        ),
        Component(
            "DynamoDB storage",
            DYNAMODB_STORAGE_GIB * month_fraction,
            "GB-month",
            prices["dynamoDbStorageGbMonth"],
            "A 0.1 GiB non-free-tier upper bound prorated for two hours.",
        ),
        Component(
            "S3 Standard storage",
            S3_STORAGE_GIB * month_fraction,
            "GB-month",
            prices["s3StandardStorageGbMonth"],
            "One GiB covers producer evidence, failure allowance, and archive fixture for two hours.",
        ),
        Component(
            "S3 Tier 1 requests",
            Decimal(S3_TIER1_REQUESTS),
            "request",
            prices["s3Tier1Request"],
            "One thousand PUT, COPY, POST, or LIST requests.",
        ),
        Component(
            "S3 Tier 2 requests",
            Decimal(S3_TIER2_REQUESTS),
            "request",
            prices["s3Tier2Request"],
            "Five thousand GET and other requests, including archive direct-query reads.",
        ),
        Component(
            "unpriced API and rounding reserve",
            Decimal("1"),
            "fixed allowance",
            UNPRICED_SERVICE_RESERVE_USD,
            "Residual allowance after all material services are priced explicitly.",
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
        "workload": "phase4-kinesis-ecs-ec2-clickhouse",
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
            "consumerHosts": CONSUMER_HOSTS,
            "interfaceEndpoints": INTERFACE_ENDPOINTS,
            "kclCustomMetricSeriesUpperBound": KCL_CUSTOM_METRIC_SERIES,
            "kclPerShardMetricCount": KCL_PER_SHARD_METRICS,
            "kclUniqueWorkersUpperBound": KCL_UNIQUE_WORKERS,
            "kclNonShardMetricSeriesReserve": KCL_NON_SHARD_METRIC_SERIES_RESERVE,
            "containerInsightsCustomMetricSeriesUpperBound": (
                CONTAINER_INSIGHTS_CUSTOM_METRIC_SERIES
            ),
            "ec2DetailedMonitoringMetricSeriesUpperBound": (
                EC2_DETAILED_MONITORING_METRIC_SERIES
            ),
            "hostMemoryCustomMetricSeriesUpperBound": HOST_MEMORY_CUSTOM_METRIC_SERIES,
            "cloudWatchLogIngestGiBUpperBound": decimal_text(CLOUDWATCH_LOG_INGEST_GIB),
            "cloudWatchGetMetricDataRequestedMetricsUpperBound": (
                CLOUDWATCH_GET_METRIC_DATA_REQUESTED_METRICS
            ),
            "ecrImageStorageGiBUpperBound": decimal_text(ECR_IMAGE_STORAGE_GIB),
            "vpcEndpointDataGiBUpperBound": decimal_text(VPC_ENDPOINT_DATA_GIB),
            "dynamoDbReadRequestUnitsUpperBound": DYNAMODB_READ_REQUEST_UNITS,
            "dynamoDbWriteRequestUnitsUpperBound": DYNAMODB_WRITE_REQUEST_UNITS,
            "dynamoDbStorageGiBUpperBound": decimal_text(DYNAMODB_STORAGE_GIB),
            "s3StorageGiBUpperBound": decimal_text(S3_STORAGE_GIB),
            "s3Tier1RequestsUpperBound": S3_TIER1_REQUESTS,
            "s3Tier2RequestsUpperBound": S3_TIER2_REQUESTS,
            "cleanupStartsAtMinute": 100,
            "unconditionalCleanupByMinute": 120,
            "errorPolicy": (
                "Any ECS/KCL/ClickHouse failure stops new load and starts cleanup; "
                "retry and cleanup cost are covered by reserves."
            ),
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
