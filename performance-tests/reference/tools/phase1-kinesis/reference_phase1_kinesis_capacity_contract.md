# Phase 1 Kinesis producer capacity-test contract

This reference freezes the contract for the Phase 1 producer capacity test. It
does not replace or relax
[`reference_aws_producer_comparison_contract.md`](./reference_aws_producer_comparison_contract.md).
The comparison-only one-host, ten-repetition, and 160/170/180-minute rules remain
unchanged. Capacity tooling must use a separate mode or script.

## Session and repository state

- Phase: `phase1-capacity`
- Experiment: `kinesis-producer-capacity`
- Session ID: `phase1-capacity-20260712T081500Z`
- Session start: `2026-07-12T08:15:00Z`
- Region: `ap-northeast-2`
- Infra branch and starting SHA: `codex/aws-perf-test-plan` at
  `6bdddaea4cbbc01e49e2edcddd91bb40fe0f49bd`
- Collector worktree: `/private/tmp/loop-ad-event-collector-phase1-20260710-171241`
- Collector branch and starting SHA: `codex/phase1-kinesis-transition` at
  `d1918b629bbbf3f4499a9671c3213687a39c5d5d`
- Infra starting status: only pre-existing untracked `_workspace/` and
  `im-not-ai/`; collector starting status: clean

Do not reset, stash, rebase, discard user changes, push, merge, or create a pull
request. Do not modify or commit `_workspace/` or `im-not-ai/`. Every capacity
run uses a new immutable run ID, artifact directory, stack name, image tag, and
digest. Earlier comparison resources and images must not be reused.

## Fixed specification

| Field | Value |
| --- | --- |
| Candidate order | `go-sync`, `go-batch`, `java-kpl` |
| Collector topology | ECS on EC2; `4 x c6i.xlarge`; exactly one task per host |
| Load generator | `1 x c6in.large`; pinned oha image `ghcr.io/hatoo/oha@sha256:76c300321fd0101d7e0588ae0486956a83034d7057a37be052619fa28204a072` |
| Architecture | `linux/amd64` |
| Stream | one session-owned provisioned Kinesis stream; 80 shards; 24-hour retention |
| Payload | `payloads/sdk-compatible-event-bodies.ndjson`; SHA-256 `f82cd61548b1be8d5df21a91b8e86390422e4d433ac6dc93d87414a3755336c2` |
| Event contract | one HTTP request and one unchanged Kinesis record per event; partition key is `event_id`; HTTP 202 only after final ACK |
| Stages | 10,000, 30,000, then 50,000 requests/s |
| Stage timing | 15-second probe; 30-second warm-up; exactly three 60-second measurements; 15-second cooldown and recovery check |
| Cost limits | initial plan at most USD 16; no new work at modeled USD 18; hard cap USD 20 including USD 4 cleanup/error reserve |
| Runtime limits | no new candidate/stage after 100 minutes; cleanup starts by 110 minutes; shared stream hard stop at 120 minutes |
| Authentication | active local AWS CLI session; never persist credentials, registry passwords, or login URLs in artifacts |
| Deploy lifecycle | synth, diff review, ownership/cost/quota preflight, deploy, state verification, correctness, load, evidence, destroy, absence verification |

Candidate settings are frozen before the first AWS deployment:

- `go-sync`: synchronous `PutRecord` with the existing bounded admission and SDK
  retry contract.
- `go-batch`: `PutRecords`, 5 ms window, 50-record maximum, 16 senders,
  2,048-entry input queue, and 32-entry sender queue.
- `java-kpl`: official KPL 1.x, aggregation disabled, collection enabled, with
  bounded outstanding records/bytes and bounded shutdown flush.

Host count, task count, shard count, payload, connection settings, producer
settings, task CPU/memory reservations and limits, JVM/Go resource envelope,
timeouts, logging, and metric collection must remain identical across stages and
must not be changed to rescue one candidate.

