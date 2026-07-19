# Phase 4 Kinesis→Lambda→EC2 ClickHouse 실행 계획

이 문서는 `docs/guide_phase4_kinesis_lambda_clickhouse_test_draft.md`를 실행 계약으로
사용하는 living execution plan이다. 계획, 실제 명령, 관찰 결과와 판정 근거를 한곳에
유지한다. 이전 Phase 4 ClickHouse Cloud/ClickPipes 문서는 배경 자료일 뿐 이 실행의
계약이 아니다.

## Frozen experiment specification

```text
PHASE=4
EXPERIMENT_NAME=kinesis-lambda-ec2-clickhouse
HYPOTHESIS=Lambda ESM과 ClickHouse async insert가 15,000,000 records를 누락 없이 30분 안에 drain한다
RUN_ID=run_20260716_101059_phase4_clickhouse_lambda
RUN_DIR=performance-tests/run_20260716_101059_phase4_clickhouse_lambda/
SESSION_ID=phase4-clickhouse-20260716T101059Z
CANDIDATE=lambda-arm64-2048m-r7g2xl
LOAD_DRIVER=Phase 3 qualified Locust producer 원본
LOAD_DRIVER_SOURCE=performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/
LOAD_DRIVER_COMPUTE=c7g.2xlarge, Locust workers=8
PAYLOAD=performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson
PAYLOAD_SHA256=93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7
EXPECTED_LOAD=50,000 records/s x 300 seconds = 15,000,000 records
KINESIS=provisioned, 120 shards, 24-hour retention, AWS-managed encryption
CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:26.3.13.31
CLICKHOUSE_COMPUTE=r7g.2xlarge, gp3 500 GiB, 3,000 IOPS, 500 MiB/s
LAMBDA=ARM64, 2,048 MiB, timeout 30s, reserved concurrency 120
ESM=batch 10,000, window 2s, parallelization 1, TRIM_HORIZON, initially disabled
ESM_FAILURE=partial batch response, bisect false, retry 5, record age 3,600s, run S3 destination
HTTP_DEADLINE=20s
ASYNC_INSERT=1/wait=1/max_data=16777216/adaptive=1/min=50ms/max=300ms/deduplicate=0
LATE_EVENT=UTC event_date older than today-7d is metric-only; no events/raw_events row
COST_LIMIT_USD=15
COST_STOP_THRESHOLD_USD=12
AWS_WALL_CLOCK=120 minutes from deploy; unconditional cleanup begins at 100 minutes
TEARDOWN_POLICY=destroy only current run/session tagged resources; verify service-by-service inventory zero
```

The first immutable run above remains the baseline. A read-only re-entry attempt on the same
contract used the following new ownership identity and did not reuse or edit the first run:

```text
REENTRY_RUN_ID=run_20260716_114704_phase4_clickhouse_lambda
REENTRY_RUN_DIR=performance-tests/run_20260716_114704_phase4_clickhouse_lambda/
REENTRY_SESSION_ID=phase4-clickhouse-20260716T114704Z
REENTRY_RESULT=aborted before deploy; Lambda account concurrency 10 < fixed reservation 120
```

The Phase 3 implementation source files are not edited or copied into a replacement producer.
`performance-tests/phase4-clickhouse/producer-env/` supplies only a `pyproject.toml` and `uv.lock`.
All producer tests and AWS execution run through `uv sync --frozen` and `uv run --project ...` while
pointing at the qualified source directory.

## Safety gates

No later gate may compensate for an earlier failure.

1. Local source, build, unit, CDK assertion, synth and Docker integration gates all pass.
2. A shared-stack baseline/diff proves no `LoopAdDev*` deletion, replacement or update.
3. AWS identity, explicit region, ownership, quota and current public price inputs are captured.
4. Deterministic modeled maximum through verified cleanup is at most `$15`, and planned accrued
   cost before a new load is below `$12` with the `$3` cleanup reserve intact.
5. `run.json`, `infra.md` and `commands.md` exist before deploy.
6. AWS correctness smoke passes completely before the qualified producer is started.
7. The ESM remains disabled until the 15,000,000-record preload finishes.
8. Any stop condition starts cleanup immediately. No second full load is started automatically.

Stop conditions are: ownership ambiguity; root/operator policy not accepted by the preflight;
quota shortfall; projected cap breach; smoke mismatch; Lambda error/throttle/final failure;
S3 on-failure object; ESM dropped/destination failure; ClickHouse insert/restart/too-many-parts;
disk usage at least 80%; iterator age not decreasing for ten consecutive minutes; drain over
30 minutes; evidence collection failure; or the 100-minute cleanup deadline.

