# Phase 4 Kinesis→ECS on EC2→ClickHouse 구현·성능 테스트 계약

## 목적과 상태

Kinesis의 `hotel_rec_promo.v1` 이벤트를 장시간 실행되는 ECS on EC2 consumer가 읽어
EC2 ClickHouse에 누락 없이 배치 INSERT할 수 있는지 검증한다. 측정 대상은 Lambda drain이
아니라 운영형 live consumer다. consumer를 먼저 정상화한 뒤 `50,000 records/s`를 300초
동안 보내고, 입력 중 backlog 안정성과 입력 종료 후 catch-up을 함께 측정한다.

이 문서가 활성 Phase 4 실행 계약이다. 이전
[`Kinesis→Lambda` 계약](guide_phase4_kinesis_lambda_clickhouse_test_draft.md)과
`run_20260716_101059_phase4_clickhouse_lambda` (external snapshot reference: `../performance-tests/run_20260716_101059_phase4_clickhouse_lambda/report.md`)는
Lambda account concurrency `10` 때문에 배포 전 `aborted`된 historical evidence다. 기존
run ID를 재사용하거나 그 결과를 ECS 경로의 AWS 검증으로 간주하지 않는다.

실제 명령, 관찰 결과, 변경 사유와 최종 판정은
[`performance-tests/phase4-clickhouse/ecs-exec-plan.md`](../../tools/phase4-clickhouse/ecs-exec-plan.md)에
누적한다.

## 실행 결과

후보 A의 최종 실행
`run_20260716_165030_phase4_clickhouse_ecs` (external snapshot reference: `../performance-tests/run_20260716_165030_phase4_clickhouse_ecs/report.md`)는
`failed(capacity)`다. 정확한 60/60 lease, correctness와 단일 task replacement는 통과했지만
50k/s warmup 약 21초 뒤 두 `1 vCPU/2 GiB` task의 KCL Java heap이 OOM으로 종료됐다.
CloudWatch는 측정 시작 전 warmup 1,721,000건, PutRecords 성공 3,442회, throttle 0을
기록했다. 300초/1,500만 건 측정과 archive fixture는 실행하지 않았다. 두 run stack과 모든
소유 리소스의 최종 service inventory는 0이다.

후보 B는 계약상 새 run ID로만 평가할 수 있다. 현재 보수적 누적 최대 `$29.634519`에서
`$35` hard-cap headroom이 `$5.365481`이라 추가 run은 승인하지 않았다.

## 검증 질문

1. `2 x c7g.large` host와 `2 x 1 vCPU/2 GiB` task가 120-shard stream의 50k/s 입력을
   backlog 누적 없이 처리할 수 있는가?
2. KCL lease/checkpoint와 ECS service replacement가 task 하나의 종료 후 누락 없이
   처리 재개하는가?
3. ClickHouse schema, late-event, async insert, 중복 수렴과 archive 안전성이 Lambda
   구현과 동일하게 유지되는가?
4. 이 구성이 Lambda의 지속 GB-second 비용과 concurrency quota 없이 `$35` 실험 상한
   안에서 재현 가능한가?

## 고정 topology와 후보

```text
Phase 3 qualified c7g.2xlarge producer, Locust workers 8
  -> run-owned provisioned Kinesis, 120 shards
  -> KCL 3.x application, polling retrieval
  -> ECS on EC2 service, desired task count 2
  -> same-AZ private HTTP 8123
  -> run-owned r7g.2xlarge EC2 ClickHouse
```

| 항목 | 후보 A: 최초 run | 후보 B: 조건부 fallback |
| --- | --- | --- |
| ECS host | `2 x c7g.large` | `2 x c7g.xlarge` |
| host당 배치 | consumer task 1개, `distinctInstance` | consumer task 1개, `distinctInstance` |
| task CPU/memory | `1 vCPU / 2 GiB` | `2 vCPU / 4 GiB` |
| ASG min/desired/max | `2/2/2` | `2/2/2` |
| architecture | ARM64 | ARM64 |
| purchase | On-Demand | On-Demand |
| 측정 중 scaling | 금지 | 금지 |

