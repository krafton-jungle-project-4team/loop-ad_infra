# Phase 1 Kinesis 단건 전환 성능 실험 가이드

이 가이드는 기존 Kafka 기반 event collector를 대체하지 않고, 별도 실험 브랜치와
별도 AWS stack에서 Kinesis Data Streams 전환 가능성을 검증하는 실행 절차다. 목표는
현재 계정의 Standard On-Demand EC2 vCPU 쿼터 안에서 `50,000 RPS`를 300초 동안
처리하는 구성을 증명하거나, 미리 정한 중단 조건에 도달했다는 근거를 남기는 것이다.

기존 Kafka Phase 1 계획과 목표 아키텍처는 유지한다. 이 실험의 collector 브랜치는
`main`에 병합하거나 push하지 않는다.

## 요구사항 변경 이력

- 2026-07-10: 최초 기준은 Standard On-Demand EC2 `32 vCPU`, 다른 Standard
  instance가 없을 때 최대 collector 7대였다.
- 2026-07-10: 사용자가 쿼터 승인을 확인해 collector 8, 12, 17대 탐색을 추가했다.
  따라서 32 vCPU/7대 기준은 폐기하고, 실제 적용 쿼터로 매번 계산한 안전 범위
  안에서 `1 -> 2 -> 4 -> 6 -> 8 -> 12 -> 17` 순서를 사용한다.
- 2026-07-10: 목표 성능 payload의 고유 `event_id` 요구를 12,288개에서 480개로
  변경했다. 80개 shard마다 profile별 2개, 총 6개 key를 사용한다.
- 2026-07-10: 사용자 지시로 Phase 1 누적 비용 상한을 `$30`에서 `$50`으로,
  선택적 최적화 run 중단선을 `$27`에서 `$45`로 변경했다.
- 2026-07-11: 후속 pprof 최적화 goal은 이전 goal 비용을 제외하고 새 누적 상한을
  `$30`으로 설정했다. 이 후속 goal에서는 `$50` 기준을 사용하지 않는다.

17대보다 큰 구성은 이 goal의 승인 범위가 아니다. 실제 적용 쿼터가 더 크더라도
별도 사용자 변경 없이 17대를 넘지 않는다.

이 가이드와 `performance-tests/phase1-kinesis/README.md`를 코드 변경 전에 별도
checkpoint로 검증·commit한다. 두 문서 commit 전에는 collector나 infra 코드를
수정하지 않는다.

## 실험 범위

### 목표

- SDK 호환 이벤트 한 개를 담은 HTTP 요청 한 개를 Kinesis `PutRecord` 한 번으로
  동기 발행한다.
- Kinesis ACK 이후에만 HTTP `202 Accepted`를 반환한다.
- 현재 EC2 vCPU 쿼터에서 안전한 collector 수 이하로 50k RPS 최종 기준을 검증한다.
- 정상 처리 한계를 넘으면 timeout, OOM 또는 무제한 대기 대신 즉시 `429`로
  제어하고, 부하를 낮춘 뒤 재배포 없이 회복하는지 확인한다.
- 성공하지 못한 run도 원인, 비용, 정리 결과와 함께 보존한다.

### 비목표

- 기존 Kafka 목표 아키텍처 교체
- collector `main` 병합, 실험 브랜치 push 또는 PR 생성
- `PutRecords`, HTTP 다중 이벤트 요청, 메모리 batcher, batch queue 또는 batch timer
- validation 제거, payload별 fast path 또는 Kinesis ACK 전 `202` 반환
- Spot, 이 실험 중 추가 쿼터 증설 요청, 기존 dev/운영 instance 중단 또는 instance
  family 우회. 이미 적용된 승인 quota는 live 값 검증 후 사용한다.
- Kinesis consumer와 ClickHouse downstream 성능 검증

## 저장소 안전

- infra의 현재 branch와 사용자 변경을 유지하고 reset, checkout, stash 또는 삭제하지
  않는다.
- 기존 untracked `_workspace/`, `im-not-ai/`를 수정하거나 stage하지 않는다.
- 다른 worktree와 branch를 건드리지 않는다.
- 기존 Kafka 계획과 `docs/architecture-modernized.html`을 Kinesis로 바꾸지 않는다.
- Kinesis 문서와 stack은 별도 이름으로만 추가한다.
- 사용자 요청 없이는 infra push나 PR을 만들지 않는다.

## 변경할 수 없는 단건 전송 계약

다음 등식은 모든 local/AWS run에서 유지해야 한다.

