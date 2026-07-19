# AWS 이벤트 파이프라인 단계별 성능 실험 가이드

이 가이드는 입력, Kinesis 수집, ClickHouse 적재와 보존 경계를 분리해 병목과 정합성을
검증하는 현재 실행 순서를 정의한다.

## Phase 0~7

| Phase | 경로 | 상태 | 종료 조건 |
| ---: | --- | --- | --- |
| 0 | `HTTP load -> fixed response` | 완료 | 실제 payload로 생성기 ceiling 확인 |
| 1 | `load -> collector -> Kinesis` | 완료 | ACK, 원문 bytes, partition key, 누락·중복 검증 |
| 2 | `load -> proxy/LB -> collector -> Kinesis` | 기준선 채택 | 목표 수집률, 병목, 비용과 cleanup 확정 |
| 3 | `Locust EC2 -> PutRecords -> Kinesis` | 완료 | 단일 producer가 50k records/s × 300초 생성 |
| 4 | `fixed producer -> Kinesis -> native Java KCL/ECS on EC2 -> EC2 ClickHouse` | AWS 완료, 측정 편차를 기록하고 진행 허용 | 데이터 경로·정합성·복구·archive fixture·cleanup 검증 |
| 5 | `load -> collector -> Kinesis -> native Java KCL/ECS on EC2 -> EC2 ClickHouse` | 사용자 결정으로 스킵 | 실행하지 않았으며 `passed`로 간주하지 않음 |
| 6 | `closed EC2 ClickHouse partition -> S3 Parquet -> direct query` | Goal 1 로컬·Goal 2 AWS 통과 | 15M archive, 삭제 전후 완전 동등성, 시간·비용·cleanup 통과 |
| 7 | `load -> HAProxy -> collector -> Kinesis -> Java KCL -> ClickHouse -> S3 archive` | Phase 7-1 통과, Phase 7-2 stack 구현·synth 통과 | AWS 50k ingest/archive overlap과 cleanup 검증 |

ClickHouse Cloud, Kinesis ClickPipes와 Lambda는 활성 Phase 4~6 topology가 아니다. 관련 과거
문서는 비교 자료일 뿐 현재 결과나 비용 ledger와 섞지 않는다.

## 현재 checkpoint

- Phase 2는 `sampled-202` connection path를 50k 수집 기준선으로 채택했다.
- Phase 3의 기준 증거는
  `run_20260716_110956_locust_kinesis_generator_qualification` (external snapshot reference: `../performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/report.md`)다.
  고정 후보는 `c7g.2xlarge`, Locust worker 8개, 50,000 records/s × 300초다.
- Phase 4 최신 증거는
  `run_20260716_194426_phase4_clickhouse_ecs` (external snapshot reference: `../performance-tests/run_20260716_194426_phase4_clickhouse_ecs/report.md`)다.
  run의 historical 판정은 `failed(producer,evidence)`로 유지한다.
- producer와 Kinesis는 정확히 15,000,000건을 성공했고 failure/retry/throttle은 0이었다.
  strict `producer_sent_at` window는 1,939.482초 뒤 14,999,990건이었지만 전역 ClickHouse
  physical/unique count는 18,001,000건으로 실제 누락은 없었다.
- correctness 1,002/1,002, task replacement 900/900, archive-before-DROP와 direct S3 query가
  통과했다. local memory peak는 47.59%, 두 task 환산 처리량은 59,661 records/s였다.
- ECS service task-level CPU/memory는 datapoint가 없어 `not measured`다. host CPU/memory p95는
  50.31%/54.31%였고 restart, OOM, Kinesis throttle, insert error는 0이었다.
- 최대 비용은 `$14.634519 <= $20`, 유료 88.758분에 28개 inventory category가 모두 0이었다.
- 따라서 기존 run verdict를 성공으로 바꾸지는 않지만 Phase 4를 다시 실행하는 대신 Phase 6
  Lite로 진행한다. 이후 같은 유형의 실험은 예상 count가 도달할 때까지 충분히 기다리고,
  예상보다 늦으면 실제 반영 시간을 별도 측정한다.
