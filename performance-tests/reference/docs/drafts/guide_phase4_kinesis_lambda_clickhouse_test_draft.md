# Phase 4 Kinesis→Lambda→ClickHouse 구현·성능 테스트 계획 초안

> Historical: 이 계약의 AWS run은 Lambda quota gate에서 배포 전 `aborted`됐다. 활성 후속
> 계약은
> [`guide_phase4_kinesis_ecs_clickhouse_test_draft.md`](guide_phase4_kinesis_ecs_clickhouse_test_draft.md)다.
> 아래 Lambda/ESM 고정값은 ECS 실험에 적용하지 않는다.

## 목적

Kinesis에 적재된 기존 `hotel_rec_promo.v1` 이벤트를 Lambda가 소비하여
ClickHouse에 누락 없이 배치 INSERT할 수 있는지 확인한다. Lambda는 기존 Kafka
Engine의 소비·변환·적재 역할만 대체하며 이벤트 모델은 변경하지 않는다.

이 문서는 처음에는 실행 전 초안으로 작성됐지만, 2026-07-16 goal에서 schema, late-event,
Lambda/ESM, async insert와 부하 고정값의 실행 계약으로 채택됐다. 실제 milestone, 명령과
판정은
`performance-tests/phase4-clickhouse/exec-plan.md` (external snapshot reference: `../performance-tests/phase4-clickhouse/exec-plan.md`)에
기록한다.

첫 실행
`run_20260716_101059_phase4_clickhouse_lambda` (external snapshot reference: `../performance-tests/run_20260716_101059_phase4_clickhouse_lambda/report.md`)은
구현과 로컬 gate를 통과했지만 AWS Lambda account concurrency `10`이 fixed reservation
`120`보다 작아 배포 전에 `aborted`됐다. AWS correctness와 15M drain은 not measured다.

같은 고정 계약을 다시 검증한
`run_20260716_114704_phase4_clickhouse_lambda` (external snapshot reference: `../performance-tests/run_20260716_114704_phase4_clickhouse_lambda/report.md`)도
2026-07-16T11:45Z preflight에서 account concurrency `10`, reservation `120`을 다시 확인해
배포 전에 `aborted`됐다. 두 번째 run은 build, Jest, uv, producer hash/contract, CDK synth,
고정 Docker correctness·50,000-row async flush·archive fixture를 재통과했지만 AWS smoke와
15M drain은 여전히 not measured다.

## 범위와 고정 결정

```text
기존 Locust Kinesis producer
  -> 전용 Kinesis Data Stream
  -> Lambda event source mapping
  -> Lambda batch consumer
  -> EC2 ClickHouse
```

- 정상 데이터는 기존 `events` 테이블에 적재한다.
- JSON 파싱 또는 기존 컬럼 변환에 실패한 데이터만 `raw_events`에 보존한다.
- `schema_version=hotel_rec_promo.v1`과 `properties_json` 구조를 범용 스키마로 재설계하지
  않는다.
- `properties_json`은 이미 JSON 문자열인 입력 필드다. 내부 호텔 속성을 추출하거나
  parse/stringify하지 않고 입력 문자열 그대로 저장한다.
- 지원하지 않는 `schema_version`은 정상 이벤트로 강제 변환하지 않고
  `raw_events(error_code='unsupported_schema_version')`에 저장한다.
- 전송 보장은 exactly-once가 아니라 at-least-once로 간주한다. 누락은 허용하지
  않고 재시도 중복은 계측한다.
- `events`는 `(project_id, event_id)` 기준 `ReplacingMergeTree`로 논리적 중복을
  수렴시킨다.
- UTC 기준 7일 초과 late event는 `events`/`raw_events` 모두에 저장하지 않고
  CloudWatch `LateEventDropped` 메트릭만 증가시킨다.

### 고정 입력

- producer: Phase 3에서 합격한 `c7g.2xlarge`, Locust worker 8개
- producer source:
  `performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/`
