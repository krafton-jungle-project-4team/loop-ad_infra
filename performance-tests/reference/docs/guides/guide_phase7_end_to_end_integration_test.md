# Phase 7 이벤트 파이프라인 전체 통합 테스트 실행 계약

이 문서는 이미 검증한 수집, 적재, archive 구현을 하나의 실제 데이터 경로로 연결해
검증하는 방법을 정의한다. Phase 7은 기능·정합성·복구·cleanup을 다시 확인한다. 이전
Phase의 historical verdict를 변경하지 않는다.

Phase 5는 사용자 결정으로 실행하지 않는다. 이는 `passed`가 아니라 `skipped`이며, Phase 7이
더 넓은 경로를 검증하도록 범위를 승계한다.

## 검증 경로

```text
oha
  -> TLS/H2 HAProxy (`sampled-202`, leastconn)
  -> Go collector (`go-batch`, Kinesis final-ACK)
  -> Kinesis
  -> native Java KCL 3.4.3 consumer
  -> EC2 ClickHouse `loopad.events` / `loopad.raw_events`
  -> run-scoped ECS one-shot task + Phase 6 Python archive core
  -> S3 Parquet/ZSTD + immutable COMMITTED
  -> source DROP 전후 ClickHouse direct S3 query
```

LocalStack은 Kinesis, DynamoDB, CloudWatch, Secrets Manager와 S3의 로컬 대체물로만 사용한다.
ClickHouse, HAProxy, collector, Java KCL consumer와 archive worker는 실제 구현을 실행한다.

## 두 실행 단계

| 단계 | 실행 위치 | 목적 | AWS 변경 |
| --- | --- | --- | --- |
| Phase 7-1 | 로컬 Docker Compose + LocalStack | 실제 구현 연결, endpoint adapter, count·archive·복구·cleanup 검증 | 금지 |
| Phase 7-2 | AWS `ap-northeast-2` 전용 stack | 실제 IAM/network/ECS/EC2/Kinesis/S3/CloudWatch 경로와 50k overlap 검증 | run-owned 자원만 허용 |

7-1이 `passed`, `awsReady=true`인 immutable handoff를 만들기 전에는 7-2를 시작하지 않는다.
7-2에서 구현 결함이 발견되면 같은 AWS run에서 코드를 고치거나 재배포하지 않는다. 증적을
남기고 cleanup한 뒤 새 7-1 run으로 돌아간다. Phase 7-2가 시작된 뒤의 이 회귀 검증, fresh
handoff와 fresh AWS run 생성은
[Phase 7-2 안정화 Goal](../processes/process_phase7_2_aws_integration_goal_prompt.md)이 같은 캠페인 안에서
반복한다. 한 attempt의 종료가 전체 Goal의 종료를 뜻하지 않는다.

## 고정 구현 입력

| 구성 | 고정 입력 |
| --- | --- |
| collector | sibling repository `loop-ad_event_collector`, commit `497315137251af82d0d203ce34702d5543553942`, `go-batch` |
| collector AWS 기준 digest | 과거 digest `sha256:ac2e96e69768492f4c7ea65a7bfa362d08315387348fc9712f6a2fe26d498260`; 재사용을 가정하지 않고 exact commit에서 다시 build 후 새 digest 기록 |
| HAProxy | `haproxy:3.2.4-alpine@sha256:1cbc82126de93c9548a7fc31141c361961ec93a8badf77c8c3e8e211d007790d` |
| oha | `ghcr.io/hatoo/oha@sha256:76c300321fd0101d7e0588ae0486956a83034d7057a37be052619fa28204a072` |
| Java consumer | 현재 native Java KCL 3.4.3 구현; 7-1 시작 전에 source commit과 image digest를 새로 고정 |
| ClickHouse | `clickhouse/clickhouse-server@sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797` |
| archive worker | Phase 6 handoff의 implementation SHA-256 `f4d455142e67dad5c66d36ade3b3cd9333e57f3bb435efb63463d99783b7c870` |
| schema | Phase 6 handoff의 schema SHA-256 `26e5589ccc6dba4ac4703dae61f5f7faae8139e2173c77e40338cc8eaa2b1fee` |
| ClickHouse memory | 현재 운영값 container/server/archive-query `8/7/6 GiB`; query는 `6..6.5 GiB` 운용 범위에서 server보다 최소 `512 MiB` 낮게 검증하며 exact acceptance 숫자로 취급하지 않음 |

