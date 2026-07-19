# Go Kinesis micro-batch 라이브러리 채택 검토

## 결론

현재 Collector의 요구사항을 그대로 만족하는 공식 또는 준공식 Go Kinesis producer
라이브러리는 확인되지 않았다. AWS SDK for Go v2는 `PutRecords` API를 제공하지만 queue,
micro-batch, partial failure retry, per-event final-ACK, backpressure와 shutdown을 관리하는
고수준 producer는 제공하지 않는다.

검토한 커뮤니티 라이브러리도 그대로 채택할 수 없다. 현재 계약에 맞추려면 partition key,
per-event 성공 결과, bounded retry/deadline, cancellation과 shutdown을 핵심부에 추가해야 한다.
이 정도 변경이면 외부 라이브러리를 사용해 직접 구현 위험을 줄이는 것이 아니라, 사실상
fork한 producer의 유지보수 책임까지 새로 갖게 된다.

따라서 현재 결정은 다음과 같다.

- 성능 테스트와 현재 운영 기본 후보는 공식 AWS SDK for Go v2 기반의 기존 bounded
  `PutRecords` micro-batcher를 유지한다.
- 검토한 제3자 Go producer는 채택하지 않는다.
- Go 구현의 correctness 위험은 이미 끝난 문제로 취급하지 않고 아래 추가 검사를 통과시킨다.
- 직접 batching/retry 구현의 소유 자체를 제거하는 것이 우선순위가 되면 Java AWS SDK의
  `PutRecords` 직접 호출이 아니라 Java KPL로의 이전을 별도 후보로 평가한다.

이 문서는 구현 변경이나 AWS 재실험을 승인하지 않는다. 현재 판단과 다음 검증 범위만
기록한다. 조사 기준일은 2026-07-16이다.

## 필요한 producer 계약

이 Collector에서 필요한 기능은 단순한 비동기 전송보다 강하다.

- 하나의 HTTP event는 원본 bytes를 보존한 하나의 Kinesis record가 된다.
- `event_id`를 partition key로 사용한다.
- HTTP `202`는 해당 record의 Kinesis sequence number와 shard ID가 확인된 뒤에만 반환한다.
- 하나의 `PutRecords` 응답에서 성공한 index는 즉시 완료하고 실패한 index만 재시도한다.
- input queue, batch queue, sender 수, 재시도 횟수, backoff, batch deadline과 shutdown 시간이
  모두 bounded여야 한다.
- queue 포화는 대기열을 무한히 늘리지 않고 즉시 명시적인 HTTP 오류로 변환한다.
- 요청 취소와 shutdown 중에도 각 event는 성공 또는 실패 중 하나의 terminal result만 받는다.
- queue, outstanding record, in-flight API, retry, final failure와 final-ACK latency를 측정할 수
  있어야 한다.

이 요구사항 때문에 “여러 record를 한 `PutRecords` 호출에 넣는다”는 기능만으로는 대체재가
되지 않는다.

## AWS가 공식적으로 제공하는 범위

### AWS SDK for Go v2

