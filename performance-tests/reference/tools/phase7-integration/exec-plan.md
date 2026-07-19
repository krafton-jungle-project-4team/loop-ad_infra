# Phase 7 living execution plan

## 상태

`Phase 8 final integration baseline / composite cleanup-recovered promotion complete`

Phase 7-2 is a resumable stabilization campaign. A terminal attempt is immutable, but after its
authoritative cleanup inventory reaches zero the campaign records the failure, applies the smallest
evidence-backed fix and starts a fresh full-stack scoped AWS diagnostic for the failed path. It reuses
the Attempt 17 integration stack definition instead of creating a diagnostic-only stack and records every
deferred whole-local obligation in `phase7_2-stabilization/issue-register.json`. The active
2026-07-19 override does not start another strict/50k attempt: it inherits Attempt 17 performance
evidence, runs one fresh standard-stack minimal smoke plus 15M retain-source archive, then creates a
composite Phase 8 handoff after cleanup zero. Routine scoped retries continue without another user
confirmation while the active `$60` cost gate passes.

## 의존성

- collector exact commit은 sibling repo object database에 존재하지만 현재 checkout은 `main`
  `1769eec50622080cf6aa17443ff7e08812f4df49`이다. 실행 시 clean detached worktree를 만든다.
- native Java consumer local adapter와 Phase 7 stack은 검증 뒤 별도 구현 커밋으로 freeze한다.
- Phase 6 local/AWS handoff는 통과 상태다.
- Phase 6의 94.1% peak와 5.00/4.90/4.50 GiB 설정은 역사적 입력이다. 현재 Phase 7 운영값은
  container/server/archive-query 8/7/6 GiB다. archive query cap은 6..6.5 GiB와 server 아래의
  safety envelope만 검증하고 exact acceptance 숫자로 고정하지 않는다. correctness,
  equivalence, DROP safety와 성능 기준점은 계속 exact gate다.
- pinned `oha 1.14.0`의 line-body mode는 random sampling with replacement다. 새 진단 mode는
  이 의미를 사전에 고정하고 warmup/score별로 분리된 480-body pool을 120 shard에 4개씩
  배치한다. 전역 고유성을 주장하지 않고 final ACK·Kinesis·KCL·ClickHouse insert count를
  대조한다. archive 15M 고유 행 계약은 그대로 유지한다.

## Phase 7-1 체크리스트

- [x] source/branch/status와 exact collector commit 기록
- [x] Java AWS client endpoint adapter와 fail-closed local mode
- [x] LocalStack 3.8.1 Kinesis/DynamoDB/CloudWatch/Secrets Manager/S3 topology
- [x] HAProxy 1, collector 4, KCL consumer 2, ClickHouse 1, archive worker 1
- [x] unit/build/memory/Compose/no-AWS gate
- [x] exact collector race/vet gate
- [x] correctness 1,002
- [x] closed partition 1M seed
- [x] 200 requested RPS x 120초, unique 24,000 live/archive overlap 최종 증적
- [x] collector/consumer planned replacement
- [x] count/archive/direct-query acceptance
- [x] Docker and LocalStack inventory zero
- [x] immutable local handoff와 implementation/evidence 분리 커밋

## Phase 7-2 체크리스트

