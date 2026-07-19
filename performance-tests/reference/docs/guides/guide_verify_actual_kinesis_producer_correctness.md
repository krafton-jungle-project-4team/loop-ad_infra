# Verify actual Kinesis producer correctness

Use this procedure after one comparison candidate is healthy in
`LoopAdPerfPhase1KinesisStack` and before its oha smoke or performance runs.

## Prerequisites

- The shared stream and exactly one run-scoped candidate stack are healthy.
- `verify-deployment.mjs` passed for one collector task and host.
- The load generator and collector EC2 instances are online in Systems Manager.
- The task definition image digest, candidate, run ID, and comparison session ID
  match the run record.
- The caller can read the shared Kinesis stream and inspect ECS. The load
  generator does not receive Kinesis read permission.

## Run the verifier

Resolve the output values and instance IDs from the checked deployment evidence,
then run:

```bash
node performance-tests/phase1-kinesis/verify-actual-kinesis-correctness.mjs \
  --run-dir performance-tests/run_<timestamp>_phase1_kinesis_compare_<candidate> \
  --run-id run_<timestamp>_phase1_kinesis_compare_<candidate> \
  --candidate <go-sync|go-batch|java-kpl> \
  --stream-name <stream-name> \
  --target-url <internal-alb-url> \
  --load-generator-instance-id <instance-id> \
  --cluster <ecs-cluster-name> \
  --service <ecs-service-name>
```

The default run sends 16 normal events and 64 shutdown-boundary events. It
chooses unique event IDs whose MD5 partition hashes all fall inside one open
shard. The low correctness volume therefore avoids scanning all 80 shards and
does not compete with the performance workload.

For the shutdown boundary, the verifier schedules two Systems Manager commands
against the AWS time-synchronized hosts. The load generator starts the concurrent
requests at one absolute timestamp. Ten milliseconds later, the collector host
sends `SIGTERM` to the exact ECS collector container. ECS restarts the task after
the process completes its graceful shutdown path.

## Inspect the evidence

The verifier writes:

- `correctness-events.json`: exact generated bodies, bytes, hashes, groups, and
  partition keys;
- `correctness-requests.json`: HTTP status and request start/completion times;
- `correctness-records.json`: actual Kinesis data, partition key, sequence,
  arrival time, bytes, and SHA-256;
- `correctness-ssm.json`: bounded command results and shutdown signal evidence;
- `correctness-summary.json`: loss, duplicate, byte/key mismatch, aggregation,
  timing lower bound, and shutdown flush verdict.

The run passes only when every normal request receives `202`, every accepted
event is observed exactly once with the original bytes and partition key, no
unexpected session event exists, no HTTP `202` precedes the record's approximate
arrival timestamp, and at least one shutdown-boundary request is accepted and
observed.

Kinesis `ApproximateArrivalTimestamp` is a server-arrival lower bound, not an
exposed acknowledgement timestamp. Therefore the AWS run combines this timing
check with candidate contract tests that prove the HTTP success path is reachable
only after the SDK or KPL future succeeds. Do not describe the timestamp alone as
direct observation of the private service ACK instant.

If the verifier exits nonzero, preserve all files, collect application logs and
metrics, complete run-scoped cleanup, and record the candidate as failed or
inconclusive. Do not replace the evidence with local stub results.
