# Phase 7 안정화 및 Phase 8 승격 Goal 실행 순서

이 문서는 Phase 7을 컨텍스트 손실 없이 안정화하고 성공한 결과를 Phase 8로 승격하기 위한
process 문서다. 실제 테스트 계약은
[Phase 7 실행 계약](../guides/guide_phase7_end_to_end_integration_test.md)이 source of truth다.

## 순서

1. [Phase 7-1 로컬 통합](process_phase7_1_local_integration_goal_prompt.md)을 실행한다.
2. `local-handoff.json`이 `passed`, `awsReady=true`이고 cleanup inventory가 0인지 확인한다.
3. handoff의 exact path를 Phase 7-2 objective에 넣는다.
4. [Phase 7-2 AWS 통합 안정화](process_phase7_2_aws_integration_goal_prompt.md)를 새 Goal로
   실행한다.
5. 실패하면 Phase 7-2가 증거와 비용을 ledger 및 campaign issue register에 남기고 cleanup
   inventory zero를 확인한다.
6. Attempt 17과 동일한 `LoopAdPerfPhase7IntegrationStack` definition을 fresh identity로 한 번
   배포하고 실패 지점과 필수 선행 stage만 실행해 focused 수정이 실제 AWS에서 해결됐는지
   확인한다. 별도 diagnostic 전용 CDK stack은 만들거나 배포하지 않는다. 이 scoped diagnostic은
   immutable evidence와 cleanup zero 계약을 지키지만 strict 승격에는 사용할 수 없다.
   query memory와 같은 비성능 운영 cap은 충분한 headroom과 safety envelope로 검증하고 exact
   point acceptance로 만들지 않는다. 성능 기준점, correctness, equivalence, DROP safety, ownership,
   cleanup과 budget만 strict gate로 유지한다.
7. AWS에서 확인된 여러 수정을 한 batch로 묶은 뒤에만 전체 Phase 7-1을 한 번 실행해 fresh
   handoff를 만든다. 각 focused 수정마다 전체 로컬 chain을 반복하지 않는다.
8. production CDK가 monolithic이고 batch handoff가 이미 통과했다면 별도 diagnostic 배포를
   추가하지 않는다. 처음부터 strict로 선언한 fresh 전체 attempt에서 기존 실패 gate를 정상
   순서상 가장 이른 위치에 확인하고, 통과하면 재배포 없이 같은 attempt를 끝까지 진행한다.
9. 현재 composite override에서는 fresh scoped 최소 smoke/15M retain-source archive와 cleanup zero가
   통과하면 Attempt 17 성능 증거와 결합한 `phase8-handoff.json`을 만들고 새 strict/50k 재실행 없이
   [Phase 8 최종 통합 승격](process_phase8_final_integration_goal_prompt.md)을 이어서 실행한다.

초기 Phase 7-1과 Phase 7-2는 합치지 않는다. 단, Phase 7-2가 시작된 뒤의 증거 기반 수정과
scoped full-stack AWS diagnostic, issue register와 batched fresh local handoff 생성은 Phase 7-2 안정화
캠페인의 책임이다. 실패할 때마다 Goal을 종료하거나 새 사용자 확인을 기다리지 않는다. 모든
AWS attempt는 서로 다른 Run ID와 evidence directory를 사용한다. 현재 active budget epoch는
이전 비용을 admission에서 제외하고 `$60` hard cap, `$55` new-paid-work stop과 `$5` cleanup
reserve를 사용한다.

## 권장 Goal objective

Phase 7-1:

```text
Read and execute /absolute/path/to/docs/process_phase7_1_local_integration_goal_prompt.md until every requirement is completed and verified.
```

Phase 7-2:

```text
Read and execute /absolute/path/to/docs/process_phase7_2_aws_integration_goal_prompt.md using LOCAL_HANDOFF=/absolute/path/to/local-handoff.json until every requirement is completed and verified.
```

Phase 8:

```text
Read and execute /absolute/path/to/docs/process_phase8_final_integration_goal_prompt.md using PHASE8_HANDOFF=/absolute/path/to/phase8-handoff.json until every requirement is completed and verified.
```
