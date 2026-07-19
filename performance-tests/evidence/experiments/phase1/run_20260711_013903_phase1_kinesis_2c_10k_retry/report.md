# run_20260711_013903_phase1_kinesis_2c_10k_retry

This is a curated evidence report. Raw artifacts remain in `snapshot_20260719T200907Z`.

## Classification

- Phase: `phase1`
- Category: `diagnostic`
- Status: `failed` (source: `failed`)
- Validity: `unknown`
- Executed at: `2026-07-11T01:39:03` (timezone not recorded)

## Purpose and hypothesis

- Purpose: not_recorded
- Hypothesis: With bounded ASG resource signals delaying service creation until both AL2023 ECS agents respond, two c6i.xlarge collectors deploy without the prior registration race and sustain 10,000 RPS for 60 seconds within all exploration acceptance gates.

## Results

- Correctness: `not_recorded`
- Cost: `unknown`
- Cleanup: `passed`
- Extracted metric fields: 66

## Conclusion

not_recorded

## Limitations

- not_recorded

## Provenance

- Snapshot: `snapshot_20260719T200907Z`
- Workspace archive SHA-256: `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source paths:
  - `run_20260711_013903_phase1_kinesis_2c_10k_retry`
- Hashed evidence files: 11