후보 A가 correctness, failover와 환경 gate는 통과했지만 CPU, memory, iterator age 또는
drain capacity만 실패한 경우에만 후보 B를 새 run ID로 한 번 평가한다. 후보 A의 설정을
run 도중 바꾸거나 같은 run에서 결과를 합치지 않는다. Spot, `t4g` burst credit, ECS
autoscaling과 placement 변경은 측정 변수가 되므로 사용하지 않는다.

기존 Go collector의 `c6i.xlarge` host당 4 vCPU/8 GiB와 직접 비교해도 후보 A host는 절반,
consumer task envelope는 1 vCPU/2 GiB다. 다만 이전 collector는 HTTP/ALB ingress와 Kinesis
write 경로였고 이번 consumer는 Kinesis read와 ClickHouse batch insert 경로이므로 과거
CPU 수치를 capacity 증명으로 재사용하지 않는다.

## 고정 입력과 데이터 계약

- producer source:
  `performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/`
- producer compute: `c7g.2xlarge`, Locust worker 8개
- payload:
  `performance-tests/phase1-kinesis/payloads/sdk-compatible-event-bodies.ndjson`
- payload SHA-256:
  `93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7`
- offered load: `50,000 records/s x 300초 = 15,000,000 records`
- event 1개 = Kinesis record 1개, partition key = `event_id`
- Kinesis: provisioned 120 shards, 24-hour retention, AWS-managed encryption
- ClickHouse: `r7g.2xlarge`, gp3 500 GiB/3,000 IOPS/500 MiB/s
- ClickHouse image: `clickhouse/clickhouse-server:26.3.13.31`; 배포 전 digest 고정

본 부하에는 위 원본 producer만 사용한다. producer code를 복사·재작성하거나 새 load
generator를 만들지 않는다. Phase 4는 `pyproject.toml`과 `uv.lock`으로 실행 환경만 고정하고
`uv sync --frozen`, 원본 contract test, source/payload hash를 매 run 재검증한다.

### schema와 변환

[`assets/clickhouse/phase4-schema.sql`](../../infra/source-tree/assets/clickhouse/phase4-schema.sql)의 DDL을 그대로
사용한다.

- 정상 event는 `events`에 적재한다.
- invalid JSON, 필수 필드/시각 변환 실패, 미지원 `schema_version`은 `raw_events`에 Base64
  원문과 Kinesis metadata를 저장한다.
- `properties_json`은 입력 문자열 그대로 저장하며 parse/stringify하지 않는다.
- UTC 기준 `event_date < 오늘 - 7일`이면 어떤 테이블에도 저장하지 않고
  `LateEventDropped`만 증가시킨다.
- `events`는 `ReplacingMergeTree(ingested_at)`이며 논리 정합성·archive query는 `FINAL`을
  사용한다.
- 기존 호환성을 위해 `raw_events.lambda_received_at` 컬럼명은 바꾸지 않는다. ECS
  consumer가 batch를 받은 UTC timestamp를 이 컬럼에 기록한다.

변환과 ClickHouse writer는 기존
`src/phase4-clickhouse-handler/index.ts` (external snapshot reference: `../src/phase4-clickhouse-handler/index.ts`)에서
runtime-neutral module로 추출해 재사용한다. Lambda event wrapper, ESM response와 Lambda
client만 consumer image에 포함하지 않는다. 기존 Jest fixture가 새 worker에도 동일하게
통과해야 한다.

## consumer 처리 계약

### KCL과 checkpoint

- KCL 3.x MultiLangDaemon이 TypeScript record processor를 실행한다. Java runtime, KCL,
  Node.js, npm dependency와 OCI base/final image는 정확한 version/digest로 고정한다.
- retrieval mode는 enhanced fan-out이 아닌 polling으로 고정한다. stream resharding과
  autoscaling은 측정 중 금지한다.
- application name은 run ID를 포함하고 다른 run과 공유하지 않는다.
- KCL 3.x가 사용하는 lease, worker metrics, coordinator state DynamoDB table은 모두 run
  tag와 명시적 이름을 갖고 on-demand mode로 생성·증거화·cleanup한다.
