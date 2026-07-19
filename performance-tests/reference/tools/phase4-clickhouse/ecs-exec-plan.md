# Phase 4 native Java KCL→ClickHouse 실행 계획

이 문서는 Phase 4의 living execution plan이다. 순서는 `local proof -> fresh AWS preflight ->
deploy -> correctness -> failover -> measured load -> evidence -> cleanup`으로 고정한다. 앞 단계
실패를 뒤 단계 성공으로 상쇄하지 않는다.

이전 MultiLangDaemon/Node candidate A는 2026-07-16 full-load warmup에서 두 2 GiB task가
`java.lang.OutOfMemoryError: Java heap space`로 반복 종료되어 `failed(capacity)`로 끝났다.
correctness와 60/60 lease, task replacement는 통과했지만 15M 측정과 archive 결과는 없다.
그 run은 cleanup inventory 0으로 종료됐으며 새 후보의 배포 승인이나 성능 증거로 재사용하지
않는다.

## Frozen experiment specification

```text
PHASE=4
EXPERIMENT_NAME=kinesis-native-java-kcl-ecs-ec2-clickhouse
HYPOTHESIS=2 x native-Java KCL tasks at 1-vCPU/2-GiB sustain 50,000 records/s without loss or OOM
RUN_ID=assign only after every local gate passes
RUN_DIR=performance-tests/run_<UTC>_phase4_clickhouse_ecs/
SESSION_ID=phase4-clickhouse-ecs-<UTC>
CANDIDATE=native-java-kcl-3.4.3-c7g-large-2x-task-1vcpu-2g
FALLBACK_CANDIDATE=none; a different resource profile requires a new user decision and new goal
LOAD_DRIVER=unmodified Phase 3 qualified Locust producer
LOAD_DRIVER_COMPUTE=c7g.2xlarge, Locust workers=8
PAYLOAD=performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson
PAYLOAD_SHA256=93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7
PAYLOAD_MAX_BYTES=1518
EXPECTED_LOAD=50,000 records/s x 300 seconds = 15,000,000 records
KINESIS=provisioned, 120 shards, 24-hour retention, AWS-managed encryption
KCL=3.4.3 native Java ShardRecordProcessor, polling, LATEST
KCL_MAX_RECORDS=1000
KCL_MAX_PENDING_PROCESS_RECORDS_INPUT=0
KCL_MAX_LEASES_PER_WORKER=60
KCL_POLL_IDLE_MS=200
KCL_GRACEFUL_LEASE_HANDOFF_MS=120000
CLICKHOUSE_BATCH_CONCURRENCY_PER_TASK=10
KCL_METADATA=run-owned lease, worker-metrics and coordinator-state DynamoDB tables, on-demand
ECS_SERVICE=EC2 capacity provider, ARM64, desired=2, distinctInstance, no autoscaling
ECS_ASG=c7g.large, min=2, desired=2, max=2, On-Demand, one AZ
ECS_TASK=1 vCPU, 2 GiB, one task per host, stopTimeout=120 seconds
JVM=G1, InitialRAMPercentage=20, MaxRAMPercentage=65, MaxDirectMemorySize=256m, Xss=256k
CLICKHOUSE_IMAGE=clickhouse/clickhouse-server@sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797
CLICKHOUSE_COMPUTE=r7g.2xlarge, gp3 500 GiB, 3,000 IOPS, 500 MiB/s
HTTP_DEADLINE=20 seconds
RETRY=exponential jitter, maximum 5 attempts and 60 seconds total
ASYNC_INSERT=1/wait=1/max_data=16777216/adaptive=1/min=50ms/max=300ms/deduplicate=0
COST_LIMIT_USD=20
COST_STOP_THRESHOLD_USD=17
CLEANUP_RESERVE_USD=3
AWS_WALL_CLOCK=120 minutes from first support-stack deploy; cleanup begins by minute 100
TEARDOWN=destroy only current run/session resources and verify service-by-service inventory zero
```

Java KCL 구현은 AWS 공식 Java consumer callback 계약을 따른다.

- `processRecords`: ClickHouse success 또는 terminal S3 archive success 이후에만 checkpoint
- `leaseLost`: checkpoint 금지
- `shardEnded`: 반드시 checkpoint
- `shutdownRequested`: 마지막 성공 위치 checkpoint
- graceful handoff timeout: 120초

## Gate summary

