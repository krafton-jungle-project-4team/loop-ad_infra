from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "local_runner.py"
SPEC = importlib.util.spec_from_file_location("phase7_local_runner", MODULE_PATH)
assert SPEC and SPEC.loader
local_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_runner)

CLEANUP_PATH = Path(__file__).resolve().parents[1] / "cleanup_inventory.py"
CLEANUP_SPEC = importlib.util.spec_from_file_location("phase7_cleanup_inventory", CLEANUP_PATH)
assert CLEANUP_SPEC and CLEANUP_SPEC.loader
cleanup_inventory = importlib.util.module_from_spec(CLEANUP_SPEC)
CLEANUP_SPEC.loader.exec_module(cleanup_inventory)

FINALIZER_PATH = Path(__file__).resolve().parents[1] / "finalize_evidence.py"
FINALIZER_SPEC = importlib.util.spec_from_file_location("phase7_finalize_evidence", FINALIZER_PATH)
assert FINALIZER_SPEC and FINALIZER_SPEC.loader
finalize_evidence = importlib.util.module_from_spec(FINALIZER_SPEC)
FINALIZER_SPEC.loader.exec_module(finalize_evidence)

RUN_LOCAL_PATH = Path(__file__).resolve().parents[1] / "run-local.sh"
LOCALSTACK_INIT_PATH = Path(__file__).resolve().parents[1] / "localstack-init.sh"
HAPROXY_PATH = Path(__file__).resolve().parents[1] / "haproxy-local.cfg"