Java consumer의 로컬 endpoint adapter와 Phase 7 stack은 구현 커밋으로 먼저 고정한다. 7-2
handoff는 그 커밋과 image digest를 명시해야 하며 dirty worktree의 암묵적 내용을 AWS image로
만들지 않는다.

Phase 6 AWS archive 단독 실행의 ClickHouse container memory peak 94.1%와 초기
`5.00/4.90/4.50 GiB` 설정은 역사적 기준이다. 현재 Phase 7은 실제 실패 증거에 따라
container/server/archive-query를 `8/7/6 GiB`로 운영한다. 이 중 query cap 같은 비성능 운영
튜닝값은 exact point acceptance가 아니라 충분한 하한과 상위 safety envelope로 검증한다. 변경
범위가 safety envelope 안이고 correctness·equivalence·DROP safety를 보존하면 숫자 하나의 차이만
으로 새 실패를 만들지 않는다. AWS attempt 안에서 설정을 바꾸지는 않으며, 변경은 cleanup 뒤 새
identity에서 확인한다.

## 공통 count 계약

모든 요청은 run별 전역 고유 `event_id`를 가진다. Phase 1 collector의 공개 HTTP schema에는
`run_id` 필드가 없으므로 HTTP payload에 이를 추가하지 않는다. 로컬 count는 `event_id` prefix로
run을 구분하고, downstream seeder/archive config만 별도 `run_id`를 사용한다. payload pool을
반복해 같은 event를 재전송하지 않는다.

```text
HTTP 202
  = collector final Kinesis ACK success
  = Kinesis accepted records

Kinesis accepted records
  = ClickHouse events FINAL unique
  + ClickHouse raw_events
  + LateEventDropped
```

at-least-once 처리로 physical duplicate가 생길 수 있으므로 physical rows와 `FINAL` unique를
모두 기록한다. HTTP/KCL count는 `event_id` prefix, archive count는 `run_id`와 workload 구간으로
분리한다. Kinesis와 CloudWatch의 지연된 metric만으로 누락을 판정하지 않고, collector counter,
KCL checkpoint, ClickHouse query와 결정적 event ID 표본을 함께 사용한다.

archive 대상 partition은 collector/KCL 경로로 만들지 않는다. Java consumer가 UTC 7일보다
오래된 event를 `LateEventDropped`로 처리하므로 Phase 6 seeder로 닫힌 partition을 먼저 넣는다.
현재 시각의 live event는 별도 open partition으로 흘려 archive 실행 중 적재가 계속되는지
검증한다.

## Phase 7-1 로컬 계약

### topology

- isolated Docker network 하나와 loopback publish만 사용한다.
- LocalStack `3.8.1`에서 `kinesis,dynamodb,cloudwatch,secretsmanager,s3`를 실행한다.
- HAProxy 한 개가 고정 IP의 collector 네 개를 H2C backend로 사용한다.
- collector 네 개는 exact collector commit에서 로컬 image를 build한다.
- Java KCL consumer 두 개는 같은 Kinesis stream과 lease table을 사용한다.
- ClickHouse 한 개와 Phase 6 worker 한 개를 사용한다.
- ClickHouse에는 Phase 6의 5 GiB container와 memory.xml을 그대로 적용한다.
- AWS SDK에는 fake credential, `AWS_EC2_METADATA_DISABLED=true`, 명시적 LocalStack endpoint만
  전달한다. runner의 before-send audit에서 non-local endpoint를 즉시 차단한다.

Java consumer는 production 기본 동작을 바꾸지 않는 endpoint adapter가 필요하다.
`PHASE7_LOCAL_MODE=true`일 때만 Kinesis, DynamoDB, CloudWatch, Secrets Manager와 S3 client에
명시적 endpoint override를 적용한다. local mode가 아니면 override 환경변수가 존재할 때
시작을 거부한다.

