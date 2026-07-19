# Phase 4: Kinesis→ECS on EC2→EC2 ClickHouse

이 디렉터리는 다음 활성 경로의 구현, 고정 계약, 로컬 하네스와 실행 전 검증 도구를
보관한다. 기존 Lambda 구현과 결과는 회귀·historical evidence로 유지한다.

```text
Phase 3 qualified producer
  -> run-owned Kinesis Data Stream
  -> KCL 3.x application
  -> ECS on EC2, 2 x arm64 consumer tasks
  -> private EC2 ClickHouse
```

Lambda, Fargate, ClickHouse Cloud와 Kinesis ClickPipes는 현재 Phase 4 후보가 아니다. 활성
계약은 [Kinesis→ECS on EC2→ClickHouse 계획](../../docs/drafts/guide_phase4_kinesis_ecs_clickhouse_test_draft.md),
실제 명령과 gate는 [ECS living execution plan](ecs-exec-plan.md)을 따른다. 기존
[Lambda plan](exec-plan.md)은 aborted run의 기록이다.

## 구현 구성

- `docker-compose.yml`: 고정 ClickHouse와 LocalStack 이미지
- `local_integration.py`: correctness, 50,000-row async flush, archive fixture
- `local-handler-harness.ts`: 실제 TypeScript handler를 호출하는 로컬 경계
- `verify_producer_contract.py`: Phase 3 원본과 payload hash 재검증
- `lookup_prices_ecs.mjs`, `cost_model_ecs.py`: compute, Kinesis, storage, endpoint, CloudWatch,
  ECR 공개 단가를 포함한 `$17/$20` 비용 gate
- `consumer/`: Java 21 native KCL `ShardRecordProcessor`, ClickHouse writer와 단위 테스트
- `run-java-memory-gate.sh`: ARM64 Java-only image와 1 CPU/2 GiB memory/processing hard gate
- `java-memory-gate-result.json`: 2026-07-17 고정 조건 local resource gate 통과 증거
- `preflight_ecs.py`: account, region, ownership, quota, offering, bootstrap, AMI read-only gate
- `aws_correctness_smoke_ecs.py`, `verify_ecs_recovery.py`: AWS 정합성과 task replacement gate
- `prepare_producer_ecs.py`, `run_full_load_ecs.py`: 원본 producer packaging/bootstrap과 15M 실행,
  15초 hard-stop 감시
- `evaluate_full_load_ecs.py`: 원본 producer 분석, KCL/ECS/Kinesis/ClickHouse capacity·drain 판정
- `archive_fixture_ecs.py`: manifest·사전 동등성 이후에만 DROP하는 S3 archive fixture
- `prepare_cleanup_ecs.py`, `cleanup_inventory_ecs.py`: 부분 배포를 포함한 owned-resource cleanup과
  service-by-service zero 검증
- `producer-env/pyproject.toml`, `uv.lock`: 검증된 producer 실행 환경

접미사 없는 `lookup_prices.mjs`, `cost_model.py`, `preflight.py`, `cleanup_inventory.py`는 기존
Lambda historical 경로용이며 ECS 배포 승인에 사용하지 않는다.

Producer 구현은
`performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/`
원본만 사용한다. 이 디렉터리에 새 producer나 대체 부하 생성기를 만들지 않는다.

## 활성 고정 후보

- producer: run-owned `c7g.2xlarge`, Locust worker 8개, evidence upload 후 즉시 stop
- full load: 50,000 records/s × 300초 = 15,000,000 records
- payload SHA-256:
  `93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7`
- Kinesis: provisioned 120 shards, retention 24시간
- ECS on EC2: `2 x c7g.large`, host당 task 1개, task `1 vCPU/2 GiB`, ARM64,
  On-Demand, desired count 2, autoscaling 없음
- KCL `3.4.3` native Java: polling/LATEST, maxRecords 1,000, pending 0, worker당 최대
  60 lease, task당 ClickHouse batch concurrency 10