- Phase 6 Goal 1의 최종 fixed handoff는
  `run_20260717_050834_phase6_archive_local_bootstrap_fix` (external snapshot reference: `../performance-tests/run_20260717_050834_phase6_archive_local_bootstrap_fix/report.md`)이고
  `passed`, `awsReady=true`다.
- Phase 6 Goal 2는
  `run_20260717_055837_phase6_clickhouse_s3_archive` (external snapshot reference: `../performance-tests/run_20260717_055837_phase6_clickhouse_s3_archive/report.md`)에서
  15,000,000 rows/unique, 3 x 5,000,000-row Parquet, 모든 pre/committed/post 차집합 0,
  cycle 805.662325초, host CPU/memory p95 24.36438%/6.903885%, modeled accrued
  `$0.481389`, run-owned billable inventory 0으로 `passed`했다.
- Phase 7-1은
  `run_20260717_093049_phase7_local` (external snapshot reference: `../performance-tests/run_20260717_093049_phase7_local/report.md`)에서
  correctness 1,002건, planned replacement, live 24,000건, closed partition 1,000,000건,
  archive/DROP 후 direct query, 실제 AWS 요청 0과 Docker inventory 0으로 `passed`했다. LocalStack
  ACK 완료 기준 123.344704 RPS는 로컬 환경 정보이며 AWS capacity 판정이 아니다.
- Phase 7-2 전용 image/runtime CDK stack은 build, unit, no-lookup synth를 통과했다. 실제 AWS
  deploy, 50k score, 비용과 cleanup은 아직 실행하지 않았다.

## 공통 lifecycle

각 AWS run은 다음 순서를 바꾸지 않는다.

```text
계약과 원본 hash 고정
-> local build/unit/integration
-> 가격/account/region/ownership/quota preflight
-> run.json, infra.md, commands.md 생성
-> 전용 stack 배포
-> correctness smoke
-> 허용된 경우에만 고정 부하 또는 archive
-> drain과 정합성/비용/실패 증거
-> run 소유 리소스 cleanup
-> service API inventory zero
-> passed/failed/aborted/inconclusive 판정
```

앞 gate 실패를 뒤 단계의 성공으로 보상하지 않는다. 공유 dev stack은 성능 실험에서
배포하거나 교체하지 않는다. 비용 상한과 cleanup 시작 시점은 각 Phase의 고정 계약을 따른다.

## 공통 count 계약

모든 이벤트는 전역 고유 `event_id`를 가진다. payload pool을 반복해도 실제 record의
`event_id`, `event_time`, `producer_sent_at`, `run_id`는 실행 시점에 생성한다.

Phase 4 smoke의 핵심 불변식은 다음과 같다.

```text
Kinesis successful input
  = events FINAL unique
  + raw_events
  + LateEventDropped
```

정상 event의 missing count, KCL terminal failure, dropped event, failure object와 ClickHouse
insert error는 모두 0이어야 한다. at-least-once 재시도는 허용하지만 physical rows,
`FINAL` unique rows와 원인을 함께 기록한다.

관측 cutoff만으로 누락을 단정하지 않는다. producer가 성공 count를 만족한 뒤 consumer의
iterator age와 ClickHouse expected count가 안정될 때까지 기다린다. 예상 시간보다 늦으면
고정 timeout을 조용히 늘리지 않고 예상 count 도달 시각과 총 반영 시간을 증거로 남긴다.

## Phase 4 고정 경계

상세 계약은
[Phase 4 Kinesis→ECS on EC2→ClickHouse 계획](../drafts/guide_phase4_kinesis_ecs_clickhouse_test_draft.md)을
따른다.

- producer는 Phase 3 immutable implementation만 사용한다.
- full load는 50,000 records/s × 300초, 정확히 15,000,000 records다.
- Kinesis는 provisioned 120 shards, retention 24시간이다.
- consumer는 native Java KCL 3.4.3 polling/LATEST, ECS on EC2 두 task다.
- 각 task는 ARM64 1 vCPU/2 GiB이고 서로 다른 `c7g.large` host에 하나씩 둔다.
- ClickHouse는 private `r7g.2xlarge`, gp3 500 GiB, 고정 image digest다.
- 정상 event는 `events`, 변환 불가 event는 `raw_events`에 적재하고 late event는 metric만
  증가시킨다.
