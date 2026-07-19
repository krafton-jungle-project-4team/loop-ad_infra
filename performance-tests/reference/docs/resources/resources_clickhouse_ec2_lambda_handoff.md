# ClickHouse EC2·Lambda 적재 작업 인수인계

## 목적

새 Codex 세션이 이전 대화를 다시 읽지 않고도 다음 작업을 이어 가기 위한 기준 문맥이다.

다음 구현의 설계, CDK 코드, 테스트와 실험 문서를 한 흐름으로 완성한다.

```text
Phase 4: 고정 producer -> Kinesis -> Lambda -> ClickHouse EC2
Phase 5: load -> collector -> Kinesis -> Lambda -> ClickHouse EC2
Phase 6: ClickHouse -> S3 Parquet archive -> ClickHouse 직접 조회
```

이 문서는 인수인계 자료다. 실제 계약은 구현과 함께 갱신할 Phase 4~6 가이드와 테스트가
결정한다.

> 2026-07-16 correction: Lambda 구현과 로컬 검증은 완료됐지만
> `run_20260716_101059_phase4_clickhouse_lambda` (external snapshot reference: `../performance-tests/run_20260716_101059_phase4_clickhouse_lambda/report.md`)와
> `run_20260716_114704_phase4_clickhouse_lambda` (external snapshot reference: `../performance-tests/run_20260716_114704_phase4_clickhouse_lambda/report.md`)가
> 모두 account concurrency `10 < 120` gate로 배포 전에 `aborted`됐다. AWS Lambda 경로는
> 통과하지 않았으며 ClickHouse Cloud/ClickPipes도 현재 실행 결과가 아니다. 활성 후속 후보는
> [`guide_phase4_kinesis_ecs_clickhouse_test_draft.md`](../drafts/guide_phase4_kinesis_ecs_clickhouse_test_draft.md)에
> 분리돼 있다.

## 먼저 읽을 파일

- `src/cdk-app.ts`: CDK 합성 단위 등록
- `src/cdk-app-config.ts`: 허용 environment와 환경변수 계약
- `src/loop-ad-stack.ts`: 기존 dev VPC, 보안 그룹, ClickHouse EC2와 secret 연결
- `src/dev-config.ts`: dev ClickHouse 크기와 공통 상수
- `assets/user-data/clickhouse.sh`: 기존 ClickHouse Docker bootstrap
- `src/perf-phase1-kinesis-stack.ts`: 성능 시험 스택 분리와 Kinesis 소유권 패턴
- `test/data.test.ts`, `test/perf-phase1-kinesis.test.ts`: CDK assertion 방식
- `docs/guide_aws_event_pipeline_performance_test.md`: 전체 Phase 계약
- `docs/guide_phase4_kinesis_clickhouse_ingest_performance_test.md`: 수정할 Phase 4 가이드
- `docs/guide_phase6_clickhouse_s3_archive_lifecycle_test.md`: archive 검증 계약
- `performance-tests/phase4-clickhouse/README.md`: 수정할 Phase 4 실행 checkpoint
- `.codex/skills/event-pipeline-loadtest-runner/SKILL.md`: Phase 0~6 실행 규칙

## 현재 저장소 상태

- 브랜치: `codex/aws-perf-test-plan`
- 문서 기준 HEAD: `eca4f09c docs: align pipeline tests with 10-day budget`
- 관련 선행 커밋:
  - `a586384b docs: add ClickHouse archive lifecycle phase`
  - `fcb57117 docs: redefine event pipeline performance phases`
  - `6851c9b2 perf: qualify c7g.2xlarge kinesis generator`
  - `df6bd1f0 perf: record locust kinesis qualification results`
- worktree에는 Phase 1 실험 산출물과 사용자의 다른 수정이 많이 남아 있다. 새 작업은
  시작 전에 `git status --short --branch`로 다시 확인하고, 관계없는 파일을 수정·정리·stage하지
  않는다.
- 중지된 이전 Codex goal의 비용 또는 deadline 계약은 재사용하지 않는다. 이 문장의 원래
  snapshot과 달리 2026-07-16 Lambda re-entry goal은 `$15` hard cap, `$12` 새 load 금지선,
  120분 wall-clock을 사용했고 위 두 번째 run에서 preflight 단계에 종료됐다.

## 확정된 실험 분리