```text
유효한 이벤트 1개
  = HTTP POST 요청 1개
  = 검증된 원문 JSON body 1개
  = Kinesis PutRecord 호출 1개
  = PutRecord ACK 이후 HTTP 202 1개
```

- Kinesis record `Data`는 검증된 HTTP body의 원문 bytes다.
- validation에서 얻은 `event_id`를 partition key로 사용한다.
- `event_id`가 Kinesis partition key 제약인 Unicode 1~256자를 벗어나면 Kinesis
  호출 전에 `400`으로 거부하고 단위 테스트한다.
- `PutRecord` retry는 at-least-once 전송이므로 timeout 경계에서 중복 가능성이 있다.
- retry를 포함한 최종 성공 ACK가 없으면 `202`를 반환하지 않는다.
- `PutRecords`, batch queue, batch flush, 다중 이벤트 body를 사용하지 않는다.
- request context와 PutRecord timeout을 명시하고 initial call 뒤 SDK retry는 최대
  3회로 제한한다. AWS SDK의 `MaxAttempts`처럼 initial call을 포함하는 설정은 이
  의미에 맞게 환산하고 단위 테스트로 확인한다.
- 무한 retry, 무한 goroutine과 무한 queue를 금지한다.

## HTTP 응답 경계

| 조건 | 응답 | 추가 계약 |
| --- | --- | --- |
| PutRecord ACK 완료 | `202` | `{"accepted":1}` |
| 빈 body 또는 schema 위반 | `400` | Kinesis를 호출하지 않음 |
| `64 KiB` 초과 | `413` | Kinesis를 호출하지 않음 |
| 지원하지 않는 content type | `415` | Kinesis를 호출하지 않음 |
| admission limit 초과 | `429` | Kinesis를 호출하지 않음 |
| 제한된 retry 뒤 Kinesis throughput/throttling | `429` | `Retry-After: 1`, `Cache-Control: no-store` |
| Kinesis 내부 오류, 권한 오류, 네트워크 실패 | `503` | 구조화된 내부 로그, ACK 전 `202` 금지 |

Admission limit의 `429` body와 header는 고정한다.

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
Cache-Control: no-store
Content-Type: application/json