- 본 부하는 `LATEST`에서 시작한다. service가 task 2개, shard lease 120개와 readiness
  metric을 확인한 뒤에만 producer를 시작한다.
- KCL의 at-least-once delivery를 전제로 누락은 허용하지 않고 physical/logical duplicate를
  각각 측정한다.

한 KCL `processRecords` batch의 순서는 다음과 같다.

```text
Kinesis metadata와 Base64 원문 보존
-> 변환 성공 / raw_events / late-event로 분류
-> events와 raw_events를 동일 query shape로 async INSERT
-> 두 INSERT와 wait_for_async_insert 성공 확인
-> LateEventDropped metric 기록
-> 마지막 성공 sequence number checkpoint
```

두 INSERT 중 하나라도 실패하면 checkpoint하지 않는다. HTTP deadline은 20초이고 consumer
내부 retry는 exponential backoff와 jitter를 포함해 최대 5회, 전체 60초로 제한한다. 최종
실패 시 원문 batch와 stream/shard/sequence metadata를 run 전용 S3 failure prefix에 durable
write한 뒤 terminal-failure metric을 기록한다. 이 object가 하나라도 생긴 run은 `failed`다.
poison batch 무한 loop를 막기 위한 terminal checkpoint는 S3 write가 성공한 경우에만 허용하며
반드시 실패 증거와 함께 기록한다.

SIGTERM을 받으면 새 batch intake를 중지하고 최대 120초 동안 in-flight INSERT를 마무리한다.
성공 batch만 checkpoint한 뒤 종료한다. timeout 또는 강제 종료된 batch는 checkpoint하지 않아
다음 worker가 다시 읽게 한다. ECS `stopTimeout`은 120초로 고정한다.

### ClickHouse async insert

다음 값을 바꾸지 않는다.

```text
async_insert=1
wait_for_async_insert=1
async_insert_max_data_size=16777216
async_insert_use_adaptive_busy_timeout=1
async_insert_busy_timeout_min_ms=50
async_insert_busy_timeout_max_ms=300
async_insert_deduplicate=0
```

`12,500 rows/flush`, 약 `4 flush/s`는 50k/s와 기존 평균 payload 크기에서 나온 가설일
뿐 합격 조건이 아니다. `system.asynchronous_insert_log`, parts와 merge backlog의 실측값으로
판정한다. 모든 worker는 table, column list, format과 settings가 같은 query shape를 사용한다.

## network, secret과 IAM

- 공유 `LoopAdDev*` stack을 import, update 또는 replace하지 않는다. run 전용 stack만
  배포하고 stack 이름을 명시한다.
- ECS host, task ENI와 ClickHouse는 `ap-northeast-2a`의 같은 VPC/AZ에 둔다. 8123은
  consumer security group에서 ClickHouse security group으로만 허용한다.
- ALB, NAT Gateway, public 8123과 cross-AZ data path를 만들지 않는다.
- private task/host가 필요한 AWS API는 Kinesis, ECR API/DKR, CloudWatch Logs,
  Secrets Manager, ECS agent/telemetry interface endpoint와 S3/DynamoDB gateway endpoint를
  사용한다. 실제 synthesized endpoint와 시간·data 비용을 cost model에 포함한다.
- ClickHouse credential은 generated secret으로 만들고 task 환경에는 secret ARN만 넣는다.
  plaintext value를 source, env, command output, logs와 evidence에 남기지 않는다.
- execution role은 ECR pull/log delivery만, task role은 대상 stream read, run 전용 KCL
  metadata table item operation, 대상 secret read, failure prefix write와 필요한 metric
  publish만 허용한다. wildcard resource는 서비스가 요구하는 최소 범위를 문서화하지 않으면
  허용하지 않는다.
- ClickHouse archive는 instance role과 S3 gateway endpoint를 사용하고 static access key를
  query나 설정에 전달하지 않는다.
- host memory는 consumer가 `/proc/meminfo`를 60초마다 읽어 run ID와 task ARN만 차원으로
  기존 `awslogs` 경로에 EMF로 기록한다. 추가 AWS API 권한이나 endpoint를 만들지 않는다.

