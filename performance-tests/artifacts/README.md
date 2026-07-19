# Raw performance artifacts

이 디렉터리는 로컬 원시 실행 결과의 출력 경로다. 이 문서를 제외한 모든
파일은 Git에서 제외된다.

허용되는 로컬 예시는 load-driver stdout/stderr, request trace, raw metrics,
서비스 로그, `cdk.out`, cache, 빌드 결과다. 이런 파일을 evidence나 reference로
이동하지 않는다. 보존이 필요하면 저장소 밖 snapshot에 남기고, Git에는
snapshot ID, 근거 파일 경로, SHA-256, 정제된 결과만 기록한다.

도구는 저장소 루트나 `performance-tests/run_*`에 새 결과를 만들면 안 된다.
항상 `performance-tests/artifacts/run_<id>/`를 명시적으로 사용한다.
