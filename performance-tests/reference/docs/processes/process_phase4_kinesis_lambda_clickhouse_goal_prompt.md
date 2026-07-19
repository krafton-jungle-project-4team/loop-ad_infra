# Phase 4 Kinesis→Lambda→ClickHouse Goal Prompt

> Historical: 이 goal은 2026-07-16 Lambda quota gate에서 종료된 run의 계약이다. 활성
> 후속 goal은
> [`process_phase4_kinesis_ecs_clickhouse_goal_prompt.md`](process_phase4_kinesis_ecs_clickhouse_goal_prompt.md)다.
> 아래 고정값과 run ID는 ECS 실험에 재사용하지 않는다.

## 검토 결과

보완된 계획은 실행 가능한 수준이다. 실행 전에 불명확했던 Lambda timeout·retry,
VPC/secret 경로, 실패 원문 보존, archive 범위와 시간 상한을 고정했다.

- `BatchSize=10,000`은 실제 배치 크기가 아니라 상한이다. 실제 크기는 2초 window와
  6 MiB invocation payload 제한으로 결정한다.
- ClickHouse INSERT 실패는 레코드별 오류가 아니므로 batch bisect를 사용하지 않는다.
- 최종 실패는 전체 invocation을 보존하는 run 전용 S3 destination으로 보낸다.
- `12,500 rows/flush`, `4 flush/s`는 실측할 tuning 가설이고 단독 합격 조건이 아니다.
- Phase 4에서는 소량 archive 안전성만 확인하고 bulk archive는 Phase 6에 남긴다.
- 전체 goal 시간은 8시간에서 16시간으로 늘렸지만 AWS 실행은 2시간, 본 부하는
  300초, 비용은 `$15`로 유지한다.

## 사용법

저장소를 연 Codex 작업에서 `/goal`을 입력한 뒤 아래 블록만 붙여 넣는다. 상세 조건은
4,000자 제한이 있는 goal 본문에 반복하지 않고 실행 계획 파일을 source of truth로
참조하게 했다.

