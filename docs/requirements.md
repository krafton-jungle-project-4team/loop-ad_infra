# loop-ad AWS CDK 요구사항

이 프로젝트는 AWS CDK v2로 loop-ad 인프라 초안을 만든다. 실제 배포가 아니라 `build`, `test`, `synth` 가능한 구조가 목표다.

## 범위

- 담당: AWS 인프라, GitHub Actions reusable workflow
- 제외: 애플리케이션 코드, SDK, React 구현, OpenAI/n8n/Discord 자체 설정, 비즈니스 로직
- 외부 연동: NAT egress, secret/env placeholder, endpoint contract처럼 AWS 관점의 연결 계약만 표현

## 리전/배포

- 리전은 `ap-northeast-2` 고정
- `deploy`/`destroy`는 기본 차단
- `dev`, `perf` synth script 제공

## 네트워크

- Public edge: ALB, NLB, CloudFront
- Private app: ECS Fargate, ECS on EC2
- Data: Aurora, Redis, ClickHouse, MSK는 우선 SSM endpoint contract로 표현
- AWS API 접근은 VPC Endpoint 우선
- NAT Gateway는 기본 off, 외부 HTTPS egress가 필요한 서비스에만 명시적으로 허용
- public ingress는 LB 80 포트에서만 허용
- ECS/data 통신은 CIDR가 아니라 security group 관계로 표현

## Edge 라우팅

- NLB는 Event SDK ingestion 전용이며 Event Collector만 target으로 허용
- ALB는 사용자/API HTTP 전용이며 Ad Decision API와 Dashboard API만 target으로 허용
- CloudFront는 S3 frontend/media bucket만 origin으로 사용하며, S3 bucket과 같은 FrontendStack에 둔다.

## Compute policy

- Event Collector: dev Fargate, perf ECS on EC2
- Ad Context Projector: dev Fargate, perf ECS on EC2
- Ad Decision API: Fargate 우선
- Dashboard API: Fargate 우선
- Recommendation Server: 초기 Fargate, 병목 확인 후 EC2 전환 가능

## Stack 분리

- Network, Edge, Frontend, Storage, Stream, Data, Collect, Decision, Dashboard, Analytics, Observability
- stack 간 연결은 props/interface로 전달
- output/import 남발 금지

## CDK 레벨

- project-owned construct 또는 L2를 우선 사용
- L1 `Cfn*` 직접 생성은 특별한 이유가 없는 한 금지
- config는 문서가 아니라 실제 ECS/ECR/LB/SG/SSM 생성 입력이어야 함
