from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PHASE7 = Path(__file__).resolve().parents[1]
AWS_DIR = PHASE7 / "aws"
sys.path.insert(0, str(AWS_DIR))

import runtime_stages  # noqa: E402


RUN_ID = "run_20260718_010203_phase7_integration"
PARTITION = "2026-07-10"


def load_entrypoint():
    path = PHASE7 / "archive" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("phase7_archive_entrypoint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load Phase 7 archive entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def equivalence(stage: str, *, left: int = 0, right: int = 0) -> dict[str, object]:
    return {
        "stage": stage,
        "passed": left == 0 and right == 0,
        "twoWayDifferences": [
            {"part": index, "leftMinusRight": left if index == 0 else 0,
             "rightMinusLeft": right if index == 0 else 0}
            for index in range(3)
        ],
    }


def archive_documents() -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
    archive_id = "00000000-0000-4000-8000-000000000001"
    parts = [
        {
            "index": index,
            "key": f"attempts/v1/table=events/event_date={PARTITION}/archive_id={archive_id}/part-{index:05d}.parquet",
            "rows": 5_000_000,
            "bytes": 100 + index,
            "sha256": str(index + 1) * 64,
        }
        for index in range(3)
    ]
    manifest = {
        "runId": RUN_ID,
        "archiveId": archive_id,
        "partition": PARTITION,
        "parts": parts,
        "archive": {"rows": 15_000_000},
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha = __import__("hashlib").sha256(manifest_bytes).hexdigest()
    commit = {
        "archiveId": archive_id,
        "manifestKey": f"attempts/v1/table=events/event_date={PARTITION}/archive_id={archive_id}/manifest.json",
        "manifestSha256": manifest_sha,
    }
    worker = {
        "status": "passed",
        "runId": RUN_ID,
        "partition": PARTITION,
        "archiveId": archive_id,
        "manifestKey": commit["manifestKey"],
        "manifestSha256": manifest_sha,
        "parts": parts,
        "preDrop": equivalence("pre-DROP"),
        "committedPreDrop": equivalence("committed-pre-DROP"),
        "postDrop": equivalence("post-DROP"),
    }
    return manifest_bytes.decode(), commit, manifest, worker


class Body:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self) -> bytes:
        return self.value


class FakeS3:
    def __init__(self, *, post_left: int = 0) -> None:
        manifest_text, self.commit, self.manifest, self.worker = archive_documents()
        if post_left:
            self.worker["postDrop"] = equivalence("post-DROP", left=post_left)
            # A malicious/inconsistent producer could claim passed despite a
            # non-zero exact difference. The consumer must still fail closed.
            self.worker["postDrop"]["passed"] = True
        self.manifest_bytes = manifest_text.encode()
        self.commit_key = f"commits/v1/table=events/event_date={PARTITION}/COMMITTED"
        self.result_key = (
            f"attempts/v1/table=events/event_date={PARTITION}/"
            f"phase7-result-{RUN_ID}.json"
        )

    def list_objects_v2(self, **_kwargs):
        return {"Contents": [{"Key": self.commit_key}]}

    def get_object(self, *, Key: str, **_kwargs):
        if Key == self.commit_key:
            value = json.dumps(self.commit).encode()
        elif Key == self.commit["manifestKey"]:
            value = self.manifest_bytes
        elif Key == self.result_key:
            value = json.dumps(self.worker).encode()
        else:
            raise AssertionError(f"unexpected S3 key: {Key}")
        return {"Body": Body(value)}

    def head_object(self, *, Key: str, **_kwargs):
        part = next(item for item in self.manifest["parts"] if item["key"] == Key)
        return {"ContentLength": part["bytes"], "Metadata": {"sha256": part["sha256"]}}


class FakeRuntime:
    def __init__(self, s3: FakeS3) -> None:
        self.bundle = runtime_stages.Bundle(
            Path("/tmp/phase7-test"), RUN_ID, "phase7-integration-20260718T010203Z",
            {"ArchiveBucketName": "run-owned-archive"},
        )
        self.s3 = s3

    def client(self, service: str):
        if service != "s3":
            raise AssertionError(service)
        return self.s3

    def clickhouse(self, _query: str, *, timeout: int = 600):
        self.clickhouse_timeout = timeout
        return [{"rows": 0}]


class Phase7ArchiveRuntimeTest(unittest.TestCase):
    def test_archive_evidence_uses_worker_exact_differences_and_part_heads(self) -> None:
        result = runtime_stages.archive_evidence(FakeRuntime(FakeS3()))
        self.assertEqual(0, result["postDropReferenceMinusArchive"])
        self.assertEqual(3, len(result["objectHeads"]))
        self.assertEqual(64, len(result["workerResultSha256"]))

    def test_source_drop_zero_cannot_synthesize_nonzero_worker_difference_to_zero(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exact differences are nonzero"):
            runtime_stages.archive_evidence(FakeRuntime(FakeS3(post_left=1)))

    def test_seed_and_archive_share_the_main_run_id_generator_contract(self) -> None:
        class SeedRuntime:
            bundle = runtime_stages.Bundle(Path("/tmp/run"), RUN_ID, "session", {})

            def assert_identity(self):
                return {}

            def clickhouse(self, _query, *, select=True, timeout=600):
                if not select:
                    return []
                return [{"rows": 15_000_000, "uniqueEvents": 15_000_000, "checksum": "1"}]

        captured = []
        with mock.patch.object(runtime_stages, "seed_insert_sql", side_effect=lambda contract: captured.append(contract) or "INSERT"), \
             mock.patch.object(runtime_stages.time, "sleep"):
            result = runtime_stages.seed_partition(SeedRuntime())
        self.assertEqual(RUN_ID, captured[0].run_id)
        self.assertEqual(RUN_ID, result["generatorContract"]["runId"])

    def test_archive_entrypoint_conditionally_publishes_the_exact_worker_result(self) -> None:
        entrypoint = load_entrypoint()
        result = {"status": "passed", "runId": RUN_ID, "partition": PARTITION}
        client = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AWS_REGION": "ap-northeast-2"}), \
                 mock.patch.object(entrypoint.boto3, "client", return_value=client):
                key = entrypoint.publish_result(
                    path, bucket="run-owned-archive", run_id=RUN_ID, partition=PARTITION
                )
        self.assertEqual(
            f"attempts/v1/table=events/event_date={PARTITION}/phase7-result-{RUN_ID}.json",
            key,
        )
        request = client.put_object.call_args.kwargs
        self.assertEqual("*", request["IfNoneMatch"])
        self.assertEqual(json.dumps(result).encode(), request["Body"])

    def test_archive_credentials_use_inherited_anonymous_memory_not_plaintext_files(self) -> None:
        entrypoint = load_entrypoint()
        with mock.patch.object(entrypoint.os, "memfd_create", return_value=41, create=True) as create, \
             mock.patch.object(entrypoint.os, "write") as write, \
             mock.patch.object(entrypoint.os, "lseek") as seek, \
             mock.patch.object(entrypoint.os, "set_inheritable") as inheritable:
            fd = entrypoint.secret_memfd("phase7-test", "not-a-real-secret")
        self.assertEqual(41, fd)
        create.assert_called_once_with("phase7-test", flags=0)
        write.assert_called_once_with(41, b"not-a-real-secret\n")
        seek.assert_called_once_with(41, 0, os.SEEK_SET)
        inheritable.assert_called_once_with(41, True)
        runtime_env = {
            "ARCHIVE_EXPECTED_ROWS": "15000000",
            "ARCHIVE_ROWS_PER_PART": "5000000",
            "ARCHIVE_PART_COUNT": "3",
            "CLICKHOUSE_HTTP_URL": "http://clickhouse:8123",
            "ARCHIVE_BUCKET": "bucket",
            "RUN_ID": RUN_ID,
            "ARCHIVE_PARTITION": PARTITION,
            "ARCHIVE_TODAY": "2026-07-18",
            "AWS_REGION": "ap-northeast-2",
            "AWS_ACCOUNT_ID": "742711170910",
            "CLICKHOUSE_IMAGE": "image@sha256:" + "1" * 64,
            "ARCHIVE_IMAGE_DIGEST": "sha256:" + "2" * 64,
        }
        with mock.patch.dict(os.environ, runtime_env):
            config = entrypoint.build_config("/proc/self/fd/41", "/proc/self/fd/42")
        self.assertEqual("/proc/self/fd/41", config["clickhouse_user_file"])
        self.assertEqual("/proc/self/fd/42", config["clickhouse_password_file"])
        self.assertGreaterEqual(config["clickhouse_memory_bytes"], 6 * 1024**3)
        self.assertLess(config["clickhouse_memory_bytes"], 7 * 1024**3)
        self.assertFalse(config["retain_source_after_commit"])
        self.assertNotIn("not-a-real-secret", json.dumps(config))

        with mock.patch.dict(os.environ, {
            **runtime_env,
            "ARCHIVE_RETAIN_SOURCE_AFTER_COMMIT": "true",
        }):
            diagnostic = entrypoint.build_config("/proc/self/fd/41", "/proc/self/fd/42")
        self.assertTrue(diagnostic["retain_source_after_commit"])

        with mock.patch.dict(os.environ, {
            **runtime_env,
            "ARCHIVE_RETAIN_SOURCE_AFTER_COMMIT": "1",
        }):
            with self.assertRaisesRegex(ValueError, "exactly true or false"):
                entrypoint.build_config("/proc/self/fd/41", "/proc/self/fd/42")

    def test_runtime_and_cdk_use_the_same_checked_topology_contract(self) -> None:
        topology = json.loads((PHASE7 / "topology-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(topology["haproxyHosts"], runtime_stages.EXPECTED_COUNTS["Haproxy"])
        self.assertEqual(2, runtime_stages.EXPECTED_COUNTS["Haproxy"])

    def test_drain_requires_two_fresh_samples_and_real_progress_or_low_state(self) -> None:
        self.assertFalse(runtime_stages.iterator_drain_complete([
            {"epoch": 1, "maximumMs": 0},
        ]))
        self.assertFalse(runtime_stages.iterator_drain_complete([
            {"epoch": 1, "maximumMs": 5_000},
            {"epoch": 2, "maximumMs": 2_000},
        ]))
        self.assertTrue(runtime_stages.iterator_drain_complete([
            {"epoch": 1, "maximumMs": 5_000},
            {"epoch": 2, "maximumMs": 500},
        ]))
        self.assertTrue(runtime_stages.iterator_drain_complete([
            {"epoch": 1, "maximumMs": 0},
            {"epoch": 2, "maximumMs": 0},
        ]))
        with self.assertRaisesRegex(RuntimeError, "10 consecutive minutes"):
            runtime_stages.iterator_drain_complete([
                {"epoch": 1, "maximumMs": 5_000},
                {"epoch": 301, "maximumMs": 5_100},
                {"epoch": 601, "maximumMs": 5_100},
            ])
        self.assertFalse(runtime_stages.iterator_drain_complete([
            {"epoch": 1, "maximumMs": 5_000},
            {"epoch": 301, "maximumMs": 4_000},
            {"epoch": 601, "maximumMs": 3_000},
        ]))

    def test_iterator_no_progress_uses_wall_clock_for_zero_one_and_stale_samples(self) -> None:
        for samples in (
            [],
            [{"epoch": 1_000, "maximumMs": 5_000}],
            [
                {"epoch": 1_000, "maximumMs": 5_000},
                {"epoch": 1_100, "maximumMs": 5_000},
            ],
            [
                {"epoch": 900, "maximumMs": 5_000},
                {"epoch": 950, "maximumMs": 500},
            ],
        ):
            with self.subTest(samples=samples), self.assertRaisesRegex(
                RuntimeError, "10 consecutive minutes"
            ):
                runtime_stages.iterator_drain_complete(
                    samples,
                    observation_started_epoch=1_000,
                    now_epoch=1_600,
                )
        self.assertFalse(runtime_stages.iterator_drain_complete(
            [{"epoch": 1_000, "maximumMs": 5_000}],
            observation_started_epoch=1_000,
            now_epoch=1_599,
        ))

    def test_drain_timeout_is_bound_to_the_actual_score_end(self) -> None:
        self.assertEqual(
            3_700,
            runtime_stages.effective_drain_deadline_epoch(1_000, 4_000),
        )
        self.assertEqual(
            3_500,
            runtime_stages.effective_drain_deadline_epoch(1_000, 3_500),
        )
        self.assertEqual(2_600, runtime_stages.remaining_drain_seconds(3_700, 1_100))
        with self.assertRaisesRegex(RuntimeError, "absolute 45-minute"):
            runtime_stages.remaining_drain_seconds(3_700, 3_700)

    def test_visibility_percentiles_are_derived_from_exact_disjoint_histogram(self) -> None:
        histogram = {"observedRecords": 100, "latencyGt60000Ms": 1}
        histogram.update({f"latencyLe{bound}Ms": 0 for bound in runtime_stages.VISIBILITY_BUCKETS_MS})
        histogram["latencyLe100Ms"] = 50
        histogram["latencyLe250Ms"] = 45
        histogram["latencyLe500Ms"] = 3
        histogram["latencyLe1000Ms"] = 1
        self.assertEqual(
            {"p50Ms": 100, "p95Ms": 250, "p99Ms": 1_000},
            runtime_stages.histogram_percentiles(histogram),
        )
        histogram["observedRecords"] = 101
        with self.assertRaisesRegex(RuntimeError, "buckets"):
            runtime_stages.histogram_percentiles(histogram)

    def test_exact_accounting_rejects_overshoot_instead_of_accepting_greater_than(self) -> None:
        self.assertTrue(runtime_stages.exact_or_raise(15_000_000, 15_000_000, "score"))
        self.assertFalse(runtime_stages.exact_or_raise(14_999_999, 15_000_000, "score"))
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            runtime_stages.exact_or_raise(15_000_001, 15_000_000, "score")


if __name__ == "__main__":
    unittest.main()