## Milestones and commands

### M0 — repository and contract baseline

Commands:

```bash
git status --short --branch
git rev-parse HEAD
shasum -a 256 \
  performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/{producer.py,locustfile.py,payload_contract.py} \
  performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json
aws cloudformation list-stacks --region ap-northeast-2 ...
aws ec2 describe-instances --region ap-northeast-2 ...
aws kinesis list-streams --region ap-northeast-2
aws lambda list-functions --region ap-northeast-2
```

Pass: branch/SHA and dirty baseline are recorded; qualified payload and source hashes match their
manifest; no active performance stream, Lambda or compute has ambiguous ownership; shared dev
resources are identified but untouched.

### M1 — implementation and static verification

Commands:

```bash
npm install
npm run build
npx jest --runInBand test/perf-phase4-clickhouse.test.ts test/phase4-clickhouse-handler.test.ts
CDK_DEFAULT_ACCOUNT=742711170910 LOOP_AD_REGION=ap-northeast-2 \
  npx cdk -c environment=perf-phase4-clickhouse \
  -c phase4RunId=run_20000101_000000_phase4_clickhouse_lambda \
  -c phase4SessionId=phase4-clickhouse-20000101T000000Z synth --quiet
git diff --check -- src test assets performance-tests/phase4-clickhouse docs package.json package-lock.json
```

Pass: TypeScript compiles; handler and CDK tests pass; synthesized template contains exactly the
fixed Lambda/ESM/schema/network/IAM/alarms and no plaintext secret; no NAT or public 8123 rule exists.

### M2 — qualified producer environment

Commands:

```bash
uv sync --project performance-tests/phase4-clickhouse/producer-env --frozen
uv run --project performance-tests/phase4-clickhouse/producer-env pytest -q \
  performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/tests
uv run --project performance-tests/phase4-clickhouse/producer-env python \
  performance-tests/phase4-clickhouse/verify_producer_contract.py
```

Pass: `uv.lock` is unchanged by `--frozen`; all original producer tests pass; source, payload and
manifest hashes match; AWS load entrypoint remains the original `run_stage.sh` with eight workers.

### M3 — local Docker correctness and archive fixture

Commands:

```bash
docker compose -f performance-tests/phase4-clickhouse/docker-compose.yml pull
docker compose -f performance-tests/phase4-clickhouse/docker-compose.yml up -d --wait
uv run --project performance-tests/phase4-clickhouse/producer-env python \
  performance-tests/phase4-clickhouse/local_integration.py --suite all
docker compose -f performance-tests/phase4-clickhouse/docker-compose.yml down -v
```

Pass: fixed image IDs/digests are captured; 1,000 valid records, invalid fixtures, duplicates,
retry and UTC late boundary satisfy the count invariant; 50,000 rows produce observable async
flush evidence without unbounded active parts/merges; archive equivalence passes before DROP and
again after DROP; all SDK endpoints are loopback/container-local with dummy credentials and the
network guard records zero real AWS API attempts.

### M4 — shared-stack no-replacement proof and AWS preflight

Commands:

```bash
npx cdk -c environment=dev-data synth --quiet
npx cdk -c environment=dev-data diff --fail --no-change-set LoopAdDevDataStack
uv run --project performance-tests/phase4-clickhouse/producer-env python \
  performance-tests/phase4-clickhouse/preflight.py --region ap-northeast-2 --output <RUN_DIR>/preflight.json
npx cdk -c environment=perf-phase4-clickhouse ... diff --fail --no-change-set LoopAdPerfPhase4ClickHouseStack
```

Pass: dev diff has no deletion/replacement/update caused by Phase 4; account, region, identity,
stack absence, tags, EC2/Lambda/Kinesis/EIP quotas, current prices and deterministic cost model all
pass; projected maximum is at most `$15`; deployment principal and ownership are acceptable.

### M5 — initialize evidence, deploy and correctness smoke

Before deploy create immutable `<RUN_DIR>/run.json`, `infra.md`, and `commands.md`. Then:

```bash
npx cdk -c environment=perf-phase4-clickhouse ... deploy \
  LoopAdPerfPhase4ClickHouseStack --require-approval never --outputs-file <RUN_DIR>/cdk-outputs.json
uv run --project performance-tests/phase4-clickhouse/producer-env python \
  performance-tests/phase4-clickhouse/aws_correctness_smoke.py --run-dir <RUN_DIR>
```