| Gate | Pass condition | Failure action |
|---|---|---|
| Java unit/static | Maven tests, banned multilang dependency, fixed query/checkpoint tests pass | stop |
| Container identity | ARM64; Java present; Node/socat/MLD absent | stop |
| Local resource gate | exact 1 CPU/2 GiB/no swap; cgroup peak <70% | stop, no AWS |
| Local processing gate | 60 x 1,000 x 1,518 B envelope; two-task equivalent >=50k/s | stop, no AWS |
| AWS preflight | identity, region, ownership, quota, price, diff, cost pass | abort before deploy |
| Readiness | 2 tasks, distinct hosts, 1 vCPU/2 GiB, lease 60/60 | cleanup |
| Correctness | 1,002 inputs accounted exactly, missing=0 | cleanup |
| Failover | one replacement, running=desired=2, resume missing=0 | cleanup |
| Full load | 15M success, no OOM/restart/read throttle, drain <=30m | record verdict |
| Cleanup | every owned inventory count zero | block another run |

로컬 sink는 실제 ClickHouse 성능을 대신하지 않는다. 로컬 gate는 bounded Java object/NDJSON/
HTTP-retention envelope와 CPU 처리율을 검사한다. 실제 Kinesis/ClickHouse 처리량은 AWS M6~M7만
판정한다.

## M0 — baseline and ownership

AWS 호출 없이 다음을 기록한다.

```bash
git status --short --branch
git rev-parse HEAD
shasum -a 256 performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson
shasum -a 256 performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/producer.py
git status --short -- docs performance-tests/phase4-clickhouse src test package.json
```

Pass: branch, starting SHA, scoped dirty baseline과 producer/payload hash가 기록된다. 기존 run
directory와 사용자 변경은 수정하지 않는다.

## M1 — native Java implementation and unit proof

구현 범위:

- Java 21 fat JAR와 native `ShardRecordProcessor`
- Java transformation, fixed ClickHouse JSONEachRow writer, bounded retry와 concurrency
- S3 terminal archive, EMF host-memory metric, graceful KCL shutdown
- pinned KCL 3.4.3 core dependency; multilang dependency 금지
- Java-only runtime image와 ECS readiness file health check
- CDK task env에서 socket/Node contract 제거

Commands:

```bash
mvn --batch-mode --no-transfer-progress -f performance-tests/phase4-clickhouse/consumer/pom.xml clean verify
npm run build
npm run test:phase4 -- --runInBand
UV_CACHE_DIR=/tmp/loopad-phase4-uv-cache uv run --project performance-tests/phase4-clickhouse/producer-env \
  python -m pytest -q performance-tests/phase4-clickhouse/tests/test_cost_model_ecs.py
```

Pass: Java transformation, ClickHouse request shape, retry/archive ordering, checkpoint callbacks,
polling/lease configuration tests pass. TypeScript build와 CDK assertions pass. Dependency tree에
`amazon-kinesis-client-multilang`이 없다.

## M2 — mandatory 2 GiB Docker gate

Command:

```bash
performance-tests/phase4-clickhouse/run-java-memory-gate.sh \
  performance-tests/phase4-clickhouse/java-memory-gate-result.json
```

스크립트는 ARM64 image를 build한 뒤 다음을 강제한다.

```text
--network none
--cpus 1
--memory 2g
--memory-swap 2g
60 shards/task
1,000 records/shard
1,518 bytes/record
6 waves
10 concurrent batches/task
100 ms simulated ClickHouse retention
```

Pass:

- image에 `java`가 있고 `node`, `socat`이 없다.
- cgroup limit가 정확히 2,147,483,648 B다.
- `peakCgroupPercent < 70`이다.
- `twoTaskFleetEquivalentRecordsPerSecond >= 50000`이다.
- 결과 JSON status가 `passed`다.

실패 시 AWS login, run directory 생성, deploy를 하지 않는다. `maxRecords`, concurrency,
JVM 또는 task size를 같은 run에서 변경하지 않는다.

2026-07-17 최신 실측 결과는
[`java-memory-gate-result.json`](java-memory-gate-result.json)에 보존한다. 조건을 바꾸지 않은
고정 warm-up 2회 뒤 6회 측정에서 task당 63,068 records/s, 두 task 환산
126,136 records/s였고 peak cgroup은 1,083,219,968 B(2 GiB의 50.44%)였다. warm-up 없는
첫 표본은 JIT와 class loading을 측정 구간에 포함해 32,303 records/s로 실패했기 때문에,
실제 AWS 실행의 별도 warm-up 계약과 맞춰 측정 경계를 고정했다. 50k 요구치와 memory 한도는
바꾸지 않았으며 AWS M7에서만 실제 50k/s 용량을 판정한다.

## M3 — producer and local repository gates

```bash
uv sync --project performance-tests/phase4-clickhouse/producer-env --frozen
PYTHONPATH=performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation \
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python -m pytest -q \
  performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/tests
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/verify_producer_contract.py
git diff --check -- docs performance-tests/phase4-clickhouse src test package.json
```

