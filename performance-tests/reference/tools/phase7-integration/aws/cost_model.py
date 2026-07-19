#!/usr/bin/env python3
"""Deterministic Phase 7-2 maximum-cost and CloudWatch log-volume gate."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from common import read_json, utc_now, write_json


HOURS = Decimal("3")
MONTH_HOURS = Decimal("730")
HARD_CAP_USD = Decimal("60")
NEW_LOAD_STOP_USD = Decimal("55")
CLEANUP_RESERVE_USD = Decimal("5")
LOG_INGEST_GIB_CAP = Decimal("5")
KINESIS_RECORDS = Decimal("24002000")
CUSTOM_METRIC_SERIES = Decimal("4500")
NLB_LCU_HOURS = Decimal("300")
NAT_DATA_GIB = Decimal("10")
EVIDENCE_AND_TRANSFER_RESERVE_USD = Decimal("1.5")
UNPRICED_FAILURE_RESERVE_USD = Decimal("0.5")

REQUIRED_PRICES = {
    "collectorC6iXlargeHour",
    "haproxyC6inXlargeHour",
    "generatorC6inLargeHour",
    "consumerC7gLargeHour",
    "clickHouseR7g2xlargeHour",
    "kinesisProvisionedShardHour",
    "kinesisPutPayloadUnit",
    "nlbHour",
    "nlbLcuHour",
    "natGatewayHour",
    "natGatewayDataGb",
    "publicIpv4Hour",
    "ebsGp3GbMonth",
    "ebsGp3ThroughputGibpsMonth",
    "secretsManagerSecretMonth",
    "secretsManagerApiRequest",
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


@dataclass(frozen=True)
class Component:
    name: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    note: str

    @property
    def cost(self) -> Decimal:
        return self.quantity * self.unit_price

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "quantity": decimal_text(self.quantity),
            "unit": self.unit,
            "unitPriceUsd": decimal_text(self.unit_price),
            "costUsd": money_text(self.cost),
            "note": self.note,
        }


def build_cost_model(price_document: dict[str, Any], log_ingest_gib: Decimal = LOG_INGEST_GIB_CAP,
                     accrued_upper_bound_usd: Decimal = Decimal("0")) -> dict[str, Any]:
    if price_document.get("region") != "ap-northeast-2":
        raise ValueError("price document must be for ap-northeast-2")
    raw = price_document.get("prices")
    if not isinstance(raw, dict):
        raise ValueError("price document has no prices object")
    missing = sorted(REQUIRED_PRICES.difference(raw))
    if missing:
        raise ValueError(f"price document is missing: {', '.join(missing)}")
    prices = {name: Decimal(str(raw[name])) for name in REQUIRED_PRICES}
    if any(value < 0 for value in prices.values()):
        raise ValueError("prices must be non-negative")
    if log_ingest_gib < 0 or accrued_upper_bound_usd < 0:
        raise ValueError("cost inputs must be non-negative")

    month_fraction = HOURS / MONTH_HOURS
    components = [
        Component("collector c6i.xlarge", Decimal(6) * HOURS, "instance-hour", prices["collectorC6iXlargeHour"], "Six fixed collector hosts for the three-hour hard window."),
        Component("HAProxy c6in.xlarge", Decimal(2) * HOURS, "instance-hour", prices["haproxyC6inXlargeHour"], "Two fixed proxy hosts."),
        Component("load generator c6in.large", Decimal(8) * HOURS, "instance-hour", prices["generatorC6inLargeHour"], "Eight fixed generators."),
        Component("consumer c7g.large", Decimal(2) * HOURS, "instance-hour", prices["consumerC7gLargeHour"], "Two native Java KCL hosts."),
        Component("ClickHouse r7g.2xlarge", HOURS, "instance-hour", prices["clickHouseR7g2xlargeHour"], "One private ClickHouse host."),
        Component("Kinesis provisioned shards", Decimal(120) * HOURS, "shard-hour", prices["kinesisProvisionedShardHour"], "120 shards for the complete paid window."),
        Component("Kinesis PUT payload units", KINESIS_RECORDS, "25-KiB unit", prices["kinesisPutPayloadUnit"], "Score, worst-case warmup, correctness, and replacement allowance."),
        Component("three Network Load Balancers", Decimal(3) * HOURS, "NLB-hour", prices["nlbHour"], "Protocol, collector, and ClickHouse NLBs."),
        Component("NLB capacity units", NLB_LCU_HOURS, "NLCU-hour", prices["nlbLcuHour"], "100 aggregate NLCUs for three hours across the three load balancers."),
        Component("single NAT gateway", HOURS, "gateway-hour", prices["natGatewayHour"], "One NAT gateway for the complete window."),
        Component("NAT processed data", NAT_DATA_GIB, "GiB", prices["natGatewayDataGb"], "Image/control/log traffic upper bound; payload, S3, and DynamoDB paths stay private or use gateway endpoints."),
        Component("NAT gateway public IPv4 address", HOURS, "IPv4-hour", prices["publicIpv4Hour"], "The single NAT gateway owns one in-use public IPv4 address."),
        Component("gp3 capacity", Decimal(1040) * month_fraction, "GB-month", prices["ebsGp3GbMonth"], "Eighteen 30-GiB roots plus one 500-GiB ClickHouse root."),
        Component("gp3 extra throughput", (Decimal(375) / Decimal(1024)) * month_fraction, "GiB/s-month", prices["ebsGp3ThroughputGibpsMonth"], "ClickHouse 500 MiB/s minus included 125 MiB/s."),
        Component("Secrets Manager secret", month_fraction, "secret-month", prices["secretsManagerSecretMonth"], "One generated run-owned secret."),
        Component("Secrets Manager API", Decimal(2000), "API request", prices["secretsManagerApiRequest"], "Task starts and replacement allowance."),
        Component("CloudWatch custom metrics", CUSTOM_METRIC_SERIES * month_fraction, "metric-month", prices["cloudWatchMetricMonth"], "KCL DETAILED, Container Insights, and host evidence series."),
        Component("CloudWatch log ingestion", log_ingest_gib, "GiB", prices["cloudWatchLogIngestGb"], "Successful HAProxy 202 responses are sampled 1:1000; errors are always logged."),
        Component("CloudWatch log storage", log_ingest_gib * month_fraction, "GiB-month", prices["cloudWatchLogStorageGbMonth"], "Seven-day retention is bounded by run cleanup."),
        Component("CloudWatch GetMetricData", Decimal(100000), "requested metric", prices["cloudWatchGetMetricDataMetric"], "Control-plane evidence allowance with batched queries."),
        Component("ECR image storage", Decimal(3) * month_fraction, "GiB-month", prices["ecrStorageGbMonth"], "Three run-owned images, 1 GiB each."),
        Component("DynamoDB on-demand reads", Decimal(2000000), "read unit", prices["dynamoDbReadRequestUnit"], "Three KCL metadata tables."),
        Component("DynamoDB on-demand writes", Decimal(1000000), "write unit", prices["dynamoDbWriteRequestUnit"], "Lease/checkpoint upper bound."),
        Component("DynamoDB storage", Decimal("0.3") * month_fraction, "GB-month", prices["dynamoDbStorageGbMonth"], "Run-owned metadata only."),
        Component("S3 standard storage", Decimal(6) * month_fraction, "GB-month", prices["s3StandardStorageGbMonth"], "Archive, failure, and evidence objects."),
        Component("S3 write/list requests", Decimal(10000), "request", prices["s3Tier1Request"], "Archive multipart and evidence writes."),
        Component("S3 read requests", Decimal(30000), "request", prices["s3Tier2Request"], "Validation and direct-query reads."),
        Component("evidence and transfer reserve", Decimal(1), "fixed reserve", EVIDENCE_AND_TRANSFER_RESERVE_USD, "Unmodeled regional transfer and evidence retrieval."),
        Component("failure-path reserve", Decimal(1), "fixed reserve", UNPRICED_FAILURE_RESERVE_USD, "Rollback and delayed metering uncertainty."),
    ]
    operational = accrued_upper_bound_usd + sum((item.cost for item in components), Decimal("0"))
    maximum = operational + CLEANUP_RESERVE_USD
    checks = {
        "haproxyLogVolumeAtOrBelowFiveGiB": log_ingest_gib <= LOG_INGEST_GIB_CAP,
        "operationalMaximumBelowNewLoadStop": operational < NEW_LOAD_STOP_USD,
        "cleanupReserveAtLeastFiveUsd": CLEANUP_RESERVE_USD >= Decimal("5"),
        "maximumIncludingCleanupAtOrBelowHardCap": maximum <= HARD_CAP_USD,
    }
    return {
        "schemaVersion": 1,
        "workload": "phase7-end-to-end-integration",
        "calculatedAt": utc_now(),
        "priceAsOf": price_document.get("asOf"),
        "region": price_document.get("region"),
        "paidWallClockHours": decimal_text(HOURS),
        "logPolicy": {"successful202SampleRate": "1/1000", "allErrorsLogged": True, "ingestUpperBoundGiB": decimal_text(log_ingest_gib)},
        "components": [item.as_dict() for item in components],
        "accruedUpperBoundUsd": money_text(accrued_upper_bound_usd),
        "operationalMaximumUsd": money_text(operational),
        "cleanupReserveUsd": money_text(CLEANUP_RESERVE_USD),
        "maximumIncludingCleanupUsd": money_text(maximum),
        "limits": {"newLoadStopUsd": money_text(NEW_LOAD_STOP_USD), "hardCapUsd": money_text(HARD_CAP_USD)},
        "checks": checks,
        "passed": all(checks.values()),
    }


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--log-ingest-gib", type=Decimal, default=LOG_INGEST_GIB_CAP)
    parser.add_argument("--accrued-upper-bound-usd", type=Decimal, default=Decimal("0"))
    args = parser.parse_args()
    result = build_cost_model(read_json(args.prices), args.log_ingest_gib, args.accrued_upper_bound_usd)
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
