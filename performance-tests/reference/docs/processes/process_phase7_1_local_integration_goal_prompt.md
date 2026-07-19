# Phase 7-1 로컬 전체 통합 Goal

## 목표

AWS 호출 없이 실제 HAProxy, Go collector, native Java KCL consumer, ClickHouse와 Phase 6
archive worker를 LocalStack에 연결한다. correctness, live/archive overlap, 계획된 교체와 전체
cleanup을 한 번의 새 whole attempt로 검증한다.

## 필수 입력

- [Phase 7 실행 계약](../guides/guide_phase7_end_to_end_integration_test.md)
- collector sibling repo와 exact commit `497315137251af82d0d203ce34702d5543553942`
- Phase 4 native Java consumer source
- Phase 6 passed handoff
  `performance-tests/run_20260717_050834_phase6_archive_local_bootstrap_fix/local-handoff.json`

## 수행 범위

1. 시작 branch/SHA/status를 기록하고 unrelated 변경을 보존한다.
2. `performance-tests/phase7-integration/`에 Compose, endpoint adapter, runner, evaluator와 tests를
   구현한다.
3. Java consumer local endpoint override는 explicit local mode에서만 허용한다.
4. 정적/unit/build/race/memory/Compose gate를 통과한다.
5. 새 `run_<timestamp>_phase7_1_local_integration/`에서 계약의 단일 whole attempt를 실행한다.
6. JSON parse, secret scan, non-AWS audit와 container/volume/network zero를 확인한다.
7. `local-handoff.json`에 모든 source/image/schema hash, verdict와 AWS readiness를 기록한다.
8. implementation과 local evidence를 분리해 논리 커밋한다.

## 금지

- AWS CLI/API, 실제 AWS endpoint, AWS credential/login 사용
- 기존 run directory 덮어쓰기 또는 부분 resume
- Phase 6 archive core의 production 안전 조건 완화
- 전역 Docker prune, 다른 project container/volume 삭제
- 로컬 결과로 50k AWS capacity를 주장

## 종료 조건

모든 acceptance가 통과하고 `awsReady=true`, real AWS attempts 0, owned Docker inventory 0이면
`passed`다. 하나라도 불충족이면 `failed` 또는 `blocked`로 종료하고 AWS로 진행하지 않는다.