class LocalRunnerTest(unittest.TestCase):
    def test_haproxy_uses_leastconn_sampled_success_logs_and_full_error_logs(self) -> None:
        config = HAPROXY_PATH.read_text(encoding="utf-8")
        self.assertIn("balance leastconn", config)
        self.assertIn("proto h2", config)
        self.assertIn("http-reuse always", config)
        self.assertIn("log stdout format raw sample 1:1000 local0", config)
        self.assertIn("log stdout format raw local1 err", config)
        self.assertIn("http-after-response set-log-level err if error_status", config)
        self.assertIn("use-service prometheus-exporter", config)
        self.assertIn("bind :8404", config)

    def test_haproxy_evidence_requires_loopback_stats_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            local_runner.collect_haproxy_evidence(
                "http://example.com:8404",
                Path("/tmp/unused"),
                Path("/tmp/unused-compose.yml"),
            )

    def test_runner_removes_exact_runtime_secret_files_before_handoff(self) -> None:
        runner = RUN_LOCAL_PATH.read_text(encoding="utf-8")
        self.assertIn("remove_runtime_secrets", runner)
        self.assertIn('"$LOCAL_RUN_DIR/clickhouse-user"', runner)
        self.assertIn('"$LOCAL_RUN_DIR/clickhouse-password"', runner)
        self.assertIn('"$LOCAL_RUN_DIR/archive-config.json"', runner)
        self.assertLess(runner.rindex("remove_runtime_secrets"), runner.rindex("finalize_evidence"))

    def test_localstack_bootstrap_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            state_dir = root / "state"
            bin_dir.mkdir()
            state_dir.mkdir()
            fake_awslocal = bin_dir / "awslocal"
            fake_awslocal.write_text(
                """#!/bin/sh
set -eu
printf '%s\\n' "$*" >>"$FAKE_AWSLOCAL_LOG"
service=$1
operation=$2
case "$service:$operation" in
  kinesis:describe-stream-summary) test -f "$FAKE_AWSLOCAL_STATE/stream" ;;
  kinesis:create-stream) test ! -f "$FAKE_AWSLOCAL_STATE/stream"; touch "$FAKE_AWSLOCAL_STATE/stream" ;;
  kinesis:wait) test -f "$FAKE_AWSLOCAL_STATE/stream" ;;
  secretsmanager:describe-secret) test -f "$FAKE_AWSLOCAL_STATE/secret" ;;
  secretsmanager:create-secret) test ! -f "$FAKE_AWSLOCAL_STATE/secret"; touch "$FAKE_AWSLOCAL_STATE/secret" ;;
  s3api:head-bucket)
    bucket=${4}
    test -f "$FAKE_AWSLOCAL_STATE/$bucket"
    ;;
  s3api:create-bucket)
    bucket=${4}
    test ! -f "$FAKE_AWSLOCAL_STATE/$bucket"
    touch "$FAKE_AWSLOCAL_STATE/$bucket"
    ;;
  *) exit 64 ;;
esac
""",
                encoding="utf-8",
            )
            fake_awslocal.chmod(0o700)
            log_path = root / "awslocal.log"
            marker_path = root / "phase7-init-complete"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_AWSLOCAL_LOG": str(log_path),
                "FAKE_AWSLOCAL_STATE": str(state_dir),
                "PHASE7_INIT_COMPLETE_PATH": str(marker_path),
            }

            for _ in range(2):
                subprocess.run(
                    ["/bin/sh", str(LOCALSTACK_INIT_PATH)],
                    check=True,
                    env=environment,
                    capture_output=True,
                    text=True,
                )

            commands = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, sum("kinesis create-stream" in line for line in commands))
            self.assertEqual(1, sum("secretsmanager create-secret" in line for line in commands))
            self.assertEqual(2, sum("s3api create-bucket" in line for line in commands))
            self.assertTrue(marker_path.is_file())

    def test_evidence_verdict_requires_result_network_audit_and_cleanup(self) -> None:
        local = {"status": "passed", "awsNetworkAudit": {"realAwsRequests": 0}}
        cleanup = {"status": "passed", "containers": [], "volumes": [], "networks": []}
        self.assertEqual(("passed", []), finalize_evidence.verdict(local, cleanup))
        cleanup["networks"] = ["leaked"]
        status, failures = finalize_evidence.verdict(local, cleanup)
        self.assertEqual("failed", status)
        self.assertIn("owned Docker inventory is not empty", failures)

    def test_implementation_digest_is_path_and_content_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b").mkdir()
            (root / "b" / "two.txt").write_text("two", encoding="utf-8")
            (root / "one.txt").write_text("one", encoding="utf-8")
            first, manifest = finalize_evidence.implementation_digest(root, ("b", "one.txt"))
            second, _ = finalize_evidence.implementation_digest(root, ("one.txt", "b"))
            self.assertEqual(first, second)
            self.assertEqual(["b/two.txt", "one.txt"], [entry["path"] for entry in manifest])

    def test_requires_explicit_loopback_endpoint(self) -> None:
        self.assertEqual(
            "http://127.0.0.1:4566",
            local_runner.require_loopback_http("http://127.0.0.1:4566", "test"),
        )
        for endpoint in (
            "https://127.0.0.1:4566",
            "http://localstack:4566",
            "http://example.com:4566",
            "http://user@127.0.0.1:4566",
            "http://127.0.0.1:4566?x=1",
        ):
            with self.assertRaises(ValueError):
                local_runner.require_loopback_http(endpoint, "test")

    def test_event_document_is_collector_compatible(self) -> None:
        document = local_runner.event_document("run_test", 7)
        self.assertEqual("hotel_rec_promo.v1", document["schema_version"])
        self.assertEqual("browser_sdk", document["source"])
        self.assertEqual("phase7-run_test-000000007", document["event_id"])
        self.assertNotIn("run_id", document)
        self.assertNotIn("producer_sent_at", document)
        self.assertIn('"sequence":7', document["properties_json"])

    def test_late_fixture_is_older_than_seven_days(self) -> None:
        current = local_runner.event_document("run_test", 1)
        late = local_runner.event_document("run_test", 2, late=True)
        self.assertLess(late["event_time"], current["event_time"])

    def test_count_uses_final_only_for_replacing_merge_tree(self) -> None:
        client = object.__new__(local_runner.ClickHouseHttp)
        queries: list[str] = []
        client.execute = lambda query: queries.append(query) or "1"

        self.assertEqual(1, client.count("events"))
        self.assertEqual(1, client.count("raw_events", "error_code = 'invalid_json'"))
        self.assertIn("loopad.events FINAL", queries[0])
        self.assertNotIn("FINAL", queries[1])

    def test_cleanup_inventory_passes_only_when_all_classes_are_empty(self) -> None:
        original = cleanup_inventory.docker_ids
        try:
            cleanup_inventory.docker_ids = lambda _resource, _session: []
            self.assertEqual("passed", cleanup_inventory.inventory("session")["status"])
            cleanup_inventory.docker_ids = lambda resource, _session: ["leftover"] if resource == "volume" else []
            self.assertEqual("failed", cleanup_inventory.inventory("session")["status"])
        finally:
            cleanup_inventory.docker_ids = original

    def test_cleanup_inventory_places_container_all_after_ls(self) -> None:
        command = cleanup_inventory.docker_command("container", "phase7-test")
        self.assertEqual(["docker", "container", "ls", "--all"], command[:4])
        self.assertEqual(
            ["docker", "volume", "ls", "-q"],
            cleanup_inventory.docker_command("volume", "phase7-test")[:4],
        )


if __name__ == "__main__":
    unittest.main()
