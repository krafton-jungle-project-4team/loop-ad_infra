#!/usr/bin/env python3
"""Single-worker ClickHouse FINAL partition archive with immutable S3 commit."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from seed_partition import (
    ARCHIVE_COLUMNS,
    DEFAULT_SEED,
    FULL_SCALE_PART_ROWS,
    FULL_SCALE_ROWS,
    GENERATOR_VERSION,
    GeneratorContract,
    generator_select_sql,
    is_eligible,
    utc_source_partition,
)

MIB = 1024 * 1024
MAX_EXPORT_MIBPS = 100
DEFAULT_CLICKHOUSE_MEMORY = 5 * 1024 ** 3
# Phase 7 runs ClickHouse with a 7 GiB server ceiling. Accept an operational
# envelope up to 6.5 GiB so callers can add headroom without consuming the
# server's final 512 MiB safety reserve.
MAX_CLICKHOUSE_MEMORY = 13 * 1024 ** 3 // 2
EXACT_UNIQUE_BUCKETS = 8
EXTERNAL_GROUP_BY_BYTES = 256 * MIB
EXACT_METRICS_BLOCK_SIZE = 8192
LOGICAL_CHECKSUM_BUCKETS = 8
UINT64_MODULUS = 1 << 64
MANIFEST_CONTRACT_VERSION = "phase6-archive-manifest-v1"
COMMIT_CONTRACT_VERSION = "phase6-archive-commit-v1"

ARCHIVE_PROJECTION = (
    "project_id",
    "write_key",
    "CAST(schema_version AS String) AS schema_version",
    "event_id",
    "CAST(event_name AS String) AS event_name",
    "event_time",
    "CAST(event_date AS Date32) AS event_date",
    "CAST(source AS String) AS source",
    "user_id",
    "session_id",
    "properties_json",
    "producer_sent_at",
    "run_id",
    "CAST(kinesis_shard_id AS String) AS kinesis_shard_id",
    "reinterpretAsFixedString(kinesis_sequence_number) AS kinesis_sequence_number",
    "ingested_at",
)


class ArchiveError(RuntimeError):
    pass


class ValidationError(ArchiveError):
    pass


class CriticalRecoveryError(ArchiveError):
    pass


class RecoveryState(str, Enum):
    NEW_ATTEMPT = "commit-absent-source-present"
    REVALIDATE_AND_DROP = "commit-present-source-present"
    POST_DROP_VALIDATE = "commit-present-source-absent"
    CRITICAL = "commit-absent-source-absent"


@dataclass(frozen=True)
class ArchiveConfig:
    clickhouse_url: str
    bucket: str
    run_id: str
    partition: str
    today: str
    s3_endpoint_url: Optional[str] = None
    s3_url_base: Optional[str] = None
    s3_unsigned: bool = False
    region: str = "ap-northeast-2"
    account: str = "not-measured-local"
    expected_rows: int = FULL_SCALE_ROWS
    rows_per_part: int = FULL_SCALE_PART_ROWS
    part_count: int = 3
    seed: int = DEFAULT_SEED
    generator_version: str = GENERATOR_VERSION
    fingerprint_interval_seconds: int = 300
    export_bandwidth_mibps: int = MAX_EXPORT_MIBPS
    clickhouse_memory_bytes: int = DEFAULT_CLICKHOUSE_MEMORY
    clickhouse_image_digest: str = "not-recorded"
    code_sha256: str = "not-recorded"
    temp_dir: str = "/tmp/loopad-phase6"
    test_mode: bool = False
    test_fault: Optional[str] = None
    clickhouse_user_file: Optional[str] = None
    clickhouse_password_file: Optional[str] = None
    retain_source_after_commit: bool = False

    def validate(self) -> None:
        if type(self.retain_source_after_commit) is not bool:
            raise ValueError("retain_source_after_commit must be a boolean")
        partition = date.fromisoformat(self.partition)
        today = date.fromisoformat(self.today)
        if not is_eligible(partition, today):
            raise ValueError("partition is not older than the UTC seven-day cutoff")
        if self.export_bandwidth_mibps <= 0 or self.export_bandwidth_mibps > MAX_EXPORT_MIBPS:
            raise ValueError("export bandwidth must be in the range 1..100 MiB/s")
        if self.clickhouse_memory_bytes <= 0 or self.clickhouse_memory_bytes > MAX_CLICKHOUSE_MEMORY:
            raise ValueError(
                "ClickHouse query memory must be positive and retain at least "
                "512 MiB below the 7 GiB server ceiling"
            )
        if self.expected_rows <= 0 or self.rows_per_part <= 0 or self.part_count <= 0:
            raise ValueError("row and part values must be positive")
        if self.expected_rows != self.rows_per_part * self.part_count:
            raise ValueError("expected rows must equal rows-per-part times part count")
        if self.generator_version != GENERATOR_VERSION:
            raise ValueError("generator version does not match the frozen contract")
        if self.test_fault and not self.test_mode:
            raise ValueError("fault injection is allowed only in test mode")
        if not self.test_mode:
            if (
                self.expected_rows != FULL_SCALE_ROWS
                or self.rows_per_part != FULL_SCALE_PART_ROWS
                or self.part_count != 3
            ):
                raise ValueError("production contract requires exactly 3 x 5,000,000 rows")
            if self.fingerprint_interval_seconds != 300:
                raise ValueError("production fingerprints must be five minutes apart")
            if partition != utc_source_partition(today):
                raise ValueError("production source partition must be UTC today minus eight days")

    @property
    def commit_key(self) -> str:
        return f"commits/v1/table=events/event_date={self.partition}/COMMITTED"

    @property
    def generator(self) -> GeneratorContract:
        return GeneratorContract(
            version=self.generator_version,
            seed=self.seed,
            partition=self.partition,
            rows=self.expected_rows,
            run_id=self.run_id,
        )


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def recovery_state(commit_exists: bool, source_exists: bool) -> RecoveryState:
    if not commit_exists and source_exists:
        return RecoveryState.NEW_ATTEMPT
    if commit_exists and source_exists:
        return RecoveryState.REVALIDATE_AND_DROP
    if commit_exists and not source_exists:
        return RecoveryState.POST_DROP_VALIDATE
    return RecoveryState.CRITICAL


def authorize_drop(*, manifest_valid: bool, pre_equivalent: bool, commit_revalidated: bool) -> None:
    if not (manifest_valid and pre_equivalent and commit_revalidated):
        raise ValidationError("source deletion is blocked by an incomplete pre-DROP gate")


class ClickHouseHttp:
    def __init__(
        self,
        url: str,
        *,
        user_file: Optional[str] = None,
        password_file: Optional[str] = None,
        timeout_seconds: int = 1800,
    ) -> None:
        self.url = url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.headers: Dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
        if bool(user_file) != bool(password_file):
            raise ValueError("ClickHouse user and password credential files must be supplied together")
        if user_file and password_file:
            user = Path(user_file).read_text(encoding="utf-8").strip()
            password = Path(password_file).read_text(encoding="utf-8").strip()
            token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            self.headers["Authorization"] = f"Basic {token}"

    def _open(self, query: str):
        request = urllib.request.Request(
            self.url,
            data=query.encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise ArchiveError(f"ClickHouse HTTP {error.code}: {body[:2000]}") from error

    def execute(self, query: str) -> str:
        with self._open(query) as response:
            return response.read().decode("utf-8")

    def json_rows(self, query: str) -> List[Dict[str, Any]]:
        text = self.execute(query.rstrip().rstrip(";") + " FORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def one(self, query: str) -> Dict[str, Any]:
        rows = self.json_rows(query)
        if len(rows) != 1:
            raise ArchiveError(f"expected one ClickHouse row, got {len(rows)}")
        return rows[0]

    def stream_to_file(self, query: str, path: Path, bandwidth_mibps: int) -> int:
        bytes_written = 0
        started = time.monotonic()
        rate = bandwidth_mibps * MIB
        with self._open(query) as response, path.open("xb") as output:
            while True:
                chunk = response.read(MIB)
                if not chunk:
                    break
                output.write(chunk)
                bytes_written += len(chunk)
                minimum_elapsed = bytes_written / rate
                remaining = minimum_elapsed - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        return bytes_written


def build_s3_client(config: ArchiveConfig):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    sdk_config = Config(
        signature_version=UNSIGNED if config.s3_unsigned else "s3v4",
        retries={"total_max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=1800,
        max_pool_connections=4,
        s3={"addressing_style": "path" if config.s3_endpoint_url else "virtual"},
    )
    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint_url,
        region_name=config.region,
        config=sdk_config,
    )


def is_precondition_failure(error: BaseException) -> bool:
    response = getattr(error, "response", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code")
    return status == 412 or code in {"PreconditionFailed", "412"}


class ArchiveWorker:
    def __init__(self, config: ArchiveConfig, clickhouse: ClickHouseHttp, s3: Any) -> None:
        config.validate()
        self.config = config
        self.clickhouse = clickhouse
        self.s3 = s3

    def _source_where(self) -> str:
        return f"event_date = toDate({sql_string(self.config.partition)})"

    def _source_relation(self) -> str:
        projection = ", ".join(ARCHIVE_PROJECTION)
        return (
            f"SELECT {projection} FROM loopad.events FINAL "
            f"WHERE {self._source_where()}"
        )

    def _source_part_relation(self, offset: int, rows: int) -> str:
        return (
            self._source_relation()
            + f" ORDER BY event_time, event_id LIMIT {rows} OFFSET {offset}"
        )

    def _object_url(self, key: str) -> str:
        if self.config.s3_url_base:
            return f"{self.config.s3_url_base.rstrip('/')}/{key}"
        return (
            f"https://{self.config.bucket}.s3.{self.config.region}.amazonaws.com/{key}"
        )

    def _s3_relation(self, key: str) -> str:
        url = sql_string(self._object_url(key))
        if self.config.s3_unsigned:
            return f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM s3({url}, NOSIGN, 'Parquet')"
        return f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM s3({url}, 'Parquet')"

    def _metrics(self, relation: str) -> Dict[str, Any]:
        self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
        row = self.clickhouse.one(
            "SELECT\n"
            "  count() AS rows,\n"
            "  min(toUnixTimestamp64Milli(event_time)) AS min_event_time_ms,\n"
            "  max(toUnixTimestamp64Milli(event_time)) AS max_event_time_ms\n"
            f"FROM ({relation})\n"
            f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
            "max_threads=1, "
            f"max_block_size={EXACT_METRICS_BLOCK_SIZE}, "
            "max_bytes_before_external_sort=536870912, "
            "max_bytes_before_external_group_by=536870912"
        )
        unique_events = 0
        for bucket in range(EXACT_UNIQUE_BUCKETS):
            self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
            unique = self.clickhouse.one(
                "SELECT toUInt64(uniqExact(event_id)) AS unique_events\n"
                f"FROM ({relation})\n"
                f"WHERE cityHash64(event_id) % {EXACT_UNIQUE_BUCKETS} = {bucket}\n"
                f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
                f"max_threads=1, max_block_size={EXACT_METRICS_BLOCK_SIZE}, "
                f"max_bytes_before_external_group_by={EXTERNAL_GROUP_BY_BYTES}, "
                "max_bytes_ratio_before_external_group_by=0.2"
            )
            unique_events += int(unique["unique_events"])
        checksum = self._logical_checksum(relation)
        return {
            "rows": int(row["rows"]),
            "uniqueEvents": unique_events,
            "uniqueAlgorithm": (
                f"sum of uniqExact(event_id) over {EXACT_UNIQUE_BUCKETS} disjoint "
                "cityHash64 buckets computed sequentially with external spill"
            ),
            "minEventTimeMs": None if row["min_event_time_ms"] is None else int(row["min_event_time_ms"]),
            "maxEventTimeMs": None if row["max_event_time_ms"] is None else int(row["max_event_time_ms"]),
            "logicalChecksum": checksum,
            "logicalChecksumAlgorithm": (
                f"UInt64 sum of the canonical row cityHash64 over {LOGICAL_CHECKSUM_BUCKETS} "
                "disjoint event_id buckets"
            ),
        }

    def _logical_checksum(self, relation: str) -> str:
        total = 0
        for bucket in range(LOGICAL_CHECKSUM_BUCKETS):
            self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
            row = self.clickhouse.one(
                "SELECT toString(ifNull(sum(cityHash64(\n"
                "  project_id, write_key, schema_version, event_id, event_name,\n"
                "  toString(event_time), toString(event_date), source,\n"
                "  ifNull(user_id, '\\0'), ifNull(session_id, '\\0'), properties_json,\n"
                "  ifNull(toString(producer_sent_at), '\\0'), ifNull(run_id, '\\0'),\n"
                "  kinesis_shard_id, toString(kinesis_sequence_number), toString(ingested_at)\n"
                ")), toUInt64(0))) AS logical_checksum\n"
                f"FROM ({relation})\n"
                f"WHERE cityHash64(event_id) % {LOGICAL_CHECKSUM_BUCKETS} = {bucket}\n"
                f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
                f"max_threads=1, max_block_size={EXACT_METRICS_BLOCK_SIZE}, "
                "optimize_move_to_prewhere=0, optimize_move_to_prewhere_if_final=0"
            )
            total = (total + int(row["logical_checksum"])) % UINT64_MODULUS
        return str(total)

    def _schema(self, relation: str) -> List[Dict[str, str]]:
        rows = self.clickhouse.json_rows(f"DESCRIBE TABLE ({relation})")
        return [{"name": str(row["name"]), "type": str(row["type"])} for row in rows]

    def _schema_hash(self, schema: Sequence[Mapping[str, str]]) -> str:
        return sha256_bytes(canonical_json_bytes({"columns": list(schema)}))

    def _source_count(self) -> int:
        row = self.clickhouse.one(
            f"SELECT count() AS rows FROM loopad.events FINAL WHERE {self._source_where()}"
        )
        return int(row["rows"])

    def _relation_count(self, relation: str) -> int:
        self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
        row = self.clickhouse.one(
            f"SELECT count() AS rows FROM ({relation}) "
            f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
            f"max_threads=1, max_block_size={EXACT_METRICS_BLOCK_SIZE}"
        )
        return int(row["rows"])

    def _head_exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except BaseException as error:
            response = getattr(error, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def _get_bytes(self, key: str) -> bytes:
        response = self.s3.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def _get_json(self, key: str) -> Dict[str, Any]:
        return json.loads(self._get_bytes(key))

    def _remote_sha256(self, key: str) -> str:
        digest = hashlib.sha256()
        response = self.s3.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=MIB):
                if chunk:
                    digest.update(chunk)
        finally:
            body.close()
        return digest.hexdigest()

    def _conditional_put_json(self, key: str, document: Mapping[str, Any]) -> Tuple[bool, str]:
        body = canonical_json_bytes(document)
        digest = sha256_bytes(body)
        try:
            self.s3.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                StorageClass="STANDARD",
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
            return True, digest
        except BaseException as error:
            if is_precondition_failure(error):
                return False, digest
            raise

    def _stable_fingerprints(self) -> List[Dict[str, Any]]:
        source_relation = self._source_relation()
        schema = self._schema(source_relation)
        schema_hash = self._schema_hash(schema)
        values = []
        for index in range(2):
            background = self.clickhouse.one(
                "SELECT\n"
                "  (SELECT count() FROM system.merges WHERE database = 'loopad' AND table = 'events') AS merges,\n"
                "  (SELECT count() FROM system.mutations WHERE database = 'loopad' AND table = 'events' AND is_done = 0) AS mutations"
            )
            if int(background["merges"]) != 0 or int(background["mutations"]) != 0:
                raise ValidationError("source has an active merge or mutation")
            values.append(
                {
                    "measuredAt": iso_now(),
                    "schemaSha256": schema_hash,
                    "activeMerges": int(background["merges"]),
                    "activeMutations": int(background["mutations"]),
                    **self._metrics(source_relation),
                }
            )
            if index == 0 and self.config.fingerprint_interval_seconds:
                time.sleep(self.config.fingerprint_interval_seconds)
        first = {key: value for key, value in values[0].items() if key != "measuredAt"}
        second = {key: value for key, value in values[1].items() if key != "measuredAt"}
        if first != second:
            raise ValidationError("source fingerprint changed between stability measurements")
        if first["rows"] != self.config.expected_rows:
            raise ValidationError("source row count does not match the deterministic contract")
        if first["uniqueEvents"] != self.config.expected_rows:
            raise ValidationError("source event_id uniqueness does not match the deterministic contract")
        return values

    def _export_query(self, offset: int, rows: int) -> str:
        max_network = self.config.export_bandwidth_mibps * MIB
        return (
            self._source_part_relation(offset, rows)
            + " SETTINGS"
            + f" max_memory_usage={self.config.clickhouse_memory_bytes},"
            + " max_threads=1,"
            + f" max_block_size={EXACT_METRICS_BLOCK_SIZE},"
            + " max_bytes_before_external_sort=134217728,"
            + " max_bytes_before_external_group_by=536870912,"
            + f" max_network_bandwidth={max_network},"
            + " output_format_parquet_compression_method='zstd'"
            + " FORMAT Parquet"
        )

    def _put_data_file(self, path: Path, key: str, digest: str) -> None:
        with path.open("rb") as handle:
            self.s3.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=handle,
                ContentType="application/vnd.apache.parquet",
                StorageClass="STANDARD",
                Metadata={"sha256": digest, "compression": "zstd"},
                IfNoneMatch="*",
            )

    def _validate_part(self, part: Mapping[str, Any]) -> None:
        key = str(part["key"])
        if not self._head_exists(key):
            raise ValidationError(f"archive part is missing: {key}")
        remote_sha = self._remote_sha256(key)
        if remote_sha != part["sha256"]:
            raise ValidationError(f"archive part SHA-256 mismatch: {key}")
        relation = self._s3_relation(key)
        if self._relation_count(relation) != int(part["rows"]):
            raise ValidationError(f"archive part row count mismatch: {key}")
        schema_hash = self._schema_hash(self._schema(relation))
        if schema_hash != part["schemaSha256"]:
            raise ValidationError(f"archive part schema mismatch: {key}")

    def _export_parts(self, archive_id: str) -> List[Dict[str, Any]]:
        prefix = (
            f"attempts/v1/table=events/event_date={self.config.partition}/"
            f"archive_id={archive_id}"
        )
        source_schema = self._schema(self._source_relation())
        schema_hash = self._schema_hash(source_schema)
        parts = []
        temp_root = Path(self.config.temp_dir)
        temp_root.mkdir(parents=True, exist_ok=True)
        for index in range(self.config.part_count):
            offset = index * self.config.rows_per_part
            rows = self.config.rows_per_part
            key = f"{prefix}/part-{index:05d}.parquet"
            started_at = iso_now()
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="attempt-", dir=str(temp_root)) as directory:
                path = Path(directory) / f"part-{index:05d}.parquet"
                self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
                byte_count = self.clickhouse.stream_to_file(
                    self._export_query(offset, rows),
                    path,
                    self.config.export_bandwidth_mibps,
                )
                digest = sha256_file(path)
                if not (self.config.test_fault == "missing-part" and index == self.config.part_count - 1):
                    self._put_data_file(path, key, digest)
                if self.config.test_fault == "kill-after-first-part" and index == 0:
                    os.kill(os.getpid(), signal.SIGKILL)
            part = {
                "index": index,
                "key": key,
                "rows": rows,
                "bytes": byte_count,
                "sha256": "0" * 64 if self.config.test_fault == "checksum-mismatch" and index == 0 else digest,
                "schemaSha256": schema_hash,
                "startedAt": started_at,
                "finishedAt": iso_now(),
                "durationSeconds": round(time.monotonic() - started, 6),
            }
            parts.append(part)
            self._validate_part(part)
        return parts

    def _archive_relation(self, parts: Sequence[Mapping[str, Any]]) -> str:
        return " UNION ALL ".join(self._s3_relation(str(part["key"])) for part in parts)

    def _archive_metrics(self, parts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        per_part = [self._metrics(self._s3_relation(str(part["key"]))) for part in parts]
        minimums = [item["minEventTimeMs"] for item in per_part if item["minEventTimeMs"] is not None]
        maximums = [item["maxEventTimeMs"] for item in per_part if item["maxEventTimeMs"] is not None]
        return {
            "rows": sum(int(item["rows"]) for item in per_part),
            "uniqueEvents": sum(int(item["uniqueEvents"]) for item in per_part),
            "uniqueAlgorithm": per_part[0]["uniqueAlgorithm"],
            "minEventTimeMs": min(minimums) if minimums else None,
            "maxEventTimeMs": max(maximums) if maximums else None,
            "logicalChecksum": str(
                sum(int(item["logicalChecksum"]) for item in per_part) % UINT64_MODULUS
            ),
            "logicalChecksumAlgorithm": per_part[0]["logicalChecksumAlgorithm"],
        }

    def _exact_difference(self, left: str, right: str) -> Dict[str, int]:
        # EXCEPT ALL can leave several GiB in jemalloc arenas after the query
        # completes. Purge between the two sequential directions so their
        # resident peaks do not accumulate across validation queries.
        self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
        left_minus_right = self.clickhouse.one(
            f"SELECT count() AS rows FROM (({left}) EXCEPT ALL ({right})) "
            f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
            "max_threads=1, "
            "max_block_size=65536, "
            "max_bytes_before_external_sort=536870912, "
            "max_bytes_before_external_group_by=536870912"
        )
        self.clickhouse.execute("SYSTEM JEMALLOC PURGE")
        right_minus_left = self.clickhouse.one(
            f"SELECT count() AS rows FROM (({right}) EXCEPT ALL ({left})) "
            f"SETTINGS max_memory_usage={self.config.clickhouse_memory_bytes}, "
            "max_threads=1, "
            "max_block_size=65536, "
            "max_bytes_before_external_sort=536870912, "
            "max_bytes_before_external_group_by=536870912"
        )
        return {
            "leftMinusRight": int(left_minus_right["rows"]),
            "rightMinusLeft": int(right_minus_left["rows"]),
        }

    def _equivalence(
        self,
        expected_relation: str,
        parts: Sequence[Mapping[str, Any]],
        *,
        stage: str,
        deterministic_reference: bool,
    ) -> Dict[str, Any]:
        expected_metrics = self._metrics(expected_relation)
        archive_metrics = self._archive_metrics(parts)
        expected_schema = self._schema(expected_relation)
        archive_schema = self._schema(self._s3_relation(str(parts[0]["key"])))
        differences = []
        for index, part in enumerate(parts):
            offset = index * self.config.rows_per_part
            rows = self.config.rows_per_part
            if deterministic_reference:
                expected_part = generator_select_sql(
                    self.config.generator,
                    offset=offset,
                    rows=rows,
                    include_event_date=True,
                )
            else:
                expected_part = self._source_part_relation(offset, rows)
            differences.append(
                {
                    "part": index,
                    **self._exact_difference(expected_part, self._s3_relation(str(part["key"]))),
                }
            )
        passed = (
            expected_metrics == archive_metrics
            and expected_schema == archive_schema
            and expected_metrics["rows"] == self.config.expected_rows
            and expected_metrics["uniqueEvents"] == self.config.expected_rows
            and sum(int(part["rows"]) for part in parts) == self.config.expected_rows
            and all(
                item["leftMinusRight"] == 0 and item["rightMinusLeft"] == 0
                for item in differences
            )
        )
        result = {
            "stage": stage,
            "passed": passed,
            "expectedMetrics": expected_metrics,
            "archiveMetrics": archive_metrics,
            "schema": expected_schema,
            "schemaSha256": self._schema_hash(expected_schema),
            "twoWayDifferences": differences,
        }
        if not passed:
            raise ValidationError(f"{stage} full equivalence failed")
        return result

    def _manifest_key(self, archive_id: str) -> str:
        return (
            f"attempts/v1/table=events/event_date={self.config.partition}/"
            f"archive_id={archive_id}/manifest.json"
        )

    def _build_manifest(
        self,
        archive_id: str,
        fingerprints: Sequence[Mapping[str, Any]],
        parts: Sequence[Mapping[str, Any]],
        export_started_at: str,
        export_seconds: float,
    ) -> Dict[str, Any]:
        schema = self._schema(self._source_relation())
        return {
            "contractVersion": MANIFEST_CONTRACT_VERSION,
            "runId": self.config.run_id,
            "archiveId": archive_id,
            "table": "loopad.events",
            "partition": self.config.partition,
            "eligibilityCutoff": str(date.fromisoformat(self.config.today) - timedelta(days=7)),
            "schema": schema,
            "schemaSha256": self._schema_hash(schema),
            "sourceFingerprints": list(fingerprints),
            "generator": {
                "version": self.config.generator_version,
                "seed": self.config.seed,
                "referenceSha256": self.config.generator.reference_sha256(),
            },
            "source": fingerprints[-1],
            "parts": list(parts),
            "archive": self._archive_metrics(parts),
            "export": {
                "startedAt": export_started_at,
                "finishedAt": iso_now(),
                "durationSeconds": round(export_seconds, 6),
                "maxBandwidthMiBps": self.config.export_bandwidth_mibps,
                "sequential": True,
                "format": "Parquet",
                "compression": "ZSTD",
                "storageClass": "STANDARD",
            },
            "implementation": {
                "codeSha256": self.config.code_sha256,
                "clickHouseImageDigest": self.config.clickhouse_image_digest,
            },
            "environment": {"account": self.config.account, "region": self.config.region},
        }

    def _validate_manifest_objects(self, manifest: Mapping[str, Any]) -> None:
        if manifest.get("contractVersion") != MANIFEST_CONTRACT_VERSION:
            raise ValidationError("manifest contract version mismatch")
        if manifest.get("runId") != self.config.run_id:
            raise ValidationError("manifest run ID mismatch")
        if manifest.get("partition") != self.config.partition:
            raise ValidationError("manifest partition mismatch")
        generator = manifest.get("generator", {})
        if generator.get("referenceSha256") != self.config.generator.reference_sha256():
            raise ValidationError("manifest deterministic reference mismatch")
        parts = manifest.get("parts", [])
        if not isinstance(parts, list) or len(parts) != self.config.part_count:
            raise ValidationError("manifest part count mismatch")
        for part in parts:
            self._validate_part(part)

    def _read_committed_manifest(self) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        commit_bytes = self._get_bytes(self.config.commit_key)
        commit = json.loads(commit_bytes)
        if commit.get("contractVersion") != COMMIT_CONTRACT_VERSION:
            raise ValidationError("commit contract version mismatch")
        manifest_key = str(commit.get("manifestKey", ""))
        manifest_bytes = self._get_bytes(manifest_key)
        manifest_sha = sha256_bytes(manifest_bytes)
        if manifest_sha != commit.get("manifestSha256"):
            raise ValidationError("committed manifest SHA-256 mismatch")
        manifest = json.loads(manifest_bytes)
        if manifest.get("archiveId") != commit.get("archiveId"):
            raise ValidationError("commit archive ID mismatch")
        self._validate_manifest_objects(manifest)
        return commit, manifest, manifest_sha

    def _drop_partition(self) -> None:
        self.clickhouse.execute(
            f"ALTER TABLE loopad.events DROP PARTITION {sql_string(self.config.partition)}"
        )
        if self._source_count() != 0:
            raise ValidationError("source partition still contains rows after DROP")

    def _post_drop_equivalence(self, manifest: Mapping[str, Any]) -> Dict[str, Any]:
        reference = generator_select_sql(self.config.generator, include_event_date=True)
        return self._equivalence(
            reference,
            manifest["parts"],
            stage="post-DROP",
            deterministic_reference=True,
        )

    def run(self) -> Dict[str, Any]:
        started_at = iso_now()
        started = time.monotonic()
        source_count = self._source_count()
        state = recovery_state(self._head_exists(self.config.commit_key), source_count > 0)
        result: Dict[str, Any] = {
            "schemaVersion": "1.0",
            "runId": self.config.run_id,
            "partition": self.config.partition,
            "recoveryState": state.value,
            "startedAt": started_at,
            "awsCalls": 0 if self.config.s3_endpoint_url else "not-counted-runtime",
        }
        if state is RecoveryState.CRITICAL:
            raise CriticalRecoveryError("COMMITTED and source partition are both absent")

        if state is RecoveryState.NEW_ATTEMPT:
            if source_count != self.config.expected_rows:
                raise ValidationError("source row count does not match expected rows")
            fingerprints = self._stable_fingerprints()
            archive_id = str(uuid.uuid4())
            export_started_at = iso_now()
            export_started = time.monotonic()
            parts = self._export_parts(archive_id)
            export_seconds = time.monotonic() - export_started
            manifest = self._build_manifest(
                archive_id,
                fingerprints,
                parts,
                export_started_at,
                export_seconds,
            )
            manifest_key = self._manifest_key(archive_id)
            created, manifest_sha = self._conditional_put_json(manifest_key, manifest)
            if not created:
                raise ValidationError("unique attempt manifest already exists")
            self._validate_manifest_objects(manifest)
            pre = self._equivalence(
                self._source_relation(),
                parts,
                stage="pre-DROP",
                deterministic_reference=False,
            )
            commit_document = {
                "contractVersion": COMMIT_CONTRACT_VERSION,
                "runId": self.config.run_id,
                "partition": self.config.partition,
                "archiveId": archive_id,
                "manifestKey": manifest_key,
                "manifestSha256": manifest_sha,
                "createdAt": iso_now(),
            }
            commit_created, _ = self._conditional_put_json(self.config.commit_key, commit_document)
            commit, committed_manifest, _ = self._read_committed_manifest()
            committed_pre = self._equivalence(
                self._source_relation(),
                committed_manifest["parts"],
                stage="committed-pre-DROP",
                deterministic_reference=False,
            )
            authorize_drop(
                manifest_valid=True,
                pre_equivalent=pre["passed"] and committed_pre["passed"],
                commit_revalidated=True,
            )
            result.update(
                {
                    "archiveId": commit["archiveId"],
                    "manifestKey": commit["manifestKey"],
                    "manifestSha256": commit["manifestSha256"],
                    "commitCreated": commit_created,
                    "parts": committed_manifest["parts"],
                    "preDrop": pre,
                    "committedPreDrop": committed_pre,
                    "sourceFingerprints": fingerprints,
                }
            )
            if self.config.retain_source_after_commit:
                result.update({
                    "diagnosticSourceRetention": True,
                    "dropExecuted": False,
                    "postDrop": None,
                })
            else:
                self._drop_partition()
                result["postDrop"] = self._post_drop_equivalence(committed_manifest)
        elif state is RecoveryState.REVALIDATE_AND_DROP:
            commit, manifest, manifest_sha = self._read_committed_manifest()
            pre = self._equivalence(
                self._source_relation(),
                manifest["parts"],
                stage="recovery-pre-DROP",
                deterministic_reference=False,
            )
            authorize_drop(
                manifest_valid=True,
                pre_equivalent=pre["passed"],
                commit_revalidated=True,
            )
            result.update(
                {
                    "archiveId": commit["archiveId"],
                    "manifestKey": commit["manifestKey"],
                    "manifestSha256": manifest_sha,
                    "preDrop": pre,
                    "parts": manifest["parts"],
                }
            )
            if self.config.retain_source_after_commit:
                result.update({
                    "diagnosticSourceRetention": True,
                    "dropExecuted": False,
                    "postDrop": None,
                })
            else:
                self._drop_partition()
                result["postDrop"] = self._post_drop_equivalence(manifest)
        else:
            if self.config.retain_source_after_commit:
                raise ValidationError(
                    "diagnostic source-retention mode requires the source partition to exist"
                )
            commit, manifest, manifest_sha = self._read_committed_manifest()
            post = self._post_drop_equivalence(manifest)
            result.update(
                {
                    "archiveId": commit["archiveId"],
                    "manifestKey": commit["manifestKey"],
                    "manifestSha256": manifest_sha,
                    "postDrop": post,
                    "parts": manifest["parts"],
                }
            )

        result.update(
            {
                "status": "passed",
                "sourceRowsAfter": self._source_count(),
                "finishedAt": iso_now(),
                "durationSeconds": round(time.monotonic() - started, 6),
            }
        )
        return result


def load_config(path: Path, test_fault: Optional[str]) -> ArchiveConfig:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not document.get("partition"):
        today = date.fromisoformat(document.get("today", date.today().isoformat()))
        document["partition"] = utc_source_partition(today).isoformat()
    if not document.get("today"):
        document["today"] = date.today().isoformat()
    if test_fault:
        document["test_fault"] = test_fault
    return ArchiveConfig(**document)


def write_result(path: Optional[Path], document: Mapping[str, Any]) -> None:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--test-fault",
        choices=["missing-part", "checksum-mismatch", "kill-after-first-part"],
    )
    args = parser.parse_args()
    started_at = iso_now()
    try:
        config = load_config(args.config, args.test_fault)
        clickhouse = ClickHouseHttp(
            config.clickhouse_url,
            user_file=config.clickhouse_user_file,
            password_file=config.clickhouse_password_file,
        )
        result = ArchiveWorker(config, clickhouse, build_s3_client(config)).run()
        write_result(args.output, result)
        return 0
    except BaseException as error:
        failure = {
            "schemaVersion": "1.0",
            "status": "failed",
            "startedAt": started_at,
            "finishedAt": iso_now(),
            "errorType": type(error).__name__,
            "error": str(error),
        }
        write_result(args.output, failure)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