Pass: actual topology, image, private IP path, IAM, secret ARN-only environment and disabled ESM
match the template; smoke invariant is `Kinesis successful = events FINAL unique + raw_events +
LateEventDropped`; all missing/unexpected/final failure/dropped/on-failure/destination failure counts
are zero and iterator age returns to zero.

### M6 — qualified 15M preload and drain

Commands use the Phase 3 packager/bootstrap/`run_stage.sh` without producer code changes. The stage
contract is exactly `50k`, eight workers, 300 measurement seconds on `c7g.2xlarge`. After producer
success, enable the existing ESM UUID with `aws lambda update-event-source-mapping --enabled`, then
poll bounded metrics and ClickHouse counts for at most 30 minutes.

Pass: producer reports exactly 15,000,000 successful logical records and zero retries/failures;
normal `event_id` missing is zero; iterator age reaches zero and count completes within 30 minutes;
Lambda/ESM/S3 final failure metrics remain zero; duplicates, latency, async flush, parts/merges,
CPU/memory/network/disk and drain time are recorded.

### M7 — archive fixture, evidence and cleanup

The fixture follows `events FINAL export -> manifest -> source/S3 equivalence -> DROP -> direct S3
equivalence`. No source DROP command is issued before the first equivalence gate passes.

Cleanup commands first re-check account/region/tags/stack ownership, terminate only the current
producer instance, then destroy only `LoopAdPerfPhase4ClickHouseStack`. Verification queries
CloudFormation, EC2, Kinesis, Lambda/ESM, ENI/EIP/SG/VPC endpoints, S3 buckets/objects, log groups
and the generated secret. Pass requires current run/session inventory zero; command success alone
is insufficient.

### M8 — final result

Finalize JSON artifacts, `report.md`, this plan and conflicting Cloud/ClickPipes guides. The final
status is exactly one of `passed`, `failed`, `aborted`, or `inconclusive`. Unknown or unavailable
measurements are written as `not measured`/`pending`, never inferred.

## Progress

- [x] 2026-07-16T08:30Z — Read prompt-provided AGENTS instructions, all three source-of-truth docs,
  `event-pipeline-loadtest-runner` and applicable AWS/CDK/IAM/observability/docs skills.
- [x] 2026-07-16T08:30Z — Recorded branch `codex/aws-perf-test-plan`, starting SHA
  `eca4f09cd85a004ebd37ebc27425a4d3d39d434a`, and scoped dirty baseline.
- [x] 2026-07-16T08:30Z — Verified Phase 3 qualification status and payload/source hashes; no source
  file has been edited.
- [x] 2026-07-16T08:31Z — Read-only AWS baseline found no Kinesis stream or Phase 4 Lambda/stack and
  no active prior performance EC2/ECS resources.
- [x] 2026-07-16T09:00Z — Implemented the isolated Phase 4 stack, schema/bootstrap, handler, ESM,
  run buckets, alarms, least-privilege policies and 20 targeted Jest assertions.
- [x] 2026-07-16T09:46Z — Completed the producer contract tests and separate local correctness,
  50,000-row async flush and archive fixture suites; scoped Docker cleanup inventory is zero.
- [x] 2026-07-16T09:50Z — `npm run build`, 14 original producer tests, 11 Phase 4 Python tests,
  clean-output CDK synth and IAM Autopilot analysis passed. Final Jest rerun is recorded after the
  endpoint-policy assertion update.
- [x] 2026-07-16T10:14Z — Completed read-only AWS price, cost, ownership, offering, quota,
  bootstrap and AMI preflight. Price and modeled cost gates passed, but Lambda concurrency failed:
  account limit `10` versus frozen reservation `120`. The active root principal was also unapproved.
- [x] 2026-07-16T10:15Z — Created the aborted run evidence and verified all service-specific
  run/session cleanup inventory counts are zero. No deploy, Kinesis write, ESM enable, or delete API
  ran.
- [x] 2026-07-16T10:31Z — Re-ran build, 20 Jest tests, 20 Phase 4 Python tests, 14 original producer
  tests, producer hashes, production dependency audit, actual-run CDK synth and isolated/shared
  read-only diffs.
- [x] 2026-07-16T10:32Z — Finalized the `aborted` run report, command/failure/infra records, evidence
  manifests, conflicting Cloud/ClickPipes documentation and a second all-zero AWS inventory.
- [x] 2026-07-16T11:43Z — Re-entered the unchanged Lambda contract without overwriting the first
  run. Re-ran build, 20 Jest tests, 20 Phase 4 Python tests, 14 immutable producer tests, hash
  verification, CDK synth, and the complete fixed-image local suite.
