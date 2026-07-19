from __future__ import annotations

import json
import unittest
from datetime import date

from archive import (
    ArchiveConfig,
    ArchiveWorker,
    RecoveryState,
    ValidationError,
    authorize_drop,
    canonical_json_bytes,
    is_precondition_failure,
    recovery_state,
)


class FakePreconditionError(Exception):
    def __init__(self) -> None:
        self.response = {
            "ResponseMetadata": {"HTTPStatusCode": 412},
            "Error": {"Code": "PreconditionFailed"},
        }


class ConditionalS3:
    def put_object(self, **kwargs):
        raise FakePreconditionError()


class MetricsClickHouse:
    def __init__(self) -> None:
        self.queries = []
        self.commands = []

    def execute(self, query):
        self.commands.append(query)
        return ""

    def one(self, query):
        self.queries.append(query)
        return {
            "rows": 1,
            "unique_events": 1,
            "min_event_time_ms": 1,
            "max_event_time_ms": 1,
            "logical_checksum": "1",
        }


class ArchivePathWorker(ArchiveWorker):
    def __init__(self, config, *, commit_exists=False, source_exists=True):
        super().__init__(config, clickhouse=object(), s3=object())
        self.commit_exists = commit_exists
        self.source_exists = source_exists
        self.drop_calls = 0
        self.post_drop_calls = 0
        self.commit_reads = 0

    def _source_count(self):
        return self.config.expected_rows if self.source_exists else 0

    def _head_exists(self, _key):
        return self.commit_exists

    def _stable_fingerprints(self):
        return [{"rows": self.config.expected_rows}]

    def _export_parts(self, _archive_id):
        return [{"key": f"part-{index}"} for index in range(3)]

    def _build_manifest(self, archive_id, fingerprints, parts, *_args):
        return {
            "archiveId": archive_id,
            "parts": parts,
            "sourceFingerprints": fingerprints,
        }

    def _manifest_key(self, _archive_id):
        return "manifest.json"

    def _conditional_put_json(self, key, _document):
        return True, "a" * 64 if key == "manifest.json" else "b" * 64

    def _validate_manifest_objects(self, _manifest):
        return None

    def _source_relation(self):
        return "source"

    def _equivalence(self, _expected, _parts, *, stage, deterministic_reference):
        if deterministic_reference:
            raise AssertionError("pre-DROP paths must use the retained source relation")
        return {"stage": stage, "passed": True}

    def _read_committed_manifest(self):
        self.commit_reads += 1
        return (
            {
                "archiveId": "archive",
                "manifestKey": "manifest.json",
                "manifestSha256": "a" * 64,
            },
            {"parts": [{"key": f"part-{index}"} for index in range(3)]},
            "a" * 64,
        )

    def _drop_partition(self):
        self.drop_calls += 1
        self.source_exists = False

    def _post_drop_equivalence(self, _manifest):
        self.post_drop_calls += 1
        return {"stage": "post-DROP", "passed": True}


