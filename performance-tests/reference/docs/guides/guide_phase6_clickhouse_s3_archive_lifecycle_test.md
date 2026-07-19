# Phase 6 Lite EC2 ClickHouse→S3 archive 실행 계약

이 문서는 Phase 6 Lite의 실행 가능한 고정 계약이다. 목표는 오래된 ClickHouse `events`
partition 하나를 S3 Parquet으로 보존하고, 원본 삭제 전후의 완전 동등성과 재실행 안전성을
입증하는 것이다.

Phase 4 최신 run의 historical `failed` 판정은 바꾸지 않는다. 다만 전역 ClickHouse에는 실제
누락이 없었고 correctness, task replacement, archive fixture와 cleanup이 확인됐으므로 다음
단계 진행에는 충분한 것으로 결정했다. Phase 5의 live ingest/archive overlap은 이 Phase 6
Lite가 끝난 뒤 검증한다.

## 범위

포함한다.

- 기존 EC2 ClickHouse topology와 `loopad.events` schema 재사용
- 닫힌 `event_date` partition 하나에 정확히 15,000,000 rows 생성
- 같은 EC2의 `systemd` timer가 단일 Python archive worker 실행
- S3 Standard에 Parquet/ZSTD data object 3개로 export
- 원본 삭제 전 완전 동등성, 삭제 후 direct S3 query 동등성
- 실패 후 partition 전체 재시작과 S3 commit 기반 복구
- 실행 비용, 자원 사용량, 증거와 run-owned resource cleanup

포함하지 않는다.

- Standard-IA 또는 Glacier 실제 transition과 restore drill
- `raw_events` archive
- 다중 worker 또는 다중 ClickHouse host
- DynamoDB lock, Step Functions, EventBridge
- ClickHouse watermark table
- live ingest와 archive의 동시 실행

## Goal 경계

Phase 6 Lite는 다음 두 Goal로 분리한다.

| Goal | 범위 | 예상 wall-clock | 종료점 |
| --- | --- | ---: | --- |
| [Goal 1](../processes/process_phase6_clickhouse_s3_archive_local_goal_prompt.md) | 구현, 작은 fixture, fault, 1M/15M local gate | 6~10시간 | local evidence, volume zero, `local-handoff.json` |
| [Goal 2](../processes/process_phase6_clickhouse_s3_archive_aws_goal_prompt.md) | fresh preflight, deploy, AWS archive, cleanup | 1.5~3시간 | AWS verdict와 service inventory zero |

Goal 1에서는 AWS API와 mutation을 금지한다. Goal 1이 `passed`, `awsReady=true`, local Docker
volume inventory zero로 끝나지 않으면 Goal 2를 시작하지 않는다. Goal 2는 handoff의 현재
implementation hash를 다시 확인하고 frozen code만 배포한다. AWS에서 구현 결함이 발견되면
cleanup 후 종료하고 새 Goal 1로 돌아간다. Goal 2 안에서 코드를 수정·재배포하지 않는다.

## 현재 검증된 로컬 기준선

Goal 1은 다음 exact handoff로 통과했다. 이후 Goal은 최신 디렉터리를 추측하지 않고 이 경로와
파일을 명시적으로 입력받는다.

| 항목 | 값 |
| --- | --- |
| local run | `/Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra/performance-tests/run_20260717_100126_phase6_archive_local_retry` |
| handoff | `local-handoff.json` |
| verdict / AWS readiness | `passed` / `awsReady=true` |
| implementation code SHA-256 | `f4d455142e67dad5c66d36ade3b3cd9333e57f3bb435efb63463d99783b7c870` |
| ClickHouse image digest | `sha256:93f557eb9258198d5c52d723287a33a2697cd76900d85cecc0b307cd6293a797` |
| archive schema SHA-256 | `26e5589ccc6dba4ac4703dae61f5f7faae8139e2173c77e40338cc8eaa2b1fee` |
| generator | `phase6-events-v1`, seed `6000017`, reference `a276200420b1b000003133a3865cbfabe2b61271f8e6c0762ee7509be094bf43` |
| successful full-scale attempt | attempt `21`, `3 x 5,000,000` rows |
| full-scale correctness | count/unique `15,000,000`, checksum `15742404871355694341`, 모든 양방향 차집합 `0` |
| full-scale resource peak | Docker memory `66.015624%`, filesystem `75.004353%`, OOM/restart `0/0` |
| local cleanup | exact session/project container `0`, volume `0` |
| AWS calls/lookups | `0/0` |

