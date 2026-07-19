# Phase 6 Lite Goal 1: 로컬 구현·검증

예상 wall-clock은 6~10시간이다. 로컬 결과가 예상과 다르면 증거와 volume cleanup을 완료하고
종료한다. 이 Goal에서 AWS API 호출, 로그인, 배포 또는 유료 resource 생성은 금지한다.

## 사용법

Codex에서 `/goal`을 시작하고 아래 블록 전체를 붙여 넣는다.

```text
Outcome

Implement Phase 6 Lite and prove it locally without making any AWS API call or mutation. Finish with
an immutable local run directory, one explicit verdict, complete local evidence, zero run-owned
Docker volumes, and a machine-readable local-handoff.json that states whether AWS execution is ready.

Expected wall-clock

- Normal: 6 to 10 hours.
- Do not start a new 15M attempt after hour 10.
- At hour 12, stop implementation work, preserve evidence, remove owned local volumes, and finish
  with passed, failed, blocked, or inconclusive.

Source of truth

1. Read AGENTS.md and preserve all existing user changes.
2. Read and follow:
   - docs/guide_phase6_clickhouse_s3_archive_lifecycle_test.md
   - performance-tests/phase6-archive/exec-plan.md
   - performance-tests/phase6-archive/README.md
   - docs/guide_aws_event_pipeline_performance_test.md
3. Use the event-pipeline-loadtest-runner skill. Use aws-cdk and aws-sdk-python-usage when their
   trigger conditions apply. Read every selected SKILL.md completely before acting.
4. Reuse the fixed Phase 4 ClickHouse image, loopad.events schema, FINAL semantics, and payload
   contract. Do not change any historical run verdict.

Hard boundary: no AWS

- Do not call AWS CLI, SDK, CDK deploy/diff, CloudFormation, STS, Price List, Cost Explorer, or any
  other AWS API. Do not run aws login.
- CDK build, unit tests, and synth are allowed only with fixed local context and no AWS lookup.
- Do not create an AWS run directory. This Goal creates only a local run directory.
- Do not claim AWS IAM, EC2, EBS, S3, systemd-on-EC2, cost, or performance success from local tests.

Frozen implementation contract

- One Python archive worker, systemd oneshot/timer, and flock.
- Source: loopad.events FINAL, UTC today - 8 days, eligibility event_date < UTC today - 7 days.
- Deterministic generator version, seed, reference hash, and exactly 15,000,000 unique rows.
- Sequential S3-compatible Parquet/ZSTD export as exactly 3 x 5,000,000-row data objects.
- Maximum configured export bandwidth 100 MiB/s.
- Canonical immutable manifest and stable partition COMMITTED with conditional-create semantics.
- Pre-DROP source/archive full equivalence and post-DROP deterministic-reference/direct-query full
  equivalence: schema, exact count, uniqExact(event_id), min/max, logical checksum, object SHA-256,
  and exact two-way difference zero.
- Four recovery states from the execution contract. Never resume a partial attempt.
- No DynamoDB lock, Step Functions, EventBridge, ClickHouse watermark, lifecycle transition,
  restore, multiworker, raw_events archive, or live-ingest overlap.

Implementation locations

- performance-tests/phase6-archive/archive.py
- performance-tests/phase6-archive/seed_partition.py
- performance-tests/phase6-archive/cost_model.py and local price fixtures only
- performance-tests/phase6-archive/preflight.py and cleanup_inventory.py with unit tests only
- performance-tests/phase6-archive/systemd/
- performance-tests/phase6-archive/tests/
- src/perf-phase6-archive-stack.ts
- test/perf-phase6-archive.test.ts

Local verification

1. Create a unique LOCAL_SESSION_ID and Compose project name/label. Create a new immutable
   performance-tests/run_<timestamp>_phase6_archive_local directory.
2. Record host/Docker CPU, memory, swap, filesystem, image digest, source hash, and starting git
   status. Require at least 30 GiB free disk.
3. Pass unit tests for eligibility, manifest canonicalization, recovery, conditional commit,
   deletion blocking, cost model, preflight, and cleanup inventory.
4. Pass a small-fixture ClickHouse/S3-compatible end-to-end test and fault tests for missing part,
   checksum mismatch, duplicate commit, process termination, and whole-partition restart.
5. Pass systemd-analyze verify, prove flock rejects overlap, and pass CDK build/test/synth without
   AWS lookup. Review IAM, public access, DeleteOnTermination, and deletion permissions statically.
6. Do not materialize 15M rows in Python, a DataFrame, or a raw payload file. Generate with
   ClickHouse numbers() or bounded streaming. Enable external sort/group-by spill. With an 8 GiB
   Docker limit, cap ClickHouse at 5 GiB. Run exports and exact-difference checks sequentially in
   chunks no larger than 5M rows.
7. Run guarded scale gates in order: 1M, then whole 15M full-scale attempts until one passes. Each
   attempt must use a new run ID, bucket/archive ID, and evidence directory; never resume a partial
   attempt or overwrite failed evidence.
8. Every 15M attempt must prove quiescence, sequential 3 x 5M export, pre-DROP equivalence,
   committed-pre-DROP revalidation, DROP, and post-DROP equivalence. Do not start another attempt
   when Docker memory peak is >= 70%, filesystem use is >= 80%, OOM/restart occurs, cleanup is not
   zero, or the hour/cost hard stop has been reached. Resolve the blocker, rerun preflight, and then
   continue with a fresh whole attempt.

Local cleanup

- Before teardown, copy manifests, hashes, query results, resource peaks, durations, and failure
  logs outside Docker volumes into the local run directory.
- On pass, failure, block, abort, timeout, or interruption, remove only the exact Compose project's
  containers and volumes with docker compose down --volumes.
- Never run docker volume prune and never delete another Phase or dev volume.
- Prove docker volume inspect/ls returns zero volumes for the LOCAL_SESSION_ID/project labels.
- A cleanup blocker prevents passed and awsReady=true.

Handoff

Write local-handoff.json with at least:

- schema version, local run ID/path, final verdict, awsReady, started/finished timestamps;
- implementation file list and SHA-256, git starting SHA/status summary;
- ClickHouse image digest, schema hash, generator version/seed/reference hash;
- small/fault/1M/15M results, row/object counts, checksums and durations;
- host/Docker resource peaks and limits;
- CDK synth artifact hash and static IAM/EBS review result;
- local Docker volume cleanup inventory and unresolved failures.

Set awsReady=true only when every required local gate passed, the 15M attempt passed, current file
hashes match the handoff, and local volume inventory is zero.

Repository boundaries

- Do not modify shared dev infrastructure.
- Never save credentials, tokens, secrets, presigned URLs, or secret values.
- Do not stash, discard, overwrite, or reformat unrelated dirty-worktree changes.
- Do not stage, commit, push, create a branch, or open a PR unless explicitly asked.

Done

- Implementation and docs match the frozen contract.
- The local run directory contains run.json, commands.md, environment.md, failures.md, report.md,
  metrics-summary.json, cleanup-verification.json, local-handoff.json, and required evidence.
- The verdict is explicit and local Docker volume inventory is zero.
- The final response gives the local run path, verdict, awsReady, gate results, resource peaks,
  volume cleanup result, files changed, tests run, and the next safe action.
- Do not start or suggest that AWS execution already happened.
```
