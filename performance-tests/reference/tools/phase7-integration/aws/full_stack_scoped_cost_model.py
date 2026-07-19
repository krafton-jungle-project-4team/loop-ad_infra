#!/usr/bin/env python3
"""Cost gate for the final full-topology archive-only Phase 7 probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from common import read_json, utc_now, validate_identifiers, write_json


# Attempt 21 proved the unchanged standard stack can deploy, smoke, seed,
# archive-fail and quiesce all paid capacity in about 25 minutes. Reserve a
# full operational hour for the fresh retry; the separate cleanup reserve and
# 120-minute hard window cover asynchronous AWS tombstones after capacity zero.
HOURS = Decimal("1")
MONTH_HOURS = Decimal("730")
HARD_CAP_USD = Decimal("60")
NEW_PAID_WORK_STOP_USD = Decimal("55")
CLEANUP_RESERVE_USD = Decimal("5")
LOG_INGEST_GIB_CAP = Decimal("0.5")
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


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def without_calculated_at(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    normalized.pop("calculatedAt", None)
    return normalized


def validate_campaign_ledger(
    ledger: dict[str, Any],
    *,
    allow_active: bool = False,
    allow_terminal_status: bool = False,
    expected_run_id: str | None = None,
    expected_session_id: str | None = None,
) -> tuple[Decimal, str, str]:
    status = ledger.get("status")
    if (
        ledger.get("schemaVersion") != 1
        or ledger.get("campaign") != "phase7-2-stabilization"
        or status not in (
            {"stabilizing", "budget-exhausted"}
            if allow_terminal_status
            else {"stabilizing"}
        )
    ):
        raise ValueError("campaign ledger status is not authorized for this operation")
    previous: str | None = None
    attempts = ledger.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("campaign ledger has no immutable attempt chain")
    for ordinal, entry in enumerate(attempts, start=1):
        if not isinstance(entry, dict) or entry.get("ordinal") != ordinal:
            raise ValueError("campaign ledger attempt ordinals are not exact")
        claimed = entry.get("entrySha256")
        unhashed = dict(entry)
        unhashed.pop("entrySha256", None)
        observed = canonical_sha256(unhashed)
        if claimed != observed or entry.get("previousEntrySha256") != previous:
            raise ValueError("campaign ledger hash chain is invalid")
        previous = str(claimed)
    if ledger.get("ledgerHeadSha256") != previous:
        raise ValueError("campaign ledger head is invalid")

    budget = ledger.get("budget")
    epochs = ledger.get("budgetEpochs")
    if not isinstance(budget, dict) or not isinstance(epochs, list):
        raise ValueError("campaign ledger budget control is missing")
    active_epochs = [item for item in epochs if item.get("status") == "active"]
    if len(active_epochs) != 1:
        raise ValueError("campaign ledger must have exactly one active budget epoch")
    epoch = active_epochs[0]
    epoch_id = str(budget.get("activeEpochId"))
    prior = Decimal(str(budget.get("activeEpochAccruedUpperBoundUsd")))
    if (
        epoch.get("epochId") != epoch_id
        or Decimal(str(epoch.get("accruedUpperBoundUsd"))) != prior
        or Decimal(str(budget.get("hardCapUsd"))) != HARD_CAP_USD
        or Decimal(str(budget.get("newPaidWorkStopUsd"))) != NEW_PAID_WORK_STOP_USD
        or Decimal(str(budget.get("cleanupReserveUsd"))) != CLEANUP_RESERVE_USD
    ):
        raise ValueError("campaign ledger active budget epoch is not exact")
    active = ledger.get("activeAttempt")
    if active is None:
        if (
            budget.get("currentAttemptOrdinal") is not None
            or budget.get("currentAttemptPaidStartAt") is not None
            or budget.get("currentAttemptReservedOperationalUpperBoundUsd")
            is not None
            or budget.get("currentAttemptMaximumIncludingCleanupUsd") is not None
        ):
            raise ValueError("idle campaign ledger retains active attempt budget state")
        if status == "budget-exhausted" and (
            budget.get("newPaidWorkAuthorized") is not False
            or prior < NEW_PAID_WORK_STOP_USD
            and Decimal(
                str(
                    budget.get(
                        "nextScopedAttemptMaximumIncludingCleanupUsd",
                        HARD_CAP_USD + Decimal("0.000001"),
                    )
                )
            )
            <= HARD_CAP_USD
        ):
            raise ValueError("budget-exhausted campaign state is not fail-closed")
        return prior, epoch_id, canonical_sha256(ledger)
    if not allow_active or not isinstance(active, dict):
        raise ValueError("new cost admission requires an idle campaign ledger")
    run_id = str(active.get("runId", ""))
    session_id = str(active.get("sessionId", ""))
    validate_identifiers(run_id, session_id)
    if (
        (expected_run_id is not None and run_id != expected_run_id)
        or (expected_session_id is not None and session_id != expected_session_id)
        or active.get("ordinal") != len(attempts) + 1
        or active.get("attemptType") != "aws-full-stack-scoped-diagnostic"
        or active.get("promotionEligible") is not False
        or active.get("state") not in {
            "sealed-unpaid",
            "image-preparation-paid",
            "images-prepared",
            "runtime-sealed",
            "runtime-deploy-started",
            "terminal-cleaned-awaiting-ledger-entry",
            "initialization-failed-cleaned-awaiting-ledger-entry",
            "image-preparation-failed-cleaned",
            "cleanup-required",
        }
        or any(
            item.get("runId") == run_id or item.get("sessionId") == session_id
            for item in attempts
            if isinstance(item, dict)
        )
        or budget.get("currentAttemptOrdinal") != active.get("ordinal")
    ):
        raise ValueError("active scoped attempt identity or state is not exact")
    admission_sha = str(active.get("admissionLedgerSha256", ""))
    if re.fullmatch(r"[0-9a-f]{64}", admission_sha) is None:
        raise ValueError("active scoped attempt admission ledger hash is invalid")
    paid_started_at = active.get("paidStartedAt")
    if active.get("state") == "sealed-unpaid":
        if paid_started_at is not None or budget.get("currentAttemptPaidStartAt") is not None:
            raise ValueError("unpaid scoped attempt has a paid timestamp")
    elif (
        not isinstance(paid_started_at, str)
        or budget.get("currentAttemptPaidStartAt") != paid_started_at
    ):
        raise ValueError("paid scoped attempt timestamp is not durable")
    if (
        str(budget.get("currentAttemptReservedOperationalUpperBoundUsd"))
        != str(active.get("chargedOperationalUpperBoundUsd"))
        or str(budget.get("currentAttemptMaximumIncludingCleanupUsd"))
        != str(active.get("maximumIncludingCleanupUsd"))
    ):
        raise ValueError("active scoped attempt budget reservation is not exact")
    return prior, epoch_id, admission_sha


def validate_phase8_promotion_policy(policy: dict[str, Any]) -> None:
    if (
        policy.get("schemaVersion") != 1
        or policy.get("recordType")
        != "phase7-2-composite-phase8-promotion-policy"
        or policy.get("decision")
        != "promote-after-minimal-smoke-and-archive-without-new-50k"
        or policy.get("phase5") != "skipped"
        or policy.get("execution", {}).get("new50kRpsAttempt") is not False
        or policy.get("execution", {}).get("newWarmupAttempt") is not False
        or policy.get("execution", {}).get("newScoreAttempt") is not False
        or policy.get("phase8", {}).get("paidAwsExperiment") is not False
        or policy.get("phase8", {}).get("defaultAwsMutation") is not False
        or policy.get("budget", {}).get("activeEpochHardCapUsd") != "60.000000"
        or policy.get("budget", {}).get("cleanupReserveUsd") != "5.000000"
    ):
        raise ValueError("Phase 8 composite promotion policy is not exact")


def build_cost_model(
    price_document: dict[str, Any],
    campaign_ledger: dict[str, Any],
    phase8_promotion_policy: dict[str, Any],
    log_ingest_gib: Decimal = LOG_INGEST_GIB_CAP,
    *,
    allow_active_ledger: bool = False,
    expected_run_id: str | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    if price_document.get("region") != "ap-northeast-2":
        raise ValueError("price document must be for ap-northeast-2")
    raw = price_document.get("prices")
    if not isinstance(raw, dict):
        raise ValueError("price document has no prices object")
    missing = sorted(REQUIRED_PRICES.difference(raw))
    if missing:
        raise ValueError(f"price document is missing: {', '.join(missing)}")
    if log_ingest_gib <= 0 or log_ingest_gib > LOG_INGEST_GIB_CAP:
        raise ValueError(
            "scoped diagnostic log allowance must be positive and at most 0.5 GiB"
        )
    prices = {name: Decimal(str(raw[name])) for name in REQUIRED_PRICES}
    active_prior_usd, active_epoch_id, ledger_binding_sha256 = (
        validate_campaign_ledger(
            campaign_ledger,
            allow_active=allow_active_ledger,
            expected_run_id=expected_run_id,
            expected_session_id=expected_session_id,
        )
    )
    validate_phase8_promotion_policy(phase8_promotion_policy)
    if any(value < 0 for value in prices.values()) or any(
        value < 0 for value in (active_prior_usd, log_ingest_gib)
    ):
        raise ValueError("cost inputs must be non-negative")

    month_fraction = HOURS / MONTH_HOURS
    components = [
        Component("collector c6i.xlarge", Decimal(6) * HOURS, "instance-hour", prices["collectorC6iXlargeHour"], "Attempt 17 full topology; no collector traffic is generated."),
        Component("HAProxy c6in.xlarge", Decimal(2) * HOURS, "instance-hour", prices["haproxyC6inXlargeHour"], "Attempt 17 full topology; no protocol load stage runs."),
        Component("load generator c6in.large", Decimal(8) * HOURS, "instance-hour", prices["generatorC6inLargeHour"], "Unavoidable full-stack hosts remain idle."),
        Component("consumer c7g.large", Decimal(2) * HOURS, "instance-hour", prices["consumerC7gLargeHour"], "Unavoidable full-stack KCL hosts remain ready without input."),
        Component("ClickHouse r7g.2xlarge", HOURS, "instance-hour", prices["clickHouseR7g2xlargeHour"], "One 15M seed and one retain-source archive diagnostic."),
        Component("Kinesis provisioned shards", Decimal(120) * HOURS, "shard-hour", prices["kinesisProvisionedShardHour"], "Unavoidable Attempt 17 stream; no PUT workload runs."),
        Component("Kinesis PUT payload units", Decimal(0), "25-KiB unit", prices["kinesisPutPayloadUnit"], "Correctness, replacement, warmup and score run zero times."),
        Component("three Network Load Balancers", Decimal(3) * HOURS, "NLB-hour", prices["nlbHour"], "Unchanged full-stack NLB topology."),
        Component("NLB capacity units", Decimal(30), "NLCU-hour", prices["nlbLcuHour"], "Archive/verification allowance without 50k protocol traffic."),
        Component("single NAT gateway", HOURS, "gateway-hour", prices["natGatewayHour"], "Unchanged full-stack network topology."),
        Component("NAT processed data", Decimal(10), "GiB", prices["natGatewayDataGb"], "Image pulls, bootstrap, logs and control traffic upper bound."),
        Component("NAT gateway public IPv4 address", HOURS, "IPv4-hour", prices["publicIpv4Hour"], "One NAT public address."),
        Component("gp3 capacity", Decimal(1040) * month_fraction, "GB-month", prices["ebsGp3GbMonth"], "Eighteen roots plus the 500-GiB ClickHouse root for one operational hour."),
        Component("gp3 extra throughput", (Decimal(375) / Decimal(1024)) * month_fraction, "GiB/s-month", prices["ebsGp3ThroughputGibpsMonth"], "ClickHouse 500 MiB/s minus included 125 MiB/s."),
        Component("Secrets Manager secret", month_fraction, "secret-month", prices["secretsManagerSecretMonth"], "One run-owned secret."),
        Component("Secrets Manager API", Decimal(500), "API request", prices["secretsManagerApiRequest"], "Service and archive task startup allowance."),
        Component("CloudWatch custom metrics", Decimal(4500) * month_fraction, "metric-month", prices["cloudWatchMetricMonth"], "Full topology Container Insights/KCL series for one operational hour."),
        Component("CloudWatch log ingestion", log_ingest_gib, "GiB", prices["cloudWatchLogIngestGb"], "No HTTP load; archive and readiness logs only."),
        Component("CloudWatch log storage", log_ingest_gib * month_fraction, "GiB-month", prices["cloudWatchLogStorageGbMonth"], "Run-owned logs are removed during cleanup."),
        Component("CloudWatch GetMetricData", Decimal(20000), "requested metric", prices["cloudWatchGetMetricDataMetric"], "Scoped deployment/archive evidence allowance."),
        Component("ECR image storage", Decimal(3) * month_fraction, "GiB-month", prices["ecrStorageGbMonth"], "Three fresh exact run-owned images."),
        Component("DynamoDB on-demand reads", Decimal(200000), "read unit", prices["dynamoDbReadRequestUnit"], "Idle KCL metadata polling allowance."),
        Component("DynamoDB on-demand writes", Decimal(100000), "write unit", prices["dynamoDbWriteRequestUnit"], "Lease/checkpoint startup allowance."),
        Component("DynamoDB storage", Decimal("0.3") * month_fraction, "GB-month", prices["dynamoDbStorageGbMonth"], "Three run-owned metadata tables."),
        Component("S3 standard storage", Decimal(6) * month_fraction, "GB-month", prices["s3StandardStorageGbMonth"], "Three Parquet parts and evidence until cleanup."),
        Component("S3 write/list requests", Decimal(10000), "request", prices["s3Tier1Request"], "Archive multipart and evidence writes."),
        Component("S3 read requests", Decimal(30000), "request", prices["s3Tier2Request"], "Repeated immutable commit, manifest and part validation."),
        Component("evidence and transfer reserve", Decimal(1), "fixed reserve", Decimal("1.5"), "Unmodeled regional transfer and evidence retrieval."),
        Component("failure-path reserve", Decimal(1), "fixed reserve", Decimal("0.5"), "Rollback and delayed metering uncertainty."),
    ]
    charge = sum((item.cost for item in components), Decimal("0"))
    active_operational = active_prior_usd + charge
    maximum = active_operational + CLEANUP_RESERVE_USD
    checks = {
        "paidCapacityWallClockAtMostOneHour": HOURS == Decimal("1"),
        "diagnosticLogVolumeAtOrBelowHalfGiB": log_ingest_gib <= LOG_INGEST_GIB_CAP,
        "activeOperationalBelowNewPaidWorkStop": active_operational < NEW_PAID_WORK_STOP_USD,
        "cleanupReserveAtLeastFiveUsd": CLEANUP_RESERVE_USD >= Decimal("5"),
        "diagnosticMaximumIncludingCleanupAtOrBelowHardCap": maximum <= HARD_CAP_USD,
        "new50kWarmupAndScoreAttemptsDisabled": (
            phase8_promotion_policy["execution"]["new50kRpsAttempt"] is False
            and phase8_promotion_policy["execution"]["newWarmupAttempt"] is False
            and phase8_promotion_policy["execution"]["newScoreAttempt"] is False
        ),
        "phase8PaidAwsExperimentUpperBoundIsZero": (
            phase8_promotion_policy["phase8"]["paidAwsExperiment"] is False
        ),
    }
    return {
        "schemaVersion": 1,
        "workload": "phase7-full-stack-scoped-archive-diagnostic",
        "attemptType": "aws-full-stack-scoped-diagnostic",
        "promotionEligible": False,
        "calculatedAt": utc_now(),
        "priceAsOf": price_document.get("asOf"),
        "priceDocumentSha256": canonical_sha256(price_document),
        "region": price_document.get("region"),
        "paidWallClockHours": decimal_text(HOURS),
        "components": [item.as_dict() for item in components],
        "activeEpochPriorUpperBoundUsd": money_text(active_prior_usd),
        "activeEpochId": active_epoch_id,
        "campaignLedgerSha256": ledger_binding_sha256,
        "chargedOperationalUpperBoundUsd": money_text(charge),
        "operationalMaximumUsd": money_text(active_operational),
        "cleanupReserveUsd": money_text(CLEANUP_RESERVE_USD),
        "maximumIncludingCleanupUsd": money_text(maximum),
        "phase8PromotionPolicySha256": canonical_sha256(phase8_promotion_policy),
        "phase8PromotionPolicy": phase8_promotion_policy,
        "phase8PaidAwsExperimentOperationalUpperBoundUsd": money_text(Decimal("0")),
        "projectedCampaignMaximumIncludingCleanupUsd": money_text(maximum),
        "limits": {
            "newPaidWorkStopUsd": money_text(NEW_PAID_WORK_STOP_USD),
            "hardCapUsd": money_text(HARD_CAP_USD),
        },
        "stageDeadlineMinutes": {
            "cleanupStart": 45,
            "hard": 120,
        },
        "logPolicy": {
            "loadStagesRun": False,
            "ingestUpperBoundGiB": decimal_text(log_ingest_gib),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate_cost_model(
    price_document: dict[str, Any],
    campaign_ledger: dict[str, Any],
    cost_model: dict[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_session_id: str | None = None,
) -> bool:
    try:
        log_ingest_gib = Decimal(str(cost_model["logPolicy"]["ingestUpperBoundGiB"]))
        phase8_promotion_policy = cost_model["phase8PromotionPolicy"]
        if not isinstance(phase8_promotion_policy, dict):
            return False
        expected = build_cost_model(
            price_document,
            campaign_ledger,
            phase8_promotion_policy,
            log_ingest_gib,
            allow_active_ledger=True,
            expected_run_id=expected_run_id,
            expected_session_id=expected_session_id,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
    if without_calculated_at(cost_model) != without_calculated_at(expected):
        return False
    active = campaign_ledger.get("activeAttempt")
    if active is not None:
        authorization = active.get("costAuthorization", {})
        if authorization != {
            "campaignLedgerSha256": cost_model.get("campaignLedgerSha256"),
            "priceDocumentSha256": cost_model.get("priceDocumentSha256"),
            "phase8PromotionPolicySha256": cost_model.get(
                "phase8PromotionPolicySha256"
            ),
        }:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--attempt-ledger", required=True, type=Path)
    parser.add_argument("--phase8-promotion-policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("scoped diagnostic cost model is immutable")
    result = build_cost_model(
        read_json(args.prices),
        read_json(args.attempt_ledger),
        read_json(args.phase8_promotion_policy),
        LOG_INGEST_GIB_CAP,
    )
    write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
