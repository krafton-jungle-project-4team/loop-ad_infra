# Performance tests

이 디렉터리는 운영 인프라와 분리된 성능 실험 기록의 경계다. Git에는
정제된 증적과 복구용 reference만 보관하고, 원시 실행 결과는 보관하지
않는다.

## Directory contract

- `evidence/`: schema로 검증되는 실행 요약, incident, 설명 문서, source
  inventory를 보관한다.
- `reference/`: 더 이상 활성 실행 대상이 아닌 인프라, 과거 도구, 과거
  문서를 복구 목적으로 보관한다.
- `tools/`: index 생성, schema 검증, completeness 검사, secret·용량 검사,
  순수 분석처럼 로컬에서 안전하게 실행할 수 있는 도구만 둔다.
- `artifacts/`: 로그, trace, raw metrics, `cdk.out`, 부하 도구 출력 등 새
  원시 결과를 생성하는 유일한 위치다. `README.md`를 제외한 내용은 Git에서
  제외된다.

운영 `src/`, `test/`, `assets/`, 루트 package 파일과 CDK 설정은
`origin/main`을 기준으로 유지한다. 성능 실험용 과거 CDK와 실행기는 운영
build, test, synth 대상이 아니며 `reference/` 아래에서만 보존한다.

## Evidence policy

- 실패, 중단, 차단, 무효, 결론 불충분 실행도 index에서 제거하지 않는다.
- 기록이 없으면 `not_recorded`, 측정하지 않았으면 `not_measured`, 확인할 수
  없으면 `unknown`, 무효면 `invalidated`, 결론을 낼 수 없으면
  `inconclusive`로 기록한다.
- 누락된 가설, 수치, 결론은 추정하지 않는다.
- 큰 근거는 외부 snapshot ID와 SHA-256으로 연결하고 Git에 복사하지 않는다.
- 자격증명, token, private key, presigned URL은 evidence나 reference에
  기록하지 않는다.

## New runs

새 도구는 `performance-tests/artifacts/run_<id>/` 아래에만 원시 결과를
생성해야 한다. 실행 후 Git에 남길 자료는 schema를 통과하는 요약으로
정제해 `evidence/experiments/<phase>/<run-id>/`에 추가한다. 원본 snapshot은
저장소 밖에서 사용자가 관리한다.
