# AWS Performance Test Result Recording Process

이 문서는 AWS 성능 테스트 실행 결과를 레포에 남기는 절차다.

## Before A Run

1. Create a new run folder.

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_<short_name>/
```

2. Write `run.json`.

Required fields:

```json
{
  "run_id": "run_YYYYMMDD_HHMMSS_name",
  "phase": "phase1",
  "target_rps": 10000,
  "event_per_request": 1,
  "event_payload_bytes": 1024,
  "duration_seconds": 300,
  "collector_tasks": 4,
  "kafka_partitions": 32,
  "load_generator_tasks": 4,
  "started_at": "YYYY-MM-DDTHH:mm:ssZ",
  "operator": "manual",
  "expected_cost_usd": null
}
```

3. Write `infra.md`.

Record:

- deployed stacks
- ECS service/task counts
- MSK or Kafka config
- ALB listener/target setup
- security groups changed
- any scale-out/scale-in commands

4. Write `commands.md`.

Record every command used to deploy, scale, run, collect metrics, and destroy.

## During A Run

Record or link:

- load generator output
- CloudWatch metric export
- Kafka offset snapshots
- pprof output if captured
- screenshots if used for evidence

If a command fails, keep the failed command and output summary in `commands.md`.

## After A Run

1. Write `metrics-summary.json`.

Include:

- actual rps
- accepted rps
- failed request rate
- p50/p95/p99 latency
- collector CPU/memory
- Kafka messages in/sec
- Kafka bytes in/sec
- Kafka produce/throttle metrics

2. Write `artifacts.md`.

Include S3 links and local file names. Do not paste large raw logs into markdown.

3. Write `report.md`.

Use `docs/template_aws_perf_test_run_report.md`.

4. Commit the run folder and any infra/docs changes.

## Do Not Delete

Do not delete:

- run folders
- report files
- S3 artifact links
- failed run records
- aborted run records

If a file is wrong, add a correction section to `report.md` instead of deleting evidence.