- [x] 2026-07-16T11:44Z — Local recheck passed in 400.452148 seconds: correctness invariant
  `1005 = 1001 + 3 + 1`, 50,000-row async suite, 25-row pre/post-DROP archive equivalence, and
  zero real AWS API attempts. Compose containers, network, and volume were removed.
- [x] 2026-07-16T11:45Z — Fresh prices and deterministic cost model passed, but AWS preflight again
  observed Lambda account concurrency `10`, fixed reservation `120`, unreserved-after-deploy
  `-110`, and an unapproved root principal. No deploy or load command ran.
- [x] 2026-07-16T11:47Z — Created immutable re-entry run evidence and verified all 16 AWS
  run/session inventory categories are zero. Final re-entry status is `aborted`.

## Surprises & Discoveries

- No repository-root `AGENTS.md` exists on disk; the instructions embedded in the active request are
  the applicable repository instructions.
- The worktree contains thousands of unrelated existing changes and run artifacts. Phase 4 work must
  use scoped status/diff commands; global cleanup or formatting is unsafe.
- `run_20260716_010529_haproxy_auto` has no `run.json`. It is unrelated and ownership is ambiguous,
  so it remains untouched.
- AWS identity is `arn:aws:iam::742711170910:root`. Root use is a security risk and must pass an
  explicit preflight policy before mutation; read-only discovery alone does not authorize deploy.
- The deployed shared dev ClickHouse is currently `t4g.large`, while current code and handoff text say
  `t4g.medium`; `LoopAdDevDataStack` is `UPDATE_ROLLBACK_COMPLETE`. This drift reinforces the decision
  to create a separate stack and not treat shared dev state as Phase 4 evidence.
- AWS currently has no Kinesis streams and no active performance stacks. Only shared dev compute is
  running. Historical tagged resources returned by Resource Groups Tagging API are not proof of live
  resources; service APIs showed the old experiment compute is absent.
- LocalStack `2026.06.1` now requires a cloud license for Kinesis and exited 55 without a token.
  The test uses the pinned pre-sunset community `3.8.1` image instead; no cloud credential or
  emulator auth secret was introduced.
- Docker Desktop does not publish host loopback ports from an `internal: true` network. The compose
  network therefore uses a bridge while both host ports remain bound to `127.0.0.1`; boto before-send
  hooks reject and count every non-loopback endpoint. All passing suites recorded zero real AWS calls.
- LocalStack's bundled Kinesis process exhausted its default 2 GiB V8 heap while retaining the
  required 50,000-record preload. A container-only 4 GiB heap limit resolved it. Failed attempts and
  the final pass are retained under `evidence/local-20260716/`.
- The strict shared `LoopAdDevDataStack` diff exposes pre-existing dirty-worktree/deployed-template
  drift: Korean user-data comments appear mangled in the deployed template, so CDK says the shared
  ClickHouse and Kafka instances may be replaced. Phase 4 has no stack dependencies/imports and its
  deployment command will name only the isolated stack; the shared stack will not be deployed.
- CDK cannot assume the bootstrap lookup role with the current root session but can perform read-only
  diff with the right-account default credentials. This must be included in deployment preflight.
- The account-level Lambda concurrent executions quota is only `10`. The fixed Phase 4 reservation
  is `120`, and the preflight also requires 100 unreserved executions to remain. This is a hard
  environmental stop before deployment; changing the contract value or requesting a shared-account
  quota increase was outside this run's authority.
- AWS CLI v2's `login` credential provider is newer than pinned boto3/botocore `1.35.48`. A
  credential-process profile bridges the active CLI login without storing credentials or changing
  the qualified producer lock.
- The original Phase 3 tests must run as `python -m pytest` from their implementation directory.
  Direct `pytest` does not put those un-packaged modules on `sys.path`.
- Initial `npm audit` found a critical transitive production path under the direct Secrets Manager
  SDK. Upgrading only that exact direct dependency from `3.883.0` to fixed `3.1088.0` removed it;
  build/Jest passed again and the final production audit is zero. One moderate dev-only Jest/Istanbul
  `js-yaml` finding remains; no broad `npm audit fix` ran.
- A fresh 2026-07-16T11:45Z preflight showed that neither external blocker changed: Lambda account
  concurrency is still `10`, and the active principal is still account root. Repeating local success
  cannot authorize deploy while these gates remain false.
- The shared strict diff still marks the existing dev ClickHouse and Kafka EC2 instances as possibly
  replaced because deployed user-data contains mangled non-ASCII text. The isolated Phase 4 stack
  has no import or dependency on those stacks, and no shared deploy command ran.

## Decision Log

