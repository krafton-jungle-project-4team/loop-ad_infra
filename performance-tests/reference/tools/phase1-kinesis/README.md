# Phase 1: Kinesis 단건 collector 전환 실험

> 현재 실제 AWS producer 비교는
> [AWS Kinesis producer comparison contract](./reference_aws_producer_comparison_contract.md)가
> 이 문서의 과거 단건 전환 비용·부하·이미지 규칙을 대체한다. 비교 상한은 `$20`,
> 초기 전체 계획 상한은 `$16`, 새 실행 중단선은 `$18`이며 공유 stream은 최대
> 3시간만 유지한다.

> Phase 1 capacity test는 comparison 계약을 완화하지 않는 별도
> [capacity-test contract](./reference_phase1_kinesis_capacity_contract.md)를 따른다.
> capacity 전용 topology, 3회 반복, 100/110/120분 runtime guard만 별도 도구에서
> 적용한다.

> Final capacity 전에 별도
> [capacity scout contract](./reference_phase1_kinesis_capacity_scout_contract.md)의
> 짧은 feasibility probe와 완전한 cleanup을 먼저 수행한다. Scout 결과는 final 측정이나
> 후보 순위로 재사용하지 않는다.

이 디렉터리는 기존 Kafka collector를 대체하지 않는 Kinesis Data Streams 전환
실험의 정적 입력, local 검증 도구와 실행 helper를 보관한다. 실제 run 결과는
형제 경로 `performance-tests/run_<timestamp>_phase1_kinesis_<name>/`에 한 번만
생성하고 덮어쓰지 않는다.

상세 실행 절차와 판정 기준은
[Phase 1 Kinesis 단건 전환 성능 실험 가이드](../../docs/guides/guide_phase1_kinesis_transition_performance_test.md)를
따른다.

## 현재 checkpoint

- collector 기준 commit `1769eec` 존재 확인 완료
- 기존 collector `main`은 clean하며 local `origin/main`보다 한 commit 앞섬
- infra 기준 branch `codex/aws-perf-test-plan`, 초기 HEAD `4cc0e48`
- Phase 0 최종 oha 근거: 1 x `c6in.large`, 54,948.88 RPS, p95 3.28ms,
  p99 35.02ms, 오류 0
- SDK 호환 Phase 0 전체 body: 192개, 1,044~1,535 bytes, 평균
  1,348.015625 bytes, 프로필별 64개
- 최초 32 vCPU/collector 7대 전제는 쿼터 승인으로 폐기됨
- 사용자 승인 scale 후보: collector `1 -> 2 -> 4 -> 6 -> 8 -> 12 -> 17`
- 17 x `c6i.xlarge`는 collector 68 vCPU이며, 기존 Standard 사용량이 0이어도
  generator 2 vCPU와 reserve 2 vCPU를 포함해 적용 쿼터 72 vCPU 이상이 필요함
- Phase 1 AWS run: 아직 없음
- Phase 1 누적 비용: `$0`

이 상태는 최초 문서 checkpoint의 기준선이다. AWS credential, quota와 가격은
deploy 직전에 다시 확인한다.

### 선행 Phase 0 run 상태

기존 증거는 수정하지 않는다. 자동 미완료-run 검사는 다음 예외를 알고 있어야 한다.

- `run_20260710_001859_phase0_alb_keepalive_4ec2`에는 `run.json`이 없다.
- `run_20260710_011033...`, `run_20260710_013753...`,
  `run_20260710_020152...`는 report/destroy 근거가 있지만 `run.json` status가
  `planned`이고 result/cleanup이 null이다.
- 최종 `run_20260710_043706_phase0_alb_oha_pool_1xc6inlarge_55krps`는 passed와
  cleanup true지만 현재 Phase 1 필수 형식의 `metrics-summary.json`이 없고
  `commands.md`가 전체 명령이 아닌 요약이다.

이 예외는 Phase 1의 새 run 형식을 완화하지 않는다. Phase 1 run은 아래 필수 파일과
최종 status/cleanup을 모두 검증한다.

## 디렉터리 계약

구현 checkpoint별로 채우는 목표 구조:

```text
performance-tests/phase1-kinesis/
  README.md
  generate-payload-pool.mjs
  payloads/
    sdk-compatible-event-bodies.ndjson
    sdk-compatible-event-bodies.manifest.json
    size-boundary-payloads/
  kinesis-stub/
  run-local-smoke.sh
  run-ec2-oha-worker.sh
  verify-al2023-hosts.mjs
  collect-metrics.sh
  collect-pprof.mjs
  verify-cleanup.sh
  calculate-cost.mjs
```

`collect-pprof.mjs` opens one local Session Manager port-forward per collector and
collects profiles without exposing the debug listener through the ALB or a security
group. Snapshot mode collects allocs, heap, and goroutine profiles; CPU mode collects
the requested steady-state CPU interval on all collectors concurrently.

파일은 구현 checkpoint에서 필요한 최소 단위로 추가한다. credential, account-wide
inventory, raw secret 또는 static AWS key는 이 디렉터리에 저장하지 않는다.

## 목표 부하 payload pool

`generate-payload-pool.mjs`는 목표 성능 run에서 그대로 사용할 사전 직렬화된
NDJSON body pool과 manifest를 결정론적으로 만든다.

```bash
node performance-tests/phase1-kinesis/generate-payload-pool.mjs
```

현재 pool 계약:

- body 480개와 고유 `event_id` 480개
- `compact`, `standard`, `expanded` 각 160개
- body 1,092~1,518 bytes, 정확한 평균 1,341 bytes
- Kinesis와 같은 MD5 partition-key hash를 80개 균등 hash range에 매핑
- shard별 pool row 6개, profile별 2개
- 50k RPS 균등 선택 가정에서 모든 shard 625 records/s,
  838,125 bytes/s
- 전체 NDJSON SHA-256과 shard별 record/byte 분포를 manifest에 기록

oha는 `-Z`로 각 요청마다 완전한 NDJSON line 하나를 선택한다. 요청 시 JSON을 다시
직렬화하지 않는다. generator를 다시 실행한 뒤 SHA-256 또는 manifest가 바뀌면
payload 변경으로 보고 별도 검증·커밋한다.

AWS load generator는 Python으로 pool을 재생성하지 않는다. `invoke-oha.mjs`가
커밋된 NDJSON의 SHA-256을 manifest와 대조하고 gzip+base64로 압축한 뒤 SSM command에
직접 전달한다. 현재 480-line pool은 raw 644,160 bytes, gzip 14,212 bytes,
base64 18,952 bytes다. 각 load 전에는 별도 20 KiB SSM 전달 probe의 byte 수와
SHA-256 왕복 검증이 먼저 통과해야 한다.

Phase 1 EC2 host는 ECS-optimized Amazon Linux 2023을 사용한다.
`verify-al2023-hosts.mjs`는 부하 전에 SSM으로 collector와 load generator를 검사해
AL2023, cgroup v2, IMDSv2-only, cloud-init/user-data 완료, Docker/SSM/ECS 상태,
`jq`/`awscli-2`, embedded worker hash와 pinned oha image를 증적으로 저장한다.

## 전송 불변식

```text
1 event = 1 HTTP request = 1 PutRecord = ACK 이후 1 HTTP 202
```

- body는 최대 `64 KiB`이며 검증된 원문 JSON bytes를 Kinesis에 저장한다.
- `event_id`가 partition key다.
- `PutRecords`, batch, queue, timer와 ACK 전 `202`는 금지한다.
- 처리 한계는 비차단 admission limiter와 `429`로 표현한다.
- 목표 50k run에서는 `429`가 한 건이라도 있으면 통과하지 못한다.

## Local checkpoint

collector 코드 변경마다 다음 결과가 모두 있어야 AWS image를 만들 수 있다.

```text
go test ./...
go test -race ./...
go vet ./...
binary/image build
10~30초 local Kinesis stub oha smoke
202/400/413/429/503 mapping
HTTP request : PutRecord = 1 : 1
ACK 전 202 없음
goroutine leak 없음
실제 AWS Kinesis 접근 없음
```

stub는 fake credential, metadata 비활성화와 localhost 또는 Docker internal endpoint만
사용한다.

## AWS run 폴더

각 run은 아래 파일을 모두 가진다.

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_phase1_kinesis_<short_name>/
  run.json
  infra.md
  commands.md
  metrics-summary.json
  report.md
  artifacts.md
  cost.md
```

필요한 raw artifact 예:

```text
oha-report.json
alb-metrics.json
collector-metrics.json
kinesis-metrics.json
load-generator-metrics.json
debug-vars/
pprof/
cleanup-verification.json
cost-summary.json
```

실패, 중단, inconclusive run도 삭제하지 않는다. 결과 작성, destroy 검증과 run별
commit이 끝나기 전에는 다음 가설로 넘어가지 않는다.

사용자 override로 load driver는 oha를 쓰지만 `event-pipeline-loadtest-runner`의
`deploy -> run -> record -> verify -> destroy -> commit` cycle은 유지한다.

## 부하와 scale 순서

```text
AWS smoke: 1k RPS x 30s
exploration: 5k -> 10k -> 20k -> 35k -> 50k, 각 60s
collector: 1 -> 2 -> 4 -> 6 -> 8 -> 12 -> 17
final: 50k x 30s, 60s cooldown, 50k x 300s
```

더 적은 collector 수로 성공하면 큰 구성을 생략한다. 매 deploy 전에 실제 적용
quota와 running/pending Standard vCPU로 `max_safe_collector_count`를 다시 계산하고,
그보다 큰 후보는 실행하지 않는다. 상한이 17 이하이고 후보값이 아니면 마지막에
상한 자체를 추가한다. 사용자 승인 탐색 상한은 17대다.

## 비용과 cleanup

- Phase 1 누적 비용 상한: `$50`
- `$45`부터 선택적 optimization run 중단
- 다음 필수 run으로 `$50` 이상 예상 시 실행 금지
- 각 run에 실시간 계산과 사후 Cost Explorer 값을 모두 기록
- deploy부터 wall-clock deadline과 signal cleanup trap을 적용하고, destroy 실패 중에도
  billable resource upper-bound 비용을 계속 누적
- 매 run 뒤 `LoopAdPerfPhase1KinesisStack` destroy와 Kinesis/ASG/EC2/ALB/VPC
  잔여 검증
- 현재 goal이 만든 정확한 `phase1-<collector-sha>` tag는 digest 확인 후 삭제
- 기존 ECR repository, `latest`, 관련 없는 image, dev/운영 resource와 Kafka는 삭제
  대상이 아님

## Commit 경계

초기 문서, collector fixture, Kinesis producer, ingest wiring, admission control,
payload/error 계약, debug metrics, CDK stack, payload pool, 실행 도구, 각 local
최적화, 각 AWS run과 최종 Explanation을 별도 논리 commit으로 남긴다.

collector 실험 branch는 `main`에 병합하거나 origin으로 push하지 않는다. infra도
사용자 요청 없이는 push 또는 PR을 만들지 않는다.
