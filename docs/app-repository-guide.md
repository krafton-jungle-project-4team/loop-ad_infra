# App Repository Guide

이 문서는 loop-ad 애플리케이션 개발자가 각 앱 repo에서 지켜야 하는 repo 형식과 env contract입니다.

앱 repo는 인프라를 직접 구성하지 않습니다. 대신 앱은 이 문서의 env 이름을 정확히 읽고, 인프라 담당자는 ECS task definition 또는 FE build workflow에서 실제 값을 주입합니다.

Public domain과 private service endpoint는 env로 받지 않습니다. 고정 endpoint contract는 [service-endpoints.md](service-endpoints.md)에 둡니다.

## 핵심 규칙

- env 값에 fallback이나 기본값을 두지 않습니다.
- 서버는 runtime 시작 시점에 필수 env를 즉시 검증합니다.
- FE는 필수 public env가 있을 때 build 시작 시점에 즉시 검증합니다.
- 검증된 env는 한 곳의 config 객체로 모읍니다.
- 필수 env가 없거나 형식이 틀리면 빠르게 실패합니다.
- secret은 repo, Docker image, GitHub Actions env, 로그, metric label, error response, FE bundle에 남기지 않습니다.
- `.env`, `.env.local`, `.env.*.local`은 commit하지 않습니다.
- 앱 코드는 SSM Parameter Store나 Secrets Manager를 직접 조회하지 않습니다.
- 앱 코드는 Fargate 같은 ECS launch type이나 AWS 리소스 구현 방식에 의존하지 않습니다.

금지 패턴:

```ts
const apiBaseUrl = process.env.API_BASE_URL || 'http://localhost:3000';
```

권장 패턴:

```ts
function requiredEnv(name: string): string {
    const value = process.env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

export const appConfig = Object.freeze({
    env: requiredEnv('LOOPAD_ENV'),
    serviceId: requiredEnv('LOOPAD_SERVICE_ID'),
    port: Number(requiredEnv('PORT')),
});
```

## Repo Type

| Repo type | 배포 단위 | 앱 repo가 준비할 것 |
|---|---|---|
| Server | ECS service | Dockerfile, config loader, health check, deploy workflow |
| Frontend Static Site | 정적 파일 | npm build script, public env 검증, deploy workflow |

## Server Repo

서버 repo는 Docker image로 빌드되어 ECS service에서 실행됩니다.

필수 구성:

```text
Dockerfile
.github/workflows/deploy.yml
config loader
```

Dockerfile 규칙:

- `linux/arm64`에서 실행 가능해야 합니다.
- 서버는 `PORT` env를 읽고 `0.0.0.0:${PORT}`로 listen합니다.
- DB endpoint, password, token, API key를 build arg나 image 안에 넣지 않습니다.
- HTTP 서버는 `/health`에서 정상 상태일 때 `200-399`를 반환합니다.

Server deploy workflow 규칙:

- 각 서버 repo는 인프라 repo의 reusable deploy workflow를 `uses:`로 호출합니다.
- workflow는 image build/push와 ECS service image 교체만 담당합니다.
- runtime env와 secret은 workflow에서 정의하지 않습니다.
- ECR repository, ECS service, container 이름 같은 deploy target 값은 인프라 담당자가 제공한 값을 사용합니다.
- 최초 개발 환경 구성 시에는 인프라 repo에서 ECR repository를 먼저 만든 뒤, 각 서버 repo가 image를 push합니다.

Dev ECR repository 이름:

| Service | ECR repository |
|---|---|
| Event Collector | `loop-ad/event-collector` |
| Ad Context Projector | `loop-ad/ad-context-projector` |
| Advertisement API | `loop-ad/advertisement-api` |
| Dashboard API | `loop-ad/dashboard-api` |
| Decision API | `loop-ad/decision-api` |

## Server Env Contract

아래 env는 서버 코드가 실제로 읽는 값입니다. Plain env와 secret env 모두 앱에서는 일반 환경변수처럼 읽지만, secret env는 절대 출력하지 않습니다.

공통 서버 env:

| Env | 값 또는 주입 방식 | 설명 |
|---|---|---|
| `LOOPAD_ENV` | `dev` | dev ECS에서 고정으로 주입되는 실행 환경 이름입니다. |
| `LOOPAD_SERVICE_ID` | 서비스별 고정값 | 서비스 식별자입니다. 아래 service ID 값을 그대로 사용합니다. |
| `LOOPAD_RUNTIME` | 앱 repo별 고정값 | 앱 런타임 구분입니다. |
| `PORT` | `80` | dev ECS에서 고정으로 주입되는 listen 포트입니다. |

