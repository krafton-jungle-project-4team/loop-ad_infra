# run_20260711_003908_phase1_kinesis_1c_5krps

This is a curated evidence report. Raw artifacts remain in `snapshot_20260719T200907Z`.

## Classification

- Phase: `phase1`
- Category: `experiment`
- Status: `passed` (source: `passed`)
- Validity: `unknown`
- Executed at: `2026-07-11T00:39:08` (timezone not recorded)

## Purpose and hypothesis

- Purpose: not_recorded
- Hypothesis: One ECS-optimized Amazon Linux 2023 c6i.xlarge collector sustains 5,000 RPS for 60 seconds through the synchronous one-event/one-PutRecord path with no throttling, restarts, unhealthy targets, OOM, or acceptance-gate violation while using the shared 80-shard Kinesis session stream.

## Results

- Correctness: `not_recorded`
- Cost: `unknown`
- Cleanup: `passed`
- Extracted metric fields: 110

## Conclusion

not_recorded

## Limitations

- not_recorded

## Provenance

- Snapshot: `snapshot_20260719T200907Z`
- Workspace archive SHA-256: `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source paths:
  - `run_20260711_003908_phase1_kinesis_1c_5krps`
- Hashed evidence files: 11
