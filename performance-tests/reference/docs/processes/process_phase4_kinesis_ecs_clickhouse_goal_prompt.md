# Phase 4 native Java KCL→ClickHouse goal prompt

## 결정

활성 후보는 `Kinesis -> native Java KCL 3.4.3 -> ECS on EC2 -> EC2 ClickHouse`다.
Phase 4 consumer에는 Java만 사용한다. MultiLangDaemon, Node.js, TypeScript record processor,
Unix socket과 `socat`은 이미지와 실행 경로에 포함하지 않는다. TypeScript Lambda handler는
과거 Lambda 경로의 회귀 테스트로만 남으며 ECS consumer에는 배포하지 않는다.

배포 profile은 `2 x c7g.large`, host당 task 한 개, task당 `1 vCPU / 2 GiB`로 고정한다.
로컬의 동일한 CPU·memory hard limit에서 메모리와 처리 envelope를 먼저 증명한다. 이 gate가
실패하면 AWS에 배포하거나 task memory를 임의로 높이지 않는다. 다른 profile은 별도 사용자
결정과 새 goal이 있어야 한다.

KCL 설정은 다음과 같이 고정한다.

```text
KCL_VERSION=3.4.3
RETRIEVAL=POLLING
INITIAL_POSITION=LATEST
MAX_RECORDS=1000
MAX_PENDING_PROCESS_RECORDS_INPUT=0
MAX_LEASES_PER_WORKER=60
POLL_INTERVAL_MS=200
MAX_CONCURRENT_CLICKHOUSE_BATCHES_PER_TASK=10
GRACEFUL_LEASE_HANDOFF_MS=120000
```

`maxRecords=1,000`은 고정 payload 최대 1,518 B와 shard당 목표 약 417 records/s를 기준으로
한다. Kinesis의 shard 읽기 한도보다 충분한 여유가 있고, 두 task의 ClickHouse batch slot
20개는 300 ms/batch에서도 50k/s보다 큰 명목 처리량을 제공한다. pending `0`은 KCL 3.4.3의
demand-driven mode로, 처리량을 위한 선읽기 대신 shard별 메모리 증폭을 제거한다. 이 계산은
설정 근거이며 실제 통과 여부는 로컬 gate와 AWS 측정으로 결정한다.

## 현재 로컬 기준선

2026-07-17 ARM64 Docker gate를 정확히 1 CPU/2 GiB/no swap으로 실행한 결과는
[`java-memory-gate-result.json`](../../tools/phase4-clickhouse/java-memory-gate-result.json)에
보존한다. 60 shards × 1,000 records × 1,518 B를 고정 warm-up 2회 뒤 6회 측정했으며
task당 63,068 records/s, 두 task 환산 126,136 records/s, peak cgroup
1,083,219,968 B(50.44%)로 통과했다. 실제 AWS
50k/s 판정은 이 결과를 재사용하지 않고 correctness/failover 이후 M7 measured load에서 한다.

## 사용법

저장소를 연 Codex 작업에서 `/goal`을 입력한 뒤 아래 블록을 붙여 넣는다.

