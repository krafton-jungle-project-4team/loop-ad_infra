# Phase 1 Kinesis 단건 collector 전환 실험 결과

## 2026-07-11 pprof 최적화 후속 실험

### 결론

후속 goal의 최종 판정은 **failed**다. 네 대의 `c6i.xlarge` collector로 30,000 RPS를
60초 처리하는 목표를 current HEAD 기준선과 A-D hot-path 최적화, 그리고 Kinesis
연결 수 1024/256/128/512에서 모두 검증했지만 성공 후보가 없었다. 따라서 동일 최종
후보의 독립 30k 재현 2회는 실행할 수 없었다.

단건 계약은 모든 run에서 유지됐다. HTTP 202, ALB request, Kinesis IncomingRecords,
collector PutRecord operation/success가 일치했고 429, 5xx, transport error, Kinesis
throttle, collector failure는 0이었다. 병목은 데이터 손실이나 shard 제한이 아니라
동기 PutRecord 경로의 요청당 비용과 1024개 oha 연결에서 발생하는 client-side
backlog다.

### 코드와 로컬 벤치마크

collector 실험 branch의 후속 최종 SHA는
`a8bb40bd2ab78df27498fab54750158c32389978`, amd64 image digest는
`sha256:976889a27e5a84a1329ac31ee9f2d5440ea980642f55b6d48333eb6fe075dc03`다.

| 변경 | 핵심 결과 |
| --- | --- |
| A, `json.Valid` + 객체 외형 검사 | properties 검증 1,294.5ns/1,488B/20 alloc → 약 232.7ns/0B/0 alloc |
| B, reflection 없는 명시적 계약 검증 | 전체 payload 6,835.5ns/3,658B/52 alloc → 약 4,161ns/1,664B/20 alloc |
| C, partition key string 재사용 | 14.19ns/32B/1 alloc → 약 2.12ns/0B/0 alloc |
| D, 고정 202 body 직접 쓰기 | 전체 성공 handler 10,775.5ns/12,830B/96 alloc → 약 7,106ns/10,644B/60 alloc |
| E, transport 연결 수 분리 | admission 1024를 유지하면서 Kinesis 연결 1024/256/128/512를 독립 검증 |

모든 단계에서 `go test ./...`, race test, vet, contract fixture와 10회 benchmark를
통과했다. properties의 빈 객체, 중첩, escape, whitespace, 배열, scalar, null,
malformed, trailing garbage, large object를 fixture로 고정했다.

### AWS 기준선과 연결 matrix

모든 행은 4 x `c6i.xlarge`, 1 x `c6in.large` oha, oha connections 1024,
admission 1024, 80 provisioned shards, 동일 480 payload pool, 60초 조건이다.

| 코드 / Kinesis 연결 | Actual RPS | p95 / p99 ms | ECS CPU max % | alloc bytes/op | retries | 판정 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| current HEAD / 1024 | 27,590.73 | 4,604.13 / 4,758.30 | 95.58 | 62,692 | 5 | failed |
| A-D / 1024 | 24,878.50 | 9,548.51 / 10,041.25 | 76.59 | 56,829 | 0 | failed |
| A-D / 256 | 24,235.81 | 10,906.86 / 11,397.24 | 69.05 | 56,754 | 0 | failed |
| A-D / 128 | 25,239.44 | 9,039.81 / 9,451.68 | 78.14 | 56,717 | 0 | failed, matrix 최고 |
| A-D / 512 | 24,383.06 | 10,829.02 / 11,154.41 | 82.16 | 56,889 | 12 | failed |

10k와 20k latency는 개선됐다. 특히 256 연결의 20k는 p95 41.51ms, p99
135.99ms였다. 그러나 30k에서는 어느 연결 수에서도 actual RPS 29,700, p95
100ms, p99 200ms를 동시에 만족하지 못했다.

### pprof 해석

기준선 30k의 대표 CPU profile은 `runtime.mallocgc` cumulative 23.49%, syscall flat
11.61%, `encoding/json.Decoder.readValue` cumulative 4.69%였다. 기준선 alloc-space
profile에서 properties validator는 대표 host 기준 4.37GB cumulative allocation을
차지했다.

A-D 이후 properties validator allocation은 top 목록에서 사라졌고 30k 할당은 약
62.7KiB/op에서 56.8KiB/op로 약 9.4% 감소했다. `runtime.mallocgc` cumulative share도
18.26%로 낮아졌다. 남은 allocation 상위 항목은 AWS SDK Kinesis operation,
SigV4, HTTP request clone/header, response deserialize, body read다. 즉 현재 추가
최적화 우선순위는 validator가 아니라 synchronous AWS SDK `PutRecord` 경로다.

### 비용과 안전

이 후속 goal의 비용은 이전 goal과 분리했다. 각 배포 전 공개 가격 기반 보수적
상한을 재계산했고 hard limit `$30` 이상이면 시작하지 않는 gate를 적용했다. 마지막
512 run까지 realtime 누적 상한은 `$17.3568530717`이다. 당일 Cost Explorer 값은
billing delay 때문에 authoritative gate로 사용하지 않았다.