### 단일 whole attempt

1. collector exact commit의 Go test/race/vet/build를 통과한다.
2. Java Maven test, memory gate, Python unit, CDK Jest/build, Compose config를 통과한다.
3. LocalStack resource, ClickHouse schema, secret와 archive bucket을 만든다.
4. KCL consumer를 먼저 시작하고 모든 shard lease와 ready marker를 확인한다.
5. HTTP로 valid 1,000건을 보내고 consumer error path용 invalid 1건과 late 1건은 LocalStack
   Kinesis에 직접 넣는다.
6. Phase 6 seeder로 UTC today-8 partition에 1,000,000 rows를 넣고 quiescence를 확인한다.
7. 200 requested RPS, 120초 profile로 unique live event 24,000건을 보낸다. LocalStack
   backpressure로 ACK 완료 시간이 늘어나면 실제 RPS와 총 duration을 그대로 기록한다.
8. live load가 시작된 뒤 production worker의 test-mode 1M archive를 실행한다.
9. overlap 전에 collector 한 개와 consumer 한 개를 각각 계획적으로 교체하고 복구·lease 재분배를
   확인한다. 로컬 1M archive가 수 초 안에 끝나므로 교체 시간을 archive window에 억지로 맞추지
   않는다.
10. KCL drain, live count, old partition archive와 source DROP 후 direct query를 확인한다.
11. 모든 run-owned container, volume, network와 LocalStack resource를 제거하고 zero를 증명한다.

### 합격 조건

- correctness: `1000 events + 1 raw_event + 1 LateEventDropped = 1002`.
- live: unique event 24,000건의 HTTP 202, final ACK, Kinesis accepted, `events FINAL` unique가
  정확히 같다. 요청한 200 RPS와 ACK 완료 기준 실제 RPS를 모두 기록하되 실제 RPS는 로컬
  에뮬레이터 성능 합격 조건으로 사용하지 않는다.
- unexpected HTTP 429/5xx/transport error, collector/Kinesis final failure와 insert error가 0이다.
- 계획한 collector/consumer 교체는 각각 1회이고 그 외 restart/OOM은 0이다.
- old partition 1,000,000 rows의 pre-DROP, committed-pre-DROP, post-DROP 양방향 차집합이 0이다.
- archive `COMMITTED`가 존재하고 old source rows는 0, live partition은 그대로 남는다.
- live request window와 archive worker window가 실제 timestamp 기준으로 겹친다.
- non-local AWS SDK attempt가 0이고 최종 container/volume/network inventory가 0이다.

로컬 처리량과 latency는 환경 정보로만 기록한다. Phase 7-1은 50k capacity를 주장하지 않는다.

현재 whole attempt
`run_20260717_093049_phase7_local` (external snapshot reference: `../performance-tests/run_20260717_093049_phase7_local/report.md`)은
correctness 1,002건, 교체 후 200건, live 24,000건, closed partition 1,000,000건, archive/DROP 후
direct query, 실제 AWS 요청 0과 Docker inventory 0으로 `passed`, `awsReady=true`다. requested
200 RPS의 ACK 완료 기준 실제 처리율은 123.344704 RPS였으며 로컬 capacity 합격값으로 사용하지
않는다.

## Phase 7-2 AWS 계약

### 전용 통합 stack

기존 Phase 1과 Phase 4 stack을 동시에 배포하지 않는다. 두 stack은 서로 다른 VPC와 Kinesis를
생성하므로 end-to-end 경로가 되지 않는다.

새 `LoopAdPerfPhase7IntegrationStack`은 다음을 한 번만 소유한다.

- 2 AZ VPC, run-owned Kinesis 120 shards와 필요한 VPC endpoints
- TLS/H2 internal protocol NLB, internal collector/ClickHouse NLB와 `2 x c6in.xlarge` HAProxy
- 입력으로 고정한 ACM certificate와 canonical DNS name; protocol NLB가 TLS를 종료하고
  HAProxy에는 plaintext H2를 전달