- producer qualification:
  `performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/result-summary.json`
- offered load: `50,000 records/s`
- 선적재 시간: `300초`
- 예상 입력: `15,000,000 records`, `20.115 GB`
- event 1개 = Kinesis record 1개
- partition key: `event_id`
- payload pool SHA-256:
  `93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7`

## 구현 계획

### ClickHouse 테이블

`events`는 기존 payload 컬럼을 그대로 받고 event date 단위로 관리한다.

```sql
CREATE TABLE events
(
    project_id String,
    write_key String,
    schema_version LowCardinality(String),
    event_id String,
    event_name LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    event_date Date MATERIALIZED toDate(event_time),
    source LowCardinality(String),
    user_id Nullable(String),
    session_id Nullable(String),
    properties_json String,
    producer_sent_at Nullable(DateTime64(3, 'UTC')),
    run_id Nullable(String),
    kinesis_shard_id LowCardinality(String),
    kinesis_sequence_number UInt256,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY event_date
ORDER BY (project_id, event_id);
```

- 중복 판정 키는 `(project_id, event_id)`다.
- merge 완료 전에도 중복 없는 결과가 필요한 정합성 검증과 archive query는
  `FINAL`을 사용한다.
- 서로 다른 event date에 동일 `(project_id, event_id)`가 들어오면 파티션 간
  deduplication이 되지 않으며, 이 경우는 이벤트 계약 위반으로 계측한다.

`raw_events`는 Lambda가 `events`로 변환하지 못한 레코드만 받는다.

```sql
CREATE TABLE raw_events
(
    stream_arn String,
    shard_id LowCardinality(String),
    sequence_number UInt256,
    partition_key String,
    approximate_arrival_at DateTime64(3, 'UTC'),
    raw_payload_base64 String,
    error_code LowCardinality(String),
    error_message String,
    lambda_received_at DateTime64(3, 'UTC'),
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
    ingested_date Date MATERIALIZED toDate(ingested_at),
    run_id Nullable(String)
)
ENGINE = MergeTree
PARTITION BY ingested_date
ORDER BY (ingested_at, shard_id, sequence_number);
```

`error_message`는 길이를 제한하고 원문 payload, `write_key`, ClickHouse credential을 로그에
남기지 않는다.

### Lambda handler

1. Kinesis 레코드의 Base64 원문과 shard/sequence 메타데이터를 보존한다.
2. Base64 디코딩, JSON 파싱, 기존 `events` 컬럼 변환을 수행한다.
3. 변환된 `event_date < UTC 오늘 - 7일`이면 저장 대상에서 제외하고
   `LateEventDropped` Count만 증가시킨다. 이 레코드는 Lambda 성공으로 처리한다.
4. 변환 성공 레코드를 `events`용 `JSONEachRow` 배치로 만든다.
5. 변환 실패 레코드를 `raw_events`용 배치로 만든다.
6. ClickHouse HTTP INSERT에 `async_insert=1`, `wait_for_async_insert=1`,
   `async_insert_max_data_size=16777216`, `async_insert_busy_timeout_max_ms=300`을 적용한다.
7. 두 INSERT가 모두 성공해야 해당 Kinesis 배치를 성공 처리한다.
8. INSERT 오류·timeout이 있으면 해당 invocation에서 가장 낮은 sequence number 하나를
   `ReportBatchItemFailures`로 반환하여 그 지점부터 재처리한다.

Lambda timeout은 30초, ClickHouse HTTP 요청 deadline은 20초로 둔다. handler 내부에서
상한 없는 자체 재시도를 하지 않고 Kinesis Event Source Mapping이 재시도를 담당하게
한다. 목적지 INSERT 실패는 개별 레코드 오류가 아니라 배치 전체 실패로 취급한다.