- ClickHouse: `r7g.2xlarge`, gp3 500 GiB/3,000 IOPS/500 MiB/s,
  `clickhouse/clickhouse-server@sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797`
- AWS paid wall-clock: 최대 120분, 100분에 무조건 cleanup 시작
- 비용: 새 load 금지선 `$17`, cleanup reserve `$3`, hard cap `$20`
- 관측 비용 상한: KCL DETAILED 4,200 series, Container Insights 100 series,
  EC2 detailed monitoring 20 series, task별 host-memory EMF 최대 3 series, Logs 0.25 GiB,
  GetMetricData 20,000 requested metrics
- data/API 비용 상한: interface endpoint 25 GiB, DynamoDB read/write request unit
  1,000,000/500,000, S3 Standard 1 GiB와 Tier 1/Tier 2 request 1,000/5,000

## historical 결과와 단계 결정

run_20260716_101059_phase4_clickhouse_lambda (external snapshot reference: `../run_20260716_101059_phase4_clickhouse_lambda/report.md`)는
`aborted`다. 구현, 정적 테스트와 모든 로컬 통합 gate는 통과했지만 AWS Lambda 계정
concurrency quota가 `10`이라 고정 reservation `120`을 생성할 수 없었다. 배포, smoke,
15M load는 실행하지 않았고 최종 AWS inventory는 0이다.

다섯 ECS 실행도 독립 historical evidence로 보존한다.

- `run_20260716_142341_phase4_clickhouse_ecs`: ECS host 등록 전 배포 circuit breaker로
  `aborted`; cleanup inventory 0.
- `run_20260716_150729_phase4_clickhouse_ecs`: regional AL2023 repository가 S3 endpoint
  policy에서 빠져 ClickHouse bootstrap 실패 후 terminal failure가 발생해 `failed`; cleanup
  inventory 0.
- `run_20260716_155254_phase4_clickhouse_ecs`: ClickHouse readiness는 통과했지만 초기 lease
  쏠림 중 `LATEST` handoff가 단일 shard의 9건을 건너뛰어 `failed`; cleanup inventory 0.
- `run_20260716_165030_phase4_clickhouse_ecs`: 60/60 lease, correctness와 task replacement는
  통과했다. 50k/s warmup에서 두 `1 vCPU/2 GiB` task의 KCL Java heap이 OOM으로 종료돼
  `failed(capacity)`; 300초 측정과 archive는 실행하지 않았고 cleanup inventory는 0이다.
- `run_20260716_194426_phase4_clickhouse_ecs`: native Java KCL로 producer/Kinesis 15,000,000건,
  correctness 1,002/1,002, task replacement 900/900, archive fixture, 비용과 cleanup을 확인했다.
  strict `producer_sent_at` window가 1,939.482초 뒤 14,999,990건이고 task-level CPU/memory가
  `not measured`여서 `failed(producer,evidence)`다. 전역 ClickHouse physical/unique는
  18,001,000건으로 실제 누락은 없었고, host CPU/memory p95는 50.31%/54.31%, 최대 비용은
  `$14.634519`, 유료 88.758분에 28개 inventory category 모두 0이었다.

최신 run의 historical verdict는 바꾸지 않는다. 다만 데이터 경로, 정합성, failover, native
Java capacity, archive fixture와 cleanup 증거는 다음 단계 진행에 충분한 것으로 결정했다.
Phase 4를 다시 실행하지 않고 [Phase 6 Lite archive](../phase6-archive/README.md)를 먼저
구현·검증한 뒤 Phase 5 최종 통합으로 진행한다.

Phase 4를 다시 측정할 일이 있으면 producer phase membership과 ClickHouse filter가 같은
기준을 사용하게 수정하고 task-level metric source를 복구한다. 예상 count가 늦게 반영될 때는
도달할 때까지 기다린 뒤 총 반영 시간을 기록한다. 로컬 gate 실패 시 AWS에 배포하거나 4 GiB
profile로 자동 전환하지 않는다.