{"error":"too_many_requests","message":"collector capacity exceeded"}
```

`LOOPAD_MAX_INFLIGHT_REQUESTS`의 최초 값은 `1024`다. limiter는 비차단으로
획득한다. 획득 실패 시 goroutine이나 메모리 queue를 추가하지 않고 즉시 `429`를
반환한다. 이 값 변경은 다른 최적화와 섞지 않고 별도 가설과 run으로 검증한다.

## Payload 정책

### 목표 성능 run

- Phase 0와 같은 `hotel_rec_promo.v1` SDK 호환 envelope를 사용한다.
- `compact`, `standard`, `expanded` 프로필을 같은 비율로 사용한다.
- body는 대략 `1,047~1,526 bytes`, 평균은 약 `1.3 KiB`다.
- 고유 `event_id`는 정확히 `480`개다.
- 80개 shard마다 key 6개를 두고, profile별 key 2개를 정확히 배치한다.
- 요청 시점에 JSON을 새로 직렬화하지 않고, 요청마다 사전 검증한 body 하나만 고른다.
- field 순서, 특정 문자열, hash 또는 고정 body pool에 특화한 collector 최적화를
  금지한다.
- manifest에서 각 `event_id`의 Kinesis hash range를 계산해 80개 shard별 예상
  records/s와 bytes/s 최대값을 기록한다. key 분포가 충분히 균등하지 않으면 AWS
  목표 run에 사용하지 않는다.

Phase 0의 전체 192개 body 근거는 `1,044~1,535 bytes`, 평균 `1,348.015625
bytes`이고 프로필별 64개다. Phase 1에서는 같은 생성 규칙으로 480개의 고유
`event_id`를 만들고 80개 shard에 정확히 균등 배치한다. Phase 0의 12-row pool을
그대로 최종 성능 근거로 쓰지 않는다.

### 크기 민감도 검증

- 유효한 `512 B`, `4 KiB`, `16 KiB`, `32 KiB`, `64 KiB` 직전 body
- `64 KiB` 초과 body의 `413`

큰 payload AWS run은 Kinesis byte capacity를 넘지 않도록 RPS를 낮추고, 50k RPS
성공 판정과 분리한다. payload 최소 크기는 별도로 강제하지 않는다.

### SDK header 정책

현재 SDK 소스가 명시하는 request header는 `Content-Type: application/json`이다.
브라우저 `fetch`가 추가하는 header는 browser/runtime에 따라 달라질 수 있다.
`Content-Length`, `Content-Encoding`, `Content-Digest` 또는 W3C/IETF 표준 header를
최적화에 쓰려면 다음을 모두 만족해야 한다.

1. pprof가 해당 검증 비용을 병목으로 지목한다.
2. 같은 목적의 표준 header와 version을 조사하고 문서화한다.
3. SDK와 collector를 함께 검증한다.
4. header가 없는 기존 body validation 경로를 유지한다.
5. header와 body가 충돌하면 요청을 거부하거나 body 검증 결과를 우선한다.
6. Kinesis에 저장되는 원문 representation을 바꾸지 않는다.
7. local before/after 뒤 별도 AWS run으로 확인한다.

## Collector 실행 계약

- 기존 chi `/`, `/events`, `/health`, `/debug/pprof/*` 경로를 유지한다.
- AWS SDK for Go v2 Kinesis client의 `PutRecord`만 사용한다.
- Kafka dependency와 필수 환경변수는 실험 브랜치에서만 Kinesis 설정으로 바꾼다.
- 필수 설정은 `AWS_REGION`, `LOOPAD_KINESIS_STREAM_NAME`,
  `LOOPAD_MAX_INFLIGHT_REQUESTS`다.
- HTTP와 AWS SDK transport는 keep-alive connection pool을 재사용한다.
- graceful shutdown이 시작되면 새 요청 수락을 중지하고 진행 중인 요청은 제한된
  시간 안에 끝낸다.

## 관측성

`/debug/vars`와 pprof는 ALB listener에 연결하지 않는다. SSM 또는 collector host
내부에서만 수집한다. collector host가 public subnet/public IPv4를 사용하더라도
security group은 ALB security group에서 ingest port로 오는 traffic만 허용하고,
SSH, pprof와 debug inbound는 0개로 유지한다. SSM에서 localhost로 수집한다.

최소 지표:

- current/max in-flight
- admission `429` count
- PutRecord attempts, successes, throttles, failures, retries
- PutRecord latency
- Go heap/memory, `NumGC`, goroutine count

CPU profile은 병목 진단 run에서만 30초 수집한다. 최종 합격 run에는 profiler를
켜지 않는다.

## EC2 vCPU 안전 계산

region은 `ap-northeast-2`로 고정한다. 최초 알려진 Standard On-Demand quota
`32 vCPU`는 이후 승인으로 폐기됐다. 승인 요청 상태가 아니라 Service Quotas에
실제로 적용된 값과 running/pending instance를 모든 deploy 직전에 다시 조회한다.

```text
available_collector_vcpu =
  applied_standard_vcpu_quota
  - existing_standard_vcpu
  - load_generator_vcpu
  - safety_reserve_vcpu

max_safe_collector_count = floor(available_collector_vcpu / collector_vcpu)

required_vcpu(collector_count) =
  existing_standard_vcpu
  + load_generator_vcpu
  + safety_reserve_vcpu
  + (collector_count * collector_vcpu)
```

고정 입력:

- load generator: `1 x c6in.large = 2 vCPU`
- collector: 기본 `c6i.xlarge = 4 vCPU/host`
- `c6in.xlarge` 비교는 pprof와 network 지표가 근거를 제공할 때만 별도 가설로 수행
- safety reserve: `2 vCPU`
- generator와 collector를 같은 host에 배치하지 않음

다른 Standard instance가 없을 때 후보별 최소 적용 쿼터는 다음과 같다.

| Collector 수 | Collector vCPU | Generator + reserve | 필요한 최소 적용 쿼터 |
| ---: | ---: | ---: | ---: |
| 8 | 32 | 4 | 36 vCPU |
| 12 | 48 | 4 | 52 vCPU |
| 17 | 68 | 4 | 72 vCPU |

기존 running/pending Standard instance가 있으면 그 vCPU까지 추가로 필요하다. 모든
계산은 script로 수행하고 `run.json`과 `infra.md`에 저장한다.
`max_safe_collector_count < 1`이면 deploy하지 않고 `blocked`로 기록한다. 특정 후보가
live `max_safe_collector_count`보다 크면 그 후보를 실행하지 않는다. 이 goal에서
실제로 탐색할 상한은 `min(max_safe_collector_count, 17)`이다. 이 상한이 정해진
후보값이 아니면 마지막 탐색값으로 상한 자체를 추가한다. 예를 들어 max safe가
10이면 `1, 2, 4, 6, 8, 10`을 사용한다.

ASG/ECS가 순간적으로 쿼터를 넘지 않게 다음을 고정한다.

- ASG `maxCapacity == desiredCapacity`
- ASG instance maintenance policy `MinHealthyPercentage=0`,
  `MaxHealthyPercentage=100`
- `AZRebalance`와 `InstanceRefresh` process 중지
- scaling policy, scheduled action, warm pool 없음
- ECS capacity provider 자동 scale-out 비활성화
- service `minimumHealthyPercent=0`, `maximumPercent=100`
- ECS service `AvailabilityZoneRebalancing=DISABLED`
- host당 collector task 1개
- host network/fixed port에서는 `distinctInstance`로 old/new task 동시 배치 금지
- launch template/AMI/user data 변경 시 기존 perf stack을 먼저 삭제
- image/task definition 변경만으로 EC2 replacement 또는 refresh를 만들지 않음
- instance refresh, replacement ASG와 deployment surge 금지

`maxCapacity == desiredCapacity`만으로는 충분하지 않다. ASG `AZRebalance`는
일시적으로 max size를 초과할 수 있으므로 위 제약을 CDK assertion과 deploy
preflight에서 모두 확인한다. ECS AZ rebalance도 새 task를 먼저 배치할 수 있으므로
CDK synth assertion과 `describe-services` 결과에서 비활성화를 확인한다.

## 비용 한도

Phase 1 비용은 Phase 0와 분리하며 누적 상한은 `$50`다.

2026-07-10의 초기 참고값은 Kinesis 80 shards 약 `$1.48/hour`,
`c6i.xlarge` 약 `$0.192/hour`, `c6in.large` 약 `$0.1281/hour`, ALB base 약
`$0.0225/hour`다. 이 값은 deploy 근거가 아니다. 첫 AWS run 전과 단가가 바뀔 수
있는 시점마다 AWS Price List API로 `ap-northeast-2` 현재 단가를 다시 조회한다.

각 run 전에 script로 다음을 계산한다.

1. 이전 run의 누적 예상/실제 비용
2. deploy, 준비, smoke, load, metric 수집, destroy를 포함한 최대 실행 시간
3. Kinesis shard-hours와 PUT payload units
4. EC2, EBS, public IPv4, ALB base/LCU, CloudWatch Logs, EC2 detailed monitoring
   metric과 metric API 비용
5. Phase 1 image의 ECR storage/scan과 유료 Cost Explorer API가 있으면 그 비용
6. 이번 run 이후 누적 최대 비용

계산 결과와 최대 실행 시간은 deploy 전에 해당 run의 `run.json`과 `cost.md` 양쪽에
기록한다. 8, 12, 17대 후보도 collector EC2뿐 아니라 shard-hours, PUT, generator,
ALB/LCU, EBS, CloudWatch metrics/logs와 Phase 1 ECR 비용을 모두 포함한다.

누적 예상 비용이 `$45` 이상이면 선택적 최적화 run을 중단한다. 필수 run도 다음
실행으로 `$50` 이상이 될 것으로 예상되면 시작하지 않는다. 실행 중 `$50` 도달이
예상되면 `aborted`로 기록하고 즉시 destroy한다. Cost Explorer의 지연과 관계없이
실시간 계산과 사후 조회 값을 모두 남긴다.

deploy 시작부터 wall-clock hard deadline을 두고 EXIT/INT/TERM cleanup trap을
설치한다. 중단 판정은 지연되는 Cost Explorer가 아니라 upper-bound 누적 계산으로
수행한다. destroy가 실패하면 새 run을 시작하지 않고, billable resource가 실제로
사라질 때까지 upper-bound 비용을 계속 누적해 기록한다.

## 최초 local 구현·검증

collector는 local `1769eec`에서 만든 별도 worktree와
`codex/phase1-kinesis-transition` 브랜치에서만 수정한다.

```text
/private/tmp/loop-ad-event-collector-phase1-<timestamp>
```

collector 코드가 바뀔 때마다 AWS 배포 전에 다음을 실행한다.

1. `go test ./...`
2. `go test -race ./...`
3. `go vet ./...`
4. collector binary와 image build
5. local Kinesis protocol stub 준비 확인
6. 외부 egress가 없는 Docker internal network에서 10~30초 oha smoke
7. `202`, `400`, `413`, `429`, `503` 검증
8. HTTP request와 stub PutRecord의 1:1 비교
9. stub ACK가 지연되는 동안 `202`가 반환되지 않는지 확인
10. throttle/failure mapping과 goroutine leak 확인
11. 실제 AWS Kinesis metric/비용이 증가하지 않았는지 확인
12. 코드와 검증 결과를 논리 단위로 commit

stub 환경은 fake credential, test region, `AWS_EC2_METADATA_DISABLED=true`,
`AWS_ENDPOINT_URL_KINESIS=http://kinesis-stub:<port>` 또는 명시적인 SDK
BaseEndpoint를 사용한다. endpoint가 localhost/internal stub가 아니면 smoke를
시작하지 않는다. stub, collector와 oha를 포함한 전체 harness 명령에도 별도 bounded
timeout을 둔다. 실행 로그와 resolved config에서 stub endpoint를 다시 확인한다. 위
검증 중 하나라도 실패하면 해당 collector SHA를 AWS에 배포하지 않는다.

infra 코드가 바뀔 때마다 Jest, TypeScript build, CDK synth, stack assertion,
`cdk diff`, `git diff --check`, secret scan을 수행한다.

## AWS 실험 stack

별도 CDK environment와 stack만 사용한다.

```text
environment: perf-phase1-kinesis
stack: LoopAdPerfPhase1KinesisStack
```

필수 구성:

- perf 전용 VPC, NAT 없음
- public subnet의 `c6in.large` load generator와 collector EC2
- internal ALB와 ECS on EC2 collector
- host당 task 1개
- provisioned Kinesis stream 80 shards, retention 24시간, AWS managed encryption
- 대상 stream의 `kinesis:PutRecord`만 허용한 task role
- SSM, EC2 detailed monitoring 1분, CloudWatch Logs retention 1일
- 기존 `loop-ad/event-collector` ECR repository import
- `phase1-<collector-sha>` image tag는 표식으로만 사용하고, ECS task definition은
  `repository@sha256:<digest>`를 직접 참조
- 같은 immutable image digest를 task definition과 `run.json`에 기록
- `latest` tag와 ECR repository 자체는 변경/삭제하지 않음

초기 80 shards는 50k records/s와 평균 약 1,341 bytes에서 총 약 67 MB/s,
shard당 약 625 records/s와 0.84 MB/s가 되도록 잡은 값이다. partition key가
균등한데 Kinesis throttle이 생기면 즉시 run을 중단·기록·destroy하고 collector
성능 실패가 아닌 stream capacity 문제로 분리한다. 같은 비용과 quota 제한 안에서만
별도 판단하며 해당 run은 `inconclusive`다.

## AWS run 절차

각 run은 단일 가설을 갖는 독립 checkpoint다.
`event-pipeline-loadtest-runner`의 `deploy -> run -> record -> verify -> destroy ->
commit` cycle을 사용한다. 사용자 요구가 load driver를 override하므로 Artillery 대신
Phase 0에서 검증한 oha 흐름을 사용한다.

1. git status, 미완료 run, 이전 판정과 commit 확인
2. 새 run directory를 만들고 단일 가설, `run.json`, `cost.md`, `infra.md` 초안 작성
3. collector SHA, image tag/digest, task definition digest와 infra SHA 확인
4. deploy 직전 아래 8개 안전 항목 확인
5. `max_safe_collector_count`, 누적 비용과 이번 run 최대 비용 계산·기록
6. 정확한 stack 이름, run ID tag와 CDK diff 확인
7. 필요한 최소 resource deploy
8. `/events`에서 1k RPS, 30초, 예상 응답 `202` AWS 기능 smoke
9. smoke가 통과한 경우에만 탐색 또는 최종 load 실행
10. generator, ALB, collector, Kinesis, EC2, `/debug/vars` metric 수집
11. 모든 필수 artifact와 JSON 형식을 검증
12. destroy 직전 같은 8개 안전 항목과 resource ownership 재확인
13. 정확한 Phase 1 stack destroy
14. Kinesis, ASG, EC2, ALB, VPC와 CloudFormation 삭제 검증
15. run report, 비용, 판정과 cleanup 근거 작성
16. `git diff --check`, secret scan, run별 commit
17. 결과 commit 이후에만 다음 가설 시작

모든 deploy와 destroy 직전 확인할 8개 안전 항목:

1. `aws sts get-caller-identity`
2. 현재 AWS CLI region
3. CDK account와 region
4. 예상 stack 이름
5. 실제 적용 Standard On-Demand EC2 vCPU quota
6. running/pending Standard instance vCPU
7. 같은 이름의 기존 perf stack 존재 여부
8. 삭제 대상의 `Project`, `Environment`, 정확한 `RunId` tag와 stack ownership

account는 repo의 명시된 CDK 설정과 대조하되 문서에 불필요하게 반복하지 않는다.
account/region이 다르거나 기존 동일명 stack의 `Project`, `Environment`, `RunId` 중
하나라도 현재 run과 다르면 deploy/destroy하지 않고 `blocked`로 기록한다. 현재 run
소유 stack만 resume 또는 cleanup한다. 결과 artifact가 없어도 비용 누수가 예상되면
안전 항목을 확인한 뒤 즉시 destroy하고 artifact 미생성 실패를 기록한다.

Kinesis `DescribeLimits`에서 `ShardLimit - OpenShardCount >= 80`인지도 deploy 전에
확인해 `run.json`에 기록한다. 공유 open-shard 여유가 부족하면 stack을 만들지 않고
`blocked`로 기록한다.

Phase 0의 `/__fixed`와 `204` 판정은 Phase 1에서 금지한다. 기능·성능 부하는
collector ingest `/events`와 정상 `202`를 사용한다.

AWS 인증이 만료되면 static key를 만들지 않는다. AWS CLI 2.32.0 이상을 확인하고,
사용자 확인을 받은 뒤 `aws login`으로 단기 credential을 갱신한다.

## 부하 단계

탐색 RPS:

```text
1k x 30s AWS 기능 smoke
5k x 60s
10k x 60s
20k x 60s
35k x 60s
50k x 60s
```

collector 수:

```text
1 -> 2 -> 4 -> 6 -> 8 -> 12 -> 17
```

live `max_safe_collector_count`보다 큰 값은 생략한다. 상한이 17 이하이고 후보값이
아니면 마지막에 그 상한을 추가한다. 더 적은 수로 성공하면 큰 수를 탐색하지 않고
최종 검증으로 넘어간다. generator가 목표 RPS를 만들지 못하거나 자체 CPU/network
병목이면 collector 실패가 아니라 `inconclusive`다.

최종 후보는 50k RPS 30초 smoke, 60초 cooldown/metric 정렬 뒤 UTC 분 경계에서
50k RPS 300초를 실행한다.

별도 overload run에서는 `429` 증가, 5xx/OOM/무제한 memory 증가 부재,
`Retry-After: 1`, 부하 감소 뒤 재배포 없는 `202` 회복을 확인한다. 목표 50k run의
`429`는 성공 조건 실패다.

## 최적화 격리

순서는 다음과 같다.

```text
pprof 근거
  -> optimization backlog 항목 1개
  -> collector code commit 1개
  -> local before/after
  -> local 검증 기록 commit
  -> AWS run 1개
  -> AWS 결과 commit
```

backlog에는 ID, profile 기여도, 병목 근거, 예상 효과, 위험, 의존성, local/AWS
검증 방법, 우선순위와 상태를 기록한다. 이전 AWS 결과를 commit하기 전에는 다음
최적화를 적용하지 않는다.

여러 변경이 컴파일이나 실행에 불가분이면 기계적인 prerequisite commit을 먼저
분리하고 성능 개선을 주장하지 않는다. 가능한 중간 단계마다 검증하며, 함께 적용할
수밖에 없는 하위 변경도 local 결과를 각각 문서화한다. 외부 오픈소스 동작을
참고하면 repository, commit 또는 version, 적용한 동작과 적용하지 않은 동작을
기록한다.

local before/after는 다음 경로에 남긴다.

```text
performance-tests/local_<timestamp>_phase1_<hypothesis>/
```

필수 파일은 `hypothesis.md`, `commands.md`, `before.json`, `after.json`,
`verdict.md`, before/after collector SHA와 local 환경 정보다. 연산/allocation
benchmark는 10회, HTTP/transport는 stub 대상 10~30초 oha를 before/after 각 3회
이상 실행하고 중앙값을 script로 계산한다.

AWS 후보는 처리량 또는 ns/op 5% 이상, CPU/B/op/allocs/op/p99 중 하나 10% 이상,
또는 명확한 overload/error 기능 개선일 때만 채택한다. 계약 위반, 오류율/p99 악화,
memory 증가, race/leak, payload 특화 또는 batch 도입은 기각하고 결과도 commit한다.
local 결과는 AWS 후보를 고르는 가능성 검증일 뿐이다. 최종 채택은 같은 조건의 AWS
before/after run으로 판단한다.

## 진행 보고

각 checkpoint가 끝나면 collector/infra SHA, run ID와 판정, actual RPS와 p95/p99,
collector 수와 총 vCPU, Phase 1 누적 예상 비용, 삭제한 AWS resource, 다음 단일
가설과 blocked 여부를 보고한다. AWS deploy, load와 metric 대기 중에도 60초 넘게
상태 업데이트 없이 두지 않는다.

## Run 기록

경로:

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_phase1_kinesis_<short_name>/
```

필수 파일:

```text
run.json
infra.md
commands.md
metrics-summary.json
report.md
artifacts.md
cost.md
```

최소 기록값:

- collector SHA, image digest, infra SHA
- instance type/count, 총 vCPU, quota, 기존 vCPU, `max_safe_collector_count`
- Kinesis mode/shards와 payload manifest
- HTTP total, `202`, `400`, `413`, `429`, `5xx`, actual RPS, p50/p95/p99
- Kinesis IncomingRecords/throttle과 HTTP 성공 count 차이
- collector CPU/memory/GC/goroutine/restart, generator CPU/network/socket
- 단일 가설, 이전 run과 차이, 판정, 다음 행동
- 예상/실제/누적 비용
- destroy와 잔여 resource 검증

판정은 `passed`, `failed`, `aborted`, `inconclusive`, `blocked` 중 하나다. 잘못된
run도 지우거나 덮어쓰지 않고 report에 correction을 추가한다.

## 최종 성공 조건

현재 `max_safe_collector_count` 이하의 300초 run에서 다음을 모두 만족해야 한다.

- actual RPS `>= 49,500`, 목표 50,000
- HTTP `202 >= 99.9%`, `429 == 0`
- HTTP/transport error `<= 0.1%`
- ALB target 5xx `<= 0.1%`, ALB 자체 5xx는 0에 가까움
- p95 `<= 100 ms`, p99 `<= 500 ms`
- 성공 HTTP와 Kinesis IncomingRecords 차이 `<= 0.1%`
- Kinesis throttling 0
- task restart, OOM, unhealthy target 0
- 모든 collector의 `NumGC`가 run 동안 3회 이상 증가
- 5분 평균 collector CPU `<= 85%`
- 1분 평균 CPU가 연속 두 번 90%를 넘지 않음
- 마지막 1분 memory 평균이 warm-up 뒤 첫 1분보다 20% 넘게 증가하지 않음
- generator가 목표 부하를 생성함
- 이벤트 1개 = HTTP 1개 = PutRecord 1개 계약 유지
- Phase 1 누적 비용 `< $50`
- 결과 commit과 AWS resource destroy 완료
- collector `main` 미병합, 실험 브랜치 미push

## 중단 조건

다음 중 하나가 검증되면 새 성능 run을 중단한다.

- 승인된 최대 안전 구성 `min(max_safe_collector_count, 17)`까지 시험했지만 50k RPS 미달
- 최대 안전 구성에서 일반적이고 독립적인 pprof 후보를 모두 단계별 검증했지만 부족
- 다음 run으로 누적 비용 `$50` 이상 예상
- payload 특화, batch, validation 제거 또는 ACK 전 응답 없이는 목표 달성 불가
- `max_safe_collector_count < 1`
- 허용 AZ의 같은 instance type도 AWS capacity가 없어 최대 안전 구성 실행 불가
- generator/Kinesis 병목을 collector와 분리할 수 없음
- 안전한 `429` 없이 OOM 또는 연쇄 장애 반복
- local 검증에서도 추가 후보가 유효하지 않음

최대 안전 구성을 실제로 실행하고 부족하면 `failed`, 외부 제약으로 실행하지 못하면
`blocked`, metric이 불완전하면 `inconclusive`다. 어떤 판정이든 실패 run 기록,
cleanup, 최종 Explanation과 commit을 완료해야 goal이 끝난다.

최종 Explanation은 `docs/explanation_phase1_kinesis_transition_results.md`에 작성하고,
이 변경 이력을 포함한 goal 전문과 사용자 요구사항을 함께 보존한다.

## Cleanup

destroy 전후에 account, region, stack 이름과 resource tag를 확인한다.

```text
Project=loop-ad
Environment=perf-phase1-kinesis
RunId=<run_id>
```

이 goal이 만든 정확한 resource만 삭제한다. tag가 없거나 소유 관계가 불명확하면
삭제하지 않고 기록한다. 기존 VPC, dev/운영 EC2, ECR repository, Kafka, database,
ClickHouse를 중지·축소·삭제하지 않는다.

resource type에 따라 CloudFormation tag가 전파되지 않을 수 있으므로 deploy 직후
physical ID inventory와 explicit tag를 함께 저장한다. ASG instance tag는 launch에
전파하고 EBS 등 tag 전파가 보장되지 않는 resource는 physical ID로 소유권을
확인한다. 임시 S3는 정확한 bucket/key prefix가 이 run 소유일 때만 지우고 공유 CDK
bootstrap asset을 삭제하지 않는다.

최소 삭제 검증:

- `LoopAdPerfPhase1KinesisStack`가 존재하지 않음
- Phase 1 Kinesis stream이 존재하지 않음
- Phase 1 ASG와 active EC2가 0
- Phase 1 ALB/target group이 존재하지 않음
- Phase 1 ECS service/cluster/running task가 없고 task definition이 active하지 않음
- Phase 1 IAM role/instance profile/inline policy가 없음
- Phase 1 security group/ENI/IGW/route/VPC/EBS가 없음
- Phase 1 alarm/metric filter/log group과 이 stack의 custom-resource log가 없음
- 정확한 run 소유 임시 S3 object가 없음
- 현재 goal이 push한 정확한 `phase1-<collector-sha>` ECR tag를 expected digest 대조 후
  삭제하고 imported repository, `latest`와 관련 없는 tag/image는 보존
- CloudFormation `DELETE_COMPLETE` 뒤 `DELETE_SKIPPED`/retained resource가 없고
  deploy 직후 physical ID inventory와 잔여 조회가 일치함

삭제 근거는 마지막 run과 최종 Explanation에 저장하고 commit한다.

## 최종 Explanation gate

최종 리포트 경로는
`docs/explanation_phase1_kinesis_transition_results.md`다. Explanation 형식으로
처음 보는 개발자에게 배경과 결론을 먼저 설명하고 다음을 모두 포함한다.

- 최종 수정된 goal 전문, 사용자 요구사항 22개와 요구사항 변경 이력
- Phase 1 목적/비목적, 단건 HTTP/PutRecord 계약, 기존 Kafka 아키텍처 유지 이유
- 전체 timeline과 모든 checkpoint
- optimization backlog, pprof 병목, local before/after
- 채택·기각한 최적화와 일반성/계약 검증
- 모든 AWS run의 목표, 단일 변경점, 결과, 비용, 판정과 다음 결정
- 실패, aborted, inconclusive, blocked run
- collector 수별 RPS, p50/p95/p99, CPU, memory와 GC
- live quota, 기존 Standard vCPU, `max_safe_collector_count` 계산과 승인된
  8/12/17대 변경
- `429` overload와 재배포 없는 회복 결과
- Kinesis shards, IncomingRecords와 throttling
- payload 형식/크기 분포와 사용한 표준 header 및 근거
- 예상/실제/누적 Phase 1 비용
- 모든 run directory, collector/infra commit SHA와 image digest
- AWS destroy와 잔여 resource 검증
- `passed`, `failed`, `blocked` 또는 `inconclusive` 최종 판정과 현재 quota 안의 결론
- 추가 vCPU가 필요하다는 결론이면 근거만 기록하고 추가 증설을 요청하지 않았다는 확인
- Spot, 기존 instance 중단, 승인 상한 초과와 금지된 최적화를 시도하지 않았다는 확인
- collector `main` 미병합과 실험 브랜치 미push 확인
- 남은 위험과 다음 단계

최종 리포트와 cleanup 근거가 commit되기 전에는 goal을 완료 처리하지 않는다.

## 관련 문서

- [Phase 1 Kinesis pprof 최적화 후속 결과](../../../evidence/explanations/explanation_phase1_kinesis_transition_results.md#2026-07-11-pprof-최적화-후속-실험)
- [AWS Event Pipeline Performance Test Guide](guide_aws_event_pipeline_performance_test.md)
- [AWS Performance Test Result Recording Process](../processes/process_aws_perf_test_result_recording.md)
- [Phase 0 Generator/ALB Explanation](../../../evidence/explanations/explanation_phase0_generator_alb_ceiling.md)
- Phase 1 Kinesis performance test artifacts (external snapshot reference: `../performance-tests/phase1-kinesis/README.md`)
- [AWS Kinesis PutRecord API](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecord.html)
- [AWS Kinesis DescribeLimits API](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_DescribeLimits.html)
- [CloudFormation resource tagging](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-resource-tags.html)
