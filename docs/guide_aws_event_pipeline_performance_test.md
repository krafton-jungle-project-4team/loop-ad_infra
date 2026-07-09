# AWS Event Pipeline Performance Test Guide

이 문서는 AWS에서 별도 성능 테스트 인프라를 띄워 `load generator -> ALB -> event-collector -> Kafka`부터 단계적으로 측정하고, 이후 ClickHouse 적재와 조회 튜닝까지 확장하는 실행 계획이다.

## Goal

최종 목표 수치는 단일 이벤트 요청 기준 `50k rps`다.

```text
event per request: 1
event payload: about 1 KiB
target: 50k HTTP requests/sec = 50k events/sec
```

이 목표는 실제 사용자 트래픽 재현이 아니라 병목을 찾고 개선하는 경험을 얻기 위한 synthetic load 목표다. batch ingest는 이 계획의 1차 전제에 포함하지 않는다.

## Principles

- 기존 dev/prod 인프라에 영향을 주지 않는 별도 perf 인프라를 만든다.
- VPC, hosted zone, ECR repository처럼 안전하게 공유 가능한 기반은 공유할 수 있다.
- ALB, ECS service, load generator, Kafka/MSK, ClickHouse는 perf 전용 리소스로 둔다.
- 비용 절감을 위해 stateless compute는 Fargate Spot 또는 EC2 Spot을 우선 사용한다.
- Kafka는 MSK를 사용할 수 있으나 MSK에는 일반적인 stop/start가 없으므로 테스트 후 삭제/재생성을 전제로 한다.
- 성능 테스트 결과, 리소스 변경, 설정 변경, S3 산출물 링크는 삭제하지 않는다.
- 실험 결과가 나오면 `performance-tests/run_<id>/`에 기록하고 커밋 대상으로 포함한다.

## Infrastructure Scope

1차 인프라:

```text
load generator
  -> perf ALB
  -> perf event-collector service
  -> perf Kafka/MSK topic
```

2차 확장:

```text
Kafka/MSK topic
  -> ClickHouse raw_events
  -> materialized views
  -> dashboard query tables
```

### Shared Components

공유 가능:

- 기존 VPC와 subnet
- 기존 ECR repository
- 기존 hosted zone 또는 certificate
- 기존 secret naming convention

공유 금지:

- 기존 dev ALB listener/target group
- 기존 dev ECS service
- 기존 dev Kafka topic
- 기존 dev ClickHouse table
- 기존 dashboard runtime database

공유 VPC를 쓰더라도 security group과 resource names는 `perf-` prefix를 사용한다.

## Cost Controls

- Fargate service는 `FARGATE_SPOT` capacity provider를 기본으로 한다.
- EC2 기반 load generator를 쓰는 경우 Spot ASG를 사용한다.
- perf stack에는 `Project=loop-ad`, `Environment=perf`, `CostScope=performance-test`, `RunId=<run_id>` 태그를 붙인다.
- 기본 desired count는 0으로 둔다.
- 테스트 시작 시에만 scale out하고, 종료 직후 scale in 또는 destroy한다.
- MSK는 stop/start가 아니라 create/delete 운영으로 본다.
- 테스트 전에 예상 실행 시간과 예상 비용을 `run.json`에 적는다.
- 테스트 후 실제 CloudWatch/Cost Explorer 확인 결과를 `report.md`에 적는다.

## Test Phases

### Phase 0: Generator And ALB Ceiling

목적: load generator와 ALB가 목표 요청량을 만들 수 있는지 확인한다.

경로:

```text
load generator -> ALB fixed response
```

목표:

- 10k rps
- 20k rps
- 50k rps

성공 기준:

- ALB `HTTPCode_ELB_5XX_Count`가 0에 가까움
- load generator `http_req_failed < 0.1%`
- p95 latency가 네트워크/ALB fixed response 수준에서 안정적

이 단계는 collector와 Kafka를 포함하지 않는다.

### Phase 1: Collector To Kafka Baseline

목적: collector가 Kafka ack 지연까지 포함해 어느 정도의 단일 이벤트 요청을 처리하는지 확인한다.

경로:

```text
load generator -> ALB -> event-collector -> Kafka/MSK
```

목표:

- 1k rps
- 5k rps
- 10k rps
- 20k rps
- 50k rps

성공 기준:

- accepted rps가 target rps의 99% 이상
- `5xx <= 0.1%`
- Kafka offset 증가량이 accepted count와 일치
- collector CPU가 장시간 85% 이상 고정되지 않음
- Kafka producer throttling이 지속되지 않음

### Phase 2: Scale-Out Matrix

목적: collector task 수와 Kafka partition 수 증가가 처리량에 선형적으로 기여하는지 확인한다.

기본 matrix:

```text
collector tasks: 1, 2, 4, 8, 17
Kafka partitions: 16, 32, 64
load generator tasks: 1, 4, 8
```

각 조합은 낮은 rps에서 시작해 실패 직전까지 올린다.

### Phase 3: ClickHouse Ingest

목적: Kafka에 적재된 이벤트를 ClickHouse가 따라잡는지 확인한다.

경로:

```text
load generator -> ALB -> event-collector -> Kafka/MSK -> ClickHouse raw_events
```

측정:

- Kafka consumer lag
- ClickHouse rows inserted/sec
- Kafka Engine consumer count
- MergeTree parts count
- merge backlog
- disk write throughput
- raw_events row count delay

### Phase 4: Query-Oriented Tables

목적: dashboard/decision 서비스 조회 패턴에 맞는 파생 테이블과 materialized view를 검증한다.

대상 패턴:

- `funnel_step_events` by `project_id`, `event_name`, `event_time`
- recent 5m/1h counts
- `toStartOfHour(event_time)` bucket
- `promotion_touch_events`
- `booking_outcome_events`
- segment and campaign breakdown
- data explorer readonly SQL

이 단계부터는 적재 속도와 조회 속도를 별도 결과로 기록한다.

## Required Metrics

Load generator:

- target rps
- actual rps
- failed requests
- p50/p95/p99 latency
- dropped iterations
- network errors

ALB:

- `RequestCount`
- `TargetResponseTime`
- `HTTPCode_ELB_5XX_Count`
- `HTTPCode_Target_2XX_Count`
- `HTTPCode_Target_5XX_Count`
- `TargetConnectionErrorCount`
- LCU usage

Collector:

- ECS desired/running task count
- CPU utilization
- memory utilization
- container restarts
- publish failed log count
- pprof CPU/heap snapshots when available

Kafka/MSK:

- messages in/sec
- bytes in/sec
- produce latency
- produce throttle time
- broker CPU
- disk usage
- partition offset distribution

ClickHouse:

- inserted rows/sec
- table row counts
- parts count
- active merges
- disk throughput
- query p50/p95/p99 latency

## Run Record Rules

Every executed test creates one immutable folder:

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_<short_name>/
```

Required files:

```text
run.json
infra.md
commands.md
metrics-summary.json
report.md
artifacts.md
```

Optional files:

```text
loadgen-summary.json
alb-metrics.json
collector-metrics.json
kafka-metrics.json
clickhouse-metrics.json
pprof/
screenshots/
```

Rules:

- Do not delete run folders.
- Do not overwrite a previous run.
- Store large raw artifacts in S3 and record links in `artifacts.md`.
- Commit the run folder after the result is written.
- If infra is changed between tests, record the change in `infra.md` and commit it.
- If a test is aborted, still write `report.md` and mark the verdict as `aborted`.

## Run ID

Use this format:

```text
run_20260709_153000_phase1_10k_collector4_kafka32
```

Include:

- date/time
- phase
- target rps
- collector count
- Kafka partition count
- special condition if any

## Minimum Run Report

Each `report.md` must answer:

- What was tested?
- What infrastructure was running?
- What was the target?
- What was achieved?
- What failed?
- What was the likely bottleneck?
- What changed from the previous run?
- What is the next action?

## First Implementation Target

Start with Phase 0 and Phase 1 only.

Required initial AWS resources:

- perf ALB with fixed response route
- perf ECS cluster/service for event collector
- perf load generator service or task
- perf Kafka/MSK topic
- CloudWatch dashboards or metric export commands
- S3 bucket/prefix for raw artifacts

Do not add ClickHouse to the first stack unless Phase 1 can sustain useful throughput and Kafka metrics are understood.