## Blocking local gate

Before creating AWS capacity resources, Go batch must expose independent
per-event ACK latency whose interval begins after successful producer-queue
enqueue and ends only after that event's final successful Kinesis ACK. Queue wait
and retry wait are included. Partial failures retain the original timestamp.
Timeouts and final failures do not enter the success histogram. Shutdown flush
outcomes are visible in metrics and logs, and histogram deltas can be compared
with final-ACK deltas.

Unit, partial-failure/retry, shutdown, race, full Go, Java Maven, image, infra,
comparison-regression, capacity-guard, summarizer/verifier, synth, diff, IAM,
preflight, and cleanup tests must pass before AWS load. Failure blocks deployment;
the failure evidence is committed and any owned AWS resource is cleaned up.

## Correctness and safety gates

Each candidate must pass an actual-Kinesis correctness run before performance
load. The verifier must prove:

- exactly four running tasks on four distinct `c6i.xlarge` hosts, all using the
  expected immutable image digest;
- baseline HTTP 202 and Kinesis incoming-record deltas agree within 0.1%;
- a deterministic task can be stopped while three tasks remain healthy;
- the replacement uses the same digest and restores the exact four-host layout;
- events accepted by the stopped task are observed after shutdown flush;
- no loss, panic, fatal error, OOM, abnormal exit, unbounded queue, or unbounded
  outstanding request remains after recovery.

A safety probe blocks its full stage on task restart, OOM, panic/fatal, target
5xx above 1%, Kinesis write throttling, sustained unbounded growth, failed
recovery, a task count other than four, or unequal host placement. After a
failed lower full stage, only the next higher safety probe is allowed if the
system recovered; no higher full measurement is allowed.

Every repetition must independently satisfy the objective's throughput, status,
latency, Kinesis agreement, throttle, producer-failure, task-count, task-balance,
and recovery thresholds. Averages cannot hide a failed repetition. All rates,
percentages, quantiles, ranges, IQRs, costs, and per-event values are calculated
by checked code, never manually.

## Runtime prediction and cost accounting

Before every candidate, stage, and repetition, a capacity-specific guard must
combine current stream age with a conservative duration estimate for the
remaining probe, warm-up, three measurements, cooldowns, state polling, evidence
collection, and cleanup. Merely checking that current age is below 100 minutes is
insufficient. The guard also combines deterministic modeled accrued cost with
the conservative cost of remaining work and cleanup. Cost Explorer delay is
recorded as `pending`, not interpreted as zero.

The cost upper bound includes EC2, Kinesis shard-hours, ALB, NAT/data transfer,
CloudWatch, ECR, and all other session-created billable resources. The optional
50k x 300-second run is `not-run-optional` unless all mandatory common evidence,
cleanup time, and reserve are decisively protected.

## Artifacts, cleanup, and commits

Each candidate writes an immutable directory named
`performance-tests/run_<timestamp>_phase1_kinesis_capacity_<candidate>/` with
the manifest, identifiers, repository state, tool pin, image digest, synth/diff,
IAM and preflight evidence, cost snapshots, resource inventory, four-task and
SIGTERM correctness evidence, raw load and CloudWatch output, task distribution,
producer metrics, checked stage decisions, skip/block reasons, and cleanup
verification. Every decision must be recomputable from raw artifacts.

On success, failure, timeout, cost stop, or authentication failure, destroy only
resources owned by this session and verify absence through service APIs. This
includes stacks, ECS/EC2/ASG/launch-template resources, ALB resources, the stream,
load generator, networking objects, logs/metrics/alarms, and exact experiment
image tags. A successful delete request is not cleanup proof.

Use separate logical commits for the capacity contract, Go ACK instrumentation,
four-host tooling, runtime/cost guards, local/image verification, each candidate's
AWS evidence, final cleanup verification, and final capacity comparison. Omit a
commit only when that stage creates no change; never create an empty commit.
