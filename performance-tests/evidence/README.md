# Curated performance evidence

이 디렉터리는 외부 snapshot의 원시 실행 결과를 Git에서 검증 가능한 요약으로
연결하는 reference 문서다. `experiment-index.json`이 canonical index이며 각
실행은 Phase별 `summary.json`과 `report.md`를 갖는다.

`snapshots/source-run-inventory.json`은 발견한 모든 run-like source 경로와
canonical run ID 매핑을 보존한다. 같은 실행의 복제 경로는 한 run ID 아래
여러 `sourcePaths`로 기록하며, validator는 각 source 경로가 정확히 한 번
매핑되는지 확인한다.

`summary.json`의 값은 기존 JSON·보고서에서 직접 확인된 값만 사용한다.
일반적인 `completed` 상태를 성능 합격으로 바꾸지 않으며, 자료가 없으면
명시적 누락 상태를 사용한다. 자세한 raw 로그와 metrics는 외부 snapshot에만
있고 각 요약의 SHA-256 근거로 연결된다.