| 범위 | 경로 | 주로 증명할 것 | 상태 |
| --- | --- | --- | --- |
| Kinesis 수집 | load -> collector -> Kinesis | accepted rate, PutRecords 오류·재시도, throttling, readback 정합성 | Phase 0~2 완료 |
| ClickHouse 적재 | 고정 producer -> Kinesis -> Lambda -> ClickHouse | consumer lag, rows/s, 누락·중복, parts/merge backlog, drain 시간 | Phase 3 producer 완료, Phase 4 다음 작업 |
| 최종 통합 | load -> collector -> Kinesis -> Lambda -> ClickHouse | 경계별 count 불변식, end-to-end 가시성 지연, 구성요소 상호작용 | Phase 5 예정 |

Phase 3 입력 계약은 다음과 같다.

- producer candidate: `c7g.2xlarge`, Locust worker 8개
- 목표: 50,000 records/s를 300초 동안 생성
- payload: 이벤트당 1,341 bytes
- event 1개 = Kinesis record 1개
- partition key: `event_id`
- KPL aggregation 미사용
- payload pool SHA-256:
  `93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7`

## 변경된 아키텍처 결정

기존 Phase 4 문서는 ClickHouse Cloud와 Kinesis ClickPipe를 전제로 한다. 이 전제는 폐기한다.
새 채택 후보는 self-managed ClickHouse EC2와 Kinesis event source Lambda다.

```text
Kinesis Data Streams
  -> Lambda event source mapping
  -> VPC 내부 Lambda
  -> ClickHouse HTTP insert
  -> EC2의 ClickHouse raw_events
```

ClickHouse Cloud AWS Marketplace와 ClickPipes 가격은 비교 자료로만 남길 수 있다. 새 구현,
합격 판정과 비용 gate에 혼합하지 않는다.

## 이미 있는 ClickHouse와 재사용 경계

`LoopAdDevDataStack`은 이미 단일 ARM ClickHouse EC2를 만든다.

- instance: `t4g.medium`
- root gp3: 100 GiB, encrypted, termination 시 삭제
- image: `clickhouse/clickhouse-server:26.3.13.31`
- HTTP port: 8123
- secret: Secrets Manager 이름만 user-data에 전달하고 instance role이 런타임에 읽음
- 운영 형태: public subnet, public IP, SSM managed instance

이 자원은 저사용량 dev 시연에는 맞지만 50k records/s 성능 증거용으로 너무 작다. 공유 dev
스택의 `ClickHouseInstance` 크기나 볼륨을 바로 바꾸면 교체와 데이터 손실 위험이 있다.

권장 경계는 별도 `perf-phase4-clickhouse` environment와 전용 Phase 4 stack이다. 기존
`clickhouse.sh`의 공통 bootstrap은 construct/helper로 재사용하되, 기존 dev construct의
logical ID와 동작을 보존한다.

## 권장 리소스 구성

### CDK 합성 단위

다음을 별도 파일과 테스트로 추가한다.

- `src/perf-phase4-clickhouse-stack.ts`
- `src/perf-phase4-clickhouse-config.ts` 또는 같은 역할의 명시적 설정 모듈
- `test/perf-phase4-clickhouse.test.ts`
- Lambda handler와 단위 테스트를 위한 전용 디렉터리
- `perf-phase4-clickhouse` environment를 `src/cdk-app.ts`와
  `src/cdk-app-config.ts`에 등록

Phase 4 stack은 run/session ownership tag, 명시적 removal policy와 cleanup 대상을 가진다.
Phase 1/3 stream을 사용할 때는 stream ARN을 명시적으로 입력받는다. 숨은 cross-stack export를
추가하지 않는다. stream을 Phase 4가 직접 만들면 측정 구간에만 유지하고 종료 후 삭제한다.

### 네트워크

- ClickHouse와 Lambda를 같은 VPC와 가능하면 같은 AZ에 둔다.
- Lambda security group에서 ClickHouse security group의 8123 포트로만 ingress를 허용한다.
- 성능 경로에 NAT Gateway를 두지 않는다. 4시간 50k 입력의 raw data는 약 965.5 GB이므로
  NAT 또는 cross-AZ 경유는 비용과 병목을 만든다.
- Lambda가 Secrets Manager나 CloudWatch에 접근하는 경로는 VPC endpoint 또는 현재 subnet
  구조를 기준으로 명시적으로 결정한다. endpoint 비용도 cost model에 넣는다.
- 인터넷에서 ClickHouse 8123을 열지 않는다. 운영자 접근은 SSM을 기본으로 한다.

### ClickHouse EC2

초기 비교 후보는 다음과 같다.

- 10일 저사용 기준: `r7g.xlarge`, 4 vCPU, 32 GiB
- 성능 시험 후보: `r7g.2xlarge`, 8 vCPU, 64 GiB
- storage 후보: gp3 500 GiB 또는 1 TiB, 500 MiB/s

