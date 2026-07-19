# Phase 6 Lite Goal 실행 순서

Phase 6 Lite는 하나의 장시간 Goal로 실행하지 않는다. 로컬 구현 결과가 예상과 다를 때 AWS
배포로 자동 진행하지 않도록 두 Goal 사이에 명시적인 handoff gate를 둔다.

| 순서 | Goal | 예상 wall-clock | AWS mutation |
| ---: | --- | ---: | --- |
| 1 | [로컬 구현·검증](process_phase6_clickhouse_s3_archive_local_goal_prompt.md) | 6~10시간 | 금지 |
| 2 | [AWS 배포·검증](process_phase6_clickhouse_s3_archive_aws_goal_prompt.md) | 1.5~3시간 | 조건부 허용 |

Goal 1이 예상 밖 결과, `failed`, `blocked` 또는 `inconclusive`로 끝나면 Goal 2를 시작하지
않는다. 결과와 다음 가설을 검토한 뒤 새 Goal 1을 만든다.

## Handoff 계약

Goal 1은 새 immutable local run 디렉터리와 `local-handoff.json`을 만든다. Goal 2를 시작하려면
다음 조건이 모두 필요하다.

- Goal 1 verdict가 `passed`
- `awsReady=true`
- small fixture, fault injection, 1M, 15M local gate 통과
- 15M pre-DROP, committed-pre-DROP 재검증과 post-DROP 완전 동등성 통과
- local Docker volume inventory zero
- implementation, schema, image, generator와 payload hash가 현재 workspace와 일치
- unresolved failure와 cleanup blocker 없음

Goal 2 프롬프트의 `LOCAL_RUN_DIR`에는 Goal 1이 반환한 정확한 절대 경로를 넣는다. 최신
디렉터리를 이름만으로 추측하거나 다른 run의 증거를 조합하지 않는다.

## 현재 checkpoint와 다음 Goal

Goal 1은 다음 exact run에서 통과했다.

```text
LOCAL_RUN_DIR=/Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra/performance-tests/run_20260717_100126_phase6_archive_local_retry
VERDICT=passed
AWS_READY=true
SUCCESSFUL_15M_ATTEMPT=21
IMPLEMENTATION_CODE_SHA256=f4d455142e67dad5c66d36ade3b3cd9333e57f3bb435efb63463d99783b7c870
```

다음 Goal은 [Goal 2 AWS 배포·검증](process_phase6_clickhouse_s3_archive_aws_goal_prompt.md)이다.
Goal 2는 위 경로를 기본 입력으로 사용하고, handoff의 모든 hash와 local cleanup zero를 AWS
호출 전에 다시 확인한다. 다른 local run을 자동 선택하지 않는다.

15M attempt 실패는 전체 작업의 즉시 종료 조건이 아니다. 실패 attempt를 immutable evidence로
보존하고 run-owned resource를 inventory zero까지 정리한 뒤 fresh preflight를 통과하면 새 run
ID, stack, bucket과 archive prefix로 whole attempt를 다시 실행한다. partial resume와 evidence
덮어쓰기는 금지한다. 비용·시간·resource·cleanup guard는 retry로 우회하지 않는다.

Goal 2에서 구현 hash가 달라졌거나 AWS에서 구현 결함이 발견되면 run-owned AWS resource를
정리하고 `failed` 또는 `blocked`로 끝낸다. Goal 2 안에서 구현을 수정하고 재배포하지 않는다.

## 작성 근거

두 프롬프트는 원하는 결과, source of truth, 경계, 검증과 완료 조건을 분리해 명시한다.

- [Codex best practices](https://learn.chatgpt.com/guides/best-practices.md)
- [Prompting Codex](https://learn.chatgpt.com/docs/prompting.md)
