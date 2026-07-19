# Phase 8 acceptance summary

This baseline combines immutable Attempt 17 performance evidence with Attempt 23's fresh
minimal-smoke/archive evidence. Attempt 17 and Attempt 23 both remain `failed`; no historical
verdict is rewritten. Attempt 23 is accepted only as cleanup-recovered composite evidence because
all functional stages passed and final authoritative/global cleanup inventories are zero.

| Gate | Required | Measured | Result | Evidence |
|---|---:|---:|---|---|
| Correctness | 1,002 | 1,002 | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/correctness-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/correctness-summary.json`) |
| Consumer replacement | 900 | 900 | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/correctness-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/correctness-summary.json`) |
| Scored completions | 15,000,000 | 15,000,000 | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json`) |
| Actual scored RPS | >=49,500 | 49987.713711 | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json`) |
| Transport / 429 / 5xx | 0 / 0 / 0 | 0 / 0 / 0 | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json`) |
| Corrected p95 | <300 ms | 112.539171 ms | passed | performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json (external snapshot reference: `../../performance-tests/run_20260719_043415_phase7_2_aws_integration/score-partial-summary.json`) |
| Fresh minimal smoke | all services + TLS + ClickHouse | passed | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/deployment-verification.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/deployment-verification.json`) |
| Fresh archive rows | 15,000,000 | 15,000,000 | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json`) |
| Parquet objects | 3 x 5,000,000 | 3 x 5,000,000 | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json`) |
| COMMITTED/equivalence | immutable re-read, all differences 0 | all checks true | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json`) |
| Source retention | 15,000,000 rows, no DROP | 15,000,000 rows, no DROP | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json`) |
| Query failures | Code 241 = 0 | 0 | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/archive-validation.json`) |
| Final cleanup | 35 service classes 0, tag residuals 0 | 0 / 0 | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/cleanup-recovery-attempt-2-verification.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/cleanup-recovery-attempt-2-verification.json`) |
| Global Phase 7 run-owned tags | 0 | 0 | passed | performance-tests/run_20260719_164311_phase7_2_aws_integration/post-cleanup-global-inventory.json (external snapshot reference: `../../performance-tests/run_20260719_164311_phase7_2_aws_integration/post-cleanup-global-inventory.json`) |
| Active cost epoch | <=$60 | $11.227766 | passed | [performance-tests/phase7_2-stabilization/attempt-ledger.json](../phase7_2-stabilization/attempt-ledger.json) |
| Phase 8 paid AWS work | $0 | $0 | passed | [performance-tests/phase7_2-stabilization/phase8-handoff.json](../phase7_2-stabilization/phase8-handoff.json) |

Known deviations remain visible: Attempt 17 warmup had one timeout, and Attempt 23's first cleanup
inventory command observed an immutable stopped-task tag tombstone before the second recovery reached
zero. Neither deviation is rewritten into a strict single-attempt pass. Phase 5 remains `skipped`.
