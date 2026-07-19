# Phase 6 Lite Goal 2: AWS 배포·검증

예상 wall-clock은 1.5~3시간이다. Goal 1의 정확한 `passed` handoff가 없으면 이 Goal을 실행하지
않는다.

## 사용 전 입력

현재 통과한 Goal 1의 exact 입력은 다음과 같다. 다른 local run으로 교체하려면 새 run도 동일한
handoff gate를 통과해야 한다.

```text
LOCAL_RUN_DIR=/Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra/performance-tests/run_20260717_100126_phase6_archive_local_retry
```

## 사용법

Codex에서 `/goal`을 시작하고 `LOCAL_RUN_DIR`과 아래 블록 전체를 붙여 넣는다.

```text
Outcome

Using the exact passed Phase 6 Lite local handoff at LOCAL_RUN_DIR, perform fresh AWS preflight,
deploy the frozen implementation, run the scheduled 15M ClickHouse-to-S3 archive test, collect
evidence, destroy every run-owned resource, prove inventory zero, and finish with passed, failed,
aborted, blocked, or inconclusive. A failed full-scale attempt is not terminal by itself: preserve
it, clean up, rerun fresh preflight, and retry a new whole attempt until one passes. Never resume or
overwrite a partial attempt. Do not modify implementation code in this Goal.

Expected wall-clock

- Total: 1.5 to 3 hours including preflight and reporting.
- Paid AWS wall-clock: at most 120 minutes from deploy.
- Begin unconditional cleanup at paid minute 100.
- The estimate is not permission to stop after the first failed attempt. Continue safe whole-attempt
  retries while every guard still passes; otherwise cleanup and state the exact continuation gate.

Source of truth

1. Read AGENTS.md and preserve all existing user changes.
2. Read LOCAL_RUN_DIR/local-handoff.json and every evidence path it references.
3. Read and follow:
   - docs/guide_phase6_clickhouse_s3_archive_lifecycle_test.md
   - performance-tests/phase6-archive/exec-plan.md
   - performance-tests/phase6-archive/README.md
   - docs/guide_aws_event_pipeline_performance_test.md
4. Use the event-pipeline-loadtest-runner skill. Use aws-cdk, aws-sdk-python-usage,
   aws-billing-and-cost-management, aws-iam, aws-observability, and signing-in-to-aws when their
   trigger conditions apply. Read every selected SKILL.md completely before acting.

Mandatory handoff gate before any AWS call

- LOCAL_RUN_DIR must be an explicit existing immutable local run path; never auto-select by mtime.
- local-handoff.json verdict must be passed and awsReady must be true.
- LOCAL_RUN_DIR must equal the exact path above unless the user explicitly supplies a different
  passed handoff.
- small fixture, fault injection, 1M, and the successful 15M full-scale gate must all be passed.
- local Docker volume inventory must be zero and unresolved failures must be empty.
- Recompute implementation, schema, generator, image-reference, CDK synth-input, and payload hashes.
  They must exactly match local-handoff.json.
- If any handoff check fails, make no AWS call, write no AWS run directory, and finish blocked with
  the exact mismatch and the requirement to run Goal 1 again.

Frozen local-success baseline

- implementationCodeSha256:
  f4d455142e67dad5c66d36ade3b3cd9333e57f3bb435efb63463d99783b7c870
- ClickHouse image digest:
  sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797
- archive schema SHA-256:
  26e5589ccc6dba4ac4703dae61f5f7faae8139e2173c77e40338cc8eaa2b1fee
- generator: phase6-events-v1, seed 6000017, reference SHA-256
  a276200420b1b000003133a3865cbfabe2b61271f8e6c0762ee7509be094bf43
- seed quiescence: poll every 2 seconds, require two consecutive observations with zero active
  merges and mutations, timeout 900 seconds.
- stable fingerprints: two equal measurements five minutes apart after quiescence.
- exact uniqueness: sum sequential uniqExact(event_id) results over eight disjoint
  cityHash64(event_id) buckets.
- logical checksum: UInt64 sum over eight disjoint event_id hash buckets.
- query/export bounds: max_threads=1, block size 8192, export external-sort threshold 128 MiB,
  ClickHouse query/server/container limits 4.50/4.90/5.00 GiB.
- local accepted result: attempt 21, 15,000,000 rows and unique IDs, three 5,000,000-row objects,
  checksum 15742404871355694341, all two-way differences zero, source rows after DROP zero.
- local resource result: Docker memory 66.015624%, filesystem 75.004353%, OOM/restart 0/0.
- Do not weaken, replace, parallelize, or approximate these paths in AWS. AWS measurements must be
  recorded independently; local success is not AWS evidence.

Frozen execution boundary

- Do not edit archive.py, seed_partition.py, cost_model.py, preflight.py, cleanup_inventory.py,
  systemd files, CDK source, tests, schema, payload, dependencies, or lockfiles.
- AWS-specific run artifacts and final status documentation may be written.
- If AWS exposes an implementation defect, collect evidence, cleanup, mark failed, and return to a
  new Goal 1. Do not patch and redeploy inside this Goal.
- Do not change a historical run verdict or reuse an old run directory.

AWS preflight

1. For every paid full-scale attempt, create a new immutable
   performance-tests/run_<timestamp>_phase6_clickhouse_s3_archive directory only after the handoff
   gate passes. Use a new run ID, stack, bucket and archive prefix; never reuse a failed attempt.
2. Verify current date, AWS identity, explicit account/region/operator, root approval when applicable,
   run ownership, exact stack absence, quota, offering, bootstrap, SSM, IAM and cleanup capability.
3. Fetch fresh public prices and run deterministic cost calculation. Operational stop is $12,
   cleanup reserve is $3, and hard cap is $15. Do not deploy if the modeled maximum exceeds $15.
4. Verify every resource and name is run-owned. Do not modify shared dev infrastructure. Repeat
   identity, ownership, quota, price, projected-cost, cleanup and inventory-zero checks before each
   retry.

AWS execution

1. Deploy the frozen stack and verify private network path, fixed ClickHouse image/schema, instance
   role, systemd units, S3 conditional-write behavior, and EBS ownership tags.
2. Seed exactly 15,000,000 unique events into UTC today - 8 days. Verify eligibility and the same
   source fingerprint twice five minutes apart with no mutation/merge.
3. Invoke the actual systemd timer/service path through a run-scoped one-shot schedule. Prove flock
   rejects overlap.
4. Export sequentially to exactly 3 x 5,000,000-row Parquet/ZSTD data objects in run-owned S3
   Standard with maximum configured bandwidth 100 MiB/s.
5. Require schema, count, uniqExact, min/max, logical checksum, object SHA-256, and exact two-way
   difference zero before DROP. Create and re-read immutable manifest and conditional COMMITTED.
6. DROP only after the complete gate passes. Then run direct S3 query equivalence against the
   deterministic reference.
7. Verify recovery states without resuming a partial attempt. If the full-scale attempt fails,
   preserve evidence and cleanup to inventory zero before deciding whether a fresh whole attempt is
   allowed.
8. Required performance: export <= 15 minutes, validation <= 15 minutes, cycle <= 30 minutes,
   host CPU/memory p95 < 70%, filesystem < 80%, and zero restart/OOM/archive failure.

Stop and cleanup

- Stop new paid work on any identity, ownership, cost, source, correctness, commit, performance,
  resource, restart/OOM, or evidence failure. Never DROP to recover from a failed check.
- Start cleanup no later than paid minute 100 even when testing is incomplete.
- Preserve evidence before teardown. Remove all run-owned S3 objects/versions, partial artifacts,
  stack resources and metadata.
- Every ClickHouse EBS volume must have DeleteOnTermination=true and destroy semantics. Create no
  snapshot. Poll EC2/EBS APIs until exact run-owned volumes and snapshots are zero; deleting is not
  zero. Continue modeled cost while anything billable remains.
- Prove service-by-service inventory zero. If cleanup fails, do not start another run and report the
  exact owned resource and blocker.

Retry-until-success policy

- Functional or transient infrastructure failure: preserve the failed AWS run, cleanup to exact
  inventory zero, rerun all preflight checks, then start a new whole attempt with new ownership
  identifiers.
- Never partial-resume, reuse an S3 attempt prefix, overwrite COMMITTED, combine evidence from
  different attempts, or silently relax correctness/performance gates.
- Do not repeat the same known deterministic failure without first establishing and recording why
  the next whole attempt can differ.
- While no hard guard is blocking and the next attempt has a recorded reason it can differ, do not
  finalize merely because an earlier attempt failed; continue with the next whole attempt.
- Resource guard, cleanup blocker, paid minute 100, projected cost stop $12, and hard cap $15 stop
  further attempts. These guards cannot be bypassed to satisfy retry-until-success.
- If implementation changes are required, cleanup and finish the current AWS run as failed/blocked,
  return to a new Goal 1, reproduce the fix through small/fault/1M/15M local gates, produce a new
  passed handoff, and then continue with a fresh Goal 2. Do not patch inside Goal 2.
- Do not report the overall Phase 6 AWS result as passed until one complete AWS attempt passes and
  its run-owned cleanup inventory is zero. Preserve all earlier attempt verdicts unchanged.

Repository and secret boundaries

- Never save credentials, tokens, secrets, presigned URLs, or secret values.
- Do not stash, discard, overwrite, or reformat unrelated dirty-worktree changes.
- Do not stage, commit, push, create a branch, or open a PR unless explicitly asked.

Done

- A fresh AWS run directory contains run.json, commands.md, infra.md, failures.md, report.md,
  metrics-summary.json, correctness-summary.json, cost evidence, archive manifest/validation,
  schedule evidence, direct-query summary, and cleanup verification.
- The report states measured values and one explicit verdict.
- A successful completion identifies the exact successful attempt and links every preserved failed
  or interrupted attempt; a non-passing completion states which guard prevents the next retry.
- Run-owned S3 objects/versions, EBS volumes/snapshots, stacks, compute, IAM, logs and network
  inventory are zero, or the verdict is not passed and the exact blocker remains explicit.
- The final response gives LOCAL_RUN_DIR, AWS run path, verdict, correctness/performance/cost values,
  cleanup state, evidence files, and whether a new Goal 1 or Goal 2 is the next safe action.
```