```text
Outcome

이 저장소에서 Phase 4 Kinesis→Lambda→EC2 ClickHouse 경로를 구현하고 로컬 및 AWS에서
검증하여, 재현 가능한 증거와 최종 passed/failed/aborted/inconclusive 판정을 남겨라.
계획 작성에서 멈추지 말고 안전 gate를 통과하는 범위에서 구현·테스트·실행·cleanup까지
완료하라.

Context and source of truth

- 저장소: /Users/sijun-yang/Documents/GitHub/krafton-jungle-project-4team/loop-ad_infra
- 실행 계약: docs/guide_phase4_kinesis_lambda_clickhouse_test_draft.md
- 배경: docs/resources_clickhouse_ec2_lambda_handoff.md
- 증거 규칙: docs/process_aws_perf_test_result_recording.md
- Phase 3에서 검증된
  performance-tests/run_20260716_110956_locust_kinesis_generator_qualification/implementation/
  의 producer(c7g.2xlarge, Locust worker 8개, payload SHA-256
  93704c35ef7ca24c9c887a439dbea011c94a852f98e12b2d51b4bf6d4f3322b7)만 AWS 본 부하에
  사용하라. 로직을 재작성하거나 새 부하 생성기를 만들지 말고 uv lock으로 실행 환경만
  고정한 뒤 source hash와 contract test를 재확인하라.
- 저장소의 현재 코드·lockfile·AWS 상태를 사실로 확인하고, 이전 ClickHouse
  Cloud/ClickPipes 문서는 구현 결과와 충돌하지 않게 갱신하라.

Constraints

- 먼저 AGENTS.md와 event-pipeline-loadtest-runner skill을 읽고 따른다.
- dirty worktree의 기존 변경을 보존한다. 관계없는 파일 수정, stash, reset, rebase,
  삭제, stage, commit, push, PR은 하지 않는다.
- Lambda와 CDK는 저장소의 TypeScript 패턴을 따른다. Python 로컬 부하·검증은 uv,
  pyproject.toml, uv.lock, uv sync --frozen, uv run만 사용한다.
- 계획의 schema, late-event metric-only 정책, ReplacingMergeTree, async insert, Lambda/ESM
  고정값을 임의로 바꾸지 않는다. properties_json은 parse/stringify하지 않는다.
- 공유 dev stack은 교체하지 않는다. Lambda→ClickHouse는 같은 VPC/AZ 사설 경로를 쓰고
  NAT와 공개 8123을 만들지 않는다. secret 평문을 코드·환경변수·출력·로그에 남기지 않는다.
- 전체 goal 상한은 2 작업일/16시간이다. AWS 유료 wall-clock은 deploy부터 120분이며
  100분에 cleanup을 시작한다. 본 부하는 50,000 records/s×300초 그대로다.
- AWS hard cap은 $15, 새 load 금지선은 계획 누적 $12다. 실행 직전 가격·quota·account·region·
  ownership을 확인하고 상한 초과 예상이면 배포하거나 본 부하를 시작하지 않는다.

Execution

1. 현재 상태를 조사하고 performance-tests/phase4-clickhouse/exec-plan.md를 만든다.
   Progress, Surprises & Discoveries, Decision Log, Outcomes & Retrospective를 작업 중 계속
   갱신하고 각 milestone에 정확한 명령과 관찰 가능한 합격 조건을 적는다.
2. 전용 Phase 4 stack, ClickHouse schema/bootstrap, Lambda handler, ESM, S3 on-failure,
   metric/alarm, least-privilege IAM과 테스트를 구현한다. 공유 stack replacement가 없는지
   assertion과 diff로 확인한다.
3. 고정 image/tag로 Docker ClickHouse와 amazon/kinesis-local 또는 LocalStack을 실행한다.
   단위·CDK·로컬 correctness·중복/retry·late event·50,000-row async flush·archive fixture를
   통과시키고 실제 AWS API 호출이 0건임을 증명한다.
4. AWS preflight 후 새 run_id와 performance-tests/run_<id>_phase4_clickhouse_lambda/를 만들고
   run.json, infra.md, commands.md를 먼저 기록한다. correctness smoke가 완전히 합격한 경우에만
   기존 producer로 15,000,000 records를 선적재하고 ESM을 활성화해 drain한다.
5. count/unique/duplicate, iterator age, Lambda/ESM, async insert, parts/merge, disk, 비용과
   S3 failure/archive 증거를 수집한다. 끝나거나 stop gate가 발생하면 즉시 run 소유 리소스를
   cleanup하고 삭제 확인을 남긴다.

Verification and done

- npm build, 대상 Jest, CDK synth/assertion/diff, uv 테스트와 로컬 통합 테스트가 통과한다.
- smoke에서 Kinesis 입력 = events unique + raw_events + LateEventDropped이고 누락 0이다.
- 본 run에서 정상 event_id 누락 0, on-failure object/ESM dropped/final failure 0,
  producer 종료 후 30분 안에 iterator age 0과 ClickHouse count 완성을 만족한다.
- archive fixture는 검증 전 DROP하지 않고 DROP 후 S3 직접 조회 결과가 동일하다.
- cleanup inventory가 0이고 보고서에 실제 비용 상한, 실행 시간, 명령, 설정, 실패·재시도,
  판정 근거가 있다. gate 실패 시 본 부하를 강행하지 말고 증거·cleanup과 실패 또는
  inconclusive 판정을 남긴다.
```

## OpenAI 공식 근거

- [Long-running work](https://learn.chatgpt.com/docs/long-running-work): goal에는 결과,
  제약과 검증 가능한 완료 조건을 넣고 같은 작업에서 계속 진행한다.
- [Prompting—Goal mode](https://learn.chatgpt.com/docs/prompting#goal-mode): outcome,
  constraints, verification을 명확히 하고 긴 세부 지침은 파일로 분리한다.
- [Using PLANS.md for multi-hour problem solving](https://developers.openai.com/cookbook/articles/codex_exec_plans):
  다시간 작업은 진행상황, 발견사항, 의사결정과 회고를 유지하며 milestone별 검증과 복구
  절차를 명시한다.