이 기준선은 로컬 성공을 증명하며 AWS 성공을 뜻하지 않는다. Goal 2는 파일 hash가 하나라도
달라지면 AWS 호출 전에 `blocked`로 끝낸다.

## 고정 topology

```text
systemd timer
  -> systemd oneshot service
  -> flock
  -> Python archive worker
  -> loopad.events FINAL
  -> run-owned S3 bucket, unique attempt prefix
  -> pre-DROP equivalence
  -> immutable manifest + conditional COMMITTED
  -> DROP PARTITION
  -> direct S3 query equivalence
```

`flock`은 같은 호스트의 중복 실행을 막는다. S3의 안정된 partition별 `COMMITTED` key는
재부팅이나 서로 다른 실행 사이의 최종 상태를 결정한다. 단일 호스트 실험이므로 별도 분산
lock은 만들지 않는다.

## KakaoPay 사례 적용 범위

[KakaoPay ssak3 사례](https://tech.kakaopay.com/post/pallas-v2-log-platform/#step-5-amazon-s3-%EC%95%84%EC%B9%B4%EC%9D%B4%EB%B9%99-ssak3)에서
Python worker, ClickHouse query 기반 S3 export, 5,000,000 rows/file, Parquet/ZSTD와 bandwidth
제한을 채택한다. 운영 사례의 watermark와 offset resume는 대규모 multiworker에 필요한
장치다. 이 실험은 단일 closed partition과 단일 host만 다루므로 S3 `COMMITTED`와 전체
partition 재시작으로 축소한다. bandwidth도 현재 EC2에 맞춰 500 MiB/s가 아니라 100 MiB/s로
제한한다.

## 고정 실행 사양

| 항목 | 값 |
| --- | --- |
| source table | `loopad.events FINAL` |
| source partition | 실행 시점 UTC today - 8 days |
| eligibility | `event_date < UTC today - 7 days` |
| source rows | 정확히 15,000,000 |
| seed 후 quiescence | 2초 간격, merge/mutation 0을 2회 연속 확인, 최대 900초 |
| 안정성 확인 | quiescence 뒤 동일 fingerprint를 5분 간격으로 2회 측정 |
| Parquet data object 수 | 정확히 3개 |
| rows/object | 정확히 5,000,000 |
| 정렬 | `(event_time, event_id)`의 결정적 순서 |
| format | Parquet + ZSTD |
| storage class | S3 Standard |
| export bandwidth | 최대 100 MiB/s |
| exact unique | 8개 disjoint `cityHash64(event_id)` bucket을 순차 `uniqExact` 후 합산 |
| logical checksum | 8개 disjoint `event_id` hash bucket을 순차 계산 후 UInt64 합산 |
| query 실행 | `max_threads=1`, metrics block `8192`, 큰 순차 query 사이 jemalloc purge |
| export memory | `max_threads=1`, block `8192`, external sort threshold `128 MiB` |
| ClickHouse memory | container `5 GiB`, server `4.90 GiB`, query `4.50 GiB` |
| production schedule | 매일 01:15 UTC, `Persistent=true` |
| export 제한 | 15분 이하 |
| validation 제한 | 15분 이하 |
| 전체 archive cycle | 30분 이하 |
| host CPU/memory | 각 p95 70% 미만 |
| filesystem 사용률 | 80% 미만 |
| 안정성 | restart, OOM, archive failure 모두 0 |
| 새 작업 금지선 | 누적 최대 비용 `$12` |
| cleanup reserve | `$3` |
| hard cap | `$15` |
| 유료 시간 | deploy부터 최대 120분, 100분에 cleanup 시작 |

실험에서는 운영 스케줄을 설치한 뒤 같은 timer/service 경로를 run-scoped one-shot schedule로
호출한다. 01:15 UTC까지 기다리는 방식으로 시간을 낭비하지 않는다.

## S3 layout과 소유권

```text
s3://<run-owned-bucket>/
├── attempts/v1/table=events/event_date=<YYYY-MM-DD>/archive_id=<UUID>/
│   ├── part-00000.parquet
│   ├── part-00001.parquet
│   ├── part-00002.parquet
│   └── manifest.json
└── commits/v1/table=events/event_date=<YYYY-MM-DD>/COMMITTED
```

고유 `archive_id` 아래 object는 commit 전에는 staging attempt이고, `COMMITTED`가 그
attempt를 가리킨 뒤에는 committed archive다. data object와 manifest는 같은 key에 다시 쓰지
않는다. 실패한 attempt를 이어 쓰거나 성공 prefix로 승격하지 않고, 새 `archive_id`로 partition
전체를 다시 export한다.

archive EC2 role은 run-owned bucket의 해당 prefix에 필요한 최소 `ListBucket`, `GetObject`,
`PutObject`만 가진다. archive worker에는 `DeleteObject`를 주지 않는다. bucket 삭제와 전체
cleanup은 실험 operator가 수행한다. credential, secret, presigned URL은 SQL, service 환경변수,
로그 또는 보고서에 기록하지 않는다.

## Manifest와 commit

`manifest.json`은 적어도 다음 필드를 가진다.

- contract version, run ID, archive ID, table, UTC partition과 cutoff
- schema와 schema hash
- source fingerprint 2회의 값과 측정 시각
- deterministic seed version, seed와 reference hash
- source count, `uniqExact(event_id)`, min/max event time, logical checksum
- part별 key, rows, bytes, S3 checksum SHA-256
- 전체 archive count, unique, min/max, logical checksum
- export·validation 시작/종료 시각과 duration
- 실행 image/code hash, ClickHouse image digest, AWS account/region

manifest는 `If-None-Match: *` 조건으로 한 번만 만든다. pre-DROP 검증이 모두 통과한 뒤 안정된
partition별 `COMMITTED` key를 같은 조건으로 생성한다. `COMMITTED`에는 manifest key와
SHA-256, archive ID를 기록한다. 이미 다른 commit이 있으면 덮어쓰지 않고 그 commit을
재검증한다.

## 실행 순서

1. account, region, run ownership, 가격, 비용, quota와 cleanup 가능성을 preflight한다.
2. `flock`을 획득한다. 실패하면 중복 실행으로 기록하고 source를 변경하지 않는다.
3. source partition과 eligibility를 확인한다.
4. `events FINAL`의 fingerprint를 5분 간격으로 두 번 계산한다. 값이 다르거나 mutation/merge가
   진행 중이면 중단한다.
5. 기존 `COMMITTED`와 source 존재 여부를 확인해 복구 상태를 결정한다.
6. 새 attempt라면 결정적 정렬을 5,000,000-row 범위로 나눠 Parquet data object 3개를
   export한다.
7. 각 object의 schema, row count와 SHA-256을 확인하고 manifest를 조건부 생성한다.
8. source와 S3 archive의 pre-DROP 완전 동등성을 확인한다.
9. 안정된 `COMMITTED` key를 조건부 생성한다.
10. commit을 다시 읽어 manifest hash와 완전 동등성을 확인한 뒤에만 source partition을
    `DROP`한다.
11. source partition이 0인지 확인하고, ClickHouse `s3()` direct query로 post-DROP 완전
    동등성을 다시 확인한다.
12. metric, log, manifest, command, 비용과 cleanup inventory를 run 디렉터리에 보존한다.

## 완전 동등성 gate

pre-DROP에서는 source와 archive, post-DROP에서는 immutable seed 계약으로 다시 생성한
deterministic reference와 direct S3 query 결과를 비교한다. 다음 항목이 모두 같아야 한다.

- schema와 column 순서/type
- `count()` = 15,000,000
- `uniqExact(event_id)` = 15,000,000
- `min/max(event_time)`
- 고정 column serialization에 대한 logical checksum
- 두 비교 대상 사이의 exact two-way difference = 0
- manifest의 part별 row count 합계와 S3 object SHA-256

단일 aggregate나 표본만으로 통과시키지 않는다. source 삭제는 pre-DROP gate 전체 통과와
commit 재검증 뒤에만 허용한다.

## 재실행 상태표

| `COMMITTED` | source partition | 행동 |
| --- | --- | --- |
| 없음 | 있음 | 새 `archive_id`로 partition 전체 export |
| 있음 | 있음 | committed archive 완전 재검증 후 `DROP` |
| 있음 | 없음 | post-DROP direct S3 검증 후 성공 종료 |
| 없음 | 없음 | 복구 근거가 없는 critical failure, 자동 변경 금지 |

부분 object, 깨진 checksum, 불완전 manifest는 실패 증거로 남긴다. resume, partial merge 또는
source 자동 삭제를 하지 않는다.

## 구현과 로컬 gate

구현 source of truth는
Phase 6 Lite 실행 계획 (external snapshot reference: `../performance-tests/phase6-archive/exec-plan.md`)과
[Goal 1 로컬 구현·검증](../processes/process_phase6_clickhouse_s3_archive_local_goal_prompt.md)이다. AWS 전에
다음을 통과해야 한다.

1. Python unit tests: eligibility, state table, manifest canonicalization, conditional commit,
   failure handling
2. 작은 fixture의 local ClickHouse/S3-compatible integration: 3-part export, pre-DROP gate,
   DROP, post-DROP query
3. fault injection: part 누락, checksum 불일치, duplicate commit, worker kill과 재실행
4. guarded scale test: 1,000,000 rows pilot -> 정확히 15,000,000 rows whole-attempt retry
5. `systemd-analyze verify`와 timer/service 경로 확인
6. CDK build, test, synth와 IAM wildcard/공개 접근/삭제 권한 검토
7. 비용 모델의 deterministic maximum이 `$15` 이하인지 확인

scale test 전에 host와 Docker의 CPU, memory, swap, filesystem 여유를 기록하고 free disk가
30 GiB 미만이면 시작하지 않는다. seed는 ClickHouse `numbers()` 또는 bounded streaming으로
생성한다. Python list/DataFrame이나 raw payload file로 전체 15,000,000 rows를 materialize하지
않는다.

각 scale은 순차 실행한다. 1M pilot이나 15M attempt에서 Docker memory peak가 limit의 70%
이상이거나, filesystem이 80% 이상이거나, OOM/restart가 발생하면 같은 implementation으로
추가 15M attempt와 AWS를 실행하지 않는다. 원인을 수정했다면 unit/static, small, fault와 1M
gate를 다시 통과한 뒤 새 whole attempt를 실행한다. Docker memory limit가 8 GiB인 현재
기준에서는 ClickHouse container를
5 GiB, server를 4.90 GiB, query를 4.50 GiB로 제한한다. exact unique와 checksum은 8개
disjoint hash bucket으로 순차 계산하고, export는 단일 thread와 128 MiB external sort를
사용한다. exact difference도 5M 이하 chunk를 순차 처리하며 part 3개를 병렬 export하지 않는다.

15M local gate는 정확히 3 x 5M Parquet data object, pre-DROP equivalence, commit 재검증,
DROP, deterministic reference 기반 post-DROP equivalence를 한 whole attempt에서 검증한다.
실패하면 그 attempt를 immutable evidence로 보존하고 partial resume 없이 새 run ID, bucket,
archive ID와 evidence 경로로 partition 전체를 다시 실행한다. 최신 사용자 override에 따라
기능 실패는 성공할 때까지 whole-attempt retry한다. 다만 resource guard, 비용·시간 hard stop,
cleanup blocker는 retry로 우회하지 않으며 먼저 원인을 해결하고 preflight를 다시 통과해야 한다.
검증된 기준선은 attempt 21의 성공 결과다. local 결과는 AWS IAM, systemd와 성능 결과를
대체하지 않는다.

## 로컬·AWS volume cleanup

모든 local test volume은 고유 `LOCAL_SESSION_ID`와 Compose project name/label을 가진다.
ClickHouse data, S3-compatible object, temporary spill volume은 같은 local session 동안만
유지한다. 성공, 실패 또는 중단으로 session이 끝나면 다음 순서를 따른다.

1. manifest, checksum, query result, resource peak와 failure log를 volume 밖의 local evidence
   디렉터리에 저장한다.
2. exact Compose project ownership을 확인한다.
3. 해당 project의 container와 volume만 `docker compose down --volumes`로 제거한다.
4. `docker volume inspect/ls`로 같은 project/session label의 volume이 0개인지 확인한다.

전역 `docker volume prune`, 이름 일부만으로 선택한 삭제, 다른 Phase/dev volume 삭제는
금지한다. cleanup이 실패하면 남은 volume ID, label, byte estimate와 원인을 기록하고 다음
full-scale/AWS run을 시작하지 않는다.

AWS ClickHouse gp3 volume은 run/session ownership tag, `DeleteOnTermination=true`와
destroy removal policy를 가진다. snapshot은 만들지 않는다. stack cleanup 뒤 EC2/EBS API로
run-owned volume과 snapshot이 모두 0개인지 bounded polling으로 확인한다. 삭제 중인 volume은
0으로 간주하지 않는다. 남아 있으면 비용을 계속 집계하고 다음 run을 금지한다.

로컬 gate 실패 시 AWS resource를 만들지 않는다.

## AWS 실행과 증거

[Goal 2 AWS 배포·검증](../processes/process_phase6_clickhouse_s3_archive_aws_goal_prompt.md)은 Goal 1의 exact
`LOCAL_RUN_DIR`과 `local-handoff.json`을 입력으로 받는다. handoff와 현재 implementation hash가
일치한 뒤에만 새 immutable AWS run 디렉터리를 만들고 preflight 결과를 기록한다. 기존 Phase 4
또는 local run ID와 판정을 재사용하지 않는다. deploy, seed, 실제 timer invocation, archive,
DROP, post-DROP query, cleanup을 한 AWS run에서 수행한다.

Goal 2의 기본 입력은 위의 검증된 local run 절대 경로다. AWS full-scale 실패도 partial resume나
같은 attempt 재사용을 하지 않는다. 실패 증거와 cleanup inventory zero를 먼저 확정하고,
identity/ownership/quota/cost/resource preflight가 다시 통과할 때만 새 AWS run ID, stack,
bucket과 archive prefix로 whole attempt를 재시도한다. 구현 변경이 필요하면 Goal 2에서 patch하지
않고 cleanup 후 새 Goal 1을 통과시킨다.

필수 증거는 다음과 같다.

- `run.json`, `commands.md`, `infra.md`, `failures.md`, `report.md`
- source fingerprint 2회와 mutation/merge 상태
- systemd timer/service status, journal과 `flock` 결과
- deterministic seed/reference hash, manifest, `COMMITTED`, S3 head/list/checksum 결과
- pre-DROP와 post-DROP query 및 exact difference 결과
- export/validation/cycle duration
- CPU, memory, filesystem, ClickHouse restart/OOM/error 지표
- actual/estimated cost ledger
- local session 종료 뒤 run-owned Docker volume inventory zero
- stack 삭제 뒤 run-owned EBS volume/snapshot과 service-by-service inventory zero

## 중단과 최종 판정

다음 중 하나면 현재 attempt의 새 유료 작업이나 source 변경을 중단하고 cleanup한다.

- account, region, ownership, IAM 또는 quota 불일치
- deterministic maximum > `$15` 또는 누적 최대 비용 >= `$12`
- source fingerprint 불안정, mutation/merge 진행 중, eligibility 불충족
- part/schema/count/checksum/two-way difference 불일치
- manifest나 `COMMITTED`의 조건부 생성·재검증 실패
- export > 15분, validation > 15분, cycle > 30분
- CPU 또는 memory p95 >= 70%, filesystem >= 80%, restart/OOM 발생
- deploy 후 100분 도달 또는 필수 증거 수집 실패

모든 기능·성능·비용·cleanup 기준을 만족할 때만 `passed`다. 구현 또는 시스템 오류는
`failed`, 외부 gate 때문에 배포 전 중단하면 `aborted`, 필수 측정이 유실되면
`inconclusive`다.

실패한 attempt 하나를 Phase 6 전체의 최종 실패로 간주하지 않는다. cleanup과 fresh preflight가
통과하고 비용·시간 hard stop 안이면 새 whole attempt를 실행한다. 같은 원인이 재현되면 먼저
원인을 확정하며, 구현 변경이 필요하거나 guard를 넘으면 `blocked`로 기록하고 새 Goal 1 또는
새로 승인된 Goal 2에서 계속한다. 과거 attempt의 verdict와 evidence는 덮어쓰지 않는다.

## 참고

- Phase 6 Lite 실행 계획 (external snapshot reference: `../performance-tests/phase6-archive/exec-plan.md`)
- [Phase 6 Lite Goal 실행 순서](../processes/process_phase6_clickhouse_s3_archive_goal_prompt.md)
- [Goal 1 로컬 구현·검증](../processes/process_phase6_clickhouse_s3_archive_local_goal_prompt.md)
- [Goal 2 AWS 배포·검증](../processes/process_phase6_clickhouse_s3_archive_aws_goal_prompt.md)
- [AWS 이벤트 파이프라인 단계별 가이드](guide_aws_event_pipeline_performance_test.md)
- [KakaoPay Pallas v2: S3 아카이빙](https://tech.kakaopay.com/post/pallas-v2-log-platform/#step-5-amazon-s3-%EC%95%84%EC%B9%B4%EC%9D%B4%EB%B9%99-ssak3)
- [Amazon S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
