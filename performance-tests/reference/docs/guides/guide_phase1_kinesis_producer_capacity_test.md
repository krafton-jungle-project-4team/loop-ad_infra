# Phase 1 Kinesis producer 처리 한계 테스트 가이드

## 목적

실제 AWS Kinesis에서 다음 세 producer 후보가 `4 x c6i.xlarge` collector 조건으로
지속 처리할 수 있는 트래픽 구간을 `10k -> 30k -> 50k RPS` 순서로 확인한다.

- Go synchronous `PutRecord`
- Go bounded `PutRecords` micro-batcher
- Java KPL 1.x, aggregation disabled, collection enabled

이 테스트는 총 16 collector vCPU를 고정한 상태에서 producer 전략의 cluster 처리
한계를 비교한다. 후보별 또는 단계별로 collector 수를 늘리지 않는다. 4대보다 큰
수평 확장 용량은 별도 scale-out 테스트로 분리한다.

현재 진행 중인 1k 공통 비교가 결과 기록, run stack 삭제, shared stream 삭제, ECR
실험 이미지 삭제와 cleanup 검증까지 끝난 뒤 새 comparison session으로 시작한다.
현재 세션의 실행 중인 stack이나 artifact를 재사용하지 않는다.

## 기존 근거

- Phase 0에서 `1 x c6in.large` oha 생성기가 SDK 호환 payload로 약 55k RPS를
  150초 동안 오류 없이 생성했다. 50k 단계에서 생성기 자체는 검증된 출발점이다.
- 이전 synchronous `PutRecord` 실험에서 `1 x c6i.xlarge`는 5k를 통과했지만
  10k 요청에서 실제 8,345.94 RPS, CPU 98.56%, p95 약 9.5초로 포화됐다.
- 같은 실험에서 `4 x c6i.xlarge`는 10k를 통과했지만 20k에서 HTTP 429 219건과
  p95 609.39ms로 실패했다. `8 x c6i.xlarge`에서야 20k를 통과했다.
- 이전 결과는 현재 세 후보의 image와 코드가 다르므로 새 실험을 대체하지 않는다.
  다만 10k 단계부터 시작하고 bounded overload를 강제해야 하는 근거로 사용한다.
- 80-shard provisioned stream은 평균 약 1,341-byte payload 기준 50k RPS를 수용하도록
  설계됐다. 실제 테스트에서는 shard 분포와 `WriteProvisionedThroughputExceeded`가 0인지
  다시 검증한다.

## 검증 가설

고정된 4대 topology에서 다음 가설을 검증한다.

- H1: Go synchronous `PutRecord`는 요청마다 Kinesis API 호출, SigV4와 응답 처리를
  수행하므로 30k 이전에 CPU 또는 ACK latency 한계에 도달한다.
- H2: Go `PutRecords` micro-batcher는 여러 record를 한 API 호출로 collection해
  API calls/event와 요청당 CPU를 낮추므로 같은 4대에서 30k 이상을 처리한다.
- H3: Java KPL도 aggregation은 끄되 collection으로 `PutRecords` 호출 수를 줄여
  30k 또는 50k를 처리할 수 있다. 다만 JVM과 KPL native child overhead 때문에
  Go batch와 동일한 결과라고 미리 가정하지 않는다.

H2와 H3는 기대 결과이지 통과 판정이 아니다. 실제 AWS correctness, accepted RPS,
latency, resource와 cost gate를 모두 만족해야 지지된 것으로 판정한다.

## 고정 조건

후보 외 조건은 모두 고정한다.

| 항목 | 고정값 |
| --- | --- |
| Region | `ap-northeast-2` |
| Collector | `4 x c6i.xlarge`, ECS on EC2, host당 task 1개, 총 task 4개 |
| Load generator | `1 x c6in.large`, oha |
| Architecture | `linux/amd64` |
| Load path | 동일 VPC, public subnet, internal ALB, 동일 target group 형태 |
| Stream | shared provisioned Kinesis, 80 shards, 24시간 retention |
| Payload | 커밋된 480개 SDK-compatible NDJSON pool과 동일 SHA-256 |
| HTTP contract | 한 요청에 사용자 이벤트 1개, 성공 ACK 뒤에만 HTTP 202 |
| CloudWatch period | 60초 |
| Candidate order | `go-sync -> go-batch -> java-kpl` |

producer 설정은 첫 AWS deploy 전에 고정하고 모든 트래픽 단계에서 유지한다.

- `go-sync`: 요청당 `PutRecord`, 현재 SDK retry와 bounded admission 설정
- `go-batch`: 5ms window, 최대 50 records, sender 16, input queue 2,048,
  sender queue 32
- `java-kpl`: 공식 KPL 1.x artifact, aggregation disabled, collection enabled,
  bounded outstanding records/bytes와 shutdown timeout

특정 후보나 트래픽 단계만 connection, queue, retry, JVM heap 또는 task resource를
변경하지 않는다. 설정을 바꾸면 새 image digest와 새 run ID로 세 단계 전체를 다시
실행한다.