Spot, 기존 instance 중단, quota 증설, PutRecords/batch/queue, ACK 전 202, payload별
fast path는 사용하지 않았다. collector branch와 infra branch는 push·merge·PR하지
않았다.

최종 cleanup 검증은 compute stack, shared stream stack/stream, ASG/active EC2, ALB,
target group, VPC, ECS cluster/service, log group, 명명 IAM role이 모두 없음을 확인했다.
기준선과 최적화 image의 정확한 두 ECR tag는 예상 digest 대조 후 삭제했고 ECR
repository와 관련 없는 tag는 유지했다. 활성 실험 SSM session도 0개였다.

### 다음 단계

네 대 topology를 유지한 채 30k를 다시 시도하려면 연결 수를 더 훑는 것보다 AWS SDK
요청당 allocation과 transport/SigV4/deserialize 비용을 줄이는 별도 가설이 필요하다.
그 변경도 단건 PutRecord와 ACK 후 202 계약을 유지해야 한다. 또는 추가 collector
용량을 별도 승인된 goal에서 검증해야 하며, 이 후속 goal에서는 quota 증설을
요청하지 않았다.

## 최종 판정

이 goal의 최종 판정은 **blocked (검증된 비용 중단)** 이다.

8 x `c6i.xlarge` collector가 20,000 RPS를 60초 동안 처리하는 탐색 run은 통과했다. 그러나 다음 필수 단계인 같은 8대 구성의 35,000 RPS x 60초 run은 보수적 누적 비용 상한이 `$51.95830863474036`으로 hard limit `$50`을 넘는다. 따라서 35k, 50k x 30초, cooldown 60초, 최종 50k x 300초는 배포하지 않았다. 50,000 RPS x 300초 목표는 증명되지 않았다.

마지막 완료 run까지 누적 보수 상한은 `$49.174260766247215`이다. Cost Explorer의 당일 값은 billing-delayed-or-zero였으므로 run별 실시간 결정론적 상한을 판정 근거로 사용했다.

## 목적과 비목적

목적은 별도 collector 실험 브랜치에서 기존 Kafka producer를 동기식 Kinesis `PutRecord`로 전환하고, 현재 계정의 안전한 Standard On-Demand vCPU 범위와 비용 제한 안에서 처리 한계를 검증하는 것이었다.

비목적은 기존 Kafka 경로 교체, 운영 트래픽 전환, collector 실험 브랜치의 main 병합·push, Spot 사용, 기존 인스턴스 중단, quota 증설, 관련 없는 AWS 리소스 변경이다. 이 항목들은 실행하지 않았다.

기존 Kafka 아키텍처는 이 실험이 전환 가능성과 용량을 검증하는 격리된 Phase 1이기 때문에 유지했다. 실험 실패나 비용 중단이 기존 수집 경로에 영향을 주지 않도록 별도 branch, 별도 ECR tag, 별도 Kinesis stream, 임시 CDK stack을 사용했다.

## 변경할 수 없는 전송 계약

- 이벤트 1개 = HTTP 요청 1개 = 논리적 `PutRecord` 1개
- 검증된 원문 JSON body bytes를 Kinesis record data로 사용
- `event_id`를 partition key로 사용
- Kinesis ACK 이후에만 HTTP 202 반환
- `PutRecords`, batch, queue, timer, flush, ACK 전 202 금지
- initial call 포함 최대 4 attempts
- timeout 경계에서는 ACK 유실로 at-least-once 중복 가능
- admission 또는 최종 Kinesis throttling은 429, 내부·권한·네트워크 실패는 503
- 통과 run에서는 HTTP 202, Kinesis IncomingRecords, collector logical operation, wire attempt, success가 정확히 일치해야 함

## 요구사항과 payload 변경 이력

초기 payload pool은 12,288개였으나 최종 goal에서 정확히 480개 이상의 고유 `event_id`로 요구가 변경됐다. `4ffc847`에서 기존 pool을 덮어쓰지 않고 별도 commit으로 480개 balanced pool을 확정했다.

최종 pool:

- 480 rows, 고유 event_id 480개
- compact/standard/expanded 각 160개
- body 1,092~1,518 bytes, 평균 1,341 bytes
- SHA-256 `f82cd61548b1be8d5df21a91b8e86390422e4d433ac6dc93d87414a3755336c2`
- 80개 shard마다 6 rows, profile별 2 rows
- Kinesis와 동일한 MD5 unsigned 128-bit hash 및 `floor(hash * 80 / 2^128)`
- 50k 가정에서 shard당 625 records/s, 838,125 bytes/s
- 로컬 NDJSON을 gzip+base64로 SSM에 직접 전달
- 매 부하 전 정확히 20 KiB SSM round-trip byte count와 SHA-256 probe 수행
- EC2에서 Python payload 생성 또는 Python script embedding을 사용하지 않음

## 구현 및 검증 checkpoint

주요 commit:

| Commit | 내용 |
|---|---|
| `fbbcca4`, `69951ef` | Phase 1 guardrail과 안전 검사 |
| `ef35600` | 단건 Kinesis ingest wiring local 검증 |
| `48b569d` | admission control 검증 |
| `a648688` | payload/HTTP error 계약 검증 |
| `b9d259d` | debug counter 검증 |
| `42acc49` | Phase 1 CDK stack |
| `78cf4b6`, `4ffc847` | balanced payload, 480-row 최종 변경 |
| `dc771df` | AWS run tooling |
| `e65d3cd` | 로컬 NDJSON gzip+base64 SSM 전달 |
| `7fb11c2` | ECS-optimized AL2에서 AL2023으로 별도 migration |
| `8753e42` | 연속 run 동안 공유 Kinesis 유지 |
| `b5e804b` | capacity-provider eventual consistency 제거, direct ECS EC2 scheduling |

Collector 실험 branch는 `codex/phase1-kinesis-transition`, 최종 SHA는 `dce50bb1472fb7bd5d37c9c355798af9606b1766`이다. ECR에서 실행한 image digest는 `sha256:4531af88ac1999bebc0d5dd147b551e2402497660ee55a6f964ba345d9cdb926`이다.

AL2023 검증은 모든 smoke/load에서 SSM으로 AMI SSM parameter, OS 2023, cgroup v2, IMDSv2-only, cloud-init/user-data success, dnf, Docker, SSM agent, ECS agent, ECR pull, cgroup memory 값을 확인했다. 코드와 Jest에는 AL2023 SSM path와 user-data 기대값이 들어 있으며 `amazonLinux2()`, AL2 SSM path, `yum install` 생성 경로가 남지 않았다.

## 전체 AWS run timeline

| Run / commit | 구성 | 결과 | actual RPS | 429 | p95 ms | Kinesis records | ECS CPU max % | ECS memory max % |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| `run_20260710_221849_phase1_kinesis_smoke_1c_1krps` / `cc28436` | 1c, 1k | failed deploy/tooling | 0 | 0 | - | 0 | - | - |
| `run_20260710_225534_phase1_kinesis_smoke_retry` / `5dc1b26` | 1c, 1k | passed | 999.73 | 0 | 12.96 | 30,000 | 12.77 | 3.26 |
| `run_20260710_232642_phase1_kinesis_explore_1c_5krps` / `2d9d75e` | 1c, 5k | failed stability-gate scope | 4,998.93 | 0 | 18.46 | 299,995 | 53.69 | 12.80 |
| `run_20260710_235854_phase1_kinesis_al2023_smoke` / `4bbdc64` | 1c, 1k | passed | 999.69 | 0 | 14.04 | 29,999 | 11.57 | 4.09 |
| `run_20260711_003908_phase1_kinesis_1c_5krps` / `676b221` | 1c, 5k | passed | 4,997.82 | 0 | 51.56 | 299,995 | 58.97 | 14.01 |
| `run_20260711_010045_phase1_kinesis_1c_10krps` / `3b985e7` | 1c, 10k | failed capacity | 8,345.94 | 0 | 9,501.75 | 501,156 | 98.56 | 33.88 |
| `run_20260711_011750_phase1_kinesis_2c_10krps` / `2a2582d` | 2c, 10k | failed deploy readiness | - | 0 | - | - | - | - |
| `run_20260711_013903_phase1_kinesis_2c_10k_retry` / `fcc8515` | 2c, 10k | failed readiness | - | 0 | - | - | - | - |
| `run_20260711_015921_phase1_kinesis_2c_10k_retry2` / `8423835` | 2c, 10k | failed association consistency | - | 0 | - | - | - | - |
| `run_20260711_101443_phase1_kinesis_2c_10k_retry3` / `bee639a` | 2c, 10k | failed p95 | 9,995.20 | 0 | 216.21 | 599,991 | 55.99 | 18.93 |
| `run_20260711_103455_phase1_kinesis_4c_10krps` / `2d93d58` | 4c, 10k | failed capacity-provider deploy | - | 0 | - | - | - | - |
| `run_20260711_105300_phase1_kinesis_4c_10k_ec2` / `7e0273d` | 4c, 10k | passed | 9,997.60 | 0 | 19.48 | 599,995 | 39.63 | 14.56 |
| `run_20260711_112221_phase1_kinesis_4c_20k` / `a79dbc3` | 4c, 20k | failed 429/p95 | 19,989.36 | 219 | 609.39 | 1,199,768 | 64.42 | 25.62 |
| `run_20260711_114008_phase1_kinesis_6c_20k` / `3bd30c9` | 6c, 20k | failed 429/p95 | 19,990.87 | 69 | 280.06 | 1,199,928 | 47.75 | 21.31 |
| `run_20260711_121544_phase1_kinesis_8c_20k` / `d57dfd4` | 8c, 20k | passed | 19,980.23 | 0 | 48.67 | 1,199,941 | 33.36 | 14.71 |