[AWS SDK for Go v2 Kinesis package](https://pkg.go.dev/github.com/aws/aws-sdk-go-v2/service/kinesis)는
`PutRecord`와 `PutRecords`를 공식 지원한다. `PutRecords` 응답은 요청과 같은 순서의 결과
배열을 반환하며, 성공과 실패가 한 응답에 함께 있을 수 있다. 그러나 SDK package에는
KPL과 같은 producer manager, batching window, per-record future, bounded queue 또는
partial-failure orchestration이 없다.

따라서 현재 구현은 인증, wire protocol, HTTP transport와 Kinesis API 자체를 다시 만든 것이
아니다. 이 부분은 공식 SDK가 담당한다. 프로젝트가 직접 소유하는 범위는 record 수집,
`PutRecords` 호출 구성, index별 결과 매핑, retry와 lifecycle orchestration이다.

### AWS Labs Kinesis aggregation

[awslabs/kinesis-aggregation](https://github.com/awslabs/kinesis-aggregation)은 KPL 형식의
aggregation/deaggregation과 serialization을 제공한다. 전송 queue나 `PutRecords` 호출,
retry와 final-ACK를 관리하는 producer는 아니다. README도 KPL 밖에서 aggregation을 사용할
때 일부 message loss가 중요한 용도에는 사용하지 말라고 경고하고, 전송을 위해서는 별도로
`PutRecord(s)`를 호출해야 한다고 명시한다.

현재 계약은 event 하나를 Kinesis record 하나로 유지하므로 aggregation 자체도 필요한
기능이 아니다.

### AWS의 Fluent Bit Kinesis plugin

[amazon-kinesis-streams-for-fluent-bit](https://github.com/aws/amazon-kinesis-streams-for-fluent-bit)은
AWS가 유지하며 batching과 retry를 구현한다. 그러나 이것은 Fluent Bit의 `OutputPlugin`이지
Go application에 삽입하는 범용 producer API가 아니다. HTTP 요청별 성공 결과를 반환하는
final-ACK 계약도 없다. 조사 시점의 `mainline`은 AWS SDK for Go v1에 의존하며, v1은 이미
[2025-07-31에 AWS 지원이 종료된 계열](https://aws.amazon.com/blogs/developer/announcing-end-of-support-for-aws-sdk-for-go-v1-on-july-31-2025/)이다.

이 plugin의 일부 구현 패턴은 참고할 수 있지만 Collector dependency로 채택할 수는 없다.

## 커뮤니티 Go 후보 평가

| 후보 | 확인된 장점 | 현재 계약과의 핵심 차이 | 판정 |
| --- | --- | --- | --- |
| [`a8m/kinesis-producer`](https://github.com/a8m/kinesis-producer) | KPL 유사 batching과 aggregation, Go 후보 중 상대적으로 큰 공개 사용 신호 | 최신 tagged release가 2018년이고 AWS SDK Go v1 기반이다. `Put` 성공은 enqueue만 뜻하며 실패 channel만 있어 요청별 final-ACK를 만들 수 없다. retry와 shutdown도 현재 bounded 계약보다 약하다. | 채택하지 않음 |
| [`kinesis-producer-go/kinesis-producer`](https://pkg.go.dev/github.com/kinesis-producer-go/kinesis-producer) | `a8m` 계열을 AWS SDK Go v2로 옮긴 비교적 최근 fork | 공개 API가 `Put(data)`와 failure channel 중심이다. 호출자가 `event_id` partition key와 요청 context를 전달하거나 record별 성공을 기다릴 수 없다. 검토한 source의 partial-failure retry와 `Stop`도 요청 deadline 없이 동작한다. | 채택하지 않음 |
| [`useinsider/go-pkg/inskinesis`](https://github.com/useinsider/go-pkg/tree/develop/inskinesis) | batch 크기, 동시 group, retry 설정을 제공 | AWS SDK Go v1 기반이고 비동기 `Put`과 error channel 중심이다. 요청별 Kinesis final-ACK, bounded request deadline과 현재 lifecycle 계약이 없다. | 채택하지 않음 |
| [`tj/go-kinesis`](https://github.com/tj/go-kinesis) | 작은 API와 기존 `PutRecords` batching 구현 | 장기간 유지보수 신호가 없고 현재 SDK, final-ACK와 lifecycle 요구를 충족하지 않는다. | 채택하지 않음 |

공개 사용 신호가 가장 큰 `a8m/kinesis-producer`도 조사 시점 약 150 stars 수준이며 최신
release와 SDK 세대가 오래됐다. 반대로 AWS SDK Go v2를 사용하는
`kinesis-producer-go/kinesis-producer`는 최근 변경이 있지만 공개 adoption과 안정성 근거가
아직 작다. “사람들이 많이 쓴다”와 “현재 final-ACK 계약에 안전하다”를 같은 의미로 볼 수
없다.

특히 다음 변경을 외부 라이브러리 fork에 넣으면 직접 구현보다 책임 범위가 줄지 않는다.

- `event_id` partition key 입력 API
- request context와 deadline 전파
- 성공 record별 future 또는 callback
- arbitrary partial failure에서 성공 index 재전송 금지
- bounded retry와 전체 batch deadline
- queue-full 즉시 거절과 bounded memory
- `Put`과 `Close` 경쟁 시 terminal result 보장
- 취소 가능한 drain과 제한 시간 내 shutdown
- 기존 telemetry와 HTTP 상태 매핑

## 현재 Go 구현의 근거와 남은 위험

현재 검토 기준 Collector commit은
`497315137251af82d0d203ce34702d5543553942`다. 구현은 두 개의 bounded queue, 하나의
aggregator와 고정 sender 수를 사용한다. 각 event가 복사된 body와 result channel을 소유하고,
성공한 Kinesis response index가 확인된 뒤에만 HTTP `202`를 반환한다. SDK 전체 요청 retry는
한 번으로 제한하고 실패 index만 자체 bounded retry한다.

현재 unit/contract test는 timer와 record-count flush, concurrent enqueue, queue-full,
partial success, final-ACK latency, index별 오류 매핑, batch deadline, caller cancellation,
graceful shutdown, shutdown timeout과 ACK 전 body 수명 등을 다룬다. 실제 AWS correctness와
1,000 RPS 반복 비교도 통과했다. [기존 producer 비교](explanation_aws_kinesis_producer_comparison.md)에서
Go batch는 Java KPL보다 API calls/event가 약 2.08% 많았지만 p95는 약 21.31% 낮았고 ECS
memory 사용량과 image 크기도 훨씬 작았다.

이후 [50k connection-path 기본 설계](explanation_connection_path_50k_baseline.md)는 Go
batch Collector 6대와 Kinesis 120 shards로 50k RPS 처리 용량을 확인했다. 다만 이 근거는
현재 구성의 성능과 관측된 correctness를 지지할 뿐, 모든 concurrency와 network failure
interleaving에서 구현 결함이 없음을 증명하지는 않는다.

가장 중요한 잔여 위험은 전송 결과가 모호한 transport timeout이다. Kinesis가 record를
저장했지만 응답이 유실되면 producer는 성공 여부를 알 수 없다. 재전송하면 duplicate가 생길
수 있고, 재전송하지 않으면 loss 가능성을 받아들여야 한다. 이 문제는 Go 구현만의 결함이
아니며 KPL도 exactly-once를 만들지는 않는다. downstream은 `event_id` 기준 idempotency를
유지해야 한다.

## Go를 유지할 때 필요한 추가 검사

### 1. 상태 모델과 property/fuzz 검사

- 임의의 batch 크기, 성공/실패 index 조합과 여러 retry round를 생성한다.
- 성공한 index가 다시 전송되지 않고 모든 event가 정확히 하나의 terminal result를 받는지
  검사한다.
- 빈 결과, 요청과 다른 response cardinality와 잘못된 result entry를 방어적으로 처리하는지
  검사한다.
- accepted, success, retry, final failure, cancellation과 outstanding counter 사이의 불변식을
  property로 고정한다.

### 2. concurrency와 lifecycle 검사

- 다수의 `Produce`, request cancellation과 `Close`가 동시에 실행되는 state-machine test를
  추가한다.
- `go test -race`를 높은 반복 횟수로 실행하고 queue-full, timer flush와 shutdown timeout을
  함께 교차시킨다.
- admission 종료 이후 새 record가 들어가지 않고, 이미 승인된 record가 drain되거나 명시적인
  실패를 받는지 확인한다.
- shutdown이 제한 시간을 넘길 때 goroutine, result waiter와 HTTP handler가 남지 않는지
  검사한다.

### 3. fault injection과 duplicate 검사

- throttle, `InternalFailure`, connection reset, TLS/DNS 오류, 응답 body 오류와 deadline을
  arbitrary partial success와 조합한다.
- service commit 뒤 response만 유실되는 모호한 timeout을 재현해 duplicate 가능성을 실제로
  계수한다.
- retry 뒤 Kinesis readback으로 원본 bytes, `event_id` partition key, loss와 duplicate를
  확인하고 downstream idempotency가 duplicate를 제거하는지 검증한다.

### 4. soak와 upgrade gate

- queue가 차고 빠지는 부하와 mixed failure를 포함한 장시간 soak에서 memory, goroutine,
  outstanding record와 final-ACK accounting이 bounded인지 확인한다.
- Go 또는 AWS SDK 버전, retry 설정, batch 크기와 sender 수가 바뀔 때 local contract 전체와
  actual-Kinesis correctness를 다시 통과시킨다.
- Kinesis `PutRecords` 한도와 response semantics를 dependency upgrade gate에서 확인한다.
- batching/retry/lifecycle 코드는 일반 기능 코드와 분리해 독립 concurrency review를 받는다.

이 검사는 기존 테스트를 부정하는 것이 아니다. 외부에서 검증된 고수준 producer가 없는 만큼
프로젝트가 직접 소유하는 correctness 영역에 더 강한 증거를 요구하는 것이다.

## Java로 이전할 때의 정확한 선택지

Java로 언어만 바꾸고 AWS SDK for Java의 `PutRecords`를 직접 호출하면 queue, batching,
partial failure retry, backpressure와 shutdown을 여전히 직접 구현해야 한다. 이 방식은 Go의
현재 위험을 제거하지 못하며 Collector 전체를 다시 작성하는 비용만 추가한다.

직접 producer orchestration 소유를 줄이는 대안은
[Amazon Kinesis Producer Library](https://docs.aws.amazon.com/streams/latest/dev/developing-producers-with-kpl.html),
즉 Java KPL이다. KPL은 record collection, batching, retry와 rate limiting을 관리하고 각
user record에 비동기 결과를 제공한다. aggregation을 끄고 collection만 사용하면 event 하나를
Kinesis record 하나로 유지하면서 여러 record를 하나의 `PutRecords` 호출로 전송할 수 있다.
기존 비교 prototype은 `software.amazon.kinesis:amazon-kinesis-producer:1.0.7`을 고정해 이
계약을 검증했다.

그러나 KPL은 단순한 Java SDK helper가 아니다. AWS 문서에 따르면 C++ core가 Java parent와
별도 process로 실행되고 IPC로 통신한다. 따라서 다음 운영 책임이 추가된다.

- JVM과 native child의 health, restart와 로그를 함께 관리한다.
- outstanding record/bytes가 무한히 증가하지 않도록 HTTP admission과 backpressure를 둔다.
- record future가 성공한 뒤에만 HTTP `202`를 반환하고 timeout/failure를 기존 상태 코드에
  매핑한다.
- aggregation disabled, 원본 bytes와 `event_id` partition key를 contract test로 고정한다.
- SIGTERM 때 HTTP admission 종료, KPL `flushSync`, outstanding zero와 child 종료 순서를
  bounded하게 검증한다.
- JVM heap, native RSS, image 크기, cold start와 child restart telemetry를 운영 항목에 넣는다.

Go Collector와 Java KPL sidecar를 조합하는 방식도 가능하지만 기본 대안으로는 적절하지 않다.
batcher 코드를 없애는 대신 별도 IPC protocol, per-event result correlation, cancellation과
sidecar lifecycle이라는 새 custom boundary를 만든다. Java를 선택한다면 완전한 Java
Collector가 더 명확한 비교 대상이다.

## Java 이전 재검토 조건

다음 조건 중 하나가 중요해지면 Java KPL migration을 다시 평가한다.

- 팀 정책상 batching/retry/lifecycle 핵심부의 자체 구현을 운영할 수 없다.
- Go 구현의 추가 fault/concurrency 검사에서 해결 비용이 큰 결함이 발견된다.
- AWS Support 또는 장기 유지보수 책임이 runtime 효율보다 우선한다.
- Java runtime과 native child의 memory 및 운영 비용을 수용할 수 있다.

이전 결정을 내리기 전에는 기존 1,000 RPS 비교만으로 판단하지 않는다. Java KPL 후보도 현재
Go baseline과 같은 payload, final-ACK, 12,000 physical connections, HAProxy 경로, Collector
6대와 Kinesis 120 shards에서 다시 검증해야 한다. 이전 30k Java 진단의 HTTP 429와 task
restart는 위험 신호지만, 최종 6-Collector HAProxy topology에서 Java를 검증한 결과는 아니므로
Java의 50k 가능성을 단정적으로 기각하는 근거로 쓰지 않는다.

판정은 최소한 다음을 함께 비교해야 한다.

- 원본 bytes, partition key, loss/duplicate와 ACK 전 성공 응답 금지
- 300초 누계 actual RPS와 corrected/worst-worker p95
- Kinesis retry/final failure와 정렬된 final-ACK accounting
- queue/outstanding drain, shutdown과 child restart
- Collector CPU, JVM heap, native RSS와 image/deploy 복잡도

Java KPL이 이 계약을 통과하고 custom producer 소유 감소의 가치가 runtime/운영 비용보다 클
때만 이전한다. 그렇지 않으면 검증된 Go baseline을 유지한다.

## 참고 자료

- [AWS SDK for Go v2 Kinesis API](https://pkg.go.dev/github.com/aws/aws-sdk-go-v2/service/kinesis)
- [KPL로 producer 개발](https://docs.aws.amazon.com/streams/latest/dev/developing-producers-with-kpl.html)
- [KPL 핵심 개념: batching, aggregation, collection](https://docs.aws.amazon.com/streams/latest/dev/kinesis-kpl-concepts.html)
- [KPL process와 IPC integration](https://docs.aws.amazon.com/streams/latest/dev/kinesis-kpl-integration.html)
- [KPL 지원 platform과 C++ child](https://docs.aws.amazon.com/streams/latest/dev/kinesis-kpl-supported-plats.html)
- [KPL retry와 rate limiting](https://docs.aws.amazon.com/streams/latest/dev/kinesis-producer-adv-retries-rate-limiting.html)
- [AWS Labs Kinesis aggregation](https://github.com/awslabs/kinesis-aggregation)
- [AWS Fluent Bit Kinesis plugin](https://github.com/aws/amazon-kinesis-streams-for-fluent-bit)
- [AWS SDK for Go v1 지원 종료 공지](https://aws.amazon.com/blogs/developer/announcing-end-of-support-for-aws-sdk-for-go-v1-on-july-31-2025/)
- [기존 실제 AWS producer 비교](explanation_aws_kinesis_producer_comparison.md)
- [현재 50k connection-path baseline](explanation_connection_path_50k_baseline.md)

## 개인 의견

Go를 유지한다면 지금까지의 검증만으로 직접 구현 위험이 충분히 해소됐다고 단정하기 어렵고,
추가적인 검사가 필요할 수 있다고 생각한다. 장기 유지보수에서 고수준 producer의 공식 지원을
더 중요하게 본다면 Java KPL 기반으로 이전하는 방안도 고려해 볼 만하다는 것이 나의 개인적인
생각이다.
