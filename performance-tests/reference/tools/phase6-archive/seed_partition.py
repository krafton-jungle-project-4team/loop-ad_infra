#!/usr/bin/env python3
"""Deterministic Phase 6 partition generator using ClickHouse numbers()."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

GENERATOR_VERSION = "phase6-events-v1"
DEFAULT_SEED = 6_000_017
FULL_SCALE_ROWS = 15_000_000
FULL_SCALE_PART_ROWS = 5_000_000

INSERT_COLUMNS = (
    "project_id",
    "write_key",
    "schema_version",
    "event_id",
    "event_name",
    "event_time",
    "source",
    "user_id",
    "session_id",
    "properties_json",
    "producer_sent_at",
    "run_id",
    "kinesis_shard_id",
    "kinesis_sequence_number",
    "ingested_at",
)

ARCHIVE_COLUMNS = (
    "project_id",
    "write_key",
    "schema_version",
    "event_id",
    "event_name",
    "event_time",
    "event_date",
    "source",
    "user_id",
    "session_id",
    "properties_json",
    "producer_sent_at",
    "run_id",
    "kinesis_shard_id",
    "kinesis_sequence_number",
    "ingested_at",
)


@dataclass(frozen=True)
class GeneratorContract:
    version: str
    seed: int
    partition: str
    rows: int
    run_id: str

    def reference_sha256(self) -> str:
        reference_sql = generator_select_sql(self, include_event_date=True)
        body = json.dumps(
            {
                "columns": ARCHIVE_COLUMNS,
                "referenceSqlSha256": hashlib.sha256(reference_sql.encode("utf-8")).hexdigest(),
                "partition": self.partition,
                "rows": self.rows,
                "runId": self.run_id,
                "seed": self.seed,
                "version": self.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()


def utc_source_partition(today: date) -> date:
    return today - timedelta(days=8)


def eligibility_cutoff(today: date) -> date:
    return today - timedelta(days=7)


def is_eligible(partition: date, today: date) -> bool:
    return partition < eligibility_cutoff(today)


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def generator_select_sql(
    contract: GeneratorContract,
    *,
    offset: int = 0,
    rows: Optional[int] = None,
    include_event_date: bool,
) -> str:
    selected_rows = contract.rows - offset if rows is None else rows
    if offset < 0 or selected_rows < 0 or offset + selected_rows > contract.rows:
        raise ValueError("generator range is outside the contract")
    partition = date.fromisoformat(contract.partition)
    if contract.version != GENERATOR_VERSION:
        raise ValueError(f"unsupported generator version: {contract.version}")
    if contract.seed < 0:
        raise ValueError("seed must be non-negative")

    base = sql_string(f"{partition.isoformat()} 00:00:00.000")
    run_id = sql_string(contract.run_id)
    source = sql_string(GENERATOR_VERSION)
    event_time = f"toDateTime64({base}, 3, 'UTC') + toIntervalMillisecond(number)"
    values = [
        "'phase6-project' AS project_id",
        "'phase6-write-key' AS write_key",
        "'hotel_rec_promo.v1' AS schema_version",
        (
            "concat('phase6-', leftPad(toString(number + "
            f"{contract.seed}), 20, '0')) AS event_id"
        ),
        "'archive_fixture' AS event_name",
        f"{event_time} AS event_time",
    ]
    if include_event_date:
        values.append(f"toDate32({event_time}) AS event_date")
    values.extend(
        [
            f"{source} AS source",
            (
                "CAST(if(number % 3 = 0, NULL, concat('user-', "
                "toString(number % 100000))) AS Nullable(String)) AS user_id"
            ),
            (
                "CAST(if(number % 5 = 0, NULL, concat('session-', "
                "toString(intDiv(number, 100)))) AS Nullable(String)) AS session_id"
            ),
            (
                "concat('{\"n\":', toString(number), ',\"seed\":', "
                f"toString({contract.seed}), '}}') AS properties_json"
            ),
            (
                "CAST(if(number % 7 = 0, NULL, "
                f"{event_time}) AS Nullable(DateTime64(3, 'UTC'))) AS producer_sent_at"
            ),
            f"CAST({run_id} AS Nullable(String)) AS run_id",
            "concat('shardId-', leftPad(toString(number % 128), 4, '0')) AS kinesis_shard_id",
            (
                f"reinterpretAsFixedString(toUInt256(number + {contract.seed})) "
                "AS kinesis_sequence_number"
                if include_event_date
                else f"toUInt256(number + {contract.seed}) AS kinesis_sequence_number"
            ),
            f"{event_time} + toIntervalSecond(1) AS ingested_at",
        ]
    )
    return (
        "SELECT\n    "
        + ",\n    ".join(values)
        + f"\nFROM numbers({offset}, {selected_rows})"
    )


def seed_insert_sql(contract: GeneratorContract) -> str:
    columns = ", ".join(INSERT_COLUMNS)
    select = generator_select_sql(contract, include_event_date=False)
    return f"INSERT INTO loopad.events ({columns})\n{select}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--rows", type=int, default=FULL_SCALE_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--reference", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--range-rows", type=int)
    args = parser.parse_args()

    contract = GeneratorContract(
        version=GENERATOR_VERSION,
        seed=args.seed,
        partition=date.fromisoformat(args.partition).isoformat(),
        rows=args.rows,
        run_id=args.run_id,
    )
    if args.reference:
        print(
            generator_select_sql(
                contract,
                offset=args.offset,
                rows=args.range_rows,
                include_event_date=True,
            )
        )
    else:
        print(seed_insert_sql(contract))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