- `6 x c6i.xlarge` collector, `go-batch`
- `2 x c7g.large` native Java KCL consumer host와 task `1 vCPU/2 GiB`
- private `r7g.2xlarge` ClickHouse, encrypted gp3 500 GiB
- KCL lease/worker/coordinator DynamoDB tables
- failure/archive S3 bucket, Secrets Manager credential, run-owned log groups와 roles
- Phase 6 `archive.py` 안전 조건을 그대로 실행하는 ARM64 one-shot ECS archive task
- `8 x c6in.large`, 16-process pinned oha load generator

세 NLB는 자동 생성 보안 그룹에 의존하지 않는다. protocol NLB는 generator SG에서 443만,
collector NLB는 run-owned diagnostic generator에서 8080만, ClickHouse NLB는 consumer/archive
SG에서 8123만 허용한다. 각 target SG는 해당 NLB SG만 허용하며 HAProxy에서 collector로 가는
Cloud Map direct path는 별도 SG rule로 제한한다.

HAProxy는 `_collector._tcp.phase7.internal` SRV record에서 collector 6개를 직접 찾고
`leastconn`, H2C, `http-reuse always`를 사용한다. successful 202 log는 1/1000 sampling하고
400~599는 모두 error level로 CloudWatch에 기록한다. `/metrics`는 run-owned generator SG에만
8404로 열고 backend 상태, queue, HTTP status class와 config SHA를 수집한다. awslogs delivery는
25 MiB non-blocking buffer를 사용한다.

collector와 consumer image lifecycle은 runtime과 분리한다. runtime stack이 먼저 삭제된 뒤 exact
tag/digest만 삭제하고 image stack을 삭제한다. 기존 shared ECR image나 dev resource는 삭제하지
않는다. CDK construct ID 변경과 stateful resource replacement를 막기 위해 `cdk synth`, unit,
`cdk diff`에서 replacement와 예상 resource 수를 gate로 검사한다.

CDK synth 자체는 ECS API의 health-check 숫자 범위를 검증하지 않는다. 합성 템플릿 테스트가 모든
`AWS::ECS::TaskDefinition` container의 `Interval 5..300`, `Retries 1..10`,
`StartPeriod 0..300`, `Timeout 2..60`을 직접 검사해야 한다.
`schema-guard`는 `Retries=10`, `Interval=10`, `StartPeriod=80`으로 고정한다. start period는
초기 실패를 retry count에서 제외하는 grace period이므로, probe cadence를 바꾸지 않고 기존의
설정상 180초 bootstrap 허용치를 유지한다. 한 번 성공하면 start period 중이어도 healthy가 되고
이후 실패는 count된다는 ECS 의미는 그대로 적용된다.