모든 경로는 `performance-tests/<run-id>/` 아래에 있다. 실패·배포 실패 run도 원인, 비용, cleanup 증적과 함께 별도 commit으로 보존했다.

## Collector 수별 관찰

- 1 collector: 5k는 통과했지만 10k에서 CPU 98.56%, 실제 8.35k RPS, p95 9.5초로 포화됐다.
- 2 collectors: 10k 요청량은 달성했지만 p95 216.21ms로 exploration 기준 100ms를 넘었다. readiness/capacity association 실패는 별도 실패 run으로 남겼다.
- 4 collectors: 10k는 p95 19.48ms로 통과했다. 20k에서는 admission 429 219건과 p95 609.39ms가 발생했다.
- 6 collectors: 20k에서 429는 69건으로 감소하고 p95는 280.06ms로 개선됐지만 통과하지 못했다.
- 8 collectors: 20k에서 429/5xx/transport error/retry/throttle이 모두 0, p95 48.67ms, p99 221.64ms로 통과했다. ECS CPU max 33.36%, memory max 14.71%였다.
- 8c 통과 run의 collector별 logical operations는 약 149,992~149,993건으로 균등했다. 전체 operation/wire/success는 각각 1,199,941이고 retry/failure/throttle은 0이었다.
- 단기 run 직후 goroutine은 host별 219~280, heap은 약 9.0~21.3 MiB였다. 300초 final run을 수행하지 못했으므로 장기 GC, heap 안정성, 연속 CPU/memory growth gate는 증명되지 않았다.

## Overload 429와 회복

별도 인위적 overload run 대신 탐색 중 실제 admission 한계가 관측됐다. 4c/20k에서 219건, 6c/20k에서 69건의 429가 발생했다. 두 run 모두 Kinesis throttle, retry, 5xx, task restart 없이 실패를 격리했다. 8c/20k에서는 admission reject가 0으로 회복됐고 HTTP 202, ALB 2xx, Kinesis IncomingRecords, collector wire success가 모두 1,199,941건으로 일치했다.

## Kinesis 결과

공유 stream은 provisioned 80 shards, retention 24h, AWS managed encryption으로 연속 테스트 동안 유지했다. 각 완료 run 후 compute stack만 삭제했고, 전체 테스트 종료 후 stream stack을 삭제했다.

최종 8c/20k 통과 run:

- IncomingRecords: 1,199,941
- IncomingBytes: 1,631,949,895
- WriteProvisionedThroughputExceeded: 0
- collector throttles/retries/failures: 0
- HTTP 202 및 ALB target 2xx: 각각 1,199,941

Kinesis 자체가 20k 단계의 병목이라는 증거는 없었다. 35k와 50k는 비용 게이트 때문에 실행하지 않아 해당 구간의 shard 분포와 throttling은 증명되지 않았다.

## 비용과 중단 조건

- hard limit: `$50`
- optional stop: `$45`
- 마지막 완료 run 전 누적 상한: `$46.438067234666065`
- 8c/20k run 상한: `$2.7361935315811508`
- 마지막 누적 상한: `$49.174260766247215`
- 다음 필수 8c/35k run 상한: `$2.7840478684931504`
- 다음 run projected cumulative: `$51.95830863474036`
- 판정: `mayStart=false`, 추가 deploy 금지

80-shard stream 비용은 사용자가 승인한 연속 3시간 창까지 한 번만 예약해 run 간 중복 계상하지 않았다. 모든 compute는 run별 1시간 올림, Kinesis PUT payload units, ALB/LCU, EBS, IPv4, detailed metrics, logs, metric API, ECR storage, Cost Explorer API를 포함해 보수적으로 계산했다.

## Quota와 안전 상한

마지막 배포 전 확인값:

- Standard On-Demand applied quota: 80 vCPU
- 관련 없는 기존 Standard 사용: 4 vCPU (1 x t4g.medium, 1 x t4g.small)
- load generator: 2 vCPU
- safety reserve: 2 vCPU
- collector: 4 vCPU/host
- 계산된 max safe collector count: 17
- 8 collector 후보 필요량: 기존 4 + generator 2 + reserve 2 + collector 32 = 40 vCPU

quota 증설, Spot, 기존 인스턴스 중단은 하지 않았다.

## AWS cleanup 검증

최종 cleanup은 `performance-tests/run_20260711_123500_phase1_kinesis_final_cleanup/final-cleanup-verification.json`에 기록했다.

검증 결과:

- `LoopAdPerfPhase1KinesisStack` 없음
- `LoopAdPerfPhase1KinesisStreamStack` 없음
- `perf-phase1-loop-ad-events` 없음
- run-owned active EC2 없음
- Phase 1 ASG, ALB, target group, VPC 없음
- ECS cluster inactive/없음
- collector log group 없음
- named IAM roles 없음
- custom-resource Lambda log group 없음
- exact ECR tag `phase1-dce50bb1472fb7bd5d37c9c355798af9606b1766`은 digest 일치 확인 후 삭제했고 이후 `ImageNotFoundException` 확인
- ECR repository, `latest`, 관련 없는 images는 수정하지 않음

