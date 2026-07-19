# Phase 6 Lite: EC2 ClickHouse→S3 archive

이 디렉터리는 닫힌 `loopad.events` partition을 S3 Parquet으로 보존하고 원본 삭제 전후의
완전 동등성을 검증하는 도구와 실행 계약을 보관한다.

현재 상태는 `aws-goal-passed`이다. 이전 AWS run에서 확인된 부트스트랩 디렉터리 탐색 권한,
컨테이너 IMDS hop limit, ClickHouse 메모리 계층과 readiness fail-open 결함을 수정하고 새
로컬 handoff를 통과한 뒤, frozen implementation으로 AWS 15M 단일 whole attempt와 전체
cleanup까지 통과했다.

- passed local run: `run_20260717_050834_phase6_archive_local_bootstrap_fix` (external snapshot reference: `../run_20260717_050834_phase6_archive_local_bootstrap_fix/`)
- local report: `report.md` (external snapshot reference: `../run_20260717_050834_phase6_archive_local_bootstrap_fix/report.md`)
- machine-readable handoff: `local-handoff.json` (external snapshot reference: `../run_20260717_050834_phase6_archive_local_bootstrap_fix/local-handoff.json`)
- passed AWS run: `run_20260717_055837_phase6_clickhouse_s3_archive` (external snapshot reference: `../run_20260717_055837_phase6_clickhouse_s3_archive/`)
- AWS report: `report.md` (external snapshot reference: `../run_20260717_055837_phase6_clickhouse_s3_archive/report.md`)
- preserved failed AWS run: `run_20260717_041219_phase6_clickhouse_s3_archive` (external snapshot reference: `../run_20260717_041219_phase6_clickhouse_s3_archive/`)
- preserved previous local handoff: `run_20260717_100126_phase6_archive_local_retry` (external snapshot reference: `../run_20260717_100126_phase6_archive_local_retry/`)

## Source of truth

- [실행 계약](../../docs/guides/guide_phase6_clickhouse_s3_archive_lifecycle_test.md)
- [living execution plan](exec-plan.md)
- [Goal 실행 순서](../../docs/processes/process_phase6_clickhouse_s3_archive_goal_prompt.md)
- [Goal 1 로컬 구현·검증](../../docs/processes/process_phase6_clickhouse_s3_archive_local_goal_prompt.md)
- [Goal 2 AWS 배포·검증](../../docs/processes/process_phase6_clickhouse_s3_archive_aws_goal_prompt.md)
- [전체 Phase 순서](../../docs/guides/guide_aws_event_pipeline_performance_test.md)

Goal 2는 Goal 1의 exact `LOCAL_RUN_DIR/local-handoff.json`이 `passed`, `awsReady=true`이고 현재
implementation hash와 일치할 때만 실행한다. 로컬 결과가 예상과 다르면 AWS로 진행하지 않는다.

## 고정 범위

```text
systemd timer -> flock -> Python worker
  -> events FINAL의 15,000,000-row closed partition
  -> S3 Standard, Parquet/ZSTD data object 3개
  -> immutable manifest + conditional COMMITTED
  -> pre-DROP equivalence -> DROP -> post-DROP direct S3 equivalence
```

DynamoDB lock, Step Functions, EventBridge, ClickHouse watermark, lifecycle transition, restore,
multiworker와 live ingest overlap은 이 단계에 포함하지 않는다.

## 구현 파일

```text
archive.py
seed_partition.py
cost_model.py
preflight.py
cleanup_inventory.py
systemd/
tests/
```

CDK는 repo의 `src/perf-phase6-archive-stack.ts`, 테스트는
`test/perf-phase6-archive.test.ts`에 둔다. 로컬 실행 결과는
`performance-tests/run_20260717_050834_phase6_archive_local_bootstrap_fix/`에 기록했다.

## Goal 1 결과

- Python unit 28개, CDK Jest 5개, TypeScript build, no-lookup synth, Compose config,
  `systemd-analyze verify`, 실제 Linux 서비스 사용자 부트스트랩, 실제 `flock` overlap exit 75: 통과
- small E2E, 4가지 fault/recovery, 1M pilot: 통과
- 1M: 1,000,000 rows와 unique ID, archive 22.876521초, CPU p95 23.73%, memory p95 31.714284%
- 15M 단일 attempt: 3 x 5,000,000 rows, exact unique 15,000,000, 모든 양방향 차집합 0
- pre-DROP와 post-DROP 완전 동등성 통과; source rows after DROP 0
- 15M: CPU p95 28.774%, memory p95 54.702834%, memory peak 63.956806%, filesystem peak
  74.083077%, OOM/restart 0
- cleanup: exact Compose project container 0, volume 0
- final verdict: `passed`, `awsReady=true`; AWS calls 0

이번 로컬 검증은 small, fault, 1M, 15M을 각각 한 번 실행했다. 각 gate는 별도 run ID와
LocalStack bucket을 사용했고 partial resume은 사용하지 않았다.

## Goal 2 결과

- account/region/operator: `742711170910` / `ap-northeast-2` /
  `arn:aws:iam::742711170910:root`
- 정확히 15,000,000 rows와 unique event 15,000,000, 두 fingerprint 간격 332.639초
- S3 Standard Parquet/ZSTD 3 x 5,000,000 rows, 모든 양방향 차집합 0
- pre-DROP, committed-pre-DROP, post-DROP 완전 동등성, source rows after DROP 0
- export 45.504484초, 보수적 validation upper bound 760.157841초, cycle 805.662325초
- host CPU/memory p95 24.36438%/6.903885%, filesystem peak 1.606676%, OOM/restart 0/0
- modeled accrued cost `$0.481389`, deterministic maximum `$6.089506 <= $15`
- cleanup: run-owned billable/service inventory 0, shared development resources 변경 없음
- final verdict: `passed`

ClickHouse 5 GiB container memory는 p95 73.3385%, peak 94.1%였다. AWS acceptance는 host
memory p95와 OOM/restart 0을 기준으로 하므로 통과하지만, production 승격 시 headroom
관찰값으로 유지한다.

공용 archive 설정의 기본 query memory는 이 Phase 6 결과와 동일한 5 GiB다. 다만 이 값은
상위 단계까지 강제하는 exact acceptance가 아니다. 현재 validator는 7 GiB server ceiling보다
최소 512 MiB 낮은 6.5 GiB까지 운용 headroom을 허용하고, 각 단계가 자기 server/container
safety envelope 안에서 값을 선택한다.

## 변경 불가 안전 규칙

- pre-DROP 완전 동등성과 `COMMITTED` 재검증 전 source partition 삭제 금지
- 실패 attempt 재사용 또는 부분 resume 금지
- `COMMITTED` 없는 상태에서 source가 없으면 자동 복구 금지
- archive worker에 S3 `DeleteObject` 권한 부여 금지
- secret, credential, presigned URL을 SQL, 환경변수, 로그 또는 보고서에 기록 금지
- local full-scale retry는 1M pilot 통과 뒤 새 run ID/bucket으로 whole attempt만 실행
- CPU/memory p95 `< 70%`, filesystem peak `< 80%`, OOM/restart 0 중 하나라도 위반하면 중단
- evidence 보존 뒤 현재 `LOCAL_SESSION_ID`의 Docker volume을 모두 제거하고 inventory zero 증명
- AWS teardown 뒤 run-owned EBS volume/snapshot inventory zero 증명
- 전역 `docker volume prune` 또는 다른 Phase/dev volume 삭제 금지
- run-owned resource와 object를 cleanup하고 service inventory zero를 증명
