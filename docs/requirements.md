# loop-ad AWS CDK 요구사항

이 프로젝트는 loop-ad 개발 환경과 성능 테스트 환경을 AWS CDK v2로 관리한다.

## 범위

- 담당: VPC, ECS, ECR, ALB/NLB, 보안그룹, VPC Endpoint, SSM endpoint contract, GitHub Actions reusable workflow
- 제외: 애플리케이션 코드, SDK, React 구현, 비즈니스 로직, 실제 데이터 적재/로그 운영
- 리전: `ap-northeast-2`

## 환경

### Dev

상시 개발용 스택이다.

- 한 VPC를 소유한다.
- Event Collector, Ad Context Projector, Ad Decision API, Dashboard API, Recommendation을 ECS Fargate로 실행한다.
- Event Collector는 NLB에만 붙인다.
- Ad Decision API와 Dashboard API는 ALB path rule에만 붙인다.
- Aurora, Redis, ClickHouse, MSK는 SSM endpoint contract로 표현한다.

### Perf

성능 테스트용 임시 스택이다.

- Dev 스택이 export한 VPC/subnet/endpoint SG를 import해서 같은 VPC 안에 뜬다.
- 성능 테스트가 끝나면 이 스택만 destroy할 수 있어야 한다.
- Event Collector와 Ad Context Projector만 둔다.
- ECS on EC2 capacity provider를 사용한다.
- 필요한 endpoint contract는 MSK, Redis, ClickHouse만 둔다.
- API route, Dashboard, Recommendation, Aurora, frontend, 데이터 로그 관리는 포함하지 않는다.

## 운영 명령

- `npm run synth:dev`
- `npm run synth:perf`
- `npm run deploy:dev`
- `npm run deploy:perf`
- `npm run destroy:perf`

`npm run deploy`와 `npm run destroy`는 실수 방지를 위해 차단한다.