두 테이블 INSERT는 원자적이지 않으므로 재시도 시 이미 저장된 행이 중복될 수 있다.
이는 Kafka Engine 대체 범위에서 at-least-once 제약으로 받아들이고 `event_id`와
`sequence_number`로 중복을 계측한다.

### Event source mapping 고정값

- architecture: ARM64
- memory: 2,048 MiB
- function timeout: 30초
- batch size: 10,000
- maximum batching window: 2초
- parallelization factor: 1
- reserved concurrency: 120, hard maximum 120
- starting position: 선적재 실험은 `TRIM_HORIZON`
- initial state: 선적재가 끝날 때까지 event source mapping 비활성
- partial batch response: 활성화
- bisect batch on function error: 비활성화
- maximum retry attempts: 5
- maximum record age: 3,600초
- on-failure destination: run 전용 S3 bucket
- event source mapping metrics: `EventCount` 활성화
- alarm: Lambda errors/throttles/duration/destination delivery failure, Kinesis iterator age

`batch size=10,000`은 레코드 수 상한이다. 실제 호출 배치는 단일 shard에서
2초 동안 모인 레코드 수 또는 Lambda 6 MiB payload 한도 중 먼저 도달한 값으로
결정된다. `parallelization factor=1`로 shard 내 처리 순서를 유지하고, 120-shard
동시 drain을 위해 Lambda concurrency 상한을 120으로 둔다.

입력 형식 오류는 `raw_events`로 정상 처리하고 ClickHouse 장애는 배치 전체 실패이므로,
이 실험에서는 batch bisect로 INSERT를 잘게 쪼개지 않는다. 최대 재시도 또는 record age를
넘긴 invocation은 원문까지 보존하는 S3 on-failure destination으로 보낸다. SQS/SNS
destination은 invocation 메타데이터만 보존하므로 원문 보존 요구에는 사용하지 않는다.
on-failure object가 1개라도 생기면 성능 결과는 실패이며, object는 cleanup 전에 증거로
복사한다.

`PolledEventCount`, `InvokedEventCount`, `DroppedEventCount`,
`OnFailureDestinationDeliveredEventCount`와 `DestinationDeliveryFailures`를 수집한다.
합격 run에서는 dropped/on-failure/destination-delivery-failure가 모두 0이어야 한다.

### 네트워크·secret·리소스 격리

- 공유 `LoopAdDevDataStack`을 변경하지 않고 run 전용 Phase 4 stack을 사용한다.
- ClickHouse와 Lambda는 같은 VPC와 같은 AZ의 사설 주소 경로를 사용한다.
- 성능 경로에 NAT Gateway와 cross-AZ 전송을 두지 않고 ClickHouse 8123을 인터넷에
  공개하지 않는다.
- ClickHouse image는 기존 검증 버전 `clickhouse/clickhouse-server:26.3.13.31`로
  고정한다.
- Lambda 환경변수에는 secret ARN/name만 넣고 평문 credential은 넣지 않는다.
- Lambda에는 대상 Kinesis read, 대상 secret read, log, VPC ENI, run 전용
  on-failure bucket write 권한만 부여한다.
- Lambda의 Secrets Manager 접근에는 같은 AZ의 interface VPC endpoint를 사용하고
  조회한 secret은 실행 환경에서 캐시한다.
- ClickHouse의 archive export·직접 조회에는 S3 gateway endpoint와 archive prefix로
  제한한 EC2 instance role을 사용한다. 정적 AWS access key는 ClickHouse 설정이나
  query에 전달하지 않는다.
- S3 on-failure bucket과 archive bucket/prefix는 용도와 cleanup 증거를 분리한다.
- 기존 dev stack의 logical ID 또는 리소스 교체가 CDK diff에 나타나면 배포하지 않는다.

### ClickHouse async insert 고정값

```text
async_insert = 1
wait_for_async_insert = 1
async_insert_max_data_size = 16 MiB
async_insert_use_adaptive_busy_timeout = 1
async_insert_busy_timeout_min_ms = 50
async_insert_busy_timeout_max_ms = 300
async_insert_deduplicate = 0
```

