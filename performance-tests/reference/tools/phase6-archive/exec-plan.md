# Phase 6 Lite 실행 계획

이 문서는 Phase 6 Lite의 living execution plan이다. 구현과 실행 중 상태, 결정, 증거 위치를
계속 갱신한다. 고정 acceptance와 안전 규칙은
[실행 계약](../../docs/guides/guide_phase6_clickhouse_s3_archive_lifecycle_test.md)을 우선한다.

## 고정 실험 정의

```text
PHASE=6-lite
EXPERIMENT=clickhouse-s3-archive
STATUS=aws-goal-passed
SOURCE=loopad.events FINAL
SOURCE_ROWS=15000000
SOURCE_PARTITION=UTC today - 8 days
ELIGIBILITY=event_date < UTC today - 7 days
PARTS=3
ROWS_PER_PART=5000000
FORMAT=Parquet
COMPRESSION=ZSTD
EXPORT_BANDWIDTH_MIBPS=100
SCHEDULE=01:15 UTC daily
EXPORT_LIMIT_MINUTES=15
VALIDATION_LIMIT_MINUTES=15
CYCLE_LIMIT_MINUTES=30
COST_STOP_USD=12
CLEANUP_RESERVE_USD=3
COST_HARD_CAP_USD=15
PAID_LIMIT_MINUTES=120
CLEANUP_START_MINUTE=100
```

`RUN_ID`, account, region, price snapshot, source date, source fingerprint와 resource name은 모든
로컬 gate를 통과한 뒤 새 AWS run에서 고정한다.

## Goal 분리

| Goal | Milestone | 예상 wall-clock | AWS |
| --- | --- | ---: | --- |
| Goal 1: local implementation | 1~3 + handoff | 6~10시간 | API/mutation 금지 |
| Goal 2: AWS execution | 4~7 | 1.5~3시간 | passed handoff 뒤 허용 |

Goal 1은 `performance-tests/run_<timestamp>_phase6_archive_local/`에 증거와
`local-handoff.json`을 남긴다. Goal 2는 사용자가 지정한 exact local run만 사용한다. verdict,
`awsReady`, 15M 결과, volume cleanup과 implementation hash 중 하나라도 불일치하면 AWS call
전에 `blocked`로 끝낸다.

## 결정 기록

- 2026-07-17: Phase 4 historical `failed`는 유지하되 다음 단계 진행에는 충분한 것으로 결정
- 2026-07-17: Phase 5보다 Phase 6 Lite를 먼저 실행
- 2026-07-17: single Python worker + systemd timer/oneshot + `flock` 채택
- 2026-07-17: 분산 lock, DynamoDB, Step Functions, EventBridge를 제외
- 2026-07-17: S3 partition별 `COMMITTED`를 최종 상태의 source of truth로 채택
- 2026-07-17: 실패 시 partial resume 대신 새 attempt로 partition 전체 재시작
- 2026-07-17: lifecycle transition, restore, multiworker와 live overlap을 후속 단계로 이관
- 2026-07-17: local scale을 1M -> 15M으로 승격하고 15M full-scale은 1회만 실행
- 2026-07-17: local/AWS test 종료 뒤 exact run-owned Docker/EBS volume inventory zero 요구
- 2026-07-17: 로컬 구현 Goal과 AWS 배포 Goal을 분리하고 immutable handoff/hash gate 요구
- 2026-07-17: small E2E, fault/recovery, 1M pilot를 통과했으나 단 한 번의 15M
  attempt는 source active merge gate에서 실패; 재시도 금지를 지키고 `failed`,
  `awsReady=false`로 종료
- 2026-07-17: 실패 후 source 15,000,000 rows와 unique event 15,000,000을 보존하고
  archive object/commit 0, exact Compose project container/volume 0을 확인
- 2026-07-17: 사용자가 이전 단일-attempt 규칙을 override하고 성공할 때까지 whole-attempt
  retry를 요구; partial resume 없이 attempt별 run ID, bucket과 evidence를 분리