## quota와 비용 gate

Lambda concurrency와 Fargate vCPU quota는 이 topology에 적용되지 않는다. 그렇다고 quota
gate 자체가 사라지는 것은 아니다.

2026-07-16 Lambda run의 read-only evidence에는 서울 리전 EC2 Standard On-Demand quota가
`80 vCPU`, 보수적 현재 사용량이 `4 vCPU`로 기록돼 있다.

| profile | ClickHouse | producer | ECS host | 현재 포함 최대 합계 |
| --- | ---: | ---: | ---: | ---: |
| 후보 A | 8 | 8 | 4 | `4 + 8 + 8 + 4 = 24` |
| 후보 B | 8 | 8 | 8 | `4 + 8 + 8 + 8 = 28` |

두 값 모두 snapshot quota 80보다 작다. 그러나 실행 직전 다음을 live read-only로 다시
확인하지 못하면 deploy하지 않는다.

- AWS account/region/operator와 root 사용 승인
- EC2 Standard On-Demand applied quota, 현재 running/pending vCPU와 profile 최대 vCPU
- `c7g.large` 또는 fallback `c7g.xlarge`, `r7g.2xlarge`, `c7g.2xlarge`의 같은 AZ offering
- Kinesis open shards + 120 <= applied shard quota
- ECS task/container-instance, ASG, VPC, ENI, endpoint, DynamoDB table, ECR와 S3 quota
- run/session ownership tag 충돌 없음

AWS paid wall-clock은 deploy부터 120분이며 100분에 새 검증을 중단하고 cleanup을 시작한다.
실행 직전 public price로 Kinesis, ClickHouse, producer, ECS hosts, EBS, endpoints, ECR,
DynamoDB, S3와 CloudWatch를 다시 계산한다. 계획 누적이 `$32` 이상이면 새 load를 시작하지
않고, cleanup reserve `$3`를 포함한 deterministic maximum이 `$35`를 넘으면 deploy하지 않는다.
Cost Explorer는 지연되므로 hard stop 시계로 사용하지 않는다.

## 검증 순서

### 1. static과 local gate

- npm build, 기존 handler Jest와 새 worker/CDK assertion 통과
- synth template에 정확히 2개 On-Demand ECS host, desired task 2, ARM64 image digest,
  run 전용 KCL metadata tables, private endpoints, alarms와 least-privilege role이 존재
- Lambda, ESM, Fargate, ALB, NAT Gateway와 public 8123 리소스가 없음
- shared dev stack diff에 이번 변경으로 인한 update/replacement/deletion이 없음
- Docker ClickHouse와 고정 Kinesis emulator로 정상/invalid/duplicate/retry/late boundary 검증
- checkpoint 이후 restart와 checkpoint 이전 crash를 각각 재현해 누락 0 및 기대 중복 계측
- 50,000-row async flush와 archive fixture 통과
- local test 중 실제 AWS API 호출 0

### 2. AWS correctness smoke

정상 1,000건, invalid fixture와 late boundary fixture를 run 전용 stream에 보낸다.

통과 조건:

- Kinesis 입력 = `events FINAL` unique + `raw_events` + `LateEventDropped`
- 정상/원문 누락 0, 예상 외 event 0, terminal-failure S3 object 0
- KCL lease 120개가 task 2개에 할당되고 processing/checkpoint error 0
- iterator age가 smoke 종료 후 0

### 3. task replacement fault smoke

소량의 지속 입력 중 task 하나만 `aws ecs stop-task`로 중단한다. host나 ASG를 종료하지 않고
본 50k/s 측정과 분리한다.

통과 조건:

- ECS service가 desired/running task 2개로 복구
- KCL lease가 생존/replacement worker에 재분배되고 checkpoint부터 처리 재개
- 정상 event 누락 0, 허용된 at-least-once duplicate만 존재
- terminal failure, OOM과 crash loop 0
- 입력 종료 후 10분 안에 iterator age 0

fault smoke 실패 시 본 부하를 시작하지 않는다.