현재 고정 입력 `50,000 records/s x 1,341 bytes`에서 동일한 query shape의 INSERT가
하나의 async buffer로 결합된다는 전제로 다음을 예상한다.

- flush당 약 12,500 rows
- 약 250ms에 16 MiB 도달
- 초당 약 4회 flush

실제 `JSONEachRow` 크기와 buffer key 분리에 따라 달라질 수 있으므로
`system.asynchronous_insert_log`에서 flush rows, bytes, duration을 실측한다. 모든
Lambda는 같은 table, column list, format과 settings를 사용해 buffer가 불필요하게
분리되지 않게 한다. ClickHouse async deduplication은 사용하지 않고
`ReplacingMergeTree`로 논리적 중복을 수렴시킨다.

위 수치는 입력 크기에서 계산한 tuning 가설이지 단독 합격 조건이 아니다. 실제 평균이
범위를 벗어나도 처리량, iterator age, active parts와 merge backlog가 안정적이면 측정값과
원인을 기록하고 판정한다.

### 파티션 보존과 S3 archive

- `events` hot retention: UTC 오늘 포함 최근 7일
- archive 대상: `event_date < UTC 오늘 - 7일`
- archive 형식: Parquet
- S3 prefix:
  `s3://<bucket>/loopad/events/event_date=YYYY-MM-DD/run_id=<archive-run-id>/`
- archive job은 UTC 날짜 경계의 in-flight Lambda를 피하기 위해 01:00 UTC 이후에
  실행한다.

파티션 하나의 처리 순서는 다음으로 고정한다.

```text
archive 대상 선택
-> events FINAL을 Parquet으로 export
-> manifest 생성
-> ClickHouse/S3 count, unique key, min/max time, checksum 비교
-> 검증 통과 시 DROP PARTITION
-> source 삭제 후 S3 직접 조회 재검증
```

- export 또는 검증 실패 시 ClickHouse 파티션을 삭제하지 않는다.
- DROP 실패 시 S3와 ClickHouse의 중복 보존을 허용하고 다음 run에서 재검증한다.
- `raw_events`는 `ingested_date`별로 동일한 export·검증·DROP 순서를 사용하되,
  실제 보존 기간은 운영 정책에서 별도로 확정한다.

## 테스트 계획

### 로컬 실행 고정 조건

- Python 부하 생성기와 검증 스크립트는 `uv`로만 실행한다.
- 기존 producer의 `PayloadFactory`, `KinesisBatchSender`, Locust workload 로직을
  재작성하지 않는다. Phase 4 실행용 의존성만 `pyproject.toml`·`uv.lock`으로 고정하고,
  source hash와 기존 contract test가 일치하는지 확인한다.
- Python 의존성은 `pyproject.toml`과 `uv.lock`에 고정하고 전역 `pip install`이나
  수동 virtualenv를 사용하지 않는다.
- 재현 시 `uv sync --frozen`을 사용하고, 부하·테스트 명령은
  `uv run python ...` 또는 `uv run pytest ...`로 실행한다.
- Kinesis 에뮬레이터는 `amazon/kinesis-local` 또는 LocalStack 중 하나를 사용한다.
- 에뮬레이터와 ClickHouse Docker image는 `latest`가 아닌 고정 version/tag를
  사용하고, run 산출물에 버전을 기록한다.
- 로컬 테스트의 AWS SDK endpoint는 명시적으로 로컬 에뮬레이터를 가리켜야 하며,
  실제 AWS endpoint로 요청하지 않도록 dummy credential과 안전 검사를 둔다.

범위는 다음처럼 나눈다.

- `amazon/kinesis-local`: stream 생성, `PutRecords`, shard read, sequence/partition key 검증과
  handler 로컬 invocation을 빠르게 테스트할 때 사용한다. Lambda Event Source Mapping은
  로컬 harness가 Kinesis 레코드를 Lambda event 형식으로 변환해 대체한다.