- 2026-07-17: seed 직후 active merge가 0이 될 때까지 기다리고 2회 연속 quiet observation을
  요구하는 quiescence gate 추가
- 2026-07-17: exact unique를 8개 disjoint hash bucket으로 순차 계산하고 export를 단일 thread,
  128 MiB external sort로 제한
- 2026-07-17: attempt 21에서 15M full-scale, 모든 equivalence, resource guard와 cleanup 통과;
  `passed`, `awsReady=true`, AWS calls 0
- 2026-07-17: fixed local handoff
  `run_20260717_050834_phase6_archive_local_bootstrap_fix`를 사용한 Goal 2 AWS whole attempt가
  15M correctness, timing, host resource, cost와 service inventory zero를 모두 통과;
  `run_20260717_055837_phase6_clickhouse_s3_archive` verdict `passed`

## Milestone 0: 기준선과 계약

- [x] Phase 4 최신 run의 수치와 historical verdict 분리
- [x] Phase 6 Lite 범위와 제외 범위 확정
- [x] source partition, row count, object 수, schedule, 시간·비용 상한 확정
- [x] 재실행 상태표와 source deletion gate 확정
- [x] 실행 계약, README, living plan 작성

## Goal 1 / Milestone 1: archive worker

- [x] canonical manifest schema와 version 정의
- [x] deterministic seed version, seed와 reference hash 고정
- [x] source eligibility와 5분 간격 stable fingerprint 구현
- [x] 5,000,000-row deterministic chunk 3개의 Parquet/ZSTD export 구현
- [x] S3 object SHA-256, schema와 row count 검증 구현
- [x] exact count/unique/min/max/checksum/two-way difference 구현
- [x] `If-None-Match: *` manifest와 stable `COMMITTED` 구현
- [x] 4-state recovery와 whole-partition restart 구현
- [x] commit 재검증 뒤에만 `DROP PARTITION` 실행
- [x] post-DROP direct S3 query 구현

## Goal 1 / Milestone 2: scheduler와 infrastructure

- [x] systemd oneshot service와 daily 01:15 UTC persistent timer 작성
- [x] `flock` 중복 실행 방지와 timeout/exit semantics 작성
- [x] run-scoped one-shot timer invocation 경로 작성
- [x] run-owned S3 bucket, EC2 instance role과 최소 IAM 작성
- [x] archive worker role에서 `DeleteObject` 제외
- [x] ClickHouse/worker journal, CPU, memory, filesystem 지표 수집
- [x] partial deployment도 정리하는 cleanup inventory 작성

## Goal 1 / Milestone 3: 로컬 검증

- [x] Python unit tests 통과
- [x] 작은 fixture local end-to-end와 post-DROP deterministic reference 비교 통과
- [x] part 누락과 checksum mismatch에서 DROP 차단 확인
- [x] duplicate commit에서 overwrite 차단과 committed archive 재검증 확인
- [x] worker kill 후 새 attempt 재시작 확인
- [x] host/Docker CPU, memory, swap과 filesystem preflight 기록, free disk `>= 30 GiB`
- [x] seed가 ClickHouse `numbers()`/bounded streaming을 사용하고 15M raw materialization 없음
- [x] 1M pilot에서 memory `< 70%`, filesystem `< 80%`, OOM/restart 0
- [x] 15M full-scale에서 sequential 3 x 5M export와 삭제 전후 완전 동등성 통과
- [x] 8 GiB Docker 기준 ClickHouse memory `<= 5 GiB`, external spill과 sequential query 설정 및 peak 66.015624% 확인
- [x] 공용 archive validator는 Phase 6 기본값 5 GiB를 유지하되 상위 단계가 7 GiB server
  ceiling 아래 최소 512 MiB reserve를 남기는 범위에서 운용 headroom을 늘릴 수 있게 함
