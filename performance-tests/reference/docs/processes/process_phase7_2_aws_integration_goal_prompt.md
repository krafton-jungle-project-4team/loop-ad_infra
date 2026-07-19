# Phase 7-2 AWS 통합 안정화 Goal

## Active campaign override: composite Phase 8 promotion

2026-07-19 사용자의 최신 승인에 따라 이 절은 이 문서의 상충하는 strict 재실행 요구보다
우선한다. 변경 내용은
`performance-tests/phase7_2-stabilization/phase8-composite-promotion-policy-20260719.json`에
고정한다.

- 새 50k RPS, warmup, score, correctness 1,002, replacement 900은 실행하지 않는다.
- Attempt 17의 완료된 correctness/replacement/300초 score 증거를 성능 기준으로 상속한다.
  Attempt 17의 `failed` verdict와 warmup의 1 timeout은 그대로 보존한다.
- fresh standard Phase 7 identity로 Attempt 17의 image/runtime stack definition 전체를 한 번만
  배포한다. 전용 targeted stack이나 축소 stack은 만들지 않는다.
- `verify` stage의 전체 service readiness, TLS `/health`, ClickHouse `SELECT 1`/schema 확인을
  event-load 없는 최소 smoke로 사용한다. 그 뒤 15M seed와 retain-source archive만 한 번 실행한다.
- fresh scoped attempt가 최소 smoke, 3×5M Parquet, COMMITTED 재읽기, pre-DROP/committed 양방향
  동등성, Code 241 zero, DROP query zero, source 15M 유지와 cleanup zero를 통과하면 즉시 composite
  `phase8-handoff.json`을 만든다.
- Composite handoff는 ledger self-hash 외에도 Attempt 17 immutable ledger-entry artifact, fresh
  runtime ledger-entry artifact, scoped source seal의 policy/commit/tree/image closure 결속을 검증한다.
- scoped attempt 자체의 `promotionEligible=false`는 바꾸지 않는다. composite handoff만 Attempt
  17의 성능 증거와 fresh archive 증거를 결합해 Phase 8 entry를 승인한다.
- 새 strict attempt 비용 예약은 `$0`이다. 비용 gate는 active-epoch prior + current scoped
  operational upper bound + `$5` cleanup reserve가 `$60` 이하인지로만 판정한다.
- 현재 policy가 활성인 동안 기존 strict preflight/image-prep/runner new-work CLI는 fail-closed다.
  향후 strict certification은 별도의 명시적 authorization artifact와 새 budget contract 없이는
  다시 열 수 없다.
- scoped pass 뒤 전체 Phase 7-1 local chain이나 새 handoff를 반복하지 않는다. Phase 8에서는
  hash 재검증과 focused unpaid finalization test만 수행하며 AWS를 다시 배포하지 않는다.
- archive query memory와 같은 비성능 운영 cap은 실제 working set에 충분한 headroom을 두는 safety
  envelope로 관리하고 exact point acceptance로 고정하지 않는다. 현재 값은 query/server/container
  `6/7/8 GiB`이며 query는 최소 `6 GiB`, server보다 최소 `512 MiB` 낮아야 한다. 이 envelope 안의
  합리적인 운영 튜닝은 숫자 불일치만으로 attempt를 실패시키지 않는다. 성능 기준점,
  correctness/count/fingerprint, archive equivalence, immutable COMMITTED, source DROP safety,
  ownership, authoritative cleanup zero와 budget은 계속 strict다.

## Goal

Phase 7-2는 AWS `ap-northeast-2`에서 실제 데이터 경로의 correctness, recovery, 50k ingest,
15M archive overlap, 정합성, 관측성, 비용과 cleanup을 통과할 때까지 수렴시키는 안정화
캠페인이다. 캠페인 전체에서는 실패 증거를 근거로 구현을 수정하고 새 attempt를 실행할 수 있다.