Pass: producer source와 payload hash가 고정되고 lockfile이 바뀌지 않는다. build output, secret,
token과 private key가 diff에 없다.

## M4 — fresh AWS preflight

M0~M3가 모두 pass일 때만 새 UTC run/session을 만들고 `run.json`, `commands.md`, `infra.md`,
local gate result와 pre-deploy inventory를 먼저 기록한다. 그 다음 AWS login/session을 확인한다.

```bash
aws sts get-caller-identity --output json
aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A \
  --region ap-northeast-2 --output json
aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters Name=location,Values=ap-northeast-2a --region ap-northeast-2 --output json
node performance-tests/phase4-clickhouse/lookup_prices_ecs.mjs \
  --output <RUN_DIR>/prices-ecs.json
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/cost_model_ecs.py \
  --prices <RUN_DIR>/prices-ecs.json --output <RUN_DIR>/cost-model-ecs.json
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/preflight_ecs.py \
  --region ap-northeast-2 --run-dir <RUN_DIR>
npx cdk <ALL_CONTEXT> diff --no-change-set \
  LoopAdPerfPhase4ClickHouseEcsImageStack LoopAdPerfPhase4ClickHouseEcsStack
```

Pass: account/region/operator가 명시되고 run stacks가 없으며 ownership, quota, offering,
network/storage/ECR/DynamoDB/Kinesis limits와 additions-only diff가 통과한다. current price로
`operationalMaximumUsd < 17`, `maximumIncludingCleanupUsd <= 20`, reserve `$3`가 모두 true다.
2026-07-16 price 문서의 `$14.634519`은 model regression evidence일 뿐 새 배포 승인이 아니다.

## M5 — deploy and readiness

첫 support-stack deploy 시각부터 paid wall clock을 시작한다.

```bash
npx cdk <ALL_CONTEXT> deploy LoopAdPerfPhase4ClickHouseEcsImageStack \
  --require-approval never --outputs-file <RUN_DIR>/image-stack-outputs.json
aws ecr get-login-password --region ap-northeast-2 | \
  docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.ap-northeast-2.amazonaws.com
docker tag loopad-phase4-native-java-kcl:memory-gate <REPOSITORY_URI>:candidate
docker push <REPOSITORY_URI>:candidate
aws ecr describe-images --repository-name <REPOSITORY> --image-ids imageTag=candidate \
  --region ap-northeast-2 --output json
npx cdk <ALL_CONTEXT_WITH_REGISTRY_DIGEST> deploy LoopAdPerfPhase4ClickHouseEcsStack \
  --require-approval never --outputs-file <RUN_DIR>/cdk-outputs.json
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/prepare_producer_ecs.py --run-dir <RUN_DIR>
```

Pass: pushed ECR digest와 task definition digest가 같다. task는 정확히 두 개, 서로 다른
`c7g.large`, 각각 1 vCPU/2 GiB다. KCL owner는 정확히 60/60이고 total leases는 120이다.
Node/MLD 프로세스가 없으며 ClickHouse 연결은 private 8123이다.

## M6 — AWS correctness and replacement

```bash
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/aws_correctness_smoke_ecs.py --run-dir <RUN_DIR>
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/verify_ecs_recovery.py \
  --run-dir <RUN_DIR> --timeout-seconds 600
```

Pass: 1,000 normal + invalid 1 + late 1이
`input = events FINAL unique + raw_events + LateEventDropped`를 만족하고 missing=0이다. task
하나를 중단한 뒤 running=desired=2, lease 재분배, all fault records present, iterator age
zero-bucket을 10분 내 만족한다. terminal archive와 checkpoint error는 0이다.

## M7 — measured 15M load

시작 직전에 cost, minute, task 2, lease 60/60, iterator age 0, ClickHouse health를 다시
검사한다. 본 부하 중 deploy, tuning, fault injection과 autoscaling을 금지한다.

```bash
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/run_full_load_ecs.py \
  --run-dir <RUN_DIR> --timeout-seconds 900
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/evaluate_full_load_ecs.py \
  --run-dir <RUN_DIR> --drain-timeout-seconds 1800
```

15초마다 producer, Kinesis incoming/read throttle/iterator age, KCL process/checkpoint/lease,
ECS task restart/CPU/memory, host CPU/memory/network, JVM OOM/GC, ClickHouse count/parts/merge/disk,
S3 failure objects, wall clock과 deterministic accrued cost를 기록한다.

Pass: producer success 15,000,000, failure 0, missing 0, terminal/checkpoint failure 0, OOM/restart
0, read throttle 0이다. task CPU/memory p95는 각각 70% 미만이다. 30분 내 iterator age 0과
ClickHouse final count가 완성되고 parts/merge backlog와 disk가 bounded다.

