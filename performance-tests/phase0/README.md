# Phase 0: ALB Fixed Response

Phase 0은 collector와 Kafka 없이 load generator와 ALB 한계만 본다.

구성:

- `perf-phase0` CDK stack이 internal ALB와 Artillery Fargate worker용 subnet/security group을 만든다.
- Artillery CLI의 `run-fargate`가 테스트 실행 시점에 Fargate worker를 만든다.
- 기본 목표는 worker 20개, worker당 2,500 rps, 총 50,000 rps다.
- Fargate Spot을 사용한다.

실행:

```bash
npm run cdk -- -c environment=perf-phase0 deploy LoopAdPerfPhase0Stack
```

배포 출력에서 다음 값을 확인한다.

- `Phase0ArtilleryTargetBaseUrl`
- `Phase0ArtillerySubnetIds`
- `Phase0ArtillerySecurityGroupId`

run 폴더를 만든다.

```bash
mkdir -p performance-tests/run_<id>
```

Artillery를 실행한다.

```bash
artillery run-fargate \
  --region ap-northeast-2 \
  --count 20 \
  --spot \
  --cpu 4 \
  --memory 8 \
  --subnet-ids "<Phase0ArtillerySubnetIds>" \
  --security-group-ids "<Phase0ArtillerySecurityGroupId>" \
  --target "<Phase0ArtilleryTargetBaseUrl>" \
  --output performance-tests/run_<id>/artillery-report.json \
  performance-tests/phase0/alb-fixed-response.yml
```

종료:

```bash
npm run cdk -- -c environment=perf-phase0 destroy LoopAdPerfPhase0Stack
```

기록:

- `performance-tests/run_<id>/artillery-report.json`는 커밋 대상이다.
- CloudWatch, S3, Artillery Cloud 링크가 있으면 `artifacts.md`에 남긴다.
- 실패한 실행도 `report.md`에 실패 지점과 에러를 기록한다.