서비스별 `LOOPAD_SERVICE_ID`:

| Service | `LOOPAD_SERVICE_ID` |
|---|---|
| Event Collector | `event-collector` |
| Ad Context Projector | `ad-context-projector` |
| Advertisement API | `advertisement-api` |
| Dashboard API | `dashboard-api` |
| Decision API | `decision-api` |

Data env:

| Env | 종류 | 값 또는 주입 방식 | 설명 |
|---|---|---|---|
| `LOOPAD_AURORA_HOST` | Plain | 인프라 주입 | Aurora PostgreSQL hostname입니다. |
| `LOOPAD_AURORA_PORT` | Plain | `5432` | Aurora PostgreSQL port입니다. |
| `LOOPAD_AURORA_DATABASE` | Plain | `loopad` | 기본 database 이름입니다. |
| `LOOPAD_AURORA_USERNAME` | Secret | secret 주입 | Aurora username입니다. |
| `LOOPAD_AURORA_PASSWORD` | Secret | secret 주입 | Aurora password입니다. |
| `LOOPAD_CLICKHOUSE_URL` | Plain | 인프라 주입 | ClickHouse HTTP endpoint입니다. |
| `LOOPAD_CLICKHOUSE_USERNAME` | Plain | `default` | ClickHouse username입니다. |
| `LOOPAD_REDIS_URL` | Plain | 인프라 주입 | Redis 호환 Valkey endpoint입니다. TLS 연결을 위해 `rediss://...:6379` 형식을 사용합니다. |
| `LOOPAD_MSK_BOOTSTRAP_BROKERS` | Plain | 인프라 주입 | MSK bootstrap broker 목록입니다. |
| `LOOPAD_EVENT_TOPIC` | Plain | `loop-ad.events.raw` | raw event topic 이름입니다. |

DataStorage env:

| Env | 종류 | 값 또는 주입 방식 | 설명 |
|---|---|---|---|
| `LOOPAD_DATA_STORAGE_BUCKET` | Plain | 인프라 주입 | GenAI 생성물을 저장하는 S3 bucket 이름입니다. |
| `LOOPAD_GENAI_GENERATED_ASSETS_PREFIX` | Plain | `genai/generated/` | GenAI 생성물 S3 prefix입니다. |

External secret env:

| Env | 사용하는 서비스 | 설명 |
|---|---|---|
| `LOOPAD_OPENAI_API_KEY` | Decision API | OpenAI API key입니다. |

서비스별 필수 env:

| Service | 필수 env |
|---|---|
| Event Collector | 공통 서버 env, `LOOPAD_MSK_BOOTSTRAP_BROKERS`, `LOOPAD_EVENT_TOPIC` |
| Ad Context Projector | 공통 서버 env, `LOOPAD_MSK_BOOTSTRAP_BROKERS`, `LOOPAD_EVENT_TOPIC`, `LOOPAD_REDIS_URL`, `LOOPAD_CLICKHOUSE_URL`, `LOOPAD_CLICKHOUSE_USERNAME` |
| Advertisement API | 공통 서버 env, `LOOPAD_REDIS_URL`, Aurora env |
| Dashboard API | 공통 서버 env, Aurora env, ClickHouse env, DataStorage env |
| Decision API | 공통 서버 env, Aurora env, ClickHouse env, DataStorage env, `LOOPAD_OPENAI_API_KEY` |

내부 service 호출 주소는 env로 받지 않습니다. Dashboard API가 Decision API를 호출할 때는 [service-endpoints.md](service-endpoints.md)의 private endpoint contract를 사용합니다.

Redis client를 사용하는 서비스는 `LOOPAD_REDIS_URL`의 `rediss://` endpoint에 TLS로 연결해야 합니다. fallback으로 local Redis나 임의 주소를 붙이면 안 됩니다.

## Server Logging Contract

서버는 파일 로그를 직접 관리하지 않고 stdout/stderr로만 로그를 남깁니다. 인프라는 ECS service별 CloudWatch LogGroup을 분리해 보관합니다.

Dev CloudWatch LogGroup 이름:

| Service | LogGroup |
|---|---|
| Event Collector | `/loop-ad/dev/ecs/event-collector` |
| Ad Context Projector | `/loop-ad/dev/ecs/ad-context-projector` |
| Advertisement API | `/loop-ad/dev/ecs/advertisement-api` |
| Dashboard API | `/loop-ad/dev/ecs/dashboard-api` |
| Decision API | `/loop-ad/dev/ecs/decision-api` |