- [x] `systemd-analyze verify` 통과
- [x] CDK build/test/synth 통과
- [x] IAM wildcard, 공개 접근, secret 노출, source deletion 경로 검토
- [x] deterministic maximum cost `<= $15` 확인
- [x] evidence를 volume 밖에 보존한 뒤 exact Compose project를 `down --volumes`
- [x] `LOCAL_SESSION_ID`/project label의 Docker volume inventory zero 확인

## Goal 1 handoff

- [x] 새 immutable `run_<timestamp>_phase6_archive_local` 디렉터리 완성
- [x] implementation/schema/generator/image/CDK input SHA-256 기록
- [x] small/fault/1M/15M 결과와 resource peak 기록
- [x] `local-handoff.json` schema와 필수 파일 검증
- [x] 모든 local gate와 cleanup이 통과했을 때만 `passed`, `awsReady=true`
- [x] 예상 밖 결과면 AWS 없이 `failed|blocked|inconclusive`로 종료하고 다음 가설 기록

## Goal 2 / Milestone 4: AWS preflight

- [x] 사용자가 지정한 exact `LOCAL_RUN_DIR` 확인
- [x] handoff verdict/`awsReady`, 15M, volume zero와 현재 implementation hash 재검증
- [x] 불일치 시 AWS call과 AWS run 디렉터리 생성 없이 `blocked`하는 gate 확인
- [x] 새 immutable run 디렉터리 생성
- [x] account/region/operator와 run ownership 확인
- [x] live public price snapshot과 비용 ledger 생성
- [x] quota, instance offering, bootstrap, AMI, SSM, S3 조건부 쓰기 확인
- [x] deploy 전 run-owned inventory zero 확인
- [x] `run.json`, `commands.md`, `infra.md`, `failures.md` 초기화

## Goal 2 / Milestone 5: AWS 실행

- [x] 전용 stack deploy
- [x] ClickHouse health와 고정 schema/image 확인
- [x] UTC today - 8 days partition에 정확히 15,000,000 rows seed
- [x] source count/unique와 stable fingerprint 2회 확인
- [x] 실제 systemd timer/service 경로로 archive 시작
- [x] 정확히 3개 Parquet data object와 5,000,000 rows/object 확인
- [x] pre-DROP 완전 동등성 확인
- [x] manifest와 `COMMITTED` 조건부 생성·재검증 확인
- [x] source partition DROP과 0 rows 확인
- [x] post-DROP direct S3 완전 동등성 확인

## Goal 2 / Milestone 6: 복구와 성능

- [x] source 있음 + commit 없음 경로 확인
- [x] source 있음 + commit 있음의 committed-pre-DROP 재검증 확인
- [x] source 없음 + commit 있음의 post-DROP direct query 확인
- [x] source 없음 + commit 없음은 frozen fail-closed 계약과 exact handoff fault gate로 차단 확인
- [x] export `<= 15분`, validation `<= 15분`, cycle `<= 30분`
- [x] host CPU/memory p95 `< 70%`, filesystem `< 80%`
- [x] ClickHouse/systemd restart, OOM, archive failure 0

## Goal 2 / Milestone 7: cleanup과 판정

- [x] deploy 후 100분 전 cleanup 시작
- [x] run-owned S3 attempt/commit object와 모든 version 삭제
- [x] ClickHouse EBS `DeleteOnTermination=true`, destroy policy와 ownership tag 확인
- [x] stack 및 연관 run-owned resource 삭제
- [x] EC2/EBS API에서 run-owned volume과 snapshot inventory zero 확인
- [x] service-by-service inventory zero 확인
- [x] local evidence, manifest/hash, query와 비용 ledger 보존
- [x] `passed|failed|aborted|inconclusive` 중 하나로 최종 판정
- [x] README와 전체 Phase 현황 갱신

## 현재 다음 작업

Goal 2는
`run_20260717_055837_phase6_clickhouse_s3_archive`에서 `passed`와 service inventory zero로
종료했다. Phase 6 Lite 재시도는 필요 없다. 다음 별도 Goal은 Phase 5에서 Phase 2 collector와
Phase 4 native Java consumer를 결합하고 live ingest/archive overlap을 검증하는 작업이다.
