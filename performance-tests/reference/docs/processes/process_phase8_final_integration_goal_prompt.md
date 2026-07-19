# Phase 8 최종 통합 승격 Goal

## Goal

Phase 7-2 안정화 캠페인의 user-authorized composite acceptance를 Phase 8 최종 통합 기준선으로
승격한다. Attempt 17의 correctness/replacement/50k score와 fresh scoped attempt의 최소
smoke/15M archive/cleanup 증거를 결합하되 두 attempt의 immutable verdict는 바꾸지 않는다.
Phase 8은 유료 AWS 실험을 반복하지 않고 source, configuration, image closure, acceptance,
cleanup과 commit을 재현 가능한 최종 handoff로 고정한다.

## Required input

- `PHASE8_HANDOFF`:
  `performance-tests/phase7_2-stabilization/phase8-handoff.json`
- Campaign ledger:
  `performance-tests/phase7_2-stabilization/attempt-ledger.json`
- Composite policy:
  `performance-tests/phase7_2-stabilization/phase8-composite-promotion-policy-20260719.json`
- The exact Attempt 17 and fresh scoped run/readiness paths referenced by the handoff. Do not
  auto-select a different or newer directory.

## Entry gate

Proceed only when all of the following are true:

- the handoff record type is `phase7-2-composite-phase8-handoff`, its self-hash, policy hash and
  pre-promotion ledger hash are valid
- the ledger hash chain is valid; Attempt 17 remains `failed`, the fresh scoped attempt is `passed`
  with `promotionEligible=false`, and neither entry or verdict was rewritten
- Attempt 17 exactly equals its immutable ledger-entry artifact; the fresh head exactly equals its
  runtime `campaign-ledger-entry.json`; the fresh source seal revalidates and binds the exact policy,
  commit, Git tree, implementation tree and image source-closure hashes
- Attempt 17's correctness 1,002 and replacement 900 passed, and its completed 300-second score has
  actual RPS `>=49,500`, 15,000,000/15,000,000 completions, zero transport/429/5xx and corrected
  p95 `<300 ms`; the known warmup one-timeout deviation remains recorded
- the fresh scoped evidence passed full service/TLS/ClickHouse minimal smoke, 15M retain-source
  archive, exactly 3×5M Parquet, immutable COMMITTED re-read, pre-DROP/committed bidirectional
  equivalence, Code 241 zero, DROP query zero and no ClickHouse restart. A runner verdict of
  `failed` is admissible only for exact Attempt 23 under
  `phase8-cleanup-recovered-amendment-20260719.json`: every functional stage must have passed, the
  only first failing gate must be the intermediate cleanup bookkeeping gate, final cleanup and
  global inventory must be zero, and the failed verdict must remain unchanged
- runtime, exact images/repositories and image stack were cleaned up
- authoritative service inventory and Tagging API residual inventory are both zero
- the active budget epoch accrued upper bound is at or below `$60` and Phase 8 paid AWS upper bound
  is exactly `$0`;
  excluded prior epochs remain preserved as lifetime history but do not block this entry gate

If any entry gate fails, do not create a Phase 8 baseline. Return to the Phase 7-2 ledger's exact
next action.

## Required work

1. Read both evidence-basis attempts, the composite handoff, policy, campaign ledger and every
   referenced hash. Preserve all Phase 7-2 attempt entries and verdicts as immutable history.
2. Verify the checkout matches the fresh archive attempt's promoted commit and implementation
   closure. Recompute the image source-closure and already-synthesized template/user-data evidence
   hashes without contacting AWS.
3. Run only focused unpaid Phase 8 entry/finalization tests. Do not repeat the full Phase 7 local
   chain, build, warmup, score or archive merely for promotion. A hash or focused-gate mismatch
   invalidates the composite entry and returns to the ledger's exact next action.
4. Create `performance-tests/phase8-final/phase8-manifest.json` containing the exact promoted commit,
   implementation tree, configuration, AMI metadata, container source/digests, both evidence-basis
   attempts, acceptance summary, cleanup proof, campaign cost and evidence-manifest hashes.
5. Create `performance-tests/phase8-final/acceptance-summary.md` with required versus measured values
   and direct relative links to immutable raw evidence. Do not copy or rewrite raw AWS evidence.
6. Create `performance-tests/phase8-final/operations-handoff.md` with the verified topology,
   deployment/readiness checks, observability signals, cleanup procedure, known limits and exact
   failure-recovery rules learned during Phase 7-2.
7. Create `performance-tests/phase8-final/failure-history.md` that lists every stabilization attempt,
   root cause, fix and verdict, including the 16,384-byte EC2 user-data deployment failure. A later
   pass does not hide earlier failures.
8. Update the nearest Phase 7/8 guide and living execution plan to point to the Phase 8 manifest as
   the final integration baseline. Avoid duplicating implementation code solely to change a phase
   number.
9. Commit Phase 8 manifest/documentation as a logical finalization commit without staging unrelated
   dirty-worktree files. Then change only campaign-level ledger state to `promoted`, bind the
   handoff/manifest hashes, update `resume.md`, and commit that status boundary separately.

## Boundaries

- Do not deploy, mutate or delete AWS resources in Phase 8 by default.
- Do not run another warmup, score or archive task. The composite Phase 7-2 evidence is the paid
  certification basis.
- A separate AWS certification rerun requires explicit user authorization and a new budget; it is
  not implied by this Goal.
- Do not rewrite Phase 7-2 evidence, attempt entries, attempt verdicts or cleanup inventory. Only the
  campaign-level `status` and promotion metadata may be appended after finalization.
- Do not expose or persist credentials, tokens or secret values.
- Preserve Phase 5 as `skipped`.
- Preserve unrelated dirty-worktree changes.

## Done when

- the Phase 8 entry gate passes from immutable Phase 7-2 evidence
- the exact promoted source hashes and focused unpaid Phase 8 gates reproduce successfully
- the Phase 8 manifest, acceptance summary, operations handoff and failure history are complete and
  internally hash-consistent
- Phase 7/8 documentation identifies this manifest as the final integration baseline
- campaign-level ledger status is `promoted` while every historical entry hash remains unchanged
- finalization is committed separately with unrelated changes untouched