- LocalStack: Kinesis→Lambda trigger, batch window, partial batch failure·retry 등 Event Source
  Mapping에 가까운 통합 동작을 테스트할 때 사용한다.

각 run에서 선택한 emulator, version, endpoint, stream/shard 수와 명령을 기록한다.
로컬 에뮬레이터 통과는 실제 AWS Kinesis Event Source Mapping correctness smoke를
대체하지 않는다.
로컬 기본 경로는 다음으로 고정한다.

```text
uv Python load generator
  -> amazon/kinesis-local 또는 LocalStack Kinesis
  -> Lambda handler/local event source
  -> Docker ClickHouse
```

### 1. 로컬 단위 테스트

- 기존 `hotel_rec_promo.v1` 페이로드가 `events` 행으로 변환됨
- `properties_json`이 변형 없이 유지됨
- invalid JSON, 필수 필드 누락, 시각 변환 실패가 `raw_events` 행으로 변환됨
- UTC 7일 경계 이전 late event는 두 테이블에 행을 만들지 않고
  `LateEventDropped=1`만 기록함
- UTC 7일 경계와 그 이후 event는 정상 적재됨
- 정상/실패 혼합 배치가 두 destination으로 분리됨
- ClickHouse 2xx에서 성공, 4xx/5xx/timeout에서 재시도 응답
- 모든 ClickHouse INSERT가 같은 query shape·settings를 사용함
- payload·secret 로그 방지

### 2. 로컬 ClickHouse 통합 테스트

Docker ClickHouse에 schema를 생성하고 정상 1,000건과 비정상 소량으로
correctness를 확인한다. 별도로 여러 동시 INSERT를 발생시켜 async buffer의
flush 크기를 측정한다. async 측정에는 정상 이벤트를 최소 50,000건 사용해 16 MiB
flush를 여러 번 관찰한다. Python 부하 명령과 검증은 모두 `uv run`으로 실행한다.

통과 조건:

- `events` unique `event_id` = 정상 입력 수
- `raw_events` = 변환 실패 입력 수
- 원문 payload 보존
- 동일 `(project_id, event_id)` 중복 INSERT 후 물리 행 수와 `FINAL` 논리 행 수를
  각각 계측할 수 있음
- Lambda가 만드는 여러 소량 배치가 하나의 async buffer로 결합됨
- async flush rows/bytes/초당 횟수와 예상 10,000~15,000 rows·약 4회/s의 차이가 기록됨
- active parts와 merge backlog가 테스트 종료 후 지속 증가하지 않음
- Kinesis emulator 입력 수 = handler 소비 수 = ClickHouse 저장 또는
  late-event 폐기 메트릭 수가 성립함
- 로컬 테스트 중 실제 AWS API 호출이 0건임

### 3. AWS correctness smoke

정상 1,000건과 의도적 비정상 레코드를 전송한다. 기존 대용량 producer에 비정상
페이로드를 추가하지 않고 별도 fixture를 사용한다.

통과 조건:

- Kinesis 성공 입력 = `events` unique 건수 + `raw_events` 원본 건수 +
  `LateEventDropped` 증가량
- 누락 0, 예상 외 event 0
- Lambda final failure 0, smoke 종료 후 iterator age 0
- S3 on-failure object 0
- `DroppedEventCount=0`, `OnFailureDestinationDeliveredEventCount=0`,
  `DestinationDeliveryFailures=0`
- 비정상 레코드가 무한 재시도되지 않음
- late event 입력 수 = CloudWatch `LateEventDropped` 증가량
- late event가 `events`와 `raw_events`에 저장되지 않음

### 4. 파티션·S3 archive 통합 테스트

Phase 4에서는 소량 fixture로 export→검증→DROP 안전성만 확인한다. 1,500만 건 bulk
partition의 실제 archive와 lifecycle 성능 평가는 Phase 6 범위로 남기며, Phase 4의
2시간 AWS 실행·`$15` 예산에 포함하지 않는다.