## M8 — archive, cleanup and evidence

archive fixture는 `FINAL export -> manifest -> source/S3 equivalence -> DROP -> direct S3
equivalence` 순서로만 실행한다. cleanup은 account/region/run/session ownership을 다시 확인한
뒤 current run 리소스에만 적용한다.

```bash
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/archive_fixture_ecs.py --run-dir <RUN_DIR>
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/prepare_cleanup_ecs.py --run-dir <RUN_DIR>
npx cdk <ALL_CONTEXT> destroy LoopAdPerfPhase4ClickHouseEcsStack --force
npx cdk <ALL_CONTEXT> destroy LoopAdPerfPhase4ClickHouseEcsImageStack --force
uv run --project performance-tests/phase4-clickhouse/producer-env \
  python performance-tests/phase4-clickhouse/cleanup_inventory_ecs.py \
  --run-dir <RUN_DIR> --output <RUN_DIR>/cleanup-inventory-final.json
```

CloudFormation, ECS service/cluster/task/container instance, ASG/launch template/EC2, Kinesis,
DynamoDB 세 table, ECR/image, ENI/endpoint/SG/VPC, S3, Logs, alarms와 secret의 owned count가
모두 0이어야 한다. cleanup이 실패하면 다른 run을 시작하지 않는다.

## M9 — verdict

`run.json`, `report.md`, `metrics-summary.json`, `correctness-summary.json`, cost artifacts,
raw logs/metrics와 cleanup verification을 완성한다. 상태는 `passed`, `failed`, `aborted`,
`inconclusive` 중 하나다. unknown은 `not measured`로 기록한다. 실패 원인은 최소한 다음 중
하나로 분류한다.

```text
local-memory
local-throughput
configuration
correctness
checkpoint
java-heap-oom
container-memory
kinesis-read
clickhouse-capacity
producer
cost
deadline
cleanup
evidence
```

## Progress

- [x] 2026-07-16 — MultiLangDaemon/Node candidate A의 correctness/failover pass와 반복 Java
  heap OOM, `failed(capacity)`, cleanup inventory 0을 보존했다.
- [x] 2026-07-17 — 사용자 결정으로 Phase 4 consumer를 native Java KCL 3.4.3 단일 runtime으로
  변경했다. 4 GiB fallback을 제거하고 동일한 1 vCPU/2 GiB profile을 다시 검증한다.
- [x] 2026-07-17 — `maxRecords=1,000`, pending=0, max leases=60, task concurrency=10,
  container-aware JVM 한도를 고정했다.
- [x] 2026-07-17 — hard cap `$20`, new-load stop `$17`, cleanup reserve `$3`로 비용 모델과
  테스트를 변경했다.
- [x] M1 — Java source compilation, 7 Java unit tests, TypeScript build와 28 targeted Phase 4
  Jest tests가 통과했다.
- [x] M2 — ARM64 Docker 2 GiB memory/processing gate 통과. 고정 warm-up 2회 뒤 task당
  63,068 records/s, 두 task 환산 126,136 records/s, peak cgroup 50.44%.
- [x] M3 — producer contract/hash, producer tests 14개, scoped diff check와 local repository
  contract tests 6개가 통과했다.
- [ ] M4~M9 — AWS 실행 전 fresh identity/price/quota/CDK diff가 필요하다.

## Decision log

- 2026-07-17 — 두 언어 runtime과 socket bridge를 제거한다. Java KCL callback에서 변환,
  ClickHouse insert, archive와 checkpoint를 직접 수행한다.
- 2026-07-17 — maxRecords 10,000은 backlog에서 응답과 변환 working set을 키우므로 유지하지
  않는다. 고정 payload와 shard 목표를 포화시키면서 memory bound가 되는 1,000을 사용한다.
- 2026-07-17 — pending 4는 throughput contract가 아니라 shard별 prefetch cache이므로 0으로
  설정해 demand-driven fetch를 사용한다.
- 2026-07-17 — local gate는 배포의 필요조건이며 AWS performance의 대체 증거가 아니다.
- 2026-07-17 — local gate 실패 시 memory 증가나 AWS trial을 자동 허용하지 않는다.
- 2026-07-17 — AWS hard cap은 `$20`; `$17` 이후 신규 load 금지, `$3` cleanup reserve 고정이다.

## Current outcome

현재 상태는 `local-qualified-ready-for-fresh-aws-preflight`다. AWS login, run directory 생성과
배포는 실행하지 않았다. 다음 안전한 작업은 fresh price, fresh identity와 fresh run ID로
M4 read-only preflight를 시작하는 것이다. M4가 실패하면 deploy하지 않는다.