각 AWS attempt 자체는 계속 immutable이다. 같은 Run ID에서는 배포, warmup, score와 archive를
각각 한 번만 실행하고 source/context를 바꾸지 않는다. 수정이 필요하면 해당 attempt의 증거와
비용을 보존하고 cleanup inventory zero를 확인한 뒤, Attempt 17에서 사용한
`LoopAdPerfPhase7IntegrationStack`의 전체 stack definition과 topology를 유지한 fresh scoped AWS
diagnostic attempt를 우선 실행한다. diagnostic을 위해 별도 전용 CDK stack이나 축소 resource
graph를 새로 만들지 않는다. 대신 전체 stack을 fresh identity로 한 번만 배포하고 실패 구간과 그
필수 선행 stage만 실행한다. 일반 캠페인은 여러 focused 수정을 batch strict candidate로 묶지만,
현재 캠페인은 active override에 따라 fresh scoped archive pass에서 곧바로 composite Phase 8
handoff로 진행한다. 사용자의 추가 확인을 기다리느라 수렴 사이클을 중단하지 않는다.

사용자가 마지막으로 승인한 active budget epoch는 이전 비용을 admission에서 제외하고 `$60`을
상한으로 한다. active-epoch upper bound가 `$55`에 도달하면 새 유료 작업을 시작하지 않고 최소
`$5` cleanup reserve를 보존한다. 현재 composite acceptance를 통과하면 exact 구현과 결합
증거를 Phase 8 handoff로 고정하고
`docs/process_phase8_final_integration_goal_prompt.md`의 최종 승격 작업을 이어서 수행한다.

## Context

- Workspace:
  `/Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra`
- Branch: 현재 checkout을 사용하되 unrelated dirty worktree를 보존한다.
- AWS account/region/operator:
  `742711170910` / `ap-northeast-2` / 사용자가 허용한
  `arn:aws:iam::742711170910:root`
- Source of truth:
  - `docs/guide_phase7_end_to_end_integration_test.md`
  - `performance-tests/phase7-integration/README.md`
  - `performance-tests/phase7-integration/exec-plan.md`
  - `docs/process_phase8_final_integration_goal_prompt.md`
- Historical failed attempt, read-only and never reusable:
  - local handoff:
    `performance-tests/run_20260717_224217_phase7_1_local_integration/local-handoff.json`
  - readiness:
    `performance-tests/run_20260717_225316_phase7_2_deployment_readiness/`
  - AWS attempt:
    `performance-tests/run_20260717_225316_phase7_2_aws_integration/`
  - Run ID: `run_20260717_225316_phase7_integration`
  - Session ID: `phase7-integration-20260717T225316Z`
  - frozen implementation tree:
    `acc8553c34e588e05de8333bf904751d61652ac43a581bbf827b5a12cfd8b2df`
  - deployment failure:
    `LoadGeneratorHostsLaunchTemplate` user data exceeded the EC2 decoded 16,384-byte limit
    (`InvalidUserData.Malformed`).
- Resumable campaign state:
  - `performance-tests/phase7_2-stabilization/attempt-ledger.json`
  - `performance-tests/phase7_2-stabilization/resume.md`
  - `performance-tests/phase7_2-stabilization/phase8-handoff.json` after composite acceptance
- Every future AWS attempt generates a fresh Run ID, Session ID, readiness directory, runtime or
  diagnostic directory and exact run-owned resources. A strict full attempt also requires the fresh
  batched local handoff. Scoped diagnostic attempts use the existing full Phase 7 integration stack,
  a sealed stage plan and fresh AWS identities. The dedicated archive-diagnostic stack and its
  Attempts 18-19 remain read-only historical evidence and are not deployed again. Historical paths
  and identifiers are inputs for diagnosis only.
- Phase 5 remains `skipped`, never `passed`.

## Required work

1. Read every source-of-truth document and the stabilization ledger before mutation. Inspect current
   Git/AWS state without overwriting any historical handoff, readiness directory or AWS attempt.
2. Finish or verify cleanup for `run_20260717_225316_phase7_integration` first. Do not begin a new
   attempt until authoritative service inventory and Tagging API residuals are both zero.
3. Initialize or update the campaign ledger with that failed attempt, its exact failure, deploy count,
   elapsed time, accrued upper bound, cleanup result, evidence paths and immutable input hashes.
4. Fix the observed `InvalidUserData.Malformed` root cause before another AWS deploy. Add a synth-time
   regression test that decodes every `AWS::EC2::LaunchTemplate` `UserData` value and fails above
   16,384 bytes. Keep the load-generator user data at or below 15,360 decoded bytes to retain margin.
   Do not merely suppress the validation or rely on CloudFormation to catch the limit.
