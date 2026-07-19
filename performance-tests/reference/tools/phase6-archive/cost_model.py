#!/usr/bin/env python3
"""Deterministic Phase 6 cost upper bound from a checked-in local fixture."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

HOURS_PER_MONTH = Decimal("730")


@dataclass(frozen=True)
class CostInputs:
    paid_hours: Decimal = Decimal("2")
    ebs_gb: Decimal = Decimal("500")
    ebs_throughput_mibps: Decimal = Decimal("500")
    baseline_throughput_mibps: Decimal = Decimal("125")
    s3_stored_gb: Decimal = Decimal("10")
    s3_tier1_requests: Decimal = Decimal("20")
    s3_tier2_requests: Decimal = Decimal("100")
    interface_endpoints: Decimal = Decimal("4")
    public_ipv4: Decimal = Decimal("1")
    log_ingest_gb: Decimal = Decimal("2")
    cleanup_reserve_usd: Decimal = Decimal("3")


def load_prices(path: Path) -> dict[str, Decimal]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {key: Decimal(str(value)) for key, value in document["pricesUsd"].items()}


def calculate(prices: dict[str, Decimal], value: CostInputs = CostInputs()) -> dict[str, object]:
    throughput_gibps = max(
        Decimal("0"),
        value.ebs_throughput_mibps - value.baseline_throughput_mibps,
    ) / Decimal("1024")
    components = {
        "ec2": prices["clickHouseR7g2xlargeHour"] * value.paid_hours,
        "ebsStorage": (
            prices["ebsGp3GbMonth"] * value.ebs_gb * value.paid_hours / HOURS_PER_MONTH
        ),
        "ebsThroughput": (
            prices["ebsGp3ThroughputGibpsMonth"]
            * throughput_gibps
            * value.paid_hours
            / HOURS_PER_MONTH
        ),
        "s3Storage": prices["s3StandardStorageGbMonth"] * value.s3_stored_gb,
        "s3Tier1": prices["s3Tier1Request"] * value.s3_tier1_requests,
        "s3Tier2": prices["s3Tier2Request"] * value.s3_tier2_requests,
        "vpcEndpoints": (
            prices["vpcEndpointHour"] * value.interface_endpoints * value.paid_hours
        ),
        "publicIpv4": prices["publicIpv4Hour"] * value.public_ipv4 * value.paid_hours,
        "logs": prices["cloudWatchLogIngestGb"] * value.log_ingest_gb,
    }
    subtotal = sum(components.values(), Decimal("0"))
    maximum = subtotal + value.cleanup_reserve_usd
    return {
        "schemaVersion": "1.0",
        "inputs": {key: str(item) for key, item in asdict(value).items()},
        "componentsUsd": {key: str(item.quantize(Decimal("0.000001"))) for key, item in components.items()},
        "subtotalUsd": str(subtotal.quantize(Decimal("0.000001"))),
        "cleanupReserveUsd": str(value.cleanup_reserve_usd),
        "deterministicMaximumUsd": str(maximum.quantize(Decimal("0.000001"))),
        "hardCapUsd": "15",
        "passed": maximum <= Decimal("15"),
        "livePriceLookup": False,
    }


def main() -> int:
    fixture = Path(__file__).with_name("price-fixtures") / "ap-northeast-2-20260716.json"
    print(json.dumps(calculate(load_prices(fixture)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
