# Phase 7 전체 통합 테스트

이 디렉터리는 Phase 7-1 로컬 통합과 Phase 7-2 AWS 통합의 재사용 구현과 계약을 보관한다.
Phase 7-1은
`run_20260717_135302_phase7_1_local_integration` (external snapshot reference: `../run_20260717_135302_phase7_1_local_integration/report.md`)
whole attempt에서 `passed`, `awsReady=true`다. 후속
`run_20260717_140022_phase7_2_deployment_readiness` (external snapshot reference: `../run_20260717_140022_phase7_2_deployment_readiness/report.md`)은
exact image 준비와 runtime synth/diff까지 통과했지만, pinned oha의 body line 무작위 복원추출이
15M 전역 고유 event ID 계약과 양립하지 않아 runtime 배포 전에 `failed`로 중단했다. exact
image와 image stack은 모두 삭제했고 authoritative inventory는 0이다.

더 최근의
`run_20260717_225316_phase7_2_aws_integration` (external snapshot reference: `../run_20260717_225316_phase7_2_aws_integration/run.json`)은
runtime deploy에서 실패했다. 보존된 `cleanup-inventory.json` snapshot은 NAT gateway 1개와
Tagging API residual을 기록해 `allZero=false`이므로, 다음 실행은 이 attempt의 live cleanup
inventory zero를 먼저 재검증해야 한다. snapshot만으로 현재 AWS 잔존 여부를 추정하지 않는다.

## Source of truth

- [전체 실행 계약](../../docs/guides/guide_phase7_end_to_end_integration_test.md)
- [Goal 실행 순서](../../docs/processes/process_phase7_end_to_end_integration_goal_prompt.md)
- [Phase 7-1 Goal](../../docs/processes/process_phase7_1_local_integration_goal_prompt.md)
- [Phase 7-2 Goal](../../docs/processes/process_phase7_2_aws_integration_goal_prompt.md)
- [Phase 7-2 배포 준비 Goal](../../docs/processes/process_phase7_2_deployment_readiness_goal_prompt.md)
- [Phase 8 최종 통합 승격 Goal](../../docs/processes/process_phase8_final_integration_goal_prompt.md)
- [living execution plan](exec-plan.md)

## 구현

```text
docker-compose.yml
local_runner.py
cleanup_inventory.py
finalize_evidence.py
archive/
aws/
localstack-init.sh
run-local.sh
tests/
```

`docker-compose.yml`은 LocalStack 3.8.1, ClickHouse, HAProxy, collector 4개, Java KCL consumer
2개와 archive worker를 isolated network에 연결한다. `local_runner.py`는 correctness, 계획된
collector/consumer 교체, 24,000건 live/archive overlap, count, direct S3 query와 non-AWS SDK
audit를 수행한다. 종료 시 `cleanup_inventory.py`가 owned container, volume, network가 0인지
확인하고 `finalize_evidence.py`가 Phase 7-2의 입력인 immutable `local-handoff.json`을 만든다.

```bash
npm run test:phase7
npm run phase7:local
```

로컬 명령은 200 RPS를 요청하지만 LocalStack의 ACK 완료 기준 처리율은 환경 정보다. AWS 50k
성능 합격은 Phase 7-2에서만 판정한다.

현재 local evidence는 valid 1,000건, invalid 1건, late 1건, planned replacement 뒤 200건,
live 24,000건과 closed partition 1,000,000건을 모두 account했다. archive/DROP 후 direct query,
실제 AWS SDK 요청 0과 owned Docker inventory 0도 통과했다. ACK 완료 기준 실제 처리율
145.071993 RPS는 로컬 환경 정보일 뿐 50k capacity 근거가 아니다. handoff는 ECS health-check
수정 커밋 `add4d26f`와 implementation tree
`3dc83067e110dcd7cf441f5edc68d866031ae92e1163e6cdf6e00fc437d27344`를 고정한다.

기존 Phase 1과 Phase 4 CDK stack은 별도 VPC/Kinesis를 만들므로 그대로 조합하지 않는다.
Phase 7은 공용 stream, ClickHouse와 archive bucket을 소유하는 전용 통합 stack을 사용한다.

AWS용 `LoopAdPerfPhase7IntegrationImageStack`과 `LoopAdPerfPhase7IntegrationStack`은 하나의
VPC, Kinesis stream, collector/HAProxy/consumer/ClickHouse ECS capacity, load generator 8대,
archive/failure bucket과 one-shot archive task를 소유한다. 실제 digest, AMI, ACM certificate와
canonical DNS context가 없으면 synth부터 거부한다.

AWS HAProxy는 collector NLB 하나를 backend로 사용하지 않는다. Cloud Map SRV로 collector 6개를
직접 탐색하고 `leastconn`, H2C와 `http-reuse always`를 사용한다. 성공 202 request log는
1/1000만 CloudWatch에 보내고 오류는 전부 기록한다. `/metrics`의 backend, queue, status class와
config SHA를 증적으로 수집한다. 모든 ECS awslogs driver는 25 MiB non-blocking buffer를 사용한다.