ALB target은 정확히 4개여야 하며 모든 task가 서로 다른 collector host에 배치돼야
한다. 집계값뿐 아니라 host/task별 요청, CPU, RSS, network와 runtime 지표 분포를
기록해 한 host로 치우친 결과를 통과시키지 않는다.

## 트래픽 매트릭스

각 후보는 다음 세 단계를 순서대로 평가한다.

| 단계 | Offered load | 사전 probe | Warm-up | 본 측정 | Cooldown |
| --- | ---: | ---: | ---: | ---: | ---: |
| T1 | 10,000 RPS | 15초 | 30초 | 60초 x 3회 | 반복 사이 15초 |
| T2 | 30,000 RPS | 15초 | 30초 | 60초 x 3회 | 반복 사이 15초 |
| T3 | 50,000 RPS | 15초 | 30초 | 60초 x 3회 | 반복 사이 15초 |

probe는 처리량 판정 자료가 아니라 안전 확인이다. probe에서 bounded `429` 또는 `503`만
발생하고 task, memory, Kinesis가 안전하면 본 측정을 실행한다. 다음 중 하나가 발생하면
해당 단계의 본 측정을 시작하지 않고 `failed-safety-probe`로 기록한다.

- task restart, OOM, panic 또는 fatal log
- ALB/target 5xx가 1% 초과
- Kinesis write throttling
- queue 또는 outstanding bytes가 설정 상한을 넘어 증가
- 부하 종료 뒤 health와 HTTP 202가 회복되지 않음

낮은 단계가 실패해도 failure가 bounded admission으로 격리됐고 service가 완전히
회복됐다면 다음 요청 단계의 15초 probe까지는 실행한다. task restart, OOM, Kinesis
throttle 또는 회복 실패가 발생하면 더 높은 단계는 `not-run-safety-stop`으로 남긴다.

50k 본 측정 3회가 모두 통과한 후보만 선택적으로 `50k x 300초` 지속성 확인을 수행한다.
이 장기 확인은 세 후보 공통 매트릭스와 cleanup 예산을 확보한 뒤 별도 비용 gate를
통과해야 한다.

## 단계별 실행 절차

### 1. 새 session 준비

1. 현재 comparison session의 최종 cleanup 증거를 확인한다.
2. 새 `phase1-capacity-<timestamp>` session ID를 생성한다.
3. collector와 infra branch, 시작 SHA, working tree 상태를 기록한다.
4. AWS identity, region, Standard On-Demand vCPU quota, running resource와 이름 충돌을
   읽기 전용으로 확인한다.
   collector 16 vCPU, load generator 2 vCPU와 safety reserve를 포함한 live quota가
   부족하면 deploy하지 않는다.
5. 현재 AWS 가격으로 전체 계획의 보수적 upper bound를 계산한다.
6. 후보별 image를 고유 tag로 push하고 remote digest와 `linux/amd64`를 확인한다.

### 2. shared stream 배포

1. CDK synth와 diff에서 80-shard stream과 session tag만 생성되는지 확인한다.
2. shared stream stack을 한 번 배포한다.
3. creation timestamp, session ownership tag, 80 open shards와 `ACTIVE` 상태를 기록한다.
4. 2시간 session timer를 시작한다.

### 3. 후보 실행

후보마다 다음 cycle을 한 번 수행한다.

```text
preflight
  -> run stack deploy
  -> 4 hosts/tasks/healthy ALB targets와 image digest 검증
  -> actual Kinesis correctness
  -> 1k RPS x 30초 smoke
  -> T1 10k
  -> T2 30k
  -> T3 50k
  -> raw metric 수집과 집계
  -> run stack destroy
  -> candidate cleanup 검증
  -> immutable run evidence commit
```

각 단계와 반복 시작 직전에 stream age, modeled accrued cost, projected total cost, task
health를 다시 확인한다. 실행 중 설정 변경이나 재배포는 금지한다.

### 4. 최종 cleanup

마지막 후보 또는 hard stop 직후 다음을 수행한다.

1. run stack이 없음을 확인한다.
2. shared stream stack을 destroy한다.
3. session 소유 VPC, ALB, target group, ECS cluster/service/task, ASG, EC2, EBS,
   security group, IAM role, log group과 Kinesis stream이 모두 없는지 확인한다.
4. 후보별 ECR tag와 expected digest를 대조한 뒤 정확한 실험 image만 삭제한다.
5. Cost Explorer를 조회하고 지연 중이면 `pending`으로 기록한다.

## 통과 기준

한 트래픽 단계는 세 번의 본 측정이 모두 다음 조건을 만족해야 통과한다.

- actual RPS가 offered load의 99% 이상
- HTTP 202 비율 99.9% 이상
- HTTP 429는 0
- transport error 비율 0.1% 이하
- ALB target 5xx 비율 0.1% 이하
- ALB 자체 5xx는 0
- HTTP p95 100ms 이하, p99 500ms 이하
- HTTP 202와 Kinesis `IncomingRecords` 차이 0.1% 이하
- Kinesis write throttle 0
- producer final failure와 timeout 0
- task restart, OOM, panic, fatal 0
- 4개 task가 전체 측정 동안 유지되고 task별 accepted event가 4개 task 평균에서
  10%를 초과해 벗어나지 않음