class ArchiveContractTest(unittest.TestCase):
    def config(self, **overrides) -> ArchiveConfig:
        values = {
            "clickhouse_url": "http://clickhouse:8123",
            "bucket": "phase6-unit",
            "run_id": "run_unit",
            "partition": "2026-07-09",
            "today": "2026-07-17",
            "expected_rows": 30,
            "rows_per_part": 10,
            "part_count": 3,
            "fingerprint_interval_seconds": 0,
            "test_mode": True,
        }
        values.update(overrides)
        return ArchiveConfig(**values)

    def test_canonical_manifest_is_stable_and_newline_terminated(self):
        first = canonical_json_bytes({"z": 1, "a": [2, 3]})
        second = canonical_json_bytes(json.loads(first))
        self.assertEqual(first, b'{"a":[2,3],"z":1}\n')
        self.assertEqual(first, second)

    def test_query_memory_accepts_phase7_operational_headroom(self):
        self.config(clickhouse_memory_bytes=6 * 1024**3).validate()

    def test_query_memory_preserves_server_safety_reserve(self):
        with self.assertRaisesRegex(ValueError, "512 MiB"):
            self.config(clickhouse_memory_bytes=7 * 1024**3).validate()

    def test_recovery_table_has_exactly_four_states(self):
        self.assertEqual(recovery_state(False, True), RecoveryState.NEW_ATTEMPT)
        self.assertEqual(recovery_state(True, True), RecoveryState.REVALIDATE_AND_DROP)
        self.assertEqual(recovery_state(True, False), RecoveryState.POST_DROP_VALIDATE)
        self.assertEqual(recovery_state(False, False), RecoveryState.CRITICAL)

    def test_deletion_is_blocked_until_all_preconditions_hold(self):
        for values in [(False, True, True), (True, False, True), (True, True, False)]:
            with self.assertRaises(ValidationError):
                authorize_drop(
                    manifest_valid=values[0],
                    pre_equivalent=values[1],
                    commit_revalidated=values[2],
                )
        authorize_drop(manifest_valid=True, pre_equivalent=True, commit_revalidated=True)

    def test_diagnostic_source_retention_re_reads_commit_without_drop(self):
        worker = ArchivePathWorker(self.config(retain_source_after_commit=True))
        result = worker.run()
        self.assertEqual(0, worker.drop_calls)
        self.assertEqual(0, worker.post_drop_calls)
        self.assertEqual(1, worker.commit_reads)
        self.assertTrue(result["diagnosticSourceRetention"])
        self.assertFalse(result["dropExecuted"])
        self.assertIsNone(result["postDrop"])
        self.assertEqual(30, result["sourceRowsAfter"])

    def test_default_production_path_still_drops_once_and_validates_post_drop(self):
        worker = ArchivePathWorker(self.config())
        result = worker.run()
        self.assertEqual(1, worker.drop_calls)
        self.assertEqual(1, worker.post_drop_calls)
        self.assertEqual(1, worker.commit_reads)
        self.assertEqual(0, result["sourceRowsAfter"])
        self.assertTrue(result["postDrop"]["passed"])
        self.assertNotIn("diagnosticSourceRetention", result)

    def test_diagnostic_recovery_revalidates_commit_and_retains_source(self):
        worker = ArchivePathWorker(
            self.config(retain_source_after_commit=True),
            commit_exists=True,
        )
        result = worker.run()
        self.assertEqual(1, worker.commit_reads)
        self.assertEqual(0, worker.drop_calls)
        self.assertTrue(result["diagnosticSourceRetention"])
        self.assertEqual(30, result["sourceRowsAfter"])

    def test_diagnostic_retention_rejects_already_missing_source(self):
        worker = ArchivePathWorker(
            self.config(retain_source_after_commit=True),
            commit_exists=True,
            source_exists=False,
        )
        with self.assertRaisesRegex(ValidationError, "requires the source partition"):
            worker.run()

    def test_diagnostic_retention_flag_requires_a_real_boolean(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.config(retain_source_after_commit="false").validate()

    def test_frozen_full_scale_contract_rejects_smaller_production_run(self):
        config = self.config(test_mode=False, fingerprint_interval_seconds=300)
        with self.assertRaises(ValueError):
            config.validate()

    def test_eligibility_rejects_cutoff_date_itself(self):
        config = self.config(partition="2026-07-10")
        with self.assertRaises(ValueError):
            config.validate()

    def test_conditional_create_reports_duplicate_without_overwrite(self):
        worker = ArchiveWorker(self.config(), clickhouse=object(), s3=ConditionalS3())
        created, digest = worker._conditional_put_json("COMMITTED", {"value": 1})
        self.assertFalse(created)
        self.assertEqual(len(digest), 64)

    def test_precondition_detection_is_typed_by_response(self):
        self.assertTrue(is_precondition_failure(FakePreconditionError()))
        self.assertFalse(is_precondition_failure(RuntimeError("other")))

    def test_exact_metrics_and_export_bound_parallelism(self):
        clickhouse = MetricsClickHouse()
        worker = ArchiveWorker(self.config(), clickhouse=clickhouse, s3=object())
        worker._metrics("SELECT 1")
        self.assertEqual(clickhouse.commands, ["SYSTEM JEMALLOC PURGE"] * 17)
        self.assertIn("max_threads=1", clickhouse.queries[0])
        self.assertIn("max_block_size=8192", clickhouse.queries[0])
        self.assertIn("uniqExact(event_id)", clickhouse.queries[1])
        self.assertIn("cityHash64(event_id) % 8 = 0", clickhouse.queries[1])
        self.assertIn("cityHash64(event_id) % 8 = 7", clickhouse.queries[8])
        self.assertIn("max_bytes_before_external_group_by=268435456", clickhouse.queries[1])
        self.assertIn("cityHash64(event_id) % 8 = 0", clickhouse.queries[9])
        self.assertIn("cityHash64(event_id) % 8 = 7", clickhouse.queries[16])
        self.assertIn("optimize_move_to_prewhere_if_final=0", clickhouse.queries[9])
        export = worker._export_query(0, 10)
        self.assertIn("max_threads=1", export)
        self.assertIn("max_block_size=8192", export)
        self.assertIn("max_bytes_before_external_sort=134217728", export)

    def test_relation_count_does_not_run_full_metrics(self):
        clickhouse = MetricsClickHouse()
        worker = ArchiveWorker(self.config(), clickhouse=clickhouse, s3=object())
        self.assertEqual(worker._relation_count("SELECT 1"), 1)
        self.assertEqual(clickhouse.commands, ["SYSTEM JEMALLOC PURGE"])
        self.assertEqual(len(clickhouse.queries), 1)
        self.assertNotIn("uniqExact", clickhouse.queries[0])
        self.assertNotIn("cityHash64", clickhouse.queries[0])

    def test_archive_metrics_combine_parts_with_uint64_checksum_wrap(self):
        worker = ArchiveWorker(self.config(), clickhouse=object(), s3=object())
        values = iter(
            [
                {
                    "rows": 10,
                    "uniqueEvents": 10,
                    "uniqueAlgorithm": "exact",
                    "minEventTimeMs": 1,
                    "maxEventTimeMs": 10,
                    "logicalChecksum": str((1 << 64) - 1),
                    "logicalChecksumAlgorithm": "checksum",
                },
                {
                    "rows": 20,
                    "uniqueEvents": 20,
                    "uniqueAlgorithm": "exact",
                    "minEventTimeMs": 11,
                    "maxEventTimeMs": 30,
                    "logicalChecksum": "2",
                    "logicalChecksumAlgorithm": "checksum",
                },
            ]
        )
        worker._metrics = lambda _relation: next(values)
        combined = worker._archive_metrics([{"key": "part-0"}, {"key": "part-1"}])
        self.assertEqual(combined["rows"], 30)
        self.assertEqual(combined["uniqueEvents"], 30)
        self.assertEqual(combined["minEventTimeMs"], 1)
        self.assertEqual(combined["maxEventTimeMs"], 30)
        self.assertEqual(combined["logicalChecksum"], "1")


if __name__ == "__main__":
    unittest.main()
