# Performance Tests

이 디렉터리는 AWS 성능 테스트 실행 기록을 보관한다.

각 실행은 별도 폴더에 기록한다.

```text
performance-tests/run_<YYYYMMDD_HHMMSS>_<short_name>/
```

필수 파일:

```text
run.json
infra.md
commands.md
metrics-summary.json
report.md
artifacts.md
```

규칙:

- 실행 기록은 삭제하지 않는다.
- 실패하거나 중단된 실행도 기록한다.
- 큰 원본 결과는 S3에 두고 `artifacts.md`에 링크를 남긴다.
- 실험 결과가 나오면 이 디렉터리 변경분을 커밋 대상으로 포함한다.
- 인프라를 띄우고 내리는 과정에서 바뀐 설정도 같은 run 폴더에 기록한다.

상세 계획은 `docs/guide_aws_event_pipeline_performance_test.md`를 본다.