5. For each evidence-backed fix, run the narrow regression tests, build or type-check needed by the
   changed surface, exact-context synth, repository template validation and cfn-lint needed to make
   the scoped full-stack deployment safe. Do not run the complete Phase 7-1 chain after every focused fix.
   Record every deferred full-local obligation in the campaign issue register. Under the active
   override, a scoped archive pass supersedes the batched whole-local and strict rerun requirement;
   Phase 8 performs only focused unpaid entry/finalization checks.
6. Before the first AWS cycle in a session, and again after credential expiry, run `aws login`.
   Verify the exact account, region and root ARN through AWS CLI and locked boto3. Never print,
   persist or pass static credentials to a workload.
7. For every AWS retry, including a scoped diagnostic, create a new Run ID, Session ID, readiness
   directory, attempt directory and exact run-owned resource set. Never reuse or overwrite an
   earlier attempt. Local build cache may be used, but every image actually used by that diagnostic
   must have its source-closure hash, platform and pushed digest reverified.
8. Refresh public prices, quota, offerings, bootstrap, ownership and resource inventory. Recompute
   the deterministic cost model with the campaign's prior accrued upper bound and pass fresh absent
   and prepared preflights for the new handoff and images.
9. Require `passed=true`, exact implementation/image hashes, an absent runtime stack, exactly owned
   image resources, no unexpected run-owned resource and sufficient remaining campaign budget.
10. Initialize and seal the new immutable attempt. Deploy the runtime stack once for that Run ID.
    A failed deploy or readiness gate ends only that attempt: preserve evidence, clean it to zero,
    update the ledger, make the smallest evidence-backed fix and start another fresh attempt while
    the campaign budget permits.
11. When deployment succeeds, verify actual CloudFormation outputs, tags, VPC/routes/endpoints/
    security groups, host counts/types/AMIs, ECS services/tasks/capacity providers, Kinesis 120
    shards, DynamoDB, ClickHouse volume/memory, buckets, log groups, roles and exact image digests.
12. Do not rerun HTTP correctness 1,002 or replacement 900. Verify and hash-bind Attempt 17's passed
    immutable correctness/replacement evidence in the composite handoff.
13. Seed the closed UTC today-8 partition with 15,000,000 rows, wait for quiescence and record the
    deterministic fingerprint before archive work.
14. Do not run a new warmup, score or 50k workload. Verify and hash-bind Attempt 17's completed
    15,000,000-request score evidence (`49,987.713711...` actual RPS, zero transport/429/5xx,
    corrected p95 `112.5391715 ms`) while preserving its attempt verdict as `failed`.
15. Do not repeat score drain/accounting. Treat Attempt 17 only as inherited performance evidence;
    the fresh attempt supplies minimal smoke and archive acceptance.
16. Validate exactly three 5,000,000-row Parquet objects and pre-DROP/committed/post-DROP
    bidirectional equivalence. Never execute source DROP unless immutable `COMMITTED`, re-read and
    every pre-DROP safety check pass exactly.
17. Collect HAProxy, collector, Kinesis, KCL, ECS/EC2, ClickHouse, archive, CloudWatch, CloudTrail,
    cost and deadline evidence. Always delete runtime, exact images/repositories and image stack in
    that order, then prove authoritative inventory zero.
18. Once the fresh scoped archive attempt passes and cleans to zero, write the composite
    `phase8-handoff.json`, freeze Attempt 17 plus fresh attempt evidence hashes, and immediately
    execute the Phase 8 finalization Goal without another paid AWS experiment.

## Stabilization cycle and resume contract

`performance-tests/phase7_2-stabilization/attempt-ledger.json` is the durable campaign control
record. Update it after every terminal deploy, readiness, correctness, load or cleanup outcome. Each
attempt entry must contain at least:

- ordinal, Run ID, Session ID, attempt type and immutable evidence paths
- git commit, implementation tree, local handoff and image source/digest hashes
- sealed command-set hash and exact per-stage attempt counts
- first failing gate, raw AWS error, diagnosis, fix applied and fix commit
- measured and upper-bound cost, paid start/end and cleanup deadline
- cleanup attempts, final authoritative inventory and attempt verdict
- `previousEntrySha256` so later sessions can detect ledger truncation or rewriting