1. 오늘, UTC 7일 경계, archive 대상 날짜의 fixture를 각각 적재한다.
2. archive 대상 `events FINAL` 파티션을 S3 Parquet으로 export한다.
3. source/S3의 count, `uniqExact(project_id, event_id)`, min/max `event_time`,
   checksum을 비교한다.
4. 일치할 때만 ClickHouse source partition을 DROP한다.
5. DROP 후 S3 직접 조회를 반복하여 결과가 같은지 확인한다.
6. 삭제된 날짜의 late event를 재전송해 파티션이 다시 생기지 않고
   `LateEventDropped`만 증가하는지 확인한다.

### 5. 짧은 대용량 drain 테스트

1. Event source mapping을 비활성화한다.
2. 기존 producer로 `50,000 records/s x 300초`를 Kinesis에 선적재한다.
3. producer를 종료한다.
4. Event source mapping을 `TRIM_HORIZON`으로 활성화한다.
5. iterator age가 0이 되고 ClickHouse count가 완성될 때까지 drain 시간을 측정한다.
6. 누락, 중복, rows/s, Lambda 오류, parts와 merge backlog을 저장한다.
7. 증거 수집 후 run 소유 리소스를 즉시 제거한다.

## 판정 기준

합격:

- 정상 `event_id` 누락 0
- 비정상 입력 원문 누락 0
- 중복 입력의 `FINAL` 결과가 `(project_id, event_id)`별 1건
- late event가 ClickHouse에 저장되지 않고 메트릭 증가량이 입력 수와 일치
- final failure 0
- S3 on-failure object 0
- ESM dropped/on-failure/destination delivery failure 0
- producer 종료 후 30분 안에 iterator age 0과 ClickHouse count 완성
- 중복 건수와 원인이 보고서에 기록됨
- ClickHouse insert error가 회복되고 parts/merge backlog이 지속 증가하지 않음
- S3 archive 검증 전에 source partition을 삭제하지 않음
- DROP 후 S3 직접 조회가 source 삭제 전과 일치

불합격 또는 중단:

- correctness smoke 누락 또는 원문 mismatch
- ClickHouse 디스크 80% 이상
- Lambda throttle/final failure 발생
- producer 종료 후 iterator age가 10분 연속 감소하지 않음
- drain 30분 초과
- 비용 또는 wall-clock 제한 도달

## 예상 일정과 시간제한

기존 최대 예상 8시간에 2배 여유를 적용해 전체 goal 시간 상한을 2 작업일,
총 16시간으로 둔다.

| 작업 | 예상 시간 |
| --- | ---: |
| 저장소·입력 계약 확인 | 최대 2시간 |
| ClickHouse schema, Lambda handler, CDK 구성 | 최대 6시간 |
| 단위·CDK assertion·handler 로컬 테스트 | 최대 3시간 |
| 로컬 ClickHouse·Kinesis emulator 통합 테스트 | 최대 2시간 |
| AWS 가격·quota·ownership preflight | 최대 1시간 |
| AWS smoke, drain 테스트, 증거 수집·cleanup | 최대 2시간 |

AWS 실행 wall-clock은 deploy 시점부터 2시간으로 제한한다.

| 구간 | 시간 상한 |
| --- | ---: |
| deploy·상태 확인 | 30분 |
| correctness smoke | 20분 |
| 50k 선적재 | 5분 |
| drain | 30분 |
| 정합성·지표 수집 | 15분 |
| cleanup·삭제 확인 | 20분 |

- deploy 후 100분이 되면 새 검증을 시작하지 않고 cleanup을 시작한다.
- 120분이 되면 성능 판정과 관계없이 cleanup을 최우선으로 수행한다.
- cleanup이 완료되지 않으면 다음 run을 시작하지 않는다.
- 2배 시간 여유는 로컬 구현·검증의 일정 버퍼다. AWS 유료 리소스 유지시간과
  `50,000 records/s x 300초` 부하 시간은 늘리지 않는다.

