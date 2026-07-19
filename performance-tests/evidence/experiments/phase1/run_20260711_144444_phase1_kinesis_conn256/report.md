# run_20260711_144444_phase1_kinesis_conn256

This is a curated evidence report. Raw artifacts remain in `snapshot_20260719T200907Z`.

## Classification

- Phase: `phase1`
- Category: `experiment`
- Status: `failed` (source: `failed`)
- Validity: `unknown`
- Executed at: `2026-07-11T14:44:44` (timezone not recorded)

## Purpose and hypothesis

- Purpose: not_recorded
- Hypothesis: Capping each process at 256 Kinesis HTTP connections, for 1024 aggregate across four collectors, will bound synchronous AWS work and eliminate the 1024-per-process backlog while retaining 1024 admission and oha connections.

## Results

- Correctness: `not_recorded`
- Cost: `unknown`
- Cleanup: `passed`
- Extracted metric fields: 85

## Conclusion

not_recorded

## Limitations

- not_recorded

## Provenance

- Snapshot: `snapshot_20260719T200907Z`
- Workspace archive SHA-256: `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source paths:
  - `run_20260711_144444_phase1_kinesis_conn256`
- Hashed evidence files: 9