단일 EC2라서 HA가 없고 인스턴스 또는 볼륨 장애가 중단으로 이어진다. 10일 내부 시험과
삭제 가능한 성능 데이터라는 조건에서만 이 trade-off를 수용한다.

10일 유지용 작은 stack과 측정 시간 전용 큰 stack을 분리할지, 같은 인스턴스를 stop/resize할지는
코드 작성 전에 결정한다. 안전한 기본값은 공유 dev stack을 건드리지 않고 전용 성능 stack을
측정 시간에만 만드는 것이다.

`raw_events`에는 최소한 다음 필드가 필요하다.

- `event_id`: count/duplicate 기준
- `event_time`: 사용자 행동 시각
- `producer_sent_at`: ingestion latency 시작점
- `run_id`: run 격리
- `ingested_at`: ClickHouse 도달 시각
- 원래 `hotel_rec_promo.v1` payload 필드

partition은 event date를 기준으로 하되, Phase 6의 기간별 archive/drop 단위와 맞춘다.
ORDER BY는 `event_id` 하나를 무조건 채택하지 말고 실제 조회와 duplicate 검증 패턴을 반영한다.

### Lambda consumer

저장소가 TypeScript CDK를 사용하므로 별도 이유가 없으면 handler도 Node.js/TypeScript로
시작한다. Lambda는 Kinesis를 직접 polling하지 않고 event source mapping으로 batch를 받는다.

초기 설정 후보:

- architecture: ARM64
- batch size: 최대 1,000
- maximum batching window: 1초
- response type: `ReportBatchItemFailures`
- bounded retry와 maximum record age
- on-failure SQS queue
- reserved concurrency와 명시적 timeout/memory
- iterator age, error, throttle, DLQ, duration alarm

batch 1,000개는 현재 payload 기준 약 1.34 MB라 Lambda event payload 한도 안이다. 실제 Kinesis
event의 base64와 JSON overhead를 포함한 payload 크기는 테스트에서 다시 확인한다.

ClickHouse write는 작은 INSERT가 과도한 part와 merge backlog를 만들지 않게 한다.

- HTTP `JSONEachRow` batch insert
- `async_insert=1`
- 정확성 시험에서는 `wait_for_async_insert=1`
- `event_id`를 보존하고 Lambda 재시도에 따른 duplicate를 계측
- payload, secret과 record 단위 로그 금지

ClickHouse batch insert 자체가 실패하면 어떤 Kinesis record만 실패했는지 알 수 없는 경우가
있다. handler는 성공하지 않은 batch 전체를 재시도 대상으로 반환하거나 검증된 분할 전략을
사용해야 한다. 임의의 마지막 record 하나만 실패로 보고하면 누락될 수 있다.

Lambda의 batch/async insert로도 충분히 큰 write를 만들지 못해 part 수나 merge backlog가 계속
증가하면 Lambda를 고집하지 않는다. 그 결과를 증거로 남기고 ECS 또는 장기 실행 consumer를
다음 후보로 평가한다.

### 권한과 secret

- Lambda에는 대상 secret의 `secretsmanager:GetSecretValue`, 필요한 log 권한과 VPC ENI 권한만
  부여한다.
- Kinesis read 권한은 event source mapping에 필요한 대상 stream으로 제한한다.
- secret 평문을 CDK context, CloudFormation output, environment variable 또는 log에 넣지 않는다.
- EC2는 기존 방식처럼 secret 이름만 받고 instance role로 런타임 조회한다.

## 비용 한도와 계산 기준

사용 조건:

- 전체 잔여 예산 약 `$400`
- 다른 인프라 보호액 최소 `$200`
- Phase 4~6 성능·보존 경로 가용액 약 `$200`
- 누적 실험비 `$160` 도달 시 새 load 단계 금지
- 나머지 `$40`은 drain, 정합성 확인, cleanup과 가격 오차 reserve
- 전체 시스템은 약 10일 유지하지만 성능 시험 외 사용량은 매우 낮음

2026-07-16 서울 리전 공개 On-Demand 단가로 만든 거친 비교다. 실행 직전에 AWS Pricing
API와 실제 계정 조건으로 다시 조회한다.

| 항목 | 계산 가정 | 예상액 |
| --- | ---: | ---: |
| `r7g.xlarge` | 240시간 × $0.2584/h | $62.02 |
| gp3 500 GiB | 10일 | $14.99 |
| gp3 1 TiB | 10일 | $30.70 |
| gp3 extra throughput | 500 MiB/s 후보, 10일 | $5.62 |
| 4시간 `r7g.2xlarge` 증분 | xlarge 대비 | $1.03 |
| Lambda | ARM 1 GiB, 120 invocations/s, 200~500 ms, 4시간 | $4.95~11.87 |
| Kinesis | 120 shards와 PUT, 50k records/s, 4시간 | $23.57 |

