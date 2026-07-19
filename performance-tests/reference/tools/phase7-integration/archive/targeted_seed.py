#!/usr/bin/env python3
"""Seed the targeted diagnostic ClickHouse partition once without exposing credentials."""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from datetime import date, timedelta

import boto3
from botocore.config import Config

from seed_partition import FULL_SCALE_ROWS, GeneratorContract, seed_insert_sql


SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 5},
    user_agent_appid="loopad-phase7-targeted-seed/1",
)


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value or value.strip() != value:
        raise ValueError(f"missing or invalid runtime configuration: {name}")
    return value


def execute(query: str, *, timeout: int = 30) -> str:
    credentials = base64.b64encode(
        f"{required('CLICKHOUSE_USER')}:{required('CLICKHOUSE_PASSWORD')}".encode()
    ).decode()
    request = urllib.request.Request(
        required("CLICKHOUSE_HTTP_URL"),
        data=query.encode(),
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode().strip()


def put_result(run_id: str, name: str, result: dict[str, object]) -> None:
    body = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
    boto3.client(
        "s3", region_name=required("AWS_REGION"), config=SDK_CONFIG
    ).put_object(
        Bucket=required("ARCHIVE_BUCKET"),
        Key=f"diagnostics/{run_id}/{name}.json",
        Body=body,
        ContentType="application/json",
        IfNoneMatch="*",
    )
    print(body.decode(), end="")


def seed(run_id: str, partition: date, today: date) -> None:
    contract = GeneratorContract(
        version="phase6-events-v1",
        seed=6_000_017,
        partition=partition.isoformat(),
        rows=FULL_SCALE_ROWS,
        run_id=run_id,
    )
    execute(seed_insert_sql(contract), timeout=900)
    observed = execute(
        "SELECT concat(toString(count()), '\\t', toString(uniqExact(event_id))) "
        "FROM loopad.events FINAL "
        f"WHERE event_date = toDate('{partition.isoformat()}')",
        timeout=300,
    )
    rows_text, unique_text = observed.split("\t", 1)
    result = {
        "schemaVersion": 1,
        "status": "passed",
        "runId": run_id,
        "partition": partition.isoformat(),
        "rows": int(rows_text),
        "uniqueEvents": int(unique_text),
        "requestedRows": FULL_SCALE_ROWS,
    }
    if result["rows"] != FULL_SCALE_ROWS or result["uniqueEvents"] != FULL_SCALE_ROWS:
        raise RuntimeError("targeted seed count or uniqueness mismatch")
    put_result(run_id, "seed", result)


def verify(run_id: str, partition: date, today: date) -> None:
    execute("SYSTEM FLUSH LOGS", timeout=60)
    observed = execute(
        "SELECT concat("
        "toString(count()), '\\t', "
        "toString(uniqExact(event_id))) "
        "FROM loopad.events FINAL "
        f"WHERE event_date = toDate('{partition.isoformat()}')",
        timeout=300,
    )
    rows_text, unique_text = observed.split("\t", 1)
    query_log = execute(
        "SELECT concat("
        "toString(countIf(type IN ('ExceptionBeforeStart', "
        "'ExceptionWhileProcessing') AND exception_code = 241)), '\\t', "
        "toString(countIf(query_kind = 'Alter' AND "
        "positionCaseInsensitive(query, 'DROP PARTITION') > 0))) "
        "FROM system.query_log "
        "WHERE event_time >= now() - INTERVAL 2 HOUR",
        timeout=60,
    )
    code_241_text, drop_text = query_log.split("\t", 1)
    result = {
        "schemaVersion": 1,
        "status": "passed",
        "action": "verify",
        "runId": run_id,
        "partition": partition.isoformat(),
        "today": today.isoformat(),
        "sourceRowsAfter": int(rows_text),
        "sourceUniqueEventsAfter": int(unique_text),
        "code241Exceptions": int(code_241_text),
        "sourceDropQueries": int(drop_text),
    }
    if (
        result["sourceRowsAfter"] != FULL_SCALE_ROWS
        or result["sourceUniqueEventsAfter"] != FULL_SCALE_ROWS
        or result["code241Exceptions"] != 0
        or result["sourceDropQueries"] != 0
    ):
        result["status"] = "failed"
        put_result(run_id, "verify", result)
        raise RuntimeError("targeted post-archive verification failed")
    put_result(run_id, "verify", result)


def main() -> int:
    run_id = required("RUN_ID")
    today = date.fromisoformat(required("ARCHIVE_TODAY"))
    partition = date.fromisoformat(required("ARCHIVE_PARTITION"))
    if partition != today - timedelta(days=8):
        raise ValueError("targeted source partition must be UTC today minus eight days")
    action = os.environ.get("TARGETED_ACTION", "seed")
    if action == "seed":
        seed(run_id, partition, today)
    elif action == "verify":
        verify(run_id, partition, today)
    else:
        raise ValueError("TARGETED_ACTION must be exactly seed or verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
