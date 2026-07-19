# AWS Kinesis producer comparison contract

This reference fixes the conditions for the actual AWS comparison of the Phase 1
Kinesis producers. It supersedes only the earlier local experiment's prohibition
on AWS deployment. The event, acknowledgement, boundedness, retry, shutdown, and
repository-safety contracts remain in force.

## Session identity and starting state

- Session ID: `phase1-compare-20260712-035928z`
- Session start: `2026-07-12T03:59:28Z`
- Region: `ap-northeast-2`
- Infra repository branch: `codex/aws-perf-test-plan`
- Infra starting SHA: `3081594bb46355ba9b7c64fa55d6a0b04d96d34a`
- Infra starting status: only the pre-existing untracked `_workspace/` and
  `im-not-ai/` directories
- Collector worktree: `/private/tmp/loop-ad-event-collector-phase1-20260710-171241`
- Collector branch: `codex/phase1-kinesis-transition`
- Collector starting SHA: `8a530d492eafe587eeeae7c275e3c36d4fd92e8c`
- Collector starting status: clean

Do not reset either repository to an earlier goal's starting SHA. Do not reset,
stash, rebase, squash, force-checkout, push, merge, or open a pull request. Do not
modify or stage `_workspace/` or `im-not-ai/`.

Every resource created for this comparison must carry the session ID. Run-scoped
resources also carry their immutable run ID and candidate name. Existing dev,
production, Kafka, repository, and unrelated resources are outside the deletion
scope.

## Candidates

| Candidate | Producer | Fixed starting configuration |
| --- | --- | --- |
| `go-sync` | Go synchronous `PutRecord` | one request per event; SDK retry contract retained |
| `go-batch` | bounded Go `PutRecords` | 5 ms window, 50 records, 16 senders, input queue 2,048, sender queue 32 |
| `java-kpl` | Java KPL 1.x HTTP service | pinned official KPL artifact, aggregation disabled, collection enabled |

The comparison runs one candidate at a time with one `c6i.xlarge` ECS on EC2
collector host and task. All candidates use the same VPC, subnet, internal ALB,
target group shape, shared provisioned Kinesis stream, load generator, task
resource envelope, `linux/amd64` architecture, payload pool, offered load,
warm-up, measurement duration, cooldown, and CloudWatch collection interval.

The two Go candidates use the same Dockerfile and runtime base. The Java image
uses a digest-pinned Linux amd64 JVM-compatible base. Its JVM and KPL native-child
overhead is part of the result, not normalized away.

## Event and HTTP contract

For every candidate:

```text
one valid SDK event
  = one HTTP request
  = one unchanged JSON byte sequence
  = one Kinesis record whose partition key is event_id
  = one HTTP 202 only after that record's successful Kinesis acknowledgement
```

- Multiple user events must never be aggregated into one Kinesis record.
- `PutRecords` and KPL collection may place multiple Kinesis records in one API
  request.
- Admission, pending records, pending bytes, sender queues, retry counts, and
  overall deadlines are bounded.
- Full admission returns `429` immediately without starting unbounded work.
- Final Kinesis throttling maps to `429`; bounded timeout and transient/internal
  failure map to `503`.
- Go `PutRecords` retries only failed response indexes and never retransmits a
  successful response index.
- Shutdown stops admission and attempts a bounded flush. The result records
  flushed, failed, and timed-out records.
- Timeout-boundary ambiguity is treated as an at-least-once duplicate risk and is
  measured rather than hidden.

## Actual Kinesis correctness run

Each candidate receives its own correctness run before performance evidence is
eligible. The verifier creates request bodies with unique `event_id` values and
records the exact request-body SHA-256, partition key, request start, HTTP
completion, status, and shutdown boundary.

The verifier reads the actual shared Kinesis stream from iterators established
before submission and produces `correctness-summary.json` with:

- expected, accepted, observed, missing, duplicate, and unexpected counts;
- per-event body SHA-256 and partition-key comparison;
- HTTP successes that cannot be associated with a successful record;
- evidence that every observed user event occupies exactly one Kinesis record;
- shutdown-boundary enqueue and flush results;
- timeout-boundary duplicate observations;
- candidate configuration proving aggregation is disabled.

A local stub result cannot replace this run. The repeating 480-body performance
pool cannot be used to infer correctness.

## Load sequence

For each candidate:

1. Wait for host, ECS task, target health, application health, and warm-up.
2. Run the actual Kinesis correctness check.
3. Run `1,000 RPS` for 30 seconds with oha.
4. If the baseline cannot safely sustain the target, run a single common
   `1k -> 5k -> 10k` pilot and select one safe common offered load.
5. Run the common offered load with 30 seconds warm-up, 60 seconds measurement,
   ten repetitions, and 15 seconds cooldown.
6. Collect all raw application, host, container, ALB, ECS/EC2, Kinesis, runtime,
   and CloudWatch evidence.
7. Destroy the run-scoped stack and verify that its VPC, ALB, target group, ECS,
   ASG, EC2, security groups, roles, and log group are absent.
8. Commit the immutable run directory before moving to the next candidate.

Candidate-specific load reductions are forbidden. A failed candidate remains a
failed comparison result at the common load.

## Required measurements

Each repetition records offered and accepted requests per second, HTTP status
counts, HTTP and per-event acknowledgement p50/p95/p99, producer API calls,
records per call, calls per accepted event, retries, partial failures, batch-size
distribution, queue high-water, timeout/final failure, loss/duplicate evidence,
host and container CPU/RSS/network, Go allocation/heap/goroutine/GC, JVM heap/GC,
KPL child CPU/RSS/restart, startup/warm-up, shutdown flush, and Kinesis/ALB/ECS/EC2
CloudWatch metrics.

All medians, ranges, IQRs, per-host, per-task, and per-accepted-event values are
calculated by a checked script or `jq`, never by hand.

## Cost and time stops

- `MAX_INCREMENTAL_COST_USD=20`
- Initial conservative full-plan upper bound must be at most `$16`.
- `$4` remains reserved for price error, failure, cleanup, and delayed metering.
- No new run starts when modeled accrued cost reaches `$18`.
- No deploy starts if the next required run plus cleanup can bring the projected
  cumulative upper bound to `$20` or more.
- Shared-stream lifetime is at most three hours.
- No candidate or repetition starts 160 minutes after stream creation.
- Cleanup starts unconditionally at 170 minutes.

Every deploy preflight refreshes current AWS prices, quota, current resources,
stream age, modeled accrued cost, and projected upper bound. Wall-clock/resource
usage is authoritative for the hard stop because Cost Explorer and Budgets lag.

## Artifact and lifecycle contract

Each candidate owns an immutable directory named
`performance-tests/run_<timestamp>_phase1_kinesis_compare_<candidate>/` with all
files required by the goal, including raw oha, CloudWatch, application, image,
correctness, cost, and cleanup evidence. Unknown values are recorded as `not
measured` or `pending`, never estimated.

The shared stream is deployed once, used sequentially, and destroyed immediately
after the final candidate or any hard stop. Final cleanup additionally verifies
the comparison's ECR images are deleted by exact digest/tag ownership and that no
collector, EC2, ECS, ASG, ALB, VPC, log group, Kinesis stream, or experiment image
owned by the session remains.

Only candidates that pass the actual Kinesis correctness run, loss-zero,
pre-ACK-success-zero, byte/key preservation, no aggregation, bounded resource,
partial-failure, clean-shutdown, reproducibility, and common-load gates are
eligible for a performance recommendation. Differences inside observed
variation do not justify a winner.