scored workload의 identity mode는 배포 전에 고정한다. `globally-unique-event-id` mode는 각 요청이
서로 다른 `event_id`를 정확히 한 번 선택해야 한다. 사용자가 사전에 승인한
`balanced-pool-sampled-with-replacement` 진단 mode는 pinned `oha 1.14.0`의
[`choices.choose(rng)`](https://github.com/hatoo/oha/blob/v1.14.0/src/request_generator.rs) 의미를
그대로 기록하고 전역 고유성을 주장하지 않는다. 대신 warmup과 score에 서로 다른 480-body
fixture를 만들고 120 shard에 정확히 4개씩 배치한다. score 판정은 HTTP 202 final ACK,
`AWS/Kinesis IncomingRecords`, consumer `phase4_batch_success`의 `inputRecords`, ClickHouse insert
완료 카운트가 같고 실제 처리율이 기준을 만족하는지로 한다. 이 완화는 실행 결과를 본 뒤 적용할
수 없으며 새 Run ID의 preflight 이전에 문서와 evaluator에 고정되어야 한다. closed partition
archive의 15M 고유 행, 3×5M Parquet, equivalence와 DROP 안전성은 완화하지 않는다.

### 빠른 안정화 진단

현재 2026-07-19 캠페인은
`phase8-composite-promotion-policy-20260719.json`의 사용자 승인 override를 적용한다. Attempt 17의
correctness/replacement와 완료된 300초 score 증거는 immutable 상태로 상속하고, 새 50k,
warmup, score는 실행하지 않는다. fresh standard integration stack에서는 `verify`의 service/TLS/
ClickHouse health를 event-load 없는 최소 smoke로 사용한 뒤 15M retain-source archive만 실행한다.
functional/archive pass와 최종 cleanup zero 뒤에는 전체 Phase 7-1/strict chain을 반복하지 않고
두 attempt의 증거를 composite `phase8-handoff.json`으로 결속한다. 중간 stopped-task tag
tombstone 때문에 runner cleanup gate가 먼저 실패했더라도 최종 service/Tagging API/global
inventory가 모두 0이면 cleanup-recovered 보충 정책으로 검증한다. Attempt 17과 scoped attempt의
`failed`, 그리고 scoped attempt의 `promotionEligible=false`는 바꾸지 않는다.

strict attempt에서 문제가 확인되면 전체 로컬 chain을 수정마다 반복하지 않는다. 먼저
`performance-tests/phase7_2-stabilization/issue-register.json`에 증상, raw evidence hash, 첫 실패
gate, 원인 가설과 confidence, 변경 파일, focused regression, AWS diagnostic 범위와 나중에 수행할
whole-local 항목을 기록한다.

cleanup inventory zero 뒤에는 fresh identity를 가진 `aws-full-stack-scoped-diagnostic` attempt를
만든다. Attempt 17의 `LoopAdPerfPhase7IntegrationStack` definition과 topology를 그대로 사용하며,
별도 diagnostic 전용 stack이나 축소 resource graph를 새로 만들지 않는다. 이 attempt는 다음
계약을 지킨다.

- 전체 Phase 7 image/runtime stack을 fresh identity로 한 번 배포하고 full topology가 만든 비용과
  resource를 unavoidable inventory로 기록한다.
- Run ID, Session ID, evidence directory, command seal, cost model과 cleanup deadline은 매번 새로
  만든다. 배포와 각 선언 stage는 한 번만 실행한다.
- 변경 범위의 unit/build/type check, exact-context synth, template validator와 cfn-lint처럼 배포
  안전성을 증명하는 focused local gate만 먼저 실행한다.
- 문제 stage와 필수 선행 stage만 실행한다. archive 진단은 `deploy`, `verify`, 15M `seed`,
  retain-source `archive`, committed/pre-DROP equivalence, evidence collection과 cleanup만 실행한다.
- diagnostic 범위 밖의 correctness, warmup, score, archive를 추가하지 않고 source DROP은 절대
  실행하지 않는다.
- 결과와 비용을 ledger/issue register에 남기고 authoritative service와 Tagging API inventory를
  zero로 만든다.

일반 scoped diagnostic의 `passed`는 해당 issue만 해결됐다는 뜻이며 Phase 7 전체 합격은 아니다.
현재 override에서는 fresh archive pass가 composite handoff의 archive 절반만 제공하고 Attempt 17이
성능/correctness 절반을 제공한다. issue register의 해결된 항목은 삭제하지 않으며, attempt 자체를
strict pass로 재분류하지 않는다.

단, production CDK가 monolithic이고 분리 작업이 오히려 추가 배포·비용·리스크를 만들며, 관련
수정 batch의 whole handoff가 이미 통과했다면 별도 diagnostic attempt를 만들지 않는 fast path를
사용한다. 새 attempt를 배포 전부터 strict full attempt로 봉인하고 correctness/recovery 뒤 기존
실패 gate를 정상 순서상 가장 먼저 확인한다. 통과하면 같은 Run ID를 재배포하거나 stage를
반복하지 않고 drain부터 cleanup까지 계속한다. 실패하면 그대로 `failed`로 보존하고 cleanup한다.
실행 중 diagnostic을 strict로 재분류하거나 strict stage를 생략하는 것은 금지한다.

### 인증과 preflight

AWS 명령 전 `aws login`을 먼저 실행한다. 다음 identity가 아니면 중단한다.

```text
account: 742711170910
region: ap-northeast-2
operator: arn:aws:iam::742711170910:root
```

루트 operator 사용은 사용자가 허용했지만 workload에는 access key를 전달하지 않는다. collector,
consumer, ClickHouse/archive와 generator는 각각 최소 권한 role을 사용한다.

배포 직전에 UTC 현재 시각을 기록하고 quota, AZ offering, CDK bootstrap, 이름 충돌, 이전 run
자원, ECR digest, current public price를 다시 조회한다. 계산기는 deploy부터 cleanup까지의 deterministic upper bound를
만든다. 기본 hard cap은 `$40`, 새 load 금지선은 `$35`, cleanup reserve는 `$5`다. 계산 결과가
`$40`을 넘으면 해당 attempt를 배포하지 않는다. 이 값은 strict full attempt의 기본 제한이다.
Targeted diagnostic은 범위에 맞는 더 작은 deterministic upper bound를 별도로 계산한다. 현재
사용자가 승인한 active budget epoch는 이전 attempt 비용을 admission에서 제외하고 `$60` hard
cap, `$55` new-paid-work stop과 `$5` cleanup reserve를 사용한다. 다음 attempt는
`active-epoch prior upper bound + next operational upper bound + $5 cleanup reserve <= $60`일 때만
추가 사용자 확인 없이 시작할 수 있다. 이전 epoch 비용은 lifetime 정보로 계속 보존한다. 유료
wall-clock은 attempt마다 최대 180분이며 160분에 무조건 cleanup을 시작한다. 사용자가 비용 reset을
명시하면 terminal attempt와 비용 증거는 수정하지 않고 기존 epoch를 닫은 뒤 새 epoch를 append한다.
새 epoch의 `$0`은 ledger에 기록한 정확한 다음 유료 경계부터만 적용한다.

CloudWatch Logs ingest upper bound는 5 GiB다. cost model은 operational maximum이 `$35` 미만이고
`$5` cleanup reserve를 더한 maximum이 `$40` 이하일 때만 통과한다. 이 상한은 successful 202
sampling을 전제로 하며 오류 log는 sampling하지 않는다.

### 실행 순서

1. 7-1 handoff와 현재 source/image/schema hash가 정확히 같은지 확인한다.
2. image를 build/push하고 tag-to-digest와 ARM64/AMD64 architecture를 검증한다.
3. image stack과 runtime stack을 배포하고 실제 resource, role, route, image, memory를 검증한다.
4. HTTP correctness 1,002건과 consumer task replacement 900건을 먼저 통과한다.
5. UTC today-8 partition에 Phase 6 production seeder로 15,000,000 rows를 넣는다.
6. 180초 non-scored warmup 뒤 별도 run ID로 50,000 RPS x 300초 score를 실행한다.
7. score 시작 뒤 출력된 task definition, capacity provider, subnet과 archive security group으로
   one-shot ECS archive task를 시작한다.
8. load 종료 후 KCL drain과 score event 15,000,000건의 ClickHouse 가시성을 기다린다.
9. archive 3 x 5,000,000 Parquet와 pre/committed/post-DROP 동등성을 검증한다.
10. metric, logs, CloudTrail, cost와 failure evidence를 수집한다.
11. runtime, exact images와 image stack을 순서대로 삭제하고 service API inventory zero를 확인한다.

warmup과 score는 다른 `run_id`를 사용한다. score 15,000,000건만 Phase 7 capacity 판정에 넣고
warmup은 별도 count로 완전히 drain한 뒤 보조 증거로 남긴다.

### 관측 증거

- oha worker별 corrected latency, error와 physical connection
- HAProxy stats/Prometheus, active backend, queue, 4xx/5xx와 config SHA
- collector debug counter, queue, batch, retry/final ACK와 runtime restart/OOM
- Kinesis IncomingRecords, throttles와 IteratorAgeMilliseconds
- KCL DETAILED metric, lease/table state, ECS task/host CPU·memory와 Container Insights
- ClickHouse `system.parts`, merge/mutation, async insert log, query log, disk와 container/host memory
- archive ECS task state/CloudWatch log, object head/checksum와 direct-query 결과
- CloudTrail operator/deploy/runtime audit와 modeled real-time cost

### 합격 조건

- Phase 2 envelope: actual RPS `>= 49,500`, corrected p95 `< 300 ms`, transport error rate
  `<= 0.001`, HTTP 429/5xx `0`.
- score 15,000,000건에 대해 HTTP 202, final ACK, Kinesis accepted, ClickHouse accounted count가
  정확히 같다.
- deterministic event ID sample의 end-to-end visibility p50/p95/p99와 전체 drain 시간을
  실제로 측정한다. drain은 최대 45분이고 iterator age가 10분 연속 감소하지 않으면 실패다.
- Kinesis throttle/final failure, KCL terminal failure, failure object, ClickHouse insert error,
  archive failure가 0이다.
- old 15,000,000-row partition은 정확히 3 x 5,000,000 Parquet이며 모든 양방향 차집합이 0이다.
- archive cycle은 30분 이내이고 score request window와 겹친다.
- source DROP 뒤 old rows는 0이고 score live partition 15,000,000 unique rows는 남아 있다.
- ClickHouse/consumer/collector host CPU와 memory p95는 70% 미만, filesystem peak는 80% 미만,
  unexpected restart/OOM은 0이다. ClickHouse container peak는 별도 headroom 지표로 남긴다.
- 실제 비용 상한과 180분 deadline을 지키고 run-owned billable/service inventory가 0이다.

## 중단과 재시도

다음 중 하나라도 발생하면 새 load/archive를 시작하지 않거나 즉시 중단한다.

- source/handoff/image/schema hash 불일치
- AWS identity, ownership, region, quota, price 또는 cost gate 실패
- smoke count mismatch, KCL terminal failure, ClickHouse readiness fail-open
- archive commit 없이 source가 없거나 pre-DROP 동등성 실패
- 비용·시간 hard stop 또는 cleanup evidence 수집 실패

부분 resume과 같은 run ID 재사용은 금지한다. 구현 오류는 `failed`, 외부 preflight 차단은
`blocked` 또는 `aborted`, 필수 증거 유실은 `inconclusive`, 모든 조건 충족만 `passed`다.
terminal attempt는 증거, 원인, 수정, 비용과 cleanup 결과를 stabilization ledger와 issue
register에 기록한다. authoritative inventory zero와 캠페인 비용 gate를 통과하면 focused 수정과
fresh full-stack scoped AWS diagnostic을 우선 이어간다. 관련 diagnostic이 통과한 변경을 batch한 뒤에만
fresh 7-1 handoff와 strict AWS attempt를 만든다. routine retry를 위해 Phase 7-2 Goal을 종료하거나
별도 승인을 기다리지 않는다.

## run 산출물

각 단계는 새 immutable directory를 사용한다.

```text
performance-tests/run_<timestamp>_phase7_1_local_integration/
performance-tests/run_<timestamp>_phase7_2_aws_integration/
performance-tests/run_<timestamp>_phase7_2_aws_full_stack_scoped_diagnostic/
performance-tests/phase7_2-stabilization/attempt-ledger.json
performance-tests/phase7_2-stabilization/issue-register.json
performance-tests/phase7_2-stabilization/resume.md
performance-tests/phase7_2-stabilization/phase8-handoff.json
```

현재 override에서는 Attempt 17 성능/correctness와 Attempt 23 최소 smoke/archive 통과 및 최종
cleanup zero를 결합한 composite `phase8-handoff.json`을
[Phase 8 최종 통합 승격 Goal](../processes/process_phase8_final_integration_goal_prompt.md)로 넘긴다. Phase 8은
유료 AWS 실험을 반복하지 않는다. 최종 통합 기준선은
[`performance-tests/phase8-final/phase8-manifest.json`](../../tools/phase8-final/phase8-manifest.json)이다.

필수 산출물은 `run.json`, `commands.md`, `infra.md`, `failures.md`, `report.md`,
`correctness-summary.json`, `metrics-summary.json`, `archive-validation.json`,
`cleanup-verification.json`이다. 7-1은 `local-handoff.json`과 non-AWS audit를, 7-2는 image,
price/cost, deployment, CloudWatch/CloudTrail과 service inventory evidence를 추가한다.

implementation/tooling, 7-1 evidence, 7-2 evidence와 최종 상태 문서는 각각 별도 논리 커밋으로
남긴다.
