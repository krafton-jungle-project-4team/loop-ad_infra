# loop-ad_aws_cdk

loop-ad 개발/성능 테스트용 AWS CDK v2 프로젝트입니다.

이 repo는 애플리케이션 코드나 비즈니스 로직을 다루지 않습니다. CDK로 관리하는 범위는 한 VPC 안의 ECS/ECR/LB/SSM endpoint contract와 GitHub Actions reusable workflow입니다.

## 구조

- `LoopAdDevStack`: 상시 개발용 스택
  - VPC, public/private subnet, VPC endpoint
  - ECR repository 5개
  - ECS Fargate 서비스 5개
  - Event Collector용 NLB
  - Ad Decision API/Dashboard API용 ALB path rule
  - Aurora/Redis/ClickHouse/MSK endpoint contract용 SSM parameter
- `LoopAdPerfStack`: 성능 테스트용 임시 스택
  - `LoopAdDevStack`이 export한 VPC/subnet/endpoint SG를 import
  - ECS on EC2 capacity provider
  - Go ECS 서비스 2개: Event Collector, Ad Context Projector
  - Event Collector용 NLB
  - Redis/ClickHouse/MSK endpoint contract용 SSM parameter
  - API 경로, Dashboard, Recommendation, Aurora, frontend는 만들지 않음

## 원칙

- 리전은 `ap-northeast-2`로 고정합니다.
- 코드는 선언형 프레임워크보다 절차형 CDK에 가깝게 둡니다.
- 스택은 lifecycle 기준으로만 나눕니다: 상시 dev, 임시 perf.
- generic `npm run deploy` / `npm run destroy`는 실수 방지를 위해 차단합니다.
- 성능 테스트 리소스는 `deploy:perf`로 올리고 `destroy:perf`로 내립니다.
- public ingress는 LB 80 포트에만 둡니다.
- ECS/data 통신은 CIDR가 아니라 security group 관계로 표현합니다.
- NAT Gateway는 기본 off입니다.

## 명령

```bash
npm run build
npm test
npm run synth:dev
npm run synth:perf
```

실제 작업 명령:

```bash
npm run deploy:dev
npm run deploy:perf
npm run destroy:perf
```
