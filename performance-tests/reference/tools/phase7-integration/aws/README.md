# Phase 7-2 AWS 자동화 참조

이 디렉터리는 Phase 7-2의 AWS 제어면 gate를 보관한다. 도구는 runtime 구현을 대신하지 않고,
검증된 Phase 1·4·6 데이터 경로를 하나의 immutable whole attempt로 실행할 때 순서와 안전 조건을
강제한다.

## 도구

| 파일 | 역할 | AWS 변경 |
| --- | --- | --- |
| `lookup-prices.mjs` | Seoul public On-Demand 가격과 provenance 수집 | 없음 |
| `cost_model.py` | 3시간 상한, 5 GiB logs, `$55/$60/$5` gate 계산 | 없음 |
| `preflight.py` | handoff, root identity, ownership, quota, offering, AMI, ACM, bootstrap, image 확인 | 없음 |
| `image_prep.py` | image stack 생성, 3개 image build/push, digest·architecture 확인 | image stack/ECR만 |
| `build_full_stack_scoped_source.py` | focused gate와 현재 full-stack source를 strict handoff와 구분해 봉인 | 없음 |
| `full_stack_scoped_cost_model.py` | archive-only 유료 capacity 1시간 상한, 120분 cleanup hard window와 reserve를 active `$60` 안에서 계산; 후속 strict 예약은 `$0` | 없음 |
| `seal_full_stack_scoped_attempt.py` | absent preflight 뒤 fresh attempt와 비용 예약을 ledger에 unpaid 상태로 봉인 | 없음 |
| `full_stack_scoped_archive.py` | Attempt 17 전체 stack에서 deploy·verify·15M retain-source archive·cleanup만 1회 실행 | run-owned full stack만 |
| `build_phase8_composite_handoff.py` | Attempt 17 성능 증거와 fresh scoped archive pass를 immutable composite Phase 8 handoff로 결속 | 없음 |
| `runner.py` | 새 run directory와 단계 순서, 비용, 160/180분, cleanup-only 전환 강제 | 명시 command만 |
| `runtime_stages.py` | 실제 배포 검증, correctness/replacement, seed, load, drain/accounting | run-owned runtime만 |
| `diagnostic_payload_pool.mjs` | warmup/score별 120-shard 균등 replacement-sampled body pool 생성 | 없음 |
| `run_diagnostic_oha.mjs` | 8 host×2 process HTTP/2 warmup 또는 score와 archive 1회 overlap | run-owned traffic/task만 |
| `evaluator.py` | correctness, 50k, archive, resource, cost, cleanup 최종 판정 | 없음 |
| `cleanup.py` | exact stack tag/output을 검증한 뒤 runtime, bucket, image, image stack 순으로 정리 | run-owned 삭제 |

`preflight.py`, `image_prep.py`, `cleanup.py`를 실행하기 전에 같은 shell에서 `aws login`과
`aws sts get-caller-identity`를 완료해야 한다. static access key를 argument, 환경 파일, run
산출물에 저장하지 않는다. `image_prep.py`의 ECR token은 임시 `DOCKER_CONFIG` 안에서만 사용된다.

## 검증

```bash
npm run test:phase7
npm run build
```

단위 테스트는 fixture만 사용하며 실제 AWS 요청을 만들지 않는다. 실제 preflight에는 명시적인
handoff, run/session ID, 현재 AMI, certificate ARN, canonical DNS와 fresh price/cost JSON을 모두
전달한다. `--image-state absent`는 image 준비 전, `prepared`는 image push 뒤 runtime 배포 직전에
사용한다.

`runner.py`의 stage command는 shell 문자열이 아니라 JSON `argv` 배열이다. AWS credential
환경변수는 거부한다. 실패한 stage는 resume하거나 덮어쓰지 않고 `cleanup`, `inventory`만
허용한다.

안정화 diagnostic은 별도 CDK stack을 만들지 않는다. `full_stack_scoped_archive.py`는
`LoopAdPerfPhase7IntegrationImageStack`과 `LoopAdPerfPhase7IntegrationStack`만 사용하고,
correctness, replacement, warmup, score와 source DROP을 실행하지 않는다. 이 attempt는 계속
`promotionEligible=false`다. 다만 현재 user-authorized composite policy에서는 pass와 cleanup zero
뒤 Attempt 17의 immutable 성능 증거와 결합한 별도 `phase8-handoff.json`을 만들고 strict/50k를
재실행하지 않는다. 과거 `archive_diagnostic_app.ts`와 targeted tooling은 Attempts 18-19의
재현·감사용으로만 보존하고 새 배포에는 사용하지 않는다.

Scoped diagnostic의 순서는 `fresh prices -> scoped cost/composite policy -> absent preflight ->
seal_full_stack_scoped_attempt.py -> image_prep.py -> prepared preflight ->
full_stack_scoped_archive.py`로 고정한다. Admission seal 전에는 유료 작업을 시작하지 않는다.
현재 composite policy가 활성인 동안 `preflight.py --handoff`, `image_prep.py --handoff`와 기존
strict runner의 deploy/correctness/seed/warmup/score_archive stage는 fail-closed다. 이후 strict
certification을 다시 열려면 별도의 명시적 authorization artifact와 새 budget contract를 먼저
구현하고 검증해야 한다.
Image 준비가 시작되면 paid timestamp와 image-stack deploy count를 즉시 ledger에 기록하고, runtime
deploy 직전에는 deploy count를 1로 봉인한다. Runner는 성공·실패와 무관하게 cleanup을 수행하며,
authoritative service inventory와 exact RunId/SessionId Tagging API가 모두 zero이면 비용을 누적한
hash-linked terminal entry를 자동 append하고 `activeAttempt`를 해제한다. Cleanup zero가 아니면
`activeAttempt=cleanup-required`를 유지하므로 다음 유료 작업은 fail-closed된다. Image 준비 또는
runtime 초기화가 일찍 실패해도 paid marker가 존재하면 같은 cleanup-zero/ledger terminalization을
수행한다. 120분 hard deadline 뒤까지 cleanup이 이어진 경우에는 `$5` cleanup reserve 전체를 추가
upper bound로 charge하고, 실패한 경우에만 다음 scoped retry와 cleanup reserve가 맞는지 다시
계산한다. 성공하면 새 유료 작업을 닫고 composite Phase 8 handoff를 다음 action으로 기록한다.
Composite builder는 ledger 내부 self-hash만 신뢰하지 않는다. Attempt 17의 immutable entry
artifact, fresh runtime의 `campaign-ledger-entry.json`, fresh scoped source seal과 policy/commit/tree/
image-source closure를 모두 다시 결속해야 한다.
`resume.md`를 먼저 안전하게 쓴 뒤
central ledger를 마지막 atomic write로 확정하므로 stale resume 상태에서 새 유료 admission이 열리지
않는다.

`balanced-pool-sampled-with-replacement` mode는 전역 고유 request ID를 보장하거나 주장하지
않는다. score 시작을 UTC 분 경계에 맞추고, HTTP 202, Kinesis `IncomingRecords`, consumer
success-log input count, ClickHouse insert 완료 count를 동일한 score window에서 비교한다.
strict 15M archive fixture와 3×5M Parquet 검증은 이 mode의 영향을 받지 않는다.