`resume.md` must always state the last completed action, current AWS inventory, cumulative cost upper
bound, remaining budget, unresolved hypothesis, exact next safe command and whether new AWS work is
currently authorized. A later task resumes from these two files and revalidates live state; it does
not infer progress from chat history.

One attempt may deploy only once. The campaign may run multiple fresh attempts. After a failed
attempt reaches authoritative cleanup zero, evidence-backed source/CDK/tooling changes and the next
fresh deployment are authorized without another user confirmation while all boundaries and the
campaign cost gate remain satisfied.

## Full-stack scoped AWS diagnostic loop

Use this loop by default after a strict or diagnostic attempt exposes an implementation, CDK,
capacity or runtime defect. Its purpose is to replace repeated whole-local runs with faster evidence
from the real AWS execution environment without introducing a second diagnostic-only infrastructure
implementation.

1. Append or update one item in
   `performance-tests/phase7_2-stabilization/issue-register.json`. Record the source attempt, first
   failing gate, raw evidence hashes, observed symptom, diagnosis and confidence, exact changed
   files/configuration, focused regression, deferred whole-local obligation and proposed AWS probe.
2. Apply only the evidence-backed fix after the source attempt has authoritative cleanup zero.
3. Run focused local tests and static deployment gates for the changed surface. A whole Phase 7-1
   run is not required at this point unless the change cannot be bounded or the focused gates fail
   to establish deployment safety.
4. Create a fresh `aws-full-stack-scoped-diagnostic` attempt with its own standard Phase 7 Run ID,
   Session ID, evidence directory, sealed command set, cost model and cleanup deadline. Set
   `promotionEligible=false`.
5. Deploy `LoopAdPerfPhase7IntegrationImageStack` and `LoopAdPerfPhase7IntegrationStack` using the
   same logical topology as Attempt 17. Do not author, synthesize or deploy a dedicated diagnostic
   stack or a reduced resource graph. Record the full-stack resources as intentionally unavoidable
   for this diagnostic and charge them to its deterministic cost model.
6. Run only the declared failure path and its prerequisites once. For the current archive issue the
   fixed plan is `deploy -> verify -> seed 15M -> retain-source archive -> committed/pre-DROP
   equivalence -> collect -> cleanup`; correctness, replacement, warmup and score run zero times.
   Never execute source DROP in a scoped diagnostic.
7. Preserve actual configuration, metrics, raw errors, expected-versus-observed decision, measured
   and upper-bound cost, and authoritative cleanup zero. Update the issue item with `passed`,
   `failed`, `blocked` or `inconclusive` and the next hypothesis.
8. Normally a scoped diagnostic is not promotable. Under the active composite override, a passing
   scoped archive attempt may supply only the archive/minimal-smoke half of `phase8-handoff.json`;
   it never becomes a strict pass itself.
9. Continue focused archive fix/diagnostic cycles while the active budget gate fits. On the first
   pass and cleanup zero, stop paid AWS work and create the composite Phase 8 handoff. Do not create
   a fresh strict attempt.

Use a monolithic strict-candidate fast path when all of the following are true: the production CDK
cannot isolate the affected graph without a refactor, the related fixes are already covered by one
passing batched Phase 7-1 handoff, a fresh strict full-attempt cost gate fits, and an extra diagnostic
deployment would materially increase time or consume the remaining retry budget. Declare that
attempt as strict and promotion-eligible before deployment. Run correctness and recovery first, then
evaluate the known failed load/archive gates at their normal earliest positions. If they pass,
continue the same immutable attempt through drain, equivalence, observability and cleanup; do not
redeploy. If any gate fails, the attempt remains failed and enters cleanup. Never relabel an attempt
from diagnostic to strict after deployment, skip a strict stage, or execute a stage twice.

An issue register item is not deleted after resolution. It retains every failed probe, implementation
commit, focused check, AWS result and the local batch/handoff that eventually covered it. This makes
deferred local work discoverable even after the campaign or task is resumed later.

## Campaign cost contract

- The active budget epoch hard cap for Phase 7-2 stabilization work is `$60.00`. Earlier epochs and
  lifetime cost remain visible but are excluded from active admission by explicit user authorization.