### 4. 50k/s live load

1. task 2개, lease 120개, ClickHouse와 metric readiness를 확인한다.
2. 새 가격·누적 비용과 100분 cleanup deadline을 다시 계산한다.
3. 원본 Phase 3 producer로 정확히 `50,000 records/s x 300초`를 전송한다.
4. 입력 중 iterator age, KCL lag, task/host CPU·memory·network, ClickHouse insert/parts/merge/
   disk를 같은 시간축으로 수집한다.
5. producer 종료 후 iterator age 0과 count 완성까지 최대 30분 기다린다.

본 부하 합격 조건:

- producer logical success `15,000,000`, retry/failure 0
- 정상 `event_id` 누락 0, `raw_events` 원문 누락 0
- terminal-failure object/metric 0, KCL processing/checkpoint error 0
- producer 종료 후 30분 안에 iterator age 0과 ClickHouse count 완성
- task CPU p95 < 70%, memory p95 < 70%, OOM/unplanned restart 0
- 두 host 모두 같은 측정 구간의 CPU, host memory, NetworkIn/NetworkOut datapoint 존재
- Kinesis read throttle 0, ClickHouse insert error 0
- active parts와 merge backlog가 종료 후 지속 증가하지 않고 disk < 80%
- physical duplicate와 `FINAL` logical duplicate 수, 원인과 비용이 보고서에 기록됨

CPU 또는 memory p95가 70% 이상이어도 correctness·drain은 통과할 수 있지만 production
headroom gate 때문에 후보는 `failed(capacity)`다. 이 경우에만 후보 B 새 run을 허용한다.

### 5. archive fixture와 cleanup

Phase 4는 소량 fixture로만 다음 순서를 검증한다.

```text
events FINAL export
-> manifest
-> ClickHouse/S3 count, unique, time range, checksum equivalence
-> equivalence 통과 후 DROP PARTITION
-> source 삭제 후 S3 direct query equivalence
```

첫 equivalence 전에는 DROP하지 않는다. 종료·stop gate·100분 deadline 중 하나가 발생하면
즉시 producer를 중단하고 current run/session tag가 붙은 stack과 리소스만 cleanup한다.
CloudFormation, ECS/ASG/EC2, Kinesis, DynamoDB 3개 KCL table, ECR, ENI/endpoint/SG, S3,
logs, secret과 alarm을 service API로 재조회해 inventory 0을 증명한다.

## 판정

- `passed`: 모든 local/AWS correctness, fault, full-load, archive와 cleanup gate 통과
- `failed`: 시스템이 존재한 상태에서 correctness, failover, capacity 또는 성능 조건 위반
- `aborted`: 비용·시간·안전 stop gate 또는 사용자가 실행을 중단해 즉시 cleanup
- `inconclusive`: 필수 evidence가 없어 통과·실패를 사실로 판정할 수 없음

quota, operator, price 또는 ownership gate 실패는 본 부하를 강행할 근거가 아니다. 배포 전
실패라면 해당 사실, not-measured 항목과 cleanup inventory를 남기고 `aborted` 또는
`inconclusive`를 선택한다.

## 완료 산출물

- TypeScript KCL record processor와 고정 ARM64 image/lock/digest
- run 전용 ECS on EC2/CDK stack, KCL metadata, failure archive, IAM과 alarms
- 기존 schema/transform/async insert 회귀 테스트
- local correctness, retry/checkpoint/fault, 50,000-row async와 archive evidence
- AWS run의 `run.json`, `infra.md`, `commands.md`, metrics, counts, cost와 report
- service별 cleanup inventory 0과 최종 판정

## 공식 참고

- [KCL 기능과 MultiLangDaemon](https://docs.aws.amazon.com/streams/latest/dev/kcl.html)
- [KCL 3.x DynamoDB metadata tables](https://docs.aws.amazon.com/streams/latest/dev/kcl-dynamoDB.html)
- [KCL consumer IAM](https://docs.aws.amazon.com/streams/latest/dev/kcl-iam-permissions.html)
- [Amazon ECS service quotas](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-quotas.html)
