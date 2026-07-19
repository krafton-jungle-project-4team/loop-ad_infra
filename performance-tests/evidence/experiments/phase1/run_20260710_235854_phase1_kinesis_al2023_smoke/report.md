# run_20260710_235854_phase1_kinesis_al2023_smoke

This is a curated evidence report. Raw artifacts remain in `snapshot_20260719T200907Z`.

## Classification

- Phase: `phase1`
- Category: `smoke`
- Status: `passed` (source: `passed`)
- Validity: `unknown`
- Executed at: `2026-07-10T23:58:54` (timezone not recorded)

## Purpose and hypothesis

- Purpose: not_recorded
- Hypothesis: The ECS-optimized Amazon Linux 2023 migration boots one c6i.xlarge collector and one c6in.large load generator with successful cloud-init, SSM, cgroup v2, IMDSv2-only, ECS registration, exact ECR image pull, ALB health and CloudWatch Logs, then sustains 1,000 RPS for 30 seconds with the synchronous one-event/one-PutRecord contract and complete cleanup.

## Results

- Correctness: `not_recorded`
- Cost: `unknown`
- Cleanup: `passed`
- Extracted metric fields: 109

## Conclusion

not_recorded

## Limitations

- not_recorded

## Provenance

- Snapshot: `snapshot_20260719T200907Z`
- Workspace archive SHA-256: `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source paths:
  - `run_20260710_235854_phase1_kinesis_al2023_smoke`
- Hashed evidence files: 11