- Preserve a `$5.00` cleanup reserve. At an active-epoch accrued/upper bound of `$55.00`, start no new
  image build, deployment, seed, warmup, score or archive work.
- Before each scoped archive attempt require:
  `active-epoch prior upper bound + new attempt operational upper bound + $5 cleanup reserve <= $60`.
- Reserve `$0` for a future strict/50k attempt under the active composite override.
- Preserve historical attempt cost in the ledger but do not charge it to the current active epoch.
- Reconcile modeled and observed cost after every attempt. Delayed billing is covered by the upper
  bound; it is not treated as zero.
- The 160-minute cleanup-start and 180-minute hard deadlines remain per attempt. The campaign may be
  resumed in a later task or session. A budget epoch changes only after explicit user authorization;
  ordinary retries never reset it. An authorized reset never rewrites terminal attempts: close the
  previous epoch, append a new epoch and apply `$0` only from the exact next paid boundary recorded
  in the ledger.

## Diagnostic continuation policy

An acceptance failure does not automatically mean execution is impossible. Do not ask for approval
or stop only because a non-safety measurement differs from the contract. Record the deviation,
identify the likely bottleneck and continue every remaining safe stage that can still produce useful
evidence within the cost and deadline.

- The inherited Attempt 17 throughput evidence remains actual RPS `>= 49,500`; no new score is
  executed under the active override.
- The explicit diagnostic continuation floor is 70% of requested throughput:
  `50,000 x 0.70 = 35,000 actual RPS`.
- Aggregate scored throughput from `35,000` through `49,499.999... RPS` must continue through drain,
  archive validation, evidence collection and cleanup if no hard stop exists. The final verdict is
  still `failed`, not `passed`.
- A transient instantaneous sample below 35,000 RPS is not enough to stop. Use aggregate progress,
  backlog trend, component health and the remaining cost/deadline to decide whether execution is
  genuinely impossible.
- Aggregate throughput below 35,000 RPS triggers immediate diagnosis. If the pipeline is still
  making forward progress and the current attempt can finish safely within the hard limits, finish
  the single diagnostic attempt without retrying or changing the deployment. If forward progress
  has stopped or completion within the limits is no longer possible, collect available evidence,
  clean up and stop.
- Corrected p95 `>= 300 ms`, nonzero transport/429/5xx, CPU or memory p95 `>= 70%`, filesystem peak
  `>= 80%`, or an unexpected restart makes the acceptance verdict fail but is not by itself an
  immediate stop. Continue only while services recover or remain available, accounting stays
  trustworthy and no hard stop develops.
- Do not weaken or rewrite the contract after observing results. A diagnostic continuation can
  increase evidence coverage; it cannot turn a failed gate into a pass.
- Do not add a second warmup, score, archive or deployment under the same Run ID. After authoritative
  cleanup zero, a source/configuration fix and another fresh Run ID are the normal stabilization
  path and do not require another user confirmation.

## Hard stops and impossible-to-continue conditions

Stop new work in the current attempt, preserve evidence and enter cleanup immediately when any of
the following is true:

- AWS login, exact identity, region, handoff/source/schema/image hash, ownership, quota, offering,
  bootstrap, fresh price or cost preflight fails.
- `cdk diff` or actual deployment shows a replacement, shared/dev/prod/DNS mutation, non-run-owned
  resource adoption, unexpected scope/count drift or credential exposure.
- Runtime cannot reach a trustworthy ready state without changing source/configuration. Never
  redeploy that run; record the failure and fix it between attempts.
- Correctness 1,002, replacement accounting, final ACK/count identity or deterministic event sample
  is inconsistent before heavy load.
- KCL has a terminal failure, ClickHouse cannot accept/verify data, data loss is suspected, or stage
  sequencing/evidence is no longer trustworthy.
- Archive cannot produce and re-read immutable `COMMITTED`, pre-DROP equivalence is nonzero, or
  source deletion safety cannot be proven. In this case source DROP is forbidden.
- The active-epoch accrued/upper bound reaches the `$55` new-work stop, the active total reaches the
  `$60` hard cap, paid time for the current attempt reaches 160 minutes, or its 180-minute hard
  deadline cannot be met.
- The pipeline stops making forward progress and the remaining required stage cannot finish within
  the cost/deadline, even after bounded observation and safe recovery checks.