- 2026-07-16 — The active user request overrides the draft sentence saying AWS execution is excluded;
  the request explicitly requires guarded AWS execution through cleanup.
- 2026-07-16 — Use one run-scoped Phase 4 stack that creates its own 120-shard stream, single-AZ VPC,
  ClickHouse EC2, Lambda/ESM, generated secret, endpoints, buckets, alarms and logs. Do not import or
  mutate `LoopAdDev*` resources and do not add hidden cross-stack exports.
- 2026-07-16 — Use one public subnet with no NAT for EC2 bootstrap/SSM, but allow ClickHouse 8123 only
  from the Lambda security group. Lambda and ClickHouse are pinned to the same subnet/AZ and use the
  EC2 private address. Add a same-AZ Secrets Manager interface endpoint and S3 gateway endpoint.
- 2026-07-16 — Use a generated run secret; Lambda environment contains only its ARN. Lambda caches the
  retrieved username/password per execution environment. No agent command retrieves a secret value.
- 2026-07-16 — Publish `LateEventDropped` through CloudWatch EMF, avoiding synchronous metric API calls
  in the hot path. Enable ESM `EventCount` metrics separately.
- 2026-07-16 — Use `ReplacingMergeTree(ingested_at)` and the exact tables/settings in the contract.
  `properties_json` is validated as a string and written unchanged; it is never parsed/stringified.
- 2026-07-16 — Use a 500 GiB gp3 volume with 500 MiB/s and the fixed r7g.2xlarge candidate. This gives
  ample headroom over the 20.115 GB preload while retaining the contract's selected performance
  storage candidate; disk at 80% remains an immediate stop.
- 2026-07-16 — Put gp3 throughput in an explicit `AWS::EC2::LaunchTemplate`; CloudFormation's direct
  `AWS::EC2::Instance` block-device schema does not support throughput. The instance references only
  that launch template.
- 2026-07-16 — IAM Autopilot found runtime `secretsmanager:GetSecretValue` and a generic
  `kms:Decrypt` candidate. Keep only exact-secret `GetSecretValue`: the generated secret uses the AWS
  managed `aws/secretsmanager` key, for which AWS does not require caller `kms:Decrypt` permission.
- 2026-07-16 — Classify the run as `aborted`, not `failed`: implementation/local correctness passed,
  but an external account quota made AWS deployment invalid before the system under test existed.
  AWS correctness and throughput claims remain explicitly not measured.
- 2026-07-16 — Do not submit a Lambda quota-increase request automatically. It changes a shared
  account and cannot be assumed to complete within the bounded run. A future run must re-enter with
  a new run ID after the quota and operator gates independently pass.
- 2026-07-16 — Preserve the first aborted run and create
  `run_20260716_114704_phase4_clickhouse_lambda` for the fresh re-entry evidence. Classify it as
  `aborted` for the same external quota gate; do not reinterpret the fresh local pass as AWS proof.
- 2026-07-16 — Do not pass `--allow-root` merely because the user requested an AWS experiment.
  The repository preflight contract requires explicit root acceptance, and the quota failure would
  independently prohibit deployment even if the operator gate were waived.

## Outcomes & Retrospective

Final status: `aborted`.

The Kinesis→Lambda→EC2 ClickHouse stack, handler, schema/bootstrap, ESM, failure destination,
alarms, IAM and reproducible local harness are implemented. TypeScript build, 20 Jest tests, 20
Phase 4 Python tests, 14 unmodified producer tests, producer source/payload hashes, CDK synth and all
local correctness/async/archive gates passed. Local Docker evidence records zero real AWS calls.

The modeled two-hour maximum is `$11.769042` before cleanup and `$14.769042` including the required
`$3` cleanup reserve. AWS preflight nevertheless failed because Lambda account concurrency is `10`
and the contract requires reserved concurrency `120`; the active root operator was unapproved as a
secondary gate. Consequently no paid resource, smoke record, 15M load, ESM execution, or archive
object was created. Attributable paid resource cost is `$0.00`; control-plane request cost is not
measured. The final inventory has zero resources in all 16 checked service categories. Full evidence
and re-entry conditions are in
`performance-tests/run_20260716_101059_phase4_clickhouse_lambda/report.md`.

The read-only re-entry run at 2026-07-16T11:43Z reached the same final status: `aborted`. It
revalidated the complete local implementation and regenerated current public prices, cost, quota,
ownership, CDK diff and cleanup evidence. The account quota remained `10`; no paid resource or AWS
data-plane request was created. Its evidence is in
`performance-tests/run_20260716_114704_phase4_clickhouse_lambda/report.md`.