```text
Outcome

이 저장소의 Phase 4 Kinesis→native Java KCL 3.4.3→ECS on EC2→EC2 ClickHouse 구현과
보존된 로컬 Docker gate 결과를 재검증하고, 모든 로컬 gate가 통과한 경우에만 새 AWS run을
배포·검증하라. correctness, 50,000 records/s x 300초, failover, 비용, cleanup 증거를 남기고
최종 상태를 passed/failed/aborted/inconclusive 중 하나로 판정하라.

Source of truth

- 저장소: /Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra
- 이 goal prompt: docs/process_phase4_kinesis_ecs_clickhouse_goal_prompt.md
- living plan: performance-tests/phase4-clickhouse/ecs-exec-plan.md
- local resource evidence: performance-tests/phase4-clickhouse/java-memory-gate-result.json
- 실행 계약: docs/guide_phase4_kinesis_ecs_clickhouse_test_draft.md
- 증거 규칙: docs/process_aws_perf_test_result_recording.md
- 공식 KCL Java 가이드: https://docs.aws.amazon.com/streams/latest/dev/develop-kcl-consumers-java.html
- 공식 KCL 설정: https://docs.aws.amazon.com/streams/latest/dev/kcl-configuration.html
- 기준 구현: https://github.com/awslabs/amazon-kinesis-client/tree/v3.4.3
- producer는 performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/
  implementation/을 수정 없이 사용한다.
- payload는 performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson이며
  SHA-256은 93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7다.

Implementation constraints

- 먼저 AGENTS.md와 event-pipeline-loadtest-runner skill을 읽고 따른다.
- dirty worktree의 기존 변경을 보존한다. 관계없는 수정, stash, reset, rebase, 삭제, stage,
  commit, push와 PR은 하지 않는다.
- consumer runtime은 Java 21과 amazon-kinesis-client 3.4.3만 사용한다. MultiLangDaemon,
  amazon-kinesis-client-multilang, Node.js, TypeScript consumer, socket bridge와 socat을 금지한다.
- KCL은 polling/LATEST, maxRecords=1,000, pending=0, maxLeasesForWorker=60으로 고정한다.
  task 전체 ClickHouse batch concurrency는 10으로 제한한다.
- JVM은 container-aware G1, MaxRAMPercentage=65, InitialRAMPercentage=20,
  MaxDirectMemorySize=256m, Xss=256k, ExitOnOutOfMemoryError를 사용한다.
- processRecords는 변환과 두 ClickHouse INSERT가 끝난 뒤 checkpoint한다. 재시도 소진 시
  원본 Kinesis batch와 metadata를 S3에 성공적으로 보존한 뒤에만 checkpoint한다.
- leaseLost에서는 checkpoint하지 않는다. shardEnded에서는 반드시 checkpoint하고,
  shutdownRequested에서는 마지막 성공 위치를 checkpoint한다. graceful lease handoff는
  120초다.
- schema, properties_json byte string, raw Base64, late-event metric-only, ReplacingMergeTree,
  async insert와 archive-before-DROP 정책을 바꾸지 않는다.
- secret은 Secrets Manager에서 task role로 읽고 payload·secret·write key를 로그에 남기지
  않는다. ClickHouse 8123은 task security group에서만 접근한다.

Mandatory local gate

1. Maven unit tests, TypeScript/CDK build와 targeted Jest, Python cost tests를 통과한다.
2. ARM64 consumer image를 빌드하고 runtime image에 java는 존재하며 node와 socat은 없음을
   검사한다.
3. performance-tests/phase4-clickhouse/run-java-memory-gate.sh를 실행한다. 이 스크립트는
   --cpus=1, --memory=2g, --memory-swap=2g, --network=none을 강제한다.
4. 한 task의 최악 lease envelope인 60 shards x 1,000 records, record당 1,518 B를 고정
   warm-up 2회 뒤 6회 측정한다. pending=0, ClickHouse batch concurrency=10, 100 ms sink
   retention을 적용한다. memory peak는 warm-up을 포함한다.
5. cgroup peak가 2 GiB의 70% 미만이고 두 task 환산 처리량이 50,000 records/s 이상이어야
   한다. 결과 JSON을 보존한다.
6. 로컬 gate는 Kinesis와 실제 ClickHouse의 AWS 처리량 증명이 아니다. AWS correctness와
   measured load를 대신하지 않는다. 어느 로컬 조건이라도 실패하면 AWS 배포는 금지한다.
7. 보존된 2026-07-17 기준선은 peak 50.44%, 두 task 환산 126,136 records/s다. 실행 source,
   Docker image 또는 고정 설정이 달라졌다면 M4 전에 gate를 다시 실행한다.

AWS execution

- 로컬 gate 통과 후에만 새 RUN_ID, SESSION_ID와 불변 run directory를 만든다. 과거 run ID,
  ECR digest, 가격 문서와 비용 누계를 재사용하지 않는다.
- 먼저 identity, account, ap-northeast-2, ownership, stack absence, quota, instance offering,
  current price, CDK diff와 shared-stack no-replacement를 확인한다.
- hard cap은 $20, 신규 load 중단선은 계획 누적 $17, cleanup reserve는 $3다. deploy부터
  최대 120분이며 100분에 무조건 cleanup을 시작한다. Cost Explorer/Budgets 지연값이 아니라
  저장소의 deterministic cost model과 paid wall clock으로 중단한다.
- image support stack 배포, ECR push·digest 재확인, runtime stack 배포 순서를 지킨다.
- desired/running task 2, 서로 다른 host, task 1 vCPU/2 GiB, lease 60/60, total 120,
  private ClickHouse와 healthy consumer를 확인한다.
- 1,002-record correctness smoke와 단일 task replacement smoke가 모두 통과해야 본 부하를
  시작한다.
- 본 부하는 고정 producer로 정확히 50,000 records/s x 300초 = 15,000,000 records다.
  측정 중 deploy, tuning, autoscaling, Spot과 fault injection은 금지한다.
- producer 종료 후 최대 30분 drain을 허용한다. 결과와 원시 ECS/KCL/Kinesis/ClickHouse/
  host/task/cost metrics를 run directory에 보존한다.
- 성공·실패·중단과 관계없이 run 소유 리소스만 cleanup하고 service별 inventory 0을 증명한다.

Stop conditions

- local gate 실패 또는 결과 JSON 부재
- identity/region/ownership 불명확, shared stack replacement, quota/offering 부족
- 예상 비용이 $20을 넘거나 신규 load 시점 계획 누계가 $17 이상
- image/source/payload/digest 불일치, task resource가 1 vCPU/2 GiB와 다름
- lease가 정확히 60/60이 아님, correctness/failover mismatch, checkpoint/terminal failure
- Java OOM, ECS restart/crash loop, Kinesis read throttle, ClickHouse insert/restart/parts/disk 이상
- iterator age가 10분 연속 감소하지 않음, drain 30분 초과, 100분 cleanup deadline 도달

Done

- native Java KCL unit tests와 이미지 검사가 통과한다.
- 로컬 cgroup peak <70%, 두 task 환산 >=50k/s 결과가 보존된다.
- AWS에서 input = events FINAL unique + raw_events + LateEventDropped, missing=0이다.
- task replacement 후 desired/running=2, lease 재분배, checkpoint resume과 missing=0이다.
- 본 부하 producer success=15,000,000, failure=0, terminal failure=0, read throttle=0,
  OOM/restart=0이며 30분 안에 iterator age=0과 최종 count가 완성된다.
- task CPU와 memory p95가 각각 70% 미만이고 ClickHouse backlog와 disk가 bounded다.
- 비용이 $20 이하고 cleanup inventory가 0이며 명령·설정·실패 원인·판정 근거가 남는다.
```
