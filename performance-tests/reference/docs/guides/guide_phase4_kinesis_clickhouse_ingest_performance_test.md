# Phase 4 Kinesis→ClickHouse 문서 전환 안내

현재 Phase 4 후보는 `Kinesis -> ECS on EC2 consumer -> EC2 ClickHouse`다. Lambda와
ClickHouse Cloud/ClickPipes는 historical context일 뿐 활성 실행 후보가 아니다.

## 현재 source of truth

- [ECS on EC2 goal prompt](../processes/process_phase4_kinesis_ecs_clickhouse_goal_prompt.md)
- [ECS on EC2 구현·성능 테스트 계약](../drafts/guide_phase4_kinesis_ecs_clickhouse_test_draft.md)
- [ECS on EC2 living execution plan](../../tools/phase4-clickhouse/ecs-exec-plan.md)
- Phase 4 도구와 checkpoint (external snapshot reference: `../performance-tests/phase4-clickhouse/README.md`)

## 활성 topology

```text
qualified c7g.2xlarge producer, 8 workers
  -> run-owned 120-shard Kinesis stream
  -> KCL 3.x polling consumer
  -> ECS on EC2, 2 x c7g.large, task 1 vCPU/2 GiB each
  -> same-AZ private 8123
  -> run-owned r7g.2xlarge EC2 ClickHouse
```

정상 `hotel_rec_promo.v1` 이벤트는 `events`, 변환 불가 레코드는 `raw_events`에 적재한다.
UTC 기준 7일 초과 late event는 row를 만들지 않고 `LateEventDropped`만 증가시킨다.
`properties_json`은 변형하지 않으며 `events`는 `ReplacingMergeTree(ingested_at)`이다.
정합성·archive 검증은 `FINAL`을 사용한다.

consumer는 run 전용 KCL application과 DynamoDB metadata를 사용한다. 두 ClickHouse INSERT가
성공한 뒤 checkpoint하며, bounded retry가 끝난 batch는 run 전용 S3에 원문을 보존한다.
ClickHouse credential은 secret ARN으로만 전달한다. ALB, NAT Gateway와 public 8123은 만들지
않고 같은 VPC/AZ의 private path를 사용한다.

Lambda concurrency와 Fargate vCPU quota는 적용되지 않는다. EC2 Standard On-Demand vCPU
quota는 계속 적용된다. 2026-07-16 snapshot은 quota `80`, 현재 `4`, ClickHouse+producer
`16`, ECS host `4`, 합계 `24/80`이었다. 새 run은 live quota와 현재 사용량을 다시 확인한다.

## 현재 AWS 결과와 단계 결정

run_20260716_194426_phase4_clickhouse_ecs (external snapshot reference: `../performance-tests/run_20260716_194426_phase4_clickhouse_ecs/report.md`)의
historical 판정은 `failed(producer,evidence)`로 유지한다.

- producer/Kinesis: 정확히 15,000,000 성공, failure/retry/throttle 0
- strict `producer_sent_at` window: 1,939.482초 뒤 14,999,990 unique rows
- 전역 ClickHouse: 18,001,000 physical/unique rows로 실제 누락 0
- correctness: 1,002/1,002, task replacement: 900/900
- archive-before-DROP와 direct S3 query: 통과
- local peak memory 47.59%, 두 task 환산 59,661 records/s
- host CPU/memory p95 50.31%/54.31%; task-level metric은 `not measured`
- 최대 비용 `$14.634519 <= $20`, 유료 88.758분에 28개 inventory category 모두 0

strict window 10건 차이는 producer가 scheduled tick으로 count를 집계하면서 payload에는 실제
wall-clock `producer_sent_at`을 기록한 경계 불일치다. 이 run을 `passed`로 바꾸지는 않지만
데이터 경로, 정합성, failover, 용량과 cleanup 증거는 다음 단계 진행에 충분한 것으로
결정했다.

같은 유형의 후속 실험은 예상 count 도달을 충분히 기다린다. 예상보다 늦으면 실제 count
도달 시각과 총 반영 시간을 측정해 fixed observation window와 분리한다. 다음 실행 단계는
[Phase 6 Lite archive](guide_phase6_clickhouse_s3_archive_lifecycle_test.md)다.

## historical Lambda run

- [Lambda 계약](../drafts/guide_phase4_kinesis_lambda_clickhouse_test_draft.md)
- Lambda execution plan (external snapshot reference: `../performance-tests/phase4-clickhouse/exec-plan.md`)
- 2026-07-16 aborted report (external snapshot reference: `../performance-tests/run_20260716_101059_phase4_clickhouse_lambda/report.md`)

Lambda 구현과 local tests는 통과했지만 account concurrency `10`으로 reservation `120`을
만들 수 없어 배포 전 `aborted`됐다. AWS correctness와 throughput은 측정하지 않았다.
배포 비용은 `$0.00`, run-owned cleanup inventory는 0이었다. 이 run ID와 판정을 ECS
실험에 재사용하지 않는다.

## ClickHouse Cloud와 ClickPipes의 지위

Cloud/ClickPipes 자료는 후보 비교의 historical context로만 사용할 수 있다. 활성 Phase 4
schema, 비용, IAM, network, failure semantics 또는 판정 기준을 대체하지 않는다. 다시
평가하려면 별도 experiment name, 비용 ledger, 계약과 run이 필요하다.