[`aws/`](aws/)의 도구가 fresh price/cost preflight, immutable runner, final evaluator와
exact-ownership cleanup을 담당한다. `runtime_stages.py`는 배포 검증, correctness 1,002,
consumer replacement 900, 15M seed, warmup, score/archive, drain/accounting을 고정한다.
`diagnostic_payload_pool.mjs`와 `run_diagnostic_oha.mjs`는 warmup/score별 480-body fixture를
120 shard에 균등 배치하고 8 host×2 process에서 한 번 실행한다. pinned `oha 1.14.0`의 `-Z`가
body line을 무작위 복원추출한다는 사실은 manifest와 evaluator에 명시한다. 이 mode는 전역
고유 ID를 주장하지 않고 final ACK·Kinesis·KCL·ClickHouse insert 카운트로 처리량을 판정한다.
archive의 exact 15M 계약은 유지한다.

Phase 7-2는 여러 fresh attempt를 허용하는 하나의 안정화 캠페인이다. 각 attempt 안에서는 source,
configuration과 배포를 바꾸지 않는다. 실패하면 증거와 비용을 보존하고 authoritative cleanup
inventory zero를 확인한 뒤, 같은 캠페인에서 focused 수정과 full-stack scoped AWS diagnostic을 이어간다.
다음 실행 위치와 명령은 `performance-tests/phase7_2-stabilization/attempt-ledger.json`,
`issue-register.json`과 `resume.md`에 기록한다. 현재 2026-07-19 composite override에서는 Attempt
17의 immutable correctness/replacement/50k evidence와 Attempt 23의 최소 smoke/15M archive
functional pass 및 cleanup-recovered zero를 결합한 `phase8-handoff.json`으로 Phase 8을 승격한다.
중간 cleanup bookkeeping 실패를 포함해 두 attempt의 기존 verdict는 바꾸지 않는다. 최종 기준선은
`performance-tests/phase8-final/phase8-manifest.json`이다.

안정화 중에는 각 focused 수정마다 `phase7:local`을 다시 실행하지 않는다. 문제와 deferred local
항목을 `performance-tests/phase7_2-stabilization/issue-register.json`에 먼저 등록하고, 변경 범위의
unit/build/synth/template gate 뒤 Attempt 17과 동일한 `LoopAdPerfPhase7IntegrationStack`을 fresh
identity로 한 번 배포하는 scoped AWS diagnostic으로 실제 해결 여부를 확인한다. 별도 diagnostic
전용 stack이나 축소 resource graph는 만들지 않는다. 전체 stack의 불가피한 resource와 비용을
기록하고 문제가 난 stage와 필수 선행 stage만 실행한다. 여러 diagnostic이
Scoped diagnostic은 계속 `promotionEligible=false`다. 현재 override에서는 새 50k/warmup/score와
전체 Phase 7-1 local chain을 반복하지 않고, scoped archive pass와 cleanup zero 뒤 composite Phase
8 handoff를 만든다. 이는 attempt를 strict pass로 재분류하는 것이 아니다.

예외적으로 production CDK가 monolithic이고 batch handoff가 이미 통과한 경우에는 별도
diagnostic 배포를 추가하지 않는다. 새 attempt를 처음부터 strict로 봉인하고 기존 실패 gate를
정상 순서에서 먼저 확인한 뒤, 통과하면 동일 attempt를 drain·equivalence·cleanup까지 계속한다.
이 fast path는 stage 생략, 반복 또는 실행 후 verdict 재분류를 허용하지 않는다.

현재 active budget epoch는 이전 비용을 admission에서 제외한 `$60` hard cap, `$55` paid-work
stop과 `$5` cleanup reserve다. 이전 attempt 비용과 실패 verdict는 lifetime ledger에서 삭제하지
않는다. 명시적 reset은 기존 epoch를 닫고 새 epoch를 append하며, ledger에 고정한 다음 유료
경계부터만 `$0`으로 계산한다.

비성능 운영 튜닝값은 point acceptance로 고정하지 않는다. 예를 들어 현재 ClickHouse
container/server/archive-query 값은 `8/7/6 GiB`지만 archive query 검증은 `6..6.5 GiB`와 server
아래 safety envelope를 요구한다. 향후 envelope 안의 합리적인 상향 조정은 exact 숫자 불일치만으로
새 AWS attempt를 요구하지 않는다. 반대로 throughput/latency 기준, count/fingerprint,
bidirectional equivalence, COMMITTED 재읽기, source DROP safety, ownership, cleanup zero와 budget은
계속 strict gate다.

Phase 5는 `skipped`이며 `passed`로 표시하지 않는다. Composite handoff는 Attempt 17에서 실제로
완료된 50k score 측정과 fresh archive pass의 결합 근거를 명시하며, 단일 strict attempt가 전체
acceptance를 통과했다고 주장하지 않는다.
