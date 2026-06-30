# App Repository Guide

이 문서는 loop-ad 앱 레포가 dev 인프라와 맞춰야 하는 repo 형식, deploy target, runtime env 계약을 정리한 how-to guide입니다.

## 공통 규칙

- 앱 코드는 필수 env에 fallback/default를 두지 않습니다.
- 서버는 시작 시점에 필수 env를 검증하고 실패 시 빠르게 종료합니다.
- secret, token, password, API key는 repo, Docker image, GitHub Actions plain env, 로그, metric label, FE bundle에 남기지 않습니다.
- 앱 코드는 Secrets Manager나 SSM Parameter Store를 직접 조회하지 않습니다.
- 서버는 `PORT` env를 읽고 `0.0.0.0:${PORT}`로 listen합니다.
- 모든 서버는 `/health`에서 정상 상태일 때 HTTP `200`을 반환합니다.
- 앱은 자신에게 주입된 env와 secret 중 필요한 값만 시작 시점에 검증하고 사용합니다.

## Server Deploy Target

서버 repo는 Docker image로 빌드되어 ECS/Fargate service에서 실행됩니다. 각 서버 repo의 deploy workflow는 인프라 repo reusable workflow를 호출하고, runtime env와 secret은 앱 workflow에서 정의하지 않습니다.

| Service | `service_name` | `ecr_repository` | `ecs_cluster` | `ecs_service` | `container_name` |
|---|---|---|---|---|---|
| Event Collector | `event-collector` | `loop-ad/event-collector` | `dev-loop-ad-cluster` | `dev-event-collector` | `event-collector` |
| Dashboard API | `dashboard-api` | `loop-ad/dashboard-api` | `dev-loop-ad-cluster` | `dev-dashboard-api` | `dashboard-api` |
| Decision API | `decision-api` | `loop-ad/decision-api` | `dev-loop-ad-cluster` | `dev-decision-api` | `decision-api` |

`advertisement-api`는 dev 인프라 대상이 아닙니다.

## Public API Domains

Public HTTPS entrypoint는 ALB 하나를 공유하고, host-header로 서비스별 target group을 나눕니다.

| Domain | Target service |
|---|---|
| `https://event.api.dev.loop-ad.org` | `event-collector` |
| `https://dashboard.api.dev.loop-ad.org` | `dashboard-api` |
| `https://decision.api.dev.loop-ad.org` | `decision-api` |

`/internal/*` 같은 내부성 요청은 앱이 `X-Loop-Ad-Internal-Key` header를 검증합니다. 인프라는 같은 key 값을 `LOOPAD_INTERNAL_API_KEY` secret env로 주입합니다. 별도 EventBridge scheduler나 internal-only load balancer는 없습니다.

## Common Server Env

| Env | 값 또는 주입 방식 |
|---|---|
| `LOOPAD_ENV` | `dev` |
| `LOOPAD_SERVICE_ID` | 서비스별 고정값 |
| `PORT` | `8080` |
| `LOOPAD_INTERNAL_API_KEY` | Secrets Manager `{ "api_key": "..." }`의 `api_key` |

## Data Env

| Env | 값 또는 주입 방식 |
|---|---|
| `LOOPAD_AURORA_HOST` | Aurora endpoint |
| `LOOPAD_AURORA_PORT` | `5432` |
| `LOOPAD_AURORA_DATABASE` | `loopad` |
| `LOOPAD_AURORA_USERNAME` | Aurora secret `username` |
| `LOOPAD_AURORA_PASSWORD` | Aurora secret `password` |
| `LOOPAD_CLICKHOUSE_URL` | `http://<public-dns>:8123` |
| `LOOPAD_CLICKHOUSE_DATABASE` | `loopad` |
| `LOOPAD_CLICKHOUSE_USERNAME` | ClickHouse secret `username` |
| `LOOPAD_CLICKHOUSE_PASSWORD` | ClickHouse secret `password` |
| `LOOPAD_KAFKA_BOOTSTRAP_BROKERS` | `<public-dns>:9094` |
| `LOOPAD_KAFKA_SECURITY_PROTOCOL` | `SASL_PLAINTEXT` |
| `LOOPAD_KAFKA_SASL_MECHANISM` | `SCRAM-SHA-512` |
| `LOOPAD_KAFKA_USERNAME` | Kafka app user secret `username` |
| `LOOPAD_KAFKA_PASSWORD` | Kafka app user secret `password` |
| `LOOPAD_EVENT_TOPIC` | `loop-ad.events.raw` |

## Storage and External Env

| Env | 값 또는 주입 방식 |
|---|---|
| `LOOPAD_DATA_STORAGE_BUCKET` | DataStorage bucket name |
| `LOOPAD_GENAI_ASSETS_BASE_PREFIX` | `genai/` |
| `LOOPAD_OPENAI_API_KEY` | OpenAI secret `api_key` |

## Frontend Static Site Repo

Frontend repo는 정적 파일을 빌드해서 S3와 CloudFront로 배포합니다.

| Site | Public domain | `s3_bucket` | `s3_prefix` |
|---|---|---|---|
| Dashboard Web | `https://dashboard.dev.loop-ad.org` | `loop-ad-dev-dashboard-web` | `.` |
| Demo shoppingmall Web | `https://demo-shoppingmall.dev.loop-ad.org` | `loop-ad-dev-demo-shoppingmall-web` | `.` |

CloudFront distribution ID와 bucket name은 인프라가 일반 SSM Parameter Store metadata로 제공합니다. FE env에는 secret, DB credential, OpenAI key, private endpoint를 넣지 않습니다.