## 예상 비용과 상한

서울 리전, provisioned Kinesis 120 shards, 전용 ClickHouse `r7g.2xlarge`, 5분
50k 선적재, deploy부터 cleanup까지 최대 2시간을 가정한 계획값이다.

2026-07-16 10:13Z AWS Price List API snapshot과 최대 30초 Lambda duration 상한을 적용한
현재 gate는 다음과 같다.

| 항목 | 계획 상한 |
| --- | ---: |
| Kinesis shard 120개, 2시간 | $4.440000 |
| Kinesis PUT 15,002,000 payload units | $0.306041 |
| ClickHouse `r7g.2xlarge`, 2시간 | $1.033600 |
| producer `c7g.2xlarge`, 1시간 | $0.326400 |
| Lambda ARM duration/request 상한 | $5.194124 |
| gp3 capacity/추가 throughput | $0.171781 |
| endpoint, secret/API, public IPv4 | $0.047096 |
| S3, CloudWatch, endpoint data, rounding reserve | $0.250000 |
| cleanup 전 operational maximum | `$11.769042` |
| cleanup reserve | `$3.000000` |
| hard-cap comparison maximum | `$14.769042` |

- 실행 승인 예산: `$15`
- 새 load 금지 선: 실시간 계획 누적 `$12`
- cleanup·지연 예비비: `$3`
- hard cap: `$15`; 도달 예상 시 즉시 load를 중지하고 cleanup
- smoke 또는 설정 보정 뒤 새 load를 시작하기 전에 가격, 실제 wall-clock과 잔여액을 다시
  계산한다. 자동으로 두 번째 50k 본 load를 시작하지 않는다.

가격 snapshot은 run마다 다시 생성한다. 예상 최대가 `$15`를 넘으면 배포하지 않고,
account 또는 quota gate가 실패해도 비용 gate 통과를 배포 승인으로 해석하지 않는다.
Cost Explorer는 지연될 수 있으므로 hard stop 시계로 사용하지 않는다.

AWS 공식 가격 기준:

- [Amazon Kinesis Data Streams pricing](https://aws.amazon.com/kinesis/data-streams/pricing/)
- [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/)

## 실행 전 체크리스트

- AWS account, region, stack, stream ARN과 run 소유 tag 확정
- 실행 시점 공개 단가로 `$15` hard cap 재계산
- 기존 producer 버전, worker 수, payload SHA-256 확인
- ClickHouse 디스크 여유와 schema 확인
- Lambda→ClickHouse 사설 SG 경로, Secrets Manager VPC endpoint와 NAT 미사용 확인
- event source mapping이 선적재 전 비활성인지 확인
- correctness smoke 통과 전 50k load 금지
- 증거 경로 `performance-tests/run_<id>_phase4_clickhouse_lambda/` 생성
- `LateEventDropped` 메트릭 namespace·alarm·보고서 집계 방식 확인
- S3 on-failure bucket이 run 전용이고 full invocation 보존·cleanup 범위에 포함되는지 확인
- archive bucket, partition prefix, manifest, lifecycle·cleanup 범위 확인
- destroy 명령과 cleanup 검증 대상 확인

## 완료 산출물

- Lambda handler와 변환 테스트
- Phase 4 CDK stack과 assertion 테스트
- ClickHouse `events`, `raw_events` schema
- late event 폐기 메트릭과 경계 테스트
- S3 Parquet archive, manifest, 정합성 검증과 partition DROP 테스트
- local 통합 테스트 결과
- AWS run 계약·명령·지표·정합성·비용·cleanup 증거
- S3 on-failure object count와 destination delivery failure 증거
- 최종 판정: `passed`, `failed`, `aborted` 또는 `inconclusive`
