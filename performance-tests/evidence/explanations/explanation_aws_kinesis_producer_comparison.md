# 실제 AWS Kinesis producer 비교 결과

## 결론

세 후보 모두 실제 AWS Kinesis correctness와 동일한 1,000 RPS × 60초 × 10회 비교를
통과했다. 이 부하에서 accepted throughput 차이는 변동폭보다 작아 처리량 승자는 없다.
운영 후보로는 **bounded Go `PutRecords` micro-batcher를 유지**하는 판단이 가장 강하다.

Go batch는 sync 대비 API calls/event를 82.94%, Go allocation/event를 45.65%, ECS CPU
중앙값을 41.55% 줄였다. 대가로 HTTP p95 중앙값은 13.78ms에서 22.35ms로 62.20%
늘었다. 이 지연 증가는 실제이지만 오류·loss·duplicate·throttle 없이 공통 부하를
처리했고, Java의 JVM/KPL native 운영 비용 없이 현재 Go collector 안에서 격리된다.

Java KPL은 Go batch보다 API calls/event가 2.08% 적었지만 p95는 21.31% 높았다.
ECS memory 중앙값은 17.00배, image는 24.53배였다. KPL native child restart는 0이었지만
JVM, native child process, 별도 runtime base와 훨씬 큰 image를 운영해야 한다. 이
비교에서는 Java prototype을 production 또는 sidecar 구조로 채택할 근거가 없다.

이 결론은 추천이며 production collector 변경은 수행하지 않았다.

## 비교 범위

- Region: `ap-northeast-2`
- Collector: 후보마다 `1 x c6i.xlarge`, ECS on EC2, task 1개
- Load generator: `1 x c6in.large`, pinned oha image
- Kinesis: 세 후보가 순차 공유한 provisioned 80-shard stream
- Architecture: `linux/amd64`
- Correctness: 후보별 고유 80 events, 실제 Kinesis readback
- Smoke: 후보별 1,000 RPS × 30초
- Common load: 30초 warm-up, 1,000 RPS × 60초 × 10회, 15초 cooldown
- Payload, VPC/subnet/ALB/target group, task envelope와 CloudWatch 범위는 동일

공통 1,000 RPS는 선행 actual-AWS sync 결과를 사용해 결정했다. 같은 1대 topology에서
5,000 RPS는 통과했지만 10,000 RPS는 actual 8,345.94 RPS, p95 9,501.75ms, ECS CPU
98.56%로 포화됐다. 현재 comparison session에서 이 pilot을 다시 실행하지 않은 점은
제한 사항이다. 별도 4대 saturation 계획은
`guide_phase1_kinesis_producer_capacity_test.md`에 분리했다.

## Correctness

| 후보 | accepted / observed | loss | duplicate | bytes/key mismatch | pre-ACK 성공 | shutdown | 판정 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Go sync PutRecord | 80 / 80 | 0 | 0 | 0 / 0 | 0 | 64 / 64 | passed |
| Go PutRecords | 80 / 80 | 0 | 0 | 0 / 0 | 0 | 64 / 64 | passed |
| Java KPL 1.x | 80 / 80 | 0 | 0 | 0 / 0 | 0 | 64 / 64 | passed |

세 후보 모두 원본 JSON bytes와 `event_id` partition key를 보존했다. 사용자 event 하나는
Kinesis record 하나로 유지됐고 aggregation은 비활성화됐다. HTTP 202는 해당 record의
producer ACK 뒤에만 반환됐다.

## 10회 성능 결과

값은 script가 계산한 중앙값이다. 괄호는 IQR이다.

| 후보 | actual RPS | HTTP p95 ms | HTTP p99 ms | API calls/event | records/call | ECS CPU avg % | ECS memory avg % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Go sync | 999.820 (0.037) | 13.779 (0.440) | 19.378 (27.223) | 1.000000 | 1.000 | 6.618 (4.608) | 3.005 (0.214) |
| Go batch | 999.713 (0.017) | 22.350 (0.390) | 37.423 (4.247) | 0.170608 | 5.861 | 3.869 (2.637) | 2.842 (0.199) |
| Java KPL | 999.636 (0.075) | 27.112 (0.090) | 44.945 (3.825) | 0.167060 | 5.986 | 5.011 (2.610) | 48.298 (0.572) |

모든 10회 반복에서 429, 5xx, transport error, producer final failure, retry와 Kinesis
write throttle은 0이었다. Go sync p99 범위는 16.47~243.91ms로 한 반복의 긴 tail 때문에
IQR이 크게 나타났다. 단일 최댓값으로 후보 순위를 바꾸지 않았다.

Producer API call 중앙값은 60초당 sync 59,999.5회, Go batch 10,236.5회, Java KPL
10,023.5회였다. Java의 `UserRecordsPerPutRecordsRequest` stream-level sample count를
pre/post snapshot에서 차감해 실제 PutRecords request 수를 계산했다.

