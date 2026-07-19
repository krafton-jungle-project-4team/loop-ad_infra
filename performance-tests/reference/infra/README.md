# Performance infrastructure reference

이 디렉터리는 과거 성능 테스트용 CDK와 그 의존 파일의 복구용 reference다.
운영 인프라의 authoritative source가 아니며, 현재 저장소의 build, test, CDK
synth 대상도 아니다.

## Provenance

- Source snapshot: `snapshot_20260719T200907Z`
- Source branch: `codex/aws-perf-test-plan`
- Source HEAD: `304bfeb8bd0808797e28293b74a073d5c5e5ef11`
- origin/main at capture: `a3d424e8dde31ee2770a0eff8094ca3063771bf2`
- Workspace archive SHA-256:
  `773aae45e7ba300535741a79bccd503704d8bc4d4716dbb72e3f59206f808417`
- Source working tree: dirty; exact bytes come from the verified workspace snapshot,
  not from HEAD alone.

`manifest.json` maps every original path to its stored and restore path and records
the byte count and SHA-256. Git history and intermediate variants remain in the
external `repo.bundle` and are not duplicated here.

## Representative source

- Phase 0: `src/perf-phase0-stack.ts`
- Phase 1: `src/perf-phase1-kinesis-stack.ts` and
  `src/perf-phase1-generator-control-stack.ts`
- Phase 4: ClickHouse config/stacks, handler, tests, and ClickHouse assets
- Phase 6: `src/perf-phase6-archive-stack.ts` and its tests
- Phase 7: integration and archive-diagnostic stacks, config, tests, and user data
- Phase 8: no separate Phase 8 CDK stack was present in the captured final tree;
  the recorded final promotion used the preserved Phase 7 integration source.

Shared CDK entry/config/runtime files and the captured package lock are included
because the phase stacks cannot be reconstructed accurately without them.

## Isolation

TypeScript source and tests are stored byte-for-byte with an added `.reference`
suffix. This prevents the root `tsc --noEmit`, whose config recursively includes
TypeScript, from compiling retired reference code. `RESTORE.md` describes how to
remove the storage suffix in a disposable restore directory.

Do not deploy directly from this directory. Restoration proves recoverability; it
does not authorize AWS resource creation, modification, deletion, or load execution.
