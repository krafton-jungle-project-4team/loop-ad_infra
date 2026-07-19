# Retired performance tools

이 디렉터리는 과거 AWS deploy·cleanup runner, 부하 생성기, 수집기, 일회성
진단 도구의 복구용 reference다. 활성 실행 도구가 아니며 root build, test,
CDK synth에 포함되지 않는다.

원본은 `snapshot_20260719T200907Z`의 `performance-tests/phase*` 경로다. cache,
virtualenv, `target`, `cdk.out`, runtime output, raw log, compressed artifact,
NDJSON, JAR/class는 복사하지 않았다. 이 파일들은 외부 snapshot과 bundle에
남아 있다.

TypeScript와 JavaScript 계열 파일은 원문 바이트를 유지한 채 `.reference`
접미사를 붙여 root TypeScript 탐색과 직접 실행을 차단했다. 복구할 때는 새
disposable directory에 복사하면서 이 접미사만 제거하고 `manifest.json`의
SHA-256을 확인한다. 이 reference의 존재는 AWS 실행 권한을 의미하지 않는다.
