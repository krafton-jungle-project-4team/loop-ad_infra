# Phase 7-2 배포 준비 Goal

## 목표

Phase 7-2 runtime 배포 직전까지 필요한 구현, 로컬 재검증, AWS image 준비와 `cdk diff`를
완료한다. 이 Goal에서는 `LoopAdPerfPhase7IntegrationStack`을 배포하거나 50k 부하·15M archive를
실행하지 않는다.

## 입력

- [Phase 7 실행 계약](../guides/guide_phase7_end_to_end_integration_test.md)
- 명시적인 최신 `passed`, `awsReady=true` Phase 7-1 handoff
- account `742711170910`, region `ap-northeast-2`
- 사용자가 허용한 root operator

## 절차

1. HAProxy를 실제 collector Cloud Map SRV backend의 `leastconn`으로 연결한다.
2. 성공 202는 1/1000만 기록하고 오류는 전부 기록한다. Prometheus `/metrics`와 config SHA를
   증적으로 남긴다.
3. CloudWatch Logs 최대 5 GiB, 새 load 금지 `$35`, cleanup reserve `$5`, hard cap `$40`을
   deterministic cost gate로 검증한다.
4. AWS preflight, immutable whole-attempt runner, evaluator와 exact-ownership cleanup 도구의 단위
   테스트를 통과한다.
5. 구현 변경 뒤 새 Phase 7-1 whole attempt를 실행해 실제 AWS 요청과 owned Docker inventory가
   모두 0인 새 handoff를 만든다.
6. 첫 AWS 호출 직전에 사용자 확인을 받고 `aws login`을 실행한다. STS identity가 허용한 root와
   일치하지 않으면 중단한다.
7. image stack만 생성하고 collector `linux/amd64`, consumer/archive `linux/arm64` image를 exact
   frozen source에서 build/push한다. tag-to-digest와 실제 architecture를 확인한다.
8. exact digest, AMI, certificate와 DNS context로 `cdk diff --no-change-set`을 저장한다.
9. 구현, 새 로컬 evidence, AWS image/diff evidence와 readiness 문서를 논리적으로 분리해
   커밋한다.

## 허용 편차

성능·resource 측정값이 예상과 다르더라도 명시된 허용 한도 안이면 중단 사유가 아니다. 특히
CPU·memory 70% 한도 안의 차이는 기록하고 원인을 분석하면서 끝까지 진행한다. count 정합성,
identity/ownership, secret, 비용 hard cap, deadline과 cleanup은 완화하지 않는다.

## 완료 조건

- 최신 Phase 7-1 handoff가 최종 구현 hash와 일치한다.
- image 3개의 immutable digest와 architecture가 검증됐다.
- runtime stack은 absent이고 image stack만 exact run-owned 상태다.
- fresh diff에 예상한 신규 runtime resource만 있으며 shared 변경이나 replacement가 없다.
- 최종 verdict는 `deployment-ready`, `not-ready`, `blocked` 중 하나다.