합계 기준선:

- 500 GiB와 Kinesis 포함: 약 `$112.18~119.10`
- 1 TiB와 Kinesis 포함: 약 `$127.90~134.81`
- Kinesis가 다른 인프라 예산에 이미 포함되면 위 합계에서 약 `$23.57` 제외

snapshot, CloudWatch log, data transfer, VPC endpoint와 실패 재실행 비용은 제외했다. 따라서
이 값은 승인 가능한 상한이 아니라 preflight의 시작값이다.

120 shards를 10일 유지하면 shard-hour만 약 `$532.80`이므로 금지한다. 성능 stream은 load
window에만 유지하거나 즉시 scale-down/destroy한다.

## 구현 순서

1. `git status`와 현재 관련 파일을 확인하고 관계없는 dirty change를 목록화한다.
2. Phase 4 stack의 소유권과 수명주기를 결정한다.
3. stream ARN 입력, VPC/AZ, ClickHouse 크기, storage, secret 이름과 cleanup 정책을 config
   계약으로 고정한다.
4. 기존 ClickHouse bootstrap을 dev와 Phase 4가 안전하게 재사용하도록 작은 helper/construct로
   분리한다. 기존 logical ID 교체 여부를 CDK diff로 확인한다.
5. 전용 ClickHouse EC2, SG, Lambda, Kinesis event source, failure SQS, log와 alarm을 CDK로
   구현한다.
6. `raw_events` schema bootstrap과 Lambda 변환/insert handler를 구현한다.
7. handler와 CDK assertion test를 추가한다.
8. 기존 Phase 4 가이드, README와 runner skill의 Cloud/ClickPipe 표현을 EC2/Lambda 계약으로
   갱신한다. Phase 5와 Phase 6 연결도 함께 정정한다.
9. local ClickHouse container로 schema, 1개 event, batch 경계, malformed input, duplicate/retry와
   async insert를 검증한다.
10. `npm run build`, 대상 Jest, `git diff --check`와 synth/diff를 통과시킨다.
11. AWS credential, account/region, resource ownership, 현재 가격, 예산, quota와 cleanup gate를
    통과한 뒤에만 deploy한다.
12. correctness smoke부터 시작하고 단계별 실험 후 증거와 cleanup을 별도 logical commit으로
    남긴다.

## 로컬 테스트와 CDK assertion 최소 항목

Handler:

- base64 Kinesis record decode
- payload schema 변환
- 1개, 999개, 1,000개와 payload 크기 경계
- malformed record 처리
- ClickHouse HTTP 성공, 전체 batch 실패, timeout과 retry
- batch item failure 응답이 실제 미처리 record 집합과 일치
- duplicate `event_id`가 누락 없이 관측됨
- payload와 credential이 log에 없음

CDK:

- Lambda 한 개와 올바른 Kinesis event source
- batch, window, retry, record age와 failure destination 값
- SQS DLQ와 보존 기간
- least-privilege IAM
- Lambda-to-ClickHouse SG 경로만 허용
- NAT dependency 없음
- ClickHouse EBS encryption, gp3 크기/throughput와 termination 정책
- secret 값이 template/output에 없음
- log retention, alarm, ownership tag와 removal policy
- 기존 dev stack synth 결과에 의도하지 않은 replacement 없음

## AWS 실험 단계와 합격 조건

실행 단계:

1. 1,000 event correctness smoke
2. 낮은 RPS pilot
3. `10k -> 25k -> 50k records/s`
4. producer 중지 후 drain
5. count/duplicate/latency/parts/merge 증거 확정
6. Phase 6 archive fixture를 제외한 bulk data와 run 소유 자원 cleanup

한 단계에서 Lambda memory, concurrency, batch, ClickHouse instance size를 동시에 바꾸지 않는다.

필수 경계 불변식:

```text
producer successful
  = Kinesis unique event_id
  = ClickHouse unique event_id
```

at-least-once 재전송에 따른 raw duplicate는 허용할 수 있지만, 수와 원인이 기록되어야 한다.
다음 조건을 모두 만족해야 Phase 4를 통과한다.