- `properties_json`을 변형하지 않고 정합성과 archive query는 `events FINAL`을 사용한다.
- 두 ClickHouse INSERT가 성공한 뒤 checkpoint하며 bounded retry가 끝난 원문은 run-owned
  S3에 보존한다.

Phase 4 AWS 유료 wall-clock은 deploy부터 120분이고 100분에 cleanup을 시작한다. 새 load
금지선은 `$17`, cleanup reserve는 `$3`, hard cap은 `$20`다. 공개 가격은 실행 직전에 다시
조회한다.

## 중단과 판정

다음 조건에서는 새 load/archive를 시작하거나 계속하지 않는다.

- account, region 또는 ownership 불일치
- quota 부족 또는 operator gate 실패
- 계획 최대 비용이 hard cap을 넘거나 누적액이 새 작업 금지선에 도달
- smoke count mismatch, KCL terminal failure, failure object 또는 ClickHouse insert error
- ClickHouse restart/parts/merge/disk stop gate
- iterator age가 10분 연속 감소하지 않거나 phase별 drain/archive 제한 초과
- 증거 수집 실패 또는 phase별 cleanup 시작 시점 도달

구현/시스템 오류로 acceptance가 깨지면 `failed`, 외부 gate로 시스템을 만들지 못하면
`aborted`, 필수 측정이 유실되면 `inconclusive`, 모든 기준을 만족할 때만 `passed`다. 단계
진행 결정은 historical run verdict와 별도로 기록하며 기존 verdict를 덮어쓰지 않는다.

## Phase 6 Lite와 Phase 7

Phase 6 Lite Goal 2 AWS 배포·검증은 통과했다. 상세 계약과 결과는
[Phase 6 Lite EC2 ClickHouse→S3 archive 실행 계약](guide_phase6_clickhouse_s3_archive_lifecycle_test.md)과
`run_20260717_055837_phase6_clickhouse_s3_archive` (external snapshot reference: `../performance-tests/run_20260717_055837_phase6_clickhouse_s3_archive/report.md`)에
있다.

Phase 6 Lite는 single Python worker, systemd timer/`flock`, run-owned S3 Standard,
15,000,000-row closed partition, immutable manifest/conditional `COMMITTED`, pre-DROP와
post-DROP 완전 동등성만 검증한다. DynamoDB, Step Functions, EventBridge, lifecycle transition,
restore와 live overlap은 넣지 않는다.

실행은 [Goal 1 로컬 구현·검증](../processes/process_phase6_clickhouse_s3_archive_local_goal_prompt.md)과
[Goal 2 AWS 배포·검증](../processes/process_phase6_clickhouse_s3_archive_aws_goal_prompt.md)으로 분리한다.
Goal 1의 exact final handoff는
`performance-tests/run_20260717_050834_phase6_archive_local_bootstrap_fix/local-handoff.json`이고,
Goal 2는 이를 재검증해 통과했다. 후속 Phase 6 재실행이 필요하면 같은 immutable handoff,
fresh preflight, 새 run-owned whole attempt와 cleanup hard stop을 다시 적용한다.

Phase 5는 사용자 결정으로 실행하지 않고 `skipped`로 남긴다. 이 결정은 합격 판정이 아니다.
후속 검증은 [Phase 7 전체 통합 실행 계약](guide_phase7_end_to_end_integration_test.md)에 따라
진행한다. Phase 7-1은 LocalStack으로 실제 구현을 한 경로에 연결하고, Phase 7-2는 그 exact
handoff를 AWS 전용 통합 stack에 배포해 50k ingest와 15M archive overlap을 검증한다.

## 필수 run 산출물

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_<phase>_<name>/
├── run.json
├── commands.md
├── infra.md
├── failures.md
├── report.md
└── evidence/
    ├── local/
    └── aws-readonly-or-runtime/
```

실패·중단·불확정 run도 보존한다. 값을 측정하지 못했으면 0으로 추정하지 않고 `not measured`
또는 `not run`으로 기록한다.