- queue/outstanding resource가 고정 상한 안에서 부하 종료 후 정상 수준으로 회복

실제 Kinesis correctness run에서 loss, duplicate, byte/key mismatch, pre-ACK HTTP 202 또는
aggregation이 하나라도 발견된 후보는 성능 단계 수치와 관계없이 추천 대상에서 제외한다.

## 처리 한계 판정

후보별 결과는 다음 방식으로 표현한다.

| 통과 결과 | 보고할 처리 한계 |
| --- | --- |
| 10k 실패 | `< 10k RPS`; 같은 4대 session의 1k smoke와 함께 `[1k, 10k)` 구간 |
| 10k 통과, 30k 실패 | `[10k, 30k)` |
| 30k 통과, 50k 실패 | `[30k, 50k)` |
| 50k 3회 통과 | `>= 50k RPS` |
| 50k x 300초도 통과 | `50k RPS sustained` |

세 후보가 같은 최고 단계를 통과하면 단일 run의 최고값으로 순위를 정하지 않는다.
세 반복의 중앙값, 범위와 IQR을 사용하고 다음 순서로 비교한다.

1. accepted events/s와 오류율
2. per-event ACK 및 HTTP p95/p99
3. producer API calls/event와 records/call
4. collector CPU, RSS와 network/event
5. Go allocation/GC 또는 JVM/KPL child CPU·RSS
6. 실제 증분 비용

관측 변동 범위가 겹치면 `동률 또는 차이 미확정`으로 판정한다.

## 필수 측정값

각 probe와 본 측정 반복에서 다음 raw 값을 남긴다.

- offered, completed, accepted requests/s
- HTTP 202/400/413/429/503/5xx와 transport errors
- HTTP 및 per-event ACK p50/p95/p99
- producer API calls, records/call, calls/accepted event
- retry, partial failure, timeout, final failure
- batch-size distribution, queue/outstanding high-water
- 4개 host와 container별 CPU/RSS/network 및 합계
- ECS/EC2, ALB와 Kinesis CloudWatch raw points
- Go heap/allocation/goroutine/GC
- JVM heap/thread/GC와 KPL native child CPU/RSS/restart
- task ID, target health, startup/warm-up와 shutdown flush

모든 중앙값, 범위, IQR와 per-accepted-event 값은 checked script로 계산한다. 수기로
계산하지 않는다.

## 비용과 시간 제한

이 saturation 테스트는 현재 comparison과 분리된 새 비용 session으로 실행한다.

- `MAX_INCREMENTAL_COST_USD=20`
- 초기 전체 계획 upper bound는 `$16` 이하
- `$4`는 가격 오차, 실패와 cleanup reserve
- modeled accrued cost가 `$18`이면 신규 probe/run 금지
- 다음 단계와 cleanup을 포함한 projected total이 `$20` 이상이면 시작 금지
- shared stream 생성 100분 뒤 신규 후보/단계 시작 금지
- 110분에 무조건 cleanup 시작
- 120분에 shared stream 수명 hard stop

각 deploy 전 가격과 quota를 갱신한다. Cost Explorer 지연값이 아니라 실제 wall-clock과
resource usage를 이용한 upper-bound 모델을 중단 기준으로 사용한다.

## Artifact 계약

후보별 디렉터리는 덮어쓰지 않는다.

```text
performance-tests/run_<timestamp>_phase1_kinesis_capacity_<candidate>/
  run.json
  image.json
  prices.json
  cost-upper-bound.json
  preflight-before-deploy.json
  deployed-state.json
  correctness-*.json
  smoke/
  stages/
    10k/
      probe/
      repetition-01/
      repetition-02/
      repetition-03/
    30k/
    50k/
  capacity-summary.json
  report.md
  cleanup-verification.json
  cost-explorer.json
```

실패, 중단, inconclusive와 안전 중단도 삭제하지 않는다. 후보별 cleanup이 끝난 뒤
run evidence를 별도 commit하고 다음 후보로 넘어간다. push, merge와 PR은 사용자가
별도로 요청하지 않는 한 수행하지 않는다.

## 완료 조건

- 세 후보가 후보별 고정 image digest/config와 동일 topology에서
  10k/30k/50k 매트릭스로 평가됨
- 실행하지 못한 단계는 정확한 safety 또는 cost blocker와 함께 기록됨
- 후보별 최고 통과 단계와 처리 한계 구간이 checked summary에 존재함
- 4개 host/task별 처리량과 자원 편중 검증이 존재함
- correctness와 common artifact gate를 통과한 후보만 추천 대상임
- 모든 session 소유 AWS resource와 ECR image가 삭제됨
- 보수적 누적 비용이 `$20` 미만임
- 후보별 evidence commit과 최종 비교 문서 commit이 존재함