Collector 실험 branch는 main에 merge하지 않았고 origin에 push하지 않았다. Infra branch도 push하지 않았다.

## 남은 위험과 다음 단계

- 목표 50k x 300초는 미검증이다.
- 20k에서 필요한 최소 확인 구성은 8 x c6i.xlarge였지만 35k/50k 최소 collector 수는 알 수 없다.
- 300초 구간의 연속 CPU, cgroup v2 memory growth, GC, goroutine 안정성은 미검증이다.
- timeout 경계의 `PutRecord` ACK 유실은 at-least-once 중복 가능성이 남는다.
- 다음 작업은 비용 예산을 명시적으로 확대한 새 goal에서 8c/35k부터 재개한다. 동일 image SHA와 payload SHA를 재사용하더라도 가격, quota, AL2023 recommended AMI, ECR image 존재 여부를 다시 검증해야 한다.
- 35k 통과 시 50k x 30초와 cooldown 후 50k x 300초를 수행하고, 통과하지 않으면 12c, 17c 또는 당시 계산된 안전 상한을 비용 게이트와 함께 적용한다.

## Goal 전문

아래는 이 실험에 적용한 authoritative goal objective의 전문이다.

```text
Phase 1 Kinesis 단건 event collector 전환 실험을 현재 작업 상태에서 이어서 완료하라.

이 프롬프트가 이전 Phase 1 goal을 대체하는 최종 기준이다. 특히 목표 성능용 payload pool의 고유 event_id 요구사항은 기존 12,288개에서 480개로 변경되었다.

# 1. 목표

별도 collector 실험 브랜치에서 기존 Kafka producer를 AWS Kinesis Data Streams의 동기식 `PutRecord`로 전환하고, 현재 AWS 계정의 Standard On-Demand vCPU 쿼터 안에서 50,000 RPS를 300초 동안 처리할 수 있는지 증명한다.

다음 중 하나에 도달할 때까지 진행한다.

성공:

- 안전한 collector 수 이하에서 최종 성공 조건을 충족
- 모든 local/AWS 결과 작성·검증·커밋
- 최종 Explanation 리포트 커밋
- 이 goal이 만든 AWS 테스트 리소스 및 ECR image tag 삭제 검증
- collector 실험 브랜치를 main에 병합하거나 push하지 않음

검증된 중단:

- 비용, 쿼터, AWS capacity, generator/Kinesis 병목 또는 기술적 한계로 정의된 중단 조건에 도달
- 실패·blocked·inconclusive 근거와 다음 단계 기록
- 실패 run도 커밋
- AWS 리소스와 Phase 1 image tag 삭제 검증
- collector 실험 브랜치를 main에 병합하거나 push하지 않음

부분 구현이나 단일 smoke 통과만으로 goal을 완료하지 않는다.

# 2. 속도 우선 원칙

이전 세션에서 안전 도구를 과도하게 확장해 진행이 느려졌다.

- 안전한 배포·비용 제한·cleanup 검증에 필수적이지 않은 자동화는 추가하지 않는다.
- 현재 작성 중인 도구를 최소한으로 마무리한 뒤 collector 1대 AWS smoke로 바로 넘어간다.
- 이미 완료된 collector 구현과 local smoke를 코드 변경 없이 반복하지 않는다.
- 문서 형식이나 범용 프레임워크를 과도하게 확장하지 않는다.
- AWS run에 필요한 최소 증거만 정확히 수집한다.

# 3. 변경할 수 없는 전송 계약

- 이벤트 1개 = HTTP 요청 1개
- 유효한 HTTP 요청 1개 = Kinesis `PutRecord` 논리 호출 1개
- Kinesis ACK 이후에만 HTTP 202 반환
- Kinesis record data는 검증된 원문 JSON body bytes
- `event_id`를 partition key로 사용
- `PutRecords`, batch, 다중 이벤트 body, batch queue/timer/flush 금지
- validation 제거 및 ACK 전 202 금지
- 무한 retry, queue, goroutine 금지
- retry는 initial call 포함 최대 4 attempts
- timeout 경계에서 at-least-once 중복 가능성 문서화

HTTP 계약:

- 정상 ACK: 202 `{"accepted":1}`
- 빈 body/schema 오류: 400
- 64 KiB 초과: 413
- 지원하지 않는 content type: 415
- admission 초과: 429
- Kinesis throttling 최종 실패: 429
- Kinesis 내부/권한/네트워크 실패: 503
- 429에는 `Retry-After: 1`, `Cache-Control: no-store`
- admission 429 body:
  `{"error":"too_many_requests","message":"collector capacity exceeded"}`

# 4. 수정된 payload 정책

목표 성능 run의 고유 `event_id`는 정확히 480개 이상이면 된다. 기존 12,288개 요구는 폐기한다.

권장 고정 구조:

- 총 body 480개
- `compact`, `standard`, `expanded` 각 160개
- Kinesis 80개 hash range마다 key 6개
- 각 shard마다 profile별 key 2개
- Kinesis와 동일하게 partition key의 MD5를 unsigned 128-bit 정수로 해석
- `floor(hash * 80 / 2^128)` 방식으로 shard mapping
- body 약 1,047~1,526 bytes
- 평균 약 1,341 bytes
- 요청 시 JSON 재직렬화 금지
- oha `-Z`로 요청마다 사전 직렬화된 line 하나 선택
- collector를 pool의 특정 값, 순서, 크기 또는 hash에 특화하지 않음

480개를 shard/profile별로 정확히 균등 생성하면 50k RPS에서 shard당 예상 부하는 약 625 records/s다. manifest에 shard별 예상 records/s와 bytes/s, 전체 SHA-256을 기록한다.

기능 검증용 payload는 별도로 유지한다.

- 512 B
- 4 KiB
- 16 KiB
- 32 KiB
- 64 KiB 직전
- 64 KiB 초과 413

# 5. 성능 성공 조건

최종 50k RPS × 300초 run에서 다음을 모두 만족해야 한다.

- actual RPS ≥ 49,500
- HTTP 202 비율 ≥ 99.9%
- 429 = 0
- HTTP/transport 오류 ≤ 0.1%
- ALB target 5xx ≤ 0.1%
- ALB 자체 5xx는 0에 가까움
- p95 ≤ 100 ms
- p99 ≤ 500 ms
- 성공 HTTP와 Kinesis IncomingRecords 차이 ≤ 0.1%
- Kinesis throttling = 0
- collector task restart = 0
- OOM = 0
- unhealthy target = 0
- 모든 collector의 NumGC가 run 동안 3회 이상 증가
- 5분 평균 collector CPU ≤ 85%
- 1분 평균 CPU가 연속 2회 90% 초과하지 않음
- 마지막 1분 memory 평균이 warm-up 후 첫 1분보다 20% 넘게 증가하지 않음
- generator가 목표 부하를 생성
- 단건 PutRecord 계약 유지
- Phase 1 누적 비용 < $30
- 결과 커밋 및 AWS cleanup 완료

부하 순서:

- AWS 기능 smoke: 1k RPS × 30초
- 탐색: 5k → 10k → 20k → 35k → 50k, 각 60초
- 최종 후보: 50k × 30초, cooldown 60초, 50k × 300초

collector 순서:

`1 → 2 → 4 → 6 → 8 → 12 → 17`

live `max_safe_collector_count`보다 큰 값은 실행하지 않는다. live 상한이 위 후보에 없으면 상한 자체를 마지막 후보로 사용할 수 있다. 더 적은 collector 수로 성공하면 더 큰 구성을 생략한다.

# 6. AWS 안전 조건

region은 `ap-northeast-2`로 고정한다.

모든 deploy/destroy 직전 확인:

1. AWS CLI identity가 repo 설정 계정과 일치
2. AWS CLI ≥ 2.32.0
3. region 일치
4. 예상 stack 이름 `LoopAdPerfPhase1KinesisStack`
5. 적용된 Standard On-Demand vCPU quota
6. 기존 running/pending Standard vCPU
7. 같은 이름의 stack/resource 충돌
8. `Project=loop-ad`, `Environment=perf-phase1-kinesis`, 정확한 RunId ownership
9. Kinesis open shard 여유 ≥ 80
10. 누적 비용과 이번 run upper bound

계산식:

`available_collector_vcpu = applied_quota - existing_standard_vcpu - 2(generator) - 2(reserve)`

`max_safe_collector_count = min(17, floor(available_collector_vcpu / 4))`

금지:

- quota 증설 요청
- Spot
- 기존 dev/운영 instance 중단
- 다른 instance family를 이용한 quota 우회
- collector와 generator를 같은 host에 배치
- 17대 초과
- main merge/push

각 run은:

`deploy → smoke/load → record → verify → destroy → cleanup verify → commit`

이전 run 결과 commit 전 다음 run을 시작하지 않는다.

# 7. 비용 제한

- Phase 1 누적 상한: $30
- 누적 upper bound가 $27 이상이면 선택적 최적화 run 중단
- 다음 run으로 $30 이상 예상되면 시작 금지
- 실행 중 $30 도달 예상 시 aborted 후 즉시 cleanup
- Price List API 현재 단가와 보수적 upper bound 사용
- Cost Explorer 지연과 관계없이 실시간 upper bound 기록
- Cost Explorer 조회 비용도 포함
- Phase 0 비용은 포함하지 않음

# 8. 인프라 계약

별도 environment/stack:

- environment: `perf-phase1-kinesis`
- stack: `LoopAdPerfPhase1KinesisStack`

구성:

- 전용 VPC, NAT 없음
- public subnet의 collector EC2와 load generator
- internal ALB
- ECS on EC2 collector
- collector `c6i.xlarge`, host당 task 1개
- load generator `c6in.large` 1대
- Kinesis provisioned 80 shards, retention 24시간
- AWS 관리형 Kinesis 암호화
- task role은 exact stream의 `kinesis:PutRecord`만 허용
- existing ECR `loop-ad/event-collector` import
- tag `phase1-<collector-sha>`
- ECS task definition은 image digest를 직접 사용
- SSM
- EC2 detailed monitoring
- collector log retention 1일
- debug/pprof는 ALB에 연결하지 않고 SSM localhost에서만 수집
- ASG min/max/desired exact count
- ECS min healthy 0, max healthy 100
- Availability Zone rebalancing disabled
- managed capacity scaling disabled
- Spot, warm pool, scheduled scaling 없음
- destroy 시 perf 리소스 제거

# 9. 저장소 안전

Infra:

`/Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra`

- branch: `codex/aws-perf-test-plan`
- `_workspace/`, `im-not-ai/`는 사용자 소유 untracked 파일이므로 수정·stage 금지
- reset, stash, checkout, 삭제 금지
- push/PR 금지

Collector 실험 worktree:

`/private/tmp/loop-ad-event-collector-phase1-20260710-171241`

- branch: `codex/phase1-kinesis-transition`
- 기준 commit: `1769eec`
- main 수정·병합·push 금지
- 실험 branch push 금지

# 10. 현재 진행 상태

Collector 완료 상태:

현재 collector HEAD:

`dce50bb1472fb7bd5d37c9c355798af9606b1766`

완료 commit:

- `128de8e` Add local Kinesis PutRecord stub
- `268e069` Add synchronous Kinesis PutRecord producer
- `eab4f61` Wire collector to synchronous Kinesis PutRecord
- `d2f7101` Add bounded Kinesis admission control
- `2c32f97` Enforce the 64 KiB ingest contract
- `dce50bb` Expose bounded collector debug metrics

구현 완료:

- AWS SDK Go v2 `PutRecord`
- ACK 후 202
- event_id partition key
- raw validated body 저장
- max in-flight 1024
- 429/503 분류
- 64 KiB 계약
- `/debug/vars`
- pprof
- PutRecord attempts/success/throttle/failure/retry/latency
- Go memory, NumGC, goroutine, GOMAXPROCS
- exact wire attempt 계측

검증 완료:

- `go test -count=1 ./...`
- race
- vet
- mod verify
- build
- local oha 정상/overload/throttle/internal/boundary smoke
- 정상 100 RPS × 10초: 1,000건 모두 202
- admission overload: 202=160, 429=839
- 65,536 bytes=202, 65,537 bytes=413
- 실제 AWS Kinesis 접근 없음

collector worktree는 마지막 확인 시 clean이었다. 코드가 변경되지 않았다면 이 전체 local 검증을 다시 반복하지 않는다.

Infra 완료 commit:

- `fbbcca4` Phase 1 문서
- `69951ef` 안전 조건 강화
- `ef35600` ingest wiring local evidence
- `48b569d` admission evidence
- `a648688` payload contract evidence
- `b9d259d` debug metrics evidence
- `42acc49` Add Phase 1 Kinesis performance stack
- `78cf4b6` Add balanced Phase 1 payload pool

현재 committed infra HEAD:

`78cf4b6`

CDK stack `42acc49`에서 검증 완료했던 내용:

- Jest 전체 통과
- TypeScript build 통과
- synth 통과
- `cdk diff --no-change-set` 통과
- task role exact stream `kinesis:PutRecord`
- no NAT
- internal ALB
- exact ASG sizes
- stop-first ECS deployment
- Kinesis 80 shards
- digest-pinned image 계약

기존 committed payload pool은 12,288개이며 더 이상 최종 요구사항이 아니다. 새 세션에서 다음을 480개 기준으로 다시 생성해야 한다.

- `generate-payload-pool.mjs`
- `sdk-compatible-event-bodies.ndjson`
- manifest
- payload 테스트
- README 수치
- Python EC2 generator
- worker의 expected SHA-256

12,288개 파일을 그대로 유지하지 않는다. 별도 새 commit으로 480개로 축소한다. 이전 commit을 amend하지 않는다.

# 11. 현재 미커밋 작업

`78cf4b6` 이후 실행 도구 작업이 미커밋 상태로 존재한다. 먼저 `git status`와 전체 diff를 확인한다. 파일을 폐기하거나 reset하지 않는다.

작성 또는 수정 중인 항목:

- `src/perf-phase1-kinesis-stack.ts`
  - collector count 1~17 허용
  - load generator user data에 압축된 worker/generator 내장
- `test/perf-phase1-kinesis.test.ts`
- `performance-tests/phase1-kinesis/generate-payload-pool.py`
- `performance-tests/phase1-kinesis/run-ec2-oha-worker.sh`
- `performance-tests/phase1-kinesis/lookup-prices.mjs`
- `performance-tests/phase1-kinesis/calculate-cost.mjs`
- `performance-tests/phase1-kinesis/preflight.mjs`
- `performance-tests/phase1-kinesis/prepare-collector-image.sh`
- `performance-tests/phase1-kinesis/delete-collector-image.sh`
- `performance-tests/phase1-kinesis/invoke-oha.mjs`

부분 검증 상태:

- Python generator가 기존 12,288개 NDJSON과 동일 SHA를 만드는 것은 확인됨
- worker/prepare/delete shell syntax 확인됨
- Price List lookup 실 API 검증됨
- cost calculator 실 API 가격으로 검증됨
- preflight 실 계정에서 검증됨
- `invoke-oha.mjs`는 아직 AWS 실행 검증 전
- 이 미커밋 변경 전체에 대한 최종 Jest/build/synth/diff/secret scan은 아직 완료되지 않음
- 실행 도구 commit도 아직 없음

이 파일들을 무조건 신뢰하지 말고 최소 범위로 검토·수정·검증한다. 불필요한 자동화 확장은 중단한다.

# 12. 최근 live AWS 사전 점검 결과

2026-07-10 당시 읽기 전용 preflight 결과:

- Standard On-Demand quota: 80 vCPU
- 기존 running/pending Standard 사용량: 4 vCPU
  - t4g.medium 1대: 2 vCPU
  - t4g.small 1대: 2 vCPU
- generator + reserve 제외 후 collector 가능량: 72 vCPU
- goal 상한을 적용한 max safe collector count: 17
- Kinesis shard limit: 1,000
- open shards: 0
- 기존 Phase 1 stack/resource collision: 없음
- ECR repository 존재
- AWS CLI 2.35.9
- identity/account/region 일치

이 값은 참고만 한다. 첫 deploy 직전에 반드시 다시 조회한다. 기존 t4g instance를 중단하거나 변경하지 않는다.

현재 상태:

- Phase 1 AWS stack 미배포
- Phase 1 collector image 미push
- Phase 1 AWS 성능 run 없음
- Phase 1 누적 AWS 비용 `$0`
- 삭제할 Phase 1 AWS resource 없음

# 13. 다음 작업 순서

1. infra와 collector `git status`, branch, HEAD 확인
2. 미커밋 실행 도구 diff 검토
3. payload pool을 480개로 축소
   - profile별 160개
   - shard별 6개, profile별 2개
   - manifest/hash/test/docs/Python generator/worker hash 동기화
4. payload 변경 검증 후 별도 commit
5. 현재 실행 도구를 최소 범위로 마무리
6. Jest, TypeScript build, shell syntax, Python generator parity, synth, stack assertion, `cdk diff --no-change-set`, diff check, secret scan
7. 실행 도구 별도 commit
8. live preflight 재실행
9. exact collector SHA image를 linux/amd64로 build/push하고 digest 기록
10. collector 1대 stack deploy
11. ASG `AZRebalance`, `InstanceRefresh` 정지 및 확인
12. ECS stable/healthy 확인
13. 1k RPS × 30초 AWS smoke
14. 결과·metrics·debug vars 수집
15. stack destroy
16. Kinesis/ASG/EC2/ALB/VPC/log group/CloudFormation 잔여 검증
17. smoke run 문서와 비용 작성 후 commit
18. 판정에 따라 다음 collector/load 단계 진행

실제 AWS resource를 만들기 전에는 안전장치가 준비되어야 하지만, 그 이후에는 부가 도구를 더 만들지 말고 run을 우선한다.

# 14. run 기록

각 run:

`performance-tests/run_<YYYYMMDD_HHMMSS>_phase1_kinesis_<name>/`

필수 파일:

- `run.json`
- `infra.md`
- `commands.md`
- `metrics-summary.json`
- `report.md`
- `artifacts.md`
- `cost.md`
- `cleanup-verification.json`

필수 데이터:

- collector SHA
- image tag/digest
- infra SHA
- collector 수와 instance type
- quota와 기존 vCPU
- max safe collector count
- payload manifest/hash
- 요청 수, 202/400/413/429/5xx
- actual RPS, p50/p95/p99
- Kinesis IncomingRecords/throttling
- collector CPU/memory/GC/restarts
- generator CPU/network/socket
- 비용 upper bound와 가능한 사후 실제 비용
- 단일 가설
- 판정
- cleanup 결과

판정:

- passed
- failed
- aborted
- inconclusive
- blocked

# 15. 종료와 최종 리포트

최종 파일:

`docs/explanation_phase1_kinesis_transition_results.md`

포함할 내용:

- 이 goal 전문
- 요구사항과 payload 12,288→480 변경 이력
- 목적/비목적
- 단건 PutRecord 계약
- 기존 Kafka 아키텍처를 유지한 이유
- 전체 timeline/checkpoint
- 모든 local/AWS run
- collector 수별 성능·CPU·memory·GC
- overload 429와 회복
- Kinesis shard/IncomingRecords/throttling
- 480개 payload 구조와 분포
- 비용
- 최종 성공/실패/blocked/inconclusive 판정
- 당시 quota와 max safe 계산
- 모든 run 경로와 commit SHA
- image digest
- AWS cleanup 검증
- quota 증설, Spot, 기존 instance 중단을 하지 않았다는 확인
- collector main merge/push를 하지 않았다는 확인
- 남은 위험과 다음 단계

```
