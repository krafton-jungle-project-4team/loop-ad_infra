# Phase 8 operations handoff

## Certified topology

The final baseline uses the standard `LoopAdPerfPhase7IntegrationImageStack` and
`LoopAdPerfPhase7IntegrationStack`; no targeted-only stack is part of the baseline.

| Role | Hosts | Instance type | AMI |
|---|---:|---|---|
| clickHouse | 1 | r7g.2xlarge | ami-000b332282fe987aa |
| collector | 6 | c6i.xlarge | ami-034e9ae07d918d27e |
| consumer | 2 | c7g.large | ami-000b332282fe987aa |
| haproxy | 2 | c6in.xlarge | ami-034e9ae07d918d27e |
| loadGenerator | 8 | c6in.large | ami-034e9ae07d918d27e |

- Region/account: `ap-northeast-2` / `742711170910`.
- Kinesis: 120 shards, verified in the fresh deployment evidence.
- ClickHouse: one `r7g.2xlarge`, encrypted 500 GiB gp3 (3,000 IOPS, 500 MiB/s), container 8 GiB,
  server 7 GiB, archive query operational envelope up to 6.5 GiB with retained reserve.
- Collector, consumer and archive images are digest-pinned in `phase8-manifest.json`.
- All instances require IMDSv2 and have no public IP.

## Deployment and readiness

Use fresh run/session identifiers and exact run-owned repositories. Verify identity, region, source,
image digests, ownership, absent stack, price/cost admission and prepared preflight before deploy.
Deploy each immutable Run ID at most once. Readiness requires stack `CREATE_COMPLETE`, exact hosts and
services, TLS/protocol health, Kinesis 120 shards, ClickHouse `SELECT 1` and the expected schema.

## Observability

Use HAProxy/collector/Kinesis/KCL/ECS/EC2/ClickHouse/CloudWatch/CloudTrail evidence from the immutable
run directory. The 1.1 GiB `metrics-summary.json` remains local raw evidence and is anchored by SHA-256
`ac1daddfd6e3b795ddfd7e05d7a971fb1934aeb2a323bb56fb5f6a2fc6ba3b57` in Attempt 23's ledger entry; it is intentionally not duplicated into Git.

## Cleanup and recovery

Delete the runtime stack, exact run-owned ECR images/repositories and image stack in that order. Then
verify all 35 service classes are zero and both exact and global Tagging API inventories are empty.
Stopped ECS tasks can remain immutable tag tombstones for about one hour. Poll them; do not redeploy or
misclassify live cost while waiting. A nonzero intermediate inventory is not terminal if later exact
recovery reaches authoritative zero, but the intermediate failure evidence must remain visible.

## Known limits and hard stops

- Performance acceptance remains >=49,500 actual RPS, zero transport/429/5xx and corrected p95 <300 ms.
- Query-memory settings are operational safety envelopes, not exact equality tests; preserve server and
  container reserve and keep Code 241 at zero.
- Never execute source DROP without immutable COMMITTED re-read and exact bidirectional equivalence.
- Identity, ownership, source/image hash, correctness/accounting, data-loss suspicion, budget and final
  cleanup-zero failures remain hard stops.
- Phase 8 performs no paid AWS experiment by default. Phase 5 remains `skipped`.
