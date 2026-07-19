---
name: event-pipeline-loadtest-runner
description: Orchestrate LoopAd Phase 0-6 event-pipeline experiments from contract resolution through local verification, AWS cost and ownership preflight, deployment, correctness checks, pinned load generation, scheduled ClickHouse-to-S3 archive and direct-query validation, optional restore drills, evidence capture, cleanup verification, and logical commits. Use for Artillery, oha, local, Fargate, ECS on EC2, Kinesis, Kafka, ClickPipes, ClickHouse, S3 lifecycle tiers, Parquet archives, restore drills, run-id artifacts, benchmark comparisons, performance investigations, teardown, or experiment evidence commits.
---

# Event pipeline experiment runner

Run each experiment as a guarded lifecycle. Do not select a load driver, compute
platform, topology, cost limit, or cleanup policy until the active contract has
been resolved.

## Resolve the active contract

Read applicable instructions in this order:

1. Current user request and attached goal objective.
2. Goal-specific contract or runbook in the repository.
3. Phase-specific README, guide, scripts, CDK outputs, and tests.
4. This skill's defaults.

Use the higher-priority rule when sources conflict. Record the override in the
run artifacts. Never preserve an older Artillery, Fargate, cost, scale, or
single-record assumption against a newer goal.

Before changing files or calling AWS:

- Read repository `AGENTS.md` files and safety instructions.
- Record each repository's branch, starting SHA, and `git status`.
- Preserve user changes and unrelated untracked files.
- Do not reset, stash, rebase, squash, force-checkout, push, merge, or create a
  pull request unless the current user request explicitly requires it.
- Find unfinished run directories and live resources owned by earlier runs.
- Do not infer ownership from a resource name alone.

If the request is only for planning, design, review, or diagnosis, stop before
cloud mutation and load execution.

## Freeze the experiment specification

Resolve these values from the active contract and repository instead of assuming
defaults:

```text
PHASE
EXPERIMENT_NAME
HYPOTHESIS
RUN_ID
RUN_DIR
SESSION_ID
CANDIDATE
LOAD_DRIVER
LOAD_DRIVER_VERSION_OR_DIGEST
EXECUTION_MODE
AUTH_FLOW
TARGET
PAYLOAD_PATH_AND_SHA256
EXPECTED_LOAD_AND_TIMINGS
CORRECTNESS_GATE
DEPLOY_COMMAND
DESTROY_COMMAND
SHARED_RESOURCES
TEARDOWN_POLICY
COST_LIMIT_USD
COST_STOP_THRESHOLD_USD
WALL_CLOCK_DEADLINE
REQUIRED_ARTIFACTS
```

For archive phases also resolve source partitions, eligibility age and safety
window, scheduler and executor, archive format, bulk/demo/evidence prefixes,
manifest checksums, direct-query gates before and after source deletion, bulk
deletion policy, and any optional storage-class transition or restore RTO.

Use the phase's chosen load driver and compute platform:

- Use oha when the phase or goal specifies oha.
- Use Artillery only when the phase or goal specifies Artillery.
- Use `run-fargate` only for an explicitly Fargate experiment.
- Keep ECS on EC2, local Docker, bare EC2, or other prescribed topology intact.
- Use checked-in helpers before inventing replacement commands.
- Pin tool versions and container images. Do not use `@latest` unless the active
  contract explicitly permits it; if permitted, record the resolved version.

Create an immutable `performance-tests/run_<id>/` directory. Never overwrite a
previous run.

Use the repository's or user's configured authentication flow. Do not assume
that every experiment has a runner role. Keep credentials in the active session;
never copy access keys, session tokens, registry passwords, or login URLs into
run artifacts.

## Execute the guarded lifecycle

Use this sequence unless the active contract is stricter:

```text
resolve contract
-> local verify
-> cost/quota/ownership preflight
-> deploy
-> verify deployment
-> correctness gate
-> smoke/pilot
-> measured load
-> collect evidence
-> destroy run-scoped resources
-> verify cleanup
-> finalize artifacts
-> logical commit
```

### 1. Local verify

Run the phase-defined tests, race checks, linters, builds, local integration
tests, image build, architecture inspection, and contract tests before deploying.

Verify at minimum when applicable:

- payload bytes and manifest SHA-256;
- response and acknowledgement semantics;
- bounded queues, retries, deadlines, and shutdown;
- image architecture, immutable digest, and source SHA;
- dependency and native-runtime behavior;
- `git diff --check` and scoped secret scanning.

Do not deploy an implementation that fails a required local gate. Local stub
success does not replace an actual cloud correctness check when the contract
requires one.

### 2. Cost, quota, and ownership preflight

For AWS runs, before every deploy and destructive cleanup, run the repository's
checked-in preflight helpers. Verify:

- AWS identity, configured account, explicit region, and CDK environment;
- exact stack names and current stack state;
- applied quota and running or pending account usage;
- resource-name collisions;
- project, environment, session, run, candidate, and stack ownership tags;
- current AWS prices and deterministic cost-model inputs;
- storage minimum-duration, transition, retrieval, temporary-restore, and
  early-deletion charges when archival is in scope;
- modeled accrued cost, projected next-run cost, cleanup reserve, and deadline;
- shared-resource existence, owner, health, and age;
- immutable image tag-to-digest mapping.

Use scripts or `jq` for arithmetic. Do not calculate cost, rates, medians, ranges,
IQRs, or per-event values manually. Treat Cost Explorer and Budgets as delayed
evidence, not the hard-stop clock.

Do not deploy when any required preflight check fails, the next run can cross the
cost cap, quota is insufficient, or ownership is ambiguous. Record the run as
`blocked` when the contract requires a run record.

### 3. Deploy and verify