## ACK와 runtime

Go sync의 per-event ACK p95 histogram bucket upper bound는 25ms였다. Java KPL도 중앙값
25ms였고 한 반복은 50ms bucket이었다. 이 값은 histogram bucket 상한이지 보간된
quantile이 아니다.

Go batch debug snapshot의 기존 per-event ACK histogram은 채워지지 않았다. 따라서
Go batch ACK p50/p95/p99를 HTTP latency에서 역산하지 않았다. HTTP 응답이 producer ACK
뒤에만 발생한다는 계약과 correctness는 확인됐지만, 독립 ACK 분포가 없다는 측정 공백은
후속 instrumentation에서 보완해야 한다.

- Go sync allocation/event 중앙값: 52,029 bytes, GC pause 92.69ms/60초
- Go batch allocation/event 중앙값: 28,279 bytes, GC pause 66.32ms/60초
- Java JVM heap 중앙값: 73.65MB, GC 187회와 231.5ms/60초
- Java KPL child RSS 중앙값: 34.90MB
- Java KPL child CPU 중앙값: 8.99 CPU-seconds/60초
- Java KPL child restart: 0
- Image: Go 두 후보 9.73MB, Java 238.60MB

Java 첫 measurement attempt는 후보 오류가 아니라 증거 수집기 오류로 repetition-01 load
전에 중단됐다. shard별 KPL metrics가 SSM stdout 24KB를 넘어 JSON이 잘렸다. 실패
artifact를 보존하고 원격 gzip+base64 수집을 검증한 뒤 10회를 처음부터 재실행했다.

## 장애 처리와 운영 판단

Go sync는 구현이 가장 단순하고 이 부하의 p95가 가장 낮다. 그러나 event마다 PutRecord,
SigV4와 response 처리를 수행해 API 효율과 allocation이 가장 나쁘고, 선행 10k 포화
결과와 일치한다.

Go batch는 input queue 2,048, sender queue 32, sender 16, 5ms window와 최대 50 records로
bounded다. partial failure에서 실패 index만 retry하는 계약과 shutdown flush가 local
contract test 및 actual correctness에서 확인됐다. 현재 Go production 구조에 적용할 때
변경 범위와 운영 위험이 세 후보 중 가장 균형적이다.

Java KPL은 collection 효율은 가장 좋았지만 Go batch와의 2.08% 차이는 JVM/native
overhead와 복잡도를 정당화하지 못한다. native child crash, health, timeout과 shutdown을
별도로 운영해야 하고 runtime base도 Go와 다르다. prototype은 비교 증거로 유지하되
production 전환 후보에서는 기각한다.

## 비용과 cleanup

- 최초 전체 계획 보수 상한: `$8.582476`, `$16` 제한 통과
- 후보별 보수 상한: `$3.171800`
- 최종 누적 보수 상한: `$9.515400`, `$20` hard cap 미만
- Cost Explorer: 세 조회 모두 `billing-delayed-or-zero`; 당일 `$0`을 실제 비용으로
  오인하지 않고 deterministic upper bound를 판정값으로 사용
- Shared stream: `05:03:58Z` 생성, 약 149분에 destroy 시작, 3시간 제한 준수
- Run stack, stream stack, Kinesis, EC2, ECS, ASG, ALB, VPC, IAM role, collector/Lambda
  log group과 세 exact ECR candidate tag: 최종 검증에서 모두 없음

기존 ECR repository와 unrelated tag, 기존 t4g 인스턴스, dev/production/Kafka resource는
변경하지 않았다.

## Evidence와 commit

- Go sync run: `b2530dd`, metrics 보완 `acd9ec5`
- Go batch run: `95bdc68`
- Java KPL run: `b2c8edc`
- 최종 cleanup: `903651e`
- 최종 비교 결과: `9d511c618af7c8d3a69545ee22547a33fe02c031`
- Collector 구현 최종 SHA: `d1918b629bbbf3f4499a9671c3213687a39c5d5d`
- Collector branch: `codex/phase1-kinesis-transition`, clean
- Infra 시작 SHA: `3081594bb46355ba9b7c64fa55d6a0b04d96d34a`
- Infra branch: `codex/aws-perf-test-plan`
- Infra 최종 status: 시작 시점부터 존재한 untracked `_workspace/`, `im-not-ai/`만 남음

Checked aggregate는
`performance-tests/phase1-kinesis/aws-producer-comparison-summary.json`, raw evidence는 각
`performance-tests/run_*_phase1_kinesis_compare_*` directory에 있다. push, merge와 PR은
수행하지 않았다.

최종 현재 HEAD 검증은 infra `npm run verify`(Jest 62/62와 correctness/time guard 8/8),
두 stack의 CDK synth, collector `go test ./...`, `go test -race ./...`, `go vet ./...`,
Java KPL `mvn -q test`를 통과했다.