- 목표 입력률을 producer가 실제 생성
- Kinesis write/read throttling 없음
- Lambda final failure와 DLQ message 0
- ClickHouse unique 누락 0
- duplicate 수와 원인 기록
- measurement 동안 iterator age가 무한 증가하지 않음
- drain timeout 안에 lag 0과 count 일치
- parts, merge backlog, CPU, memory와 disk가 안전선 안에 있음
- `producer_sent_at -> ingested_at/query visibility` p50/p95/p99 기록
- cleanup 검증 완료

즉시 중단 조건:

- 비용 gate 또는 ownership gate 실패
- Lambda error/throttle/DLQ 증가
- 두 연속 window에서 lag 증가 후 회복 신호 없음
- ClickHouse `too many parts`, insert error, restart 또는 merge backlog 지속 증가
- disk 안전 여유 미달
- 필수 metric/evidence 수집 실패

## Phase 6 보존 계약

성능 데이터 자체를 10일 이후 보존할 필요는 없다. 다음 사실을 재현 가능한 자료로 남기면 된다.

- 기간 조건을 만족한 ClickHouse partition을 고객 소유 S3에 Parquet로 archive
- source가 있을 때 ClickHouse `s3()` 등으로 직접 조회 가능
- 원본 partition을 drop한 뒤에도 같은 S3 data를 직접 조회 가능
- row count, unique count, checksum과 query evidence 일치
- 대량 성능 데이터는 삭제
- 1~100 MiB 최소 시연 Parquet와 run report, 설정·가격 snapshot만 유지

Standard-IA는 최소 저장 기간이 30일이라 10일 시험에서 실제 전환 검증 대상으로 쓰지 않는다.
필요하면 lifecycle rule syntax를 검증하고, 10일 안의 실제 archive/read 증명은 S3 Standard로 한다.

## 아직 증거로 결정할 항목

다음 값은 추측으로 고정하지 않는다.

- Phase 4 전용 VPC를 만들지, 기존 dev network construct를 안전하게 재사용할지
- 10일 demo EC2와 4시간 성능 EC2를 분리할지
- 500 GiB와 1 TiB 중 preflight disk upper bound를 만족하는 크기
- Lambda memory, timeout, reserved concurrency, parallelization factor와 batching window
- ClickHouse `PARTITION BY`, `ORDER BY`, TTL과 schema
- async insert의 duplicate/failure semantics를 보완할 idempotency 방식
- 50k에서 Lambda 구조가 충분한지, ECS consumer 비교가 필요한지
- ClickHouse HTTP TLS를 이번 내부 시험 범위에 넣을지

## 하지 말아야 할 것

- 관계없는 Phase 1 dirty files와 run evidence를 삭제, 포맷, stage 또는 commit하지 않는다.
- 공유 dev `t4g.medium` ClickHouse를 성능 합격 근거로 사용하지 않는다.
- Cloud/ClickPipes 비용과 EC2/Lambda 비용을 한 run ledger에 섞지 않는다.
- 120-shard stream을 10일 방치하지 않는다.
- NAT를 기본 경로로 추가하지 않는다.
- secret이나 payload를 CloudFormation, Lambda env 또는 log에 평문으로 남기지 않는다.
- local stub만 통과하고 AWS correctness가 증명됐다고 판정하지 않는다.
- AWS deploy, 부하 발생과 cleanup은 local gate와 비용/소유권 preflight 전에는 실행하지 않는다.

## 새 세션 시작 프롬프트

다음을 새 Codex 세션의 첫 요청으로 사용한다.

```text
`docs/resources_clickhouse_ec2_lambda_handoff.md`를 처음부터 끝까지 읽고 이 문서를 현재 작업의
기준 문맥으로 사용하세요. 먼저 `git status --short --branch`와 관련 파일을 검사하고, 기존의
관계없는 dirty change는 수정·삭제·stage하지 마세요.

목표는 전용 `perf-phase4-clickhouse` CDK 합성 단위에
`고정 producer -> Kinesis -> Lambda -> self-managed ClickHouse EC2` 경로를 추가하고,
handler/CDK 테스트와 Phase 4~6 문서를 EC2/Lambda 기준으로 갱신하는 것입니다.

먼저 기존 dev ClickHouse의 재사용 경계와 Phase 4 resource ownership/lifecycle을 확정한 뒤
구현하세요. local build/test/synth/diff와 비용·소유권 preflight 전에는 AWS에 deploy하거나
부하를 발생시키지 마세요. 구현 중에는 aws-cdk, aws-serverless,
aws-billing-and-cost-management, event-pipeline-loadtest-runner, dev-docs-rules 스킬을
해당 범위에 맞게 사용하세요. 이번 작업의 첫 단계는 설계 검증과 로컬 구현이며, 실제 AWS
실험은 별도 승인 범위로 취급하세요.
```