Deploy only the exact experiment stacks and resources in the active contract.
Apply the session ID to all experiment resources and the run ID and candidate to
run-scoped resources. Keep shared and run-scoped lifecycles separate.

After deployment, verify actual state rather than trusting command success:

- image digest and architecture;
- host and task count, placement, health, and resource envelope;
- load generator version and readiness;
- target and listener routing;
- IAM scope;
- shared backend identity and capacity;
- absence of unintended replacement, scaling, or unrelated-resource changes.

Run one candidate at a time when the comparison contract requires isolation.

### 4. Correctness, smoke, and load

Run the phase-defined correctness gate before accepting performance evidence.
When actual backend verification is required, read records from that backend and
verify the event count, original bytes, key or partition, loss, duplicates,
acknowledgement boundary, retry behavior, and shutdown flush. Do not infer these
properties from a repeating performance payload pool.

For an archive phase, require the scheduled executor to select an age-eligible
partition. Verify source-to-Parquet full-set equivalence and ClickHouse direct
S3-query equivalence before deleting a source partition. Query the same archive
again after source deletion. Treat export and source deletion as separate
non-atomic operations. Never use a manual export, successful command, or row
count alone as deletion authorization. Require restore equivalence only when the
active contract includes a restore drill.

Then run the prescribed smoke, common pilot, warm-up, repetitions, cooldown, and
measured load. Do not reduce load for only one comparison candidate. Preserve a
candidate failure at the common load as evidence.

Capture the exact command, driver configuration, payload SHA-256, target,
offered load, start and end times, exit code, stdout, stderr, and raw driver
output. Bound every command and polling loop with a timeout.

### 5. Collect evidence

Collect the phase-required raw evidence before planned teardown:

- HTTP results and status distribution;
- application counters, acknowledgement latency, queues, batches, and retries;
- backend service metrics and readback correctness;
- host, task or container CPU, memory, network, and runtime/GC data;
- ALB, ECS/EC2, Kinesis, Kafka, ClickHouse, or other relevant service metrics;
- S3 object inventory, checksums, direct-query results, scheduler cycles,
  storage classes, and, when in scope, lifecycle transition or restore state,
  retrieval bytes, and restore elapsed time;
- logs, deployment state, environment, architecture, image, and dependency data;
- real-time cost model and Cost Explorer response or explicit `pending` state.

Write unknown values as `not measured` or `pending`. Never manufacture a value or
silently substitute local evidence for cloud evidence.

### 6. Destroy and verify cleanup

Re-run identity, region, stack, tag, and ownership checks before deletion.
Destroy only resources owned by the current run or session.

- Preserve explicitly shared resources only while the active contract allows.
- Destroy shared resources at the final candidate, hard stop, maximum age, or
  session termination.
- If an artifact is missing but resources may still incur cost, clean up first
  and record the artifact failure afterward.
- If cleanup fails, do not start another run. Continue bounded verification and
  cost accounting or report the exact blocker.
- Delete experiment images only after exact tag and digest ownership checks.
- Never delete repositories, mutable shared tags, dev, production, Kafka, or
  unrelated resources unless explicitly in scope.
- Distinguish bulk, demo, evidence, and temporary archive prefixes. Delete bulk
  data only after the active direct-query and evidence gates pass. Preserve the
  contract-defined minimum demo and evidence prefixes.

Verify absence through service APIs for every resource type required by the
phase, including stacks, compute, scaling groups, services/tasks, load balancers,
target groups, VPC/network objects, log groups, streams, and experiment images.
Command success alone is not cleanup evidence.

## Cost and time hard stops

Use the active goal's numeric limits. Do not reuse a previous experiment's cap.

- Keep a conservative deterministic upper bound from deployment through verified
  cleanup.
- Reserve the contract-defined budget for failures, delayed metering, and cleanup.
- Start no new run after the cost or stream-age stop threshold.
- Begin cleanup unconditionally at the hard deadline.
- Continue accruing modeled cost while billable resources remain after a failed
  destroy.
- Do not bypass a guardrail to complete all candidates.

## Run artifacts

Use the active contract's required file list. For AWS performance experiments,
expect the run directory to include, when applicable:

```text
run.json
commands.md
infra.md
image.json
artifacts.md
report.md
metrics-summary.json
correctness-summary.json
cost-upper-bound.json
cost-realtime.json
cost-explorer.json
cost.md
cleanup-verification.json
archive-manifest.json
archive-validation.json
archive-schedule.json
direct-query-summary.json
restore-summary.json
lifecycle-policy.json
raw load-driver output
raw service metrics
application metrics and logs
environment and architecture evidence
```

Keep commands reproducible without credentials. Store no access key, session
token, registry password, private key, dependency cache, native binary, or build
output in Git.

Preserve reports and summary evidence for failed, aborted, blocked, and
inconclusive runs. The active contract may delete large raw event or Parquet
datasets after recording their identity, counts, checksums, validation result,
and deletion time. Finalize `run.json` with one of `passed`, `failed`, `aborted`,
`blocked`, or `inconclusive`, plus cause, cost, artifact state, and cleanup result.

## Commit boundary

Commit implementation, tooling, and AWS run evidence as separate logical units.
For a run-evidence commit:

1. Confirm required artifacts exist and JSON files parse.
2. Confirm cleanup evidence is final for that lifecycle stage.
3. Run `git diff --check`.
4. Scan staged content for credentials, tokens, private keys, caches, binaries,
   and generated build output.
5. Review the staged diff and stage only the run and directly related files.
6. Commit the result even when it failed or was inconclusive.
7. Start the next hypothesis only after the evidence commit succeeds.

Report the phase, session, run ID, candidate, status, commit SHA, cost state,
cleanup result, essential measurements, blockers, and the next safe action.
