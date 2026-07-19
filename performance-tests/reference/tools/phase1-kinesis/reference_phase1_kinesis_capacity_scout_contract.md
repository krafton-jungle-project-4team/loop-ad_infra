# Phase 1 Kinesis capacity scout contract

## Purpose

This procedure is a feasibility scout. It cannot establish final capacity, rank candidates, or exclude a candidate from the final session.

## Preconditions

Local collector verification, including Go bounded PutRecords independent per-event final-ACK latency and shutdown recovery tests, must pass before AWS resources are created. Use a new `phase1-capacity-scout-<UTC timestamp>` session, run IDs, scout stacks, ECR tags, and artifacts.

## Fixed procedure

Run Go synchronous PutRecord, Go bounded PutRecords, and Java KPL in order. Each uses four `c6i.xlarge` ECS-on-EC2 collector hosts, one task per host, one `c6in.large` oha host, 80 Kinesis shards, and the exact final capacity payload, partition key, connection, timeout, CPU, memory, logging, collector code, and producer configuration.

After deployment and actual-Kinesis correctness pass, run 1,000, 10,000, 30,000, then 50,000 RPS for 15 seconds each. Follow every probe with a 15-second cooldown and bounded recovery verification. Record each stage only as `feasible`, `unsafe`, or `inconclusive`. OOM, restart, panic/fatal, Kinesis throttling, or recovery failure stops higher probes. Missing evidence is inconclusive and stops higher probes.

Do not run warm-up, 60-second repetitions, variance/IQR/ranking, or the optional 50,000 RPS 300-second measurement.

## Evidence, budget, and cleanup

Store raw oha, CloudWatch and shard metrics, task/host placement, image digest, producer queue/ACK/retry snapshots, safety decision, cost snapshots, and cleanup evidence under `performance-tests/run_<timestamp>_phase1_kinesis_capacity_scout_<candidate>/`.

After all candidates or any blocker, delete both scout stacks and all scout ECR tags, then verify the owned AWS API inventory is empty. Final capacity cannot start before this passes. Scout and final have independent 100/110/120-minute guards. Combine their modeled costs: initial upper bound at most USD 16, USD 4 cleanup/error reserve, no new stage at modeled accrued cost of USD 18 or more, no start when projected total is USD 20 or more, and a hard total below USD 20. Cost Explorer lag is not zero cost.