Hard stop does not authorize abandoning resources. Disable new traffic/archive starts, capture the
failure and run exact cleanup. Once authoritative inventory is zero, update the ledger and continue
with the next evidence-backed fix and fresh attempt if the campaign cost gate passes. Stop the whole
campaign only when the `$60` active cap leaves insufficient cleanup reserve, the next required
diagnostic or full attempt cannot
fit, or cleanup/identity/permissions/external AWS state creates a genuine blocker. In that case write
the exact residual inventory and continuation command to `resume.md`.

## Output

Use the immutable runtime run directory and preserve at least:

- `run.json`, `commands.md`, `infra.md`, `failures.md`, `report.md`
- fresh price/cost/preflight and runner stage-gate evidence
- deployment outputs, stack/resource inventory and ownership tags
- `correctness-summary.json`, replacement evidence and count accounting
- warmup/score manifests, worker summaries and exact load timestamps
- `metrics-summary.json`, HAProxy/collector/Kinesis/KCL/ECS/ClickHouse evidence
- `archive-validation.json`, object/checksum/fingerprint and DROP safety evidence
- CloudWatch/CloudTrail evidence and accrued/upper-bound cost
- `cleanup-verification.json` with authoritative service-class inventory
- `improvement-backlog.md` containing observed bottlenecks, deviations, likely causes, confidence,
  and the next experiment or implementation change in priority order
- scoped full-stack diagnostic manifest, expected/actual decision and unavoidable-resource record
- campaign-level `issue-register.json` with deferred whole-local obligations and resolution history
- campaign-level `attempt-ledger.json` and `resume.md`
- after composite acceptance, `phase8-handoff.json` containing Attempt 17 performance/correctness
  and fresh scoped source, image, archive, cleanup and commit hashes

Every attempt report must state exactly one verdict: `passed`, `failed`, `blocked` or `inconclusive`.
The campaign ledger separately records `stabilizing`, `promoted`, `budget-exhausted` or `blocked`.
Failed attempts remain visible after a later pass; promotion never rewrites their verdicts.

## Boundaries

- Modify, deploy, query and delete only resources with the exact run/session ownership contract.
- Never modify or delete dev, prod, shared infrastructure, shared ECR images, certificates or DNS.
- Never store secrets, authorization tokens, static keys or secret values in Git, logs, commands or
  model-visible evidence.
- Never reuse an immutable run directory, overwrite evidence, silently resume a failed stage or
  hide a failed measurement.
- Never edit implementation or CDK source inside an active attempt. Evidence-backed fixes are
  explicitly allowed after that attempt is cleaned to zero and before a fresh run chain starts.
- Do not pause for routine approval between stabilization attempts when ownership, cleanup, identity,
  deadlines and active-epoch `$60` cost gates all pass.
- Never execute source DROP on partial, sampled, inferred or eventually consistent evidence.
- Keep Phase 5 recorded as `skipped`.
- Preserve unrelated dirty-worktree changes. Do not stash, reset, discard or commit them.

## Done when

- The historical failed deployment and every later attempt are present in the hash-linked ledger.
- The EC2 decoded user-data byte regression gate passes before the next AWS deployment.
- Every attempt used fresh identities/directories, one deploy maximum and fresh preflight/cost gates.
- Attempt 17 remains `failed` while its completed correctness/replacement/50k score evidence passes
  the inherited checks, and one fresh scoped attempt passes minimal smoke, 15M archive, equivalence,
  source retention and cleanup. Neither attempt verdict is rewritten.
- Runtime, exact images/repositories and image stack are cleaned up and authoritative service and
  Tagging API inventories are zero after every attempt.
- Active-epoch upper-bound cost plus cleanup reserve remains at or below `$60`; all excluded prior
  epochs remain visible in lifetime accounting.
- Every issue is retained in `issue-register.json` with focused checks and its declared AWS
  diagnostic decision; deferred whole-local work is marked superseded by the active override.
- composite `phase8-handoff.json` freezes Attempt 17 performance/correctness hashes and the fresh
  scoped archive source/evidence/commit hashes, and the Phase 8
  finalization Goal completes without repeating the paid AWS experiment by default.
- Implementation fixes, AWS raw evidence, ledger/status and Phase 8 finalization are committed at
  logical boundaries, with unrelated worktree changes untouched.