로그 규칙:

- 로그는 JSON structured log를 권장합니다.
- 모든 로그에는 `timestamp`, `level`, `service`, `env`, `message`를 포함합니다.
- 요청 단위 로그에는 `requestId` 또는 `traceId`를 포함합니다.
- secret, token, password, API key, DB credential, 개인 식별 정보는 로그에 남기지 않습니다.
- dev 로그 보존 기간은 인프라에서 3일로 관리합니다.

## Frontend Static Site Repo

FE repo는 Docker image가 아니라 정적 파일을 빌드해서 배포합니다. Dashboard FE와 demo-shoppingmall FE 모두 같은 규칙을 따릅니다.

필수 구성:

```text
package.json
.github/workflows/deploy.yml
public env validator, if needed
```

npm 규칙:

- 기본 install 명령은 `npm ci`입니다.
- 기본 build 명령은 `npm run build`입니다.
- 기본 build output directory는 `dist`입니다.
- 다른 값이 필요하면 FE repo의 deploy workflow input으로 명시합니다.

FE env 규칙:

- FE env는 build 결과물에 포함될 수 있습니다.
- 꼭 필요한 public 값만 사용합니다.
- secret, DB credential, webhook URL, private endpoint를 FE env에 넣지 않습니다.
- Vite를 쓴다면 public env는 `VITE_` prefix를 사용합니다.
- FE도 env를 사용한다면 fallback 없이 build 시작 시점에 검증합니다.
- loop-ad public domain은 [service-endpoints.md](service-endpoints.md)의 고정 contract를 사용하고 `VITE_API_BASE_URL` 같은 env로 다시 빼지 않습니다.

금지 패턴:

```text
VITE_API_BASE_URL=https://api.dev.loop-ad.org
VITE_INGEST_BASE_URL=https://ingest.dev.loop-ad.org
VITE_OPENAI_API_KEY=...
VITE_AURORA_PASSWORD=...
```

Frontend deploy workflow 규칙:

- 각 FE repo는 인프라 repo의 reusable deploy workflow를 `uses:`로 호출합니다.
- workflow는 정적 파일 업로드와 CDN invalidation만 담당합니다.
- bucket, CDN distribution 같은 deploy target 값은 인프라 담당자가 제공한 값을 사용합니다.
- FE build env 값은 꼭 필요할 때만 GitHub Environment variables 등으로 관리할 수 있지만, secret과 고정 loop-ad domain은 넣지 않습니다.

## Local Development

- `.env.example`은 필요한 env 이름과 형식을 알려주는 용도로만 둡니다.
- 실제 local 값은 `.env.local` 또는 개인 shell 환경에서 관리하고 commit하지 않습니다.
- local 개발에서도 fallback/default를 넣지 않습니다.
- local에서 필요한 DB/cache/broker 주소도 명시적으로 env에 넣고 실행합니다.

로컬 `.env.local` 형태:

```text
LOOPAD_ENV=local
LOOPAD_SERVICE_ID=dashboard-api
LOOPAD_RUNTIME=go
PORT=8080
LOOPAD_AURORA_HOST=localhost
LOOPAD_AURORA_PORT=15432
LOOPAD_AURORA_DATABASE=loopad
```

## 개발자가 몰라도 되는 세부 구현

아래 값은 앱 코드가 직접 의존하지 않습니다. 필요한 경우 인프라 담당자가 deploy workflow input이나 runtime env로 제공합니다.

- AWS resource ARN
- SSM parameter path
- Secrets Manager secret 이름
- bucket 이름
- CDN distribution ID
- ALB/NLB listener rule
- ECS launch type

앱 repo는 값의 출처보다 "어떤 env가 필요하고, 없으면 실패한다"는 contract를 정확히 지키면 됩니다.

## Review Checklist

- 필수 env에 fallback/default가 없는가?
- env 검증이 서버 시작 또는 FE build 시작 시점에 실행되는가?
- 검증된 값이 config 객체로 모이는가?
- 앱 코드가 SSM/Secrets Manager를 직접 조회하지 않는가?
- secret이 repo, image, workflow env, 로그, FE bundle에 들어가지 않는가?
- 서버 repo는 Dockerfile과 deploy workflow를 갖고 있는가?
- 서버 health check가 준비되어 있는가?
- FE repo는 `npm ci`, `npm run build`, `dist` 기본 규칙을 따르는가?
- FE env는 public 값만 사용하는가?
- deploy workflow가 인프라 repo reusable workflow를 `uses:`로 호출하는가?
