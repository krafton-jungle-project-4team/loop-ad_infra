# run_20260711_150400_phase1_kinesis_conn128

This is a curated evidence report. Raw artifacts remain in `snapshot_20260719T200907Z`.

## Classification

- Phase: `phase1`
- Category: `experiment`
- Status: `failed` (source: `failed`)
- Validity: `unknown`
- Executed at: `2026-07-11T15:04:00` (timezone not recorded)

## Purpose and hypothesis

- Purpose: not_recorded
- Hypothesis: Reducing the per-process Kinesis pool from 256 to 128 will further bound synchronous transport concurrency and lower target latency enough to increase 30k completion throughput, without changing admission or oha connections.

## Results

- Correctness: `not_recorded`
- Cost: `unknown`
- Cleanup: `passed`
- Extracted metric fields: 33

## Conclusion

not_recorded

## Limitations

- not_recorded

## Provenance

- Snapshot: `snapshot_20260719T200907Z`
- Workspace archive SHA-256: `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source paths:
  - `run_20260711_150400_phase1_kinesis_conn128`
- Hashed evidence files: 9