- [x] HAProxy Cloud Map SRV `leastconn`, H2C, 1/1000 successful log sampling, error log와 Prometheus
- [x] explicit NLB security group source/target path
- [x] 5 GiB CloudWatch Logs와 attempt별 `$35/$40/$5` deterministic cost gate
- [x] AWS preflight/runner/evaluator/cleanup/image automation unit test
- [x] buildx isolation 수정 뒤 새 Phase 7-1 whole attempt와 handoff
- [x] AWS 변경 직전 fresh `aws login`과 root account/region 확인
- [x] AWS 시작 직전 exact 7-1 handoff 재검증
- [x] fresh quota/ownership/price/cost preflight
- [x] Phase 7 image/runtime CDK synth, unit, build
- [x] fresh AWS context를 사용한 `cdk diff`
- [x] exact image digest/architecture 검증
- [x] schema-guard `Retries=10`, `StartPeriod=80`과 전체 합성 health-check 범위 검증
- [x] 새 image 3개 준비 후 pre-runtime hard stop에 따른 exact cleanup
- [x] `run_20260717_225316_phase7_integration` runtime deploy 1회 시도와 실패 증거 보존
- [x] historical attempt의 authoritative service/Tagging API cleanup inventory zero 재검증
- [x] hash-linked `phase7_2-stabilization/attempt-ledger.json`과 `resume.md` 초기화
- [x] EC2 Launch Template decoded user data 16,384-byte 초과 원인 수정
- [x] 모든 Launch Template user data를 decode하는 synth-time regression gate 통과
- [x] 수정 뒤 fresh Run ID, Session ID, readiness/runtime directory 생성
- [x] 각 새 failure를 campaign issue register에 raw evidence와 deferred local 항목으로 기록
- [x] 변경 범위 focused gate 뒤 Attempt 17 전체 stack의 fresh scoped AWS diagnostic으로 실제 해결 확인
- [x] 전체 test/build/Phase 7-1 반복은 active composite override로 superseded
- [x] run-owned 통합 stack 배포와 실제 state 검증
- [x] Attempt 17 correctness 1,002와 consumer replacement 900 immutable pass 증거 상속
- [x] closed partition 15M seed/quiescence/fingerprint
- [x] 새 warmup/score는 active override로 금지; Attempt 17 완료 score 증거 상속
- [x] Attempt 17 50k RPS x 300초 score `49,987.713711...` actual RPS 증거 상속
- [x] 새 score drain/accounting은 수행하지 않음
- [x] fresh scoped 3 x 5M Parquet pre/committed equivalence와 source retention
- [x] metric/log/CloudTrail/cost evidence
- [x] earlier pre-runtime attempt: runtime absent -> exact image -> image stack cleanup
- [x] earlier pre-runtime attempt: service inventory zero와 evidence/status 분리 커밋
- [x] Attempt 17 + Attempt 23 functional/archive pass + cleanup-recovered zero의 exact hashes를
      composite `phase8-handoff.json`에 고정하고 unpaid Phase 8 승격

## hard stops

- local gate 또는 handoff hash mismatch
- non-local AWS attempt in 7-1
- identity/region/ownership/quota/cost gate failure in 7-2
- smoke mismatch, KCL terminal failure, insert/archive failure
- attempt별 `$35` new-load stop, `$40` hard cap, 160분 cleanup start, 180분 deadline
- active budget epoch `$55` new-paid-work stop, `$60` hard cap과 `$5` cleanup reserve
- source delete safety gate failure 또는 cleanup inventory nonzero
- 선택한 identity mode가 preflight 전에 manifest/evaluator에 고정되지 않았거나, 진단 mode에서
  120-shard 균등 pool·warmup/score 분리·처리 카운트 대조가 불가능함

## 캠페인 기록과 승격

- durable control:
  `performance-tests/phase7_2-stabilization/attempt-ledger.json`,
  `performance-tests/phase7_2-stabilization/issue-register.json`,
  `performance-tests/phase7_2-stabilization/resume.md`
- 다음 attempt gate:
  `active-epoch prior upper bound + next operational upper bound + $5 cleanup reserve <= $60`
- 명시적 비용 reset은 terminal attempt를 재작성하지 않고 기존 epoch를 닫아 보존한 뒤 새 epoch를
  append하며, ledger의 exact next paid boundary부터 `$0`을 적용
- 같은 Run ID에서 source/configuration 수정, 재배포, 두 번째 warmup/score/archive 금지
- failed attempt cleanup zero 뒤 focused fix와 fresh full-stack scoped diagnostic을 자동 계속
- diagnostic 전용 CDK stack이나 축소 resource graph는 새로 만들지 않음
- scoped diagnostic은 `promotionEligible=false`; pass 뒤에도 attempt 자체를 strict로 바꾸지 않음
- monolithic stack이고 batch handoff가 이미 passed이면 별도 diagnostic 배포 대신 처음부터
  strict로 봉인한 한 attempt에서 known failure gate를 먼저 확인하고 통과 시 끝까지 계속
- 현재 override는 Attempt 17 performance/correctness와 Attempt 23 functional/archive pass 및
  최종 cleanup zero를 composite `phase8-handoff.json`에 고정한다. Attempt 23의 중간 cleanup
  bookkeeping 실패와 원 verdict `failed`는 그대로 보존한다.
- Phase 8 paid AWS upper bound는 `$0`; 새 유료 배포, warmup, score, archive를 수행하지 않음
- 최종 기준선: `performance-tests/phase8-final/phase8-manifest.json`
