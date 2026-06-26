# Data Contract Repo Plan

이 문서는 `loop-ad_data_contract` repo를 만들 때의 구현 계획입니다.

목표는 Postgres, ClickHouse, Redis의 로컬 개발용 데이터 계약을 한 곳에서 관리하는 것입니다. 각 서버 repo는 이 repo가 제공하는 Docker 기반 로컬 DB와 `.env.example`을 사용하고, AWS dev 환경에서는 CDK가 같은 env 이름을 ECS task definition에 주입합니다.

## 범위

이 repo가 담당합니다.

- macOS 로컬 개발용 Postgres, ClickHouse, Redis Docker compose 구성
- Postgres schema와 dummy data
- ClickHouse schema와 dummy data
- Redis key contract와 dummy data
- 전체 DB 대상 `init`, `dummy`, `drop` 스크립트
- 개별 DB 대상 `postgres`, `clickhouse`, `redis` 전용 `init`, `dummy`, `drop` 스크립트
- 각 서버 repo가 참고할 local `.env.example`

이 repo가 담당하지 않습니다.

- 운영 migration history 관리
- AWS dev/prod DB 직접 변경
- 서버 애플리케이션 코드
- GitHub Actions 배포 workflow

## 전제

- macOS만 지원합니다.
- Windows/Linux 호환성은 고려하지 않습니다.
- Docker Desktop이 설치되어 있고 `docker compose` 명령을 사용할 수 있다고 가정합니다.
- DB client는 가능하면 host에 설치하지 않고 Docker container 안의 client를 사용합니다.
  - Postgres: `docker compose exec postgres psql`
  - ClickHouse: `docker compose exec clickhouse clickhouse-client`
  - Redis: `docker compose exec redis redis-cli`

## 운영 원칙

마이그레이션 기록은 중요하지 않습니다. 정확한 상태가 필요하면 항상 지우고 다시 만듭니다.

```bash
./scripts/drop.sh local
./scripts/init.sh local
./scripts/dummy.sh local
```

빠른 보완이 필요할 때만 임시 patch SQL/Redis script를 만들어 적용합니다. 단, 그 경우에도 최종 기준 파일인 `schema.sql`, `dummy.sql`, `dummy.redis`, `contract.md`에는 반드시 같은 내용을 반영합니다.

## Repo 구조

```text
loop-ad_data_contract/
  README.md
  docker-compose.yml

  environments/
    local.env
    ci.env

  env/
    event-collector.env.example
    ad-context-projector.env.example
    advertisement-api.env.example
    dashboard-api.env.example
    decision.env.example

  scripts/
    init.sh
    dummy.sh
    drop.sh

    postgres-init.sh
    postgres-dummy.sh
    postgres-drop.sh

    clickhouse-init.sh
    clickhouse-dummy.sh
    clickhouse-drop.sh

    redis-init.sh
    redis-dummy.sh
    redis-drop.sh

    lib/
      env.sh
      docker.sh
      wait.sh

  postgres/
    schema.sql
    dummy.sql

  clickhouse/
    schema.sql
    dummy.sql

  redis/
    contract.md
    dummy.redis

  docs/
    local-development.md
    ai-patch-guide.md
```

## Top-level Scripts

### `scripts/init.sh`

전체 DB를 띄우고 schema를 적용합니다.

```bash
./scripts/init.sh local
```

동작:

1. `environments/local.env`를 읽습니다.
2. `docker compose up -d postgres clickhouse redis`를 실행합니다.
3. Postgres readiness를 기다립니다.
4. ClickHouse readiness를 기다립니다.
5. Redis readiness를 기다립니다.
6. `scripts/postgres-init.sh local`을 실행합니다.
7. `scripts/clickhouse-init.sh local`을 실행합니다.
8. `scripts/redis-init.sh local`을 실행합니다.

### `scripts/dummy.sh`

전체 DB에 dummy data를 넣습니다.

```bash
./scripts/dummy.sh local
```

동작:

1. `scripts/postgres-dummy.sh local`을 실행합니다.
2. `scripts/clickhouse-dummy.sh local`을 실행합니다.
3. `scripts/redis-dummy.sh local`을 실행합니다.

### `scripts/drop.sh`

전체 DB를 삭제합니다.

```bash
./scripts/drop.sh local
```

동작:

1. `docker compose down -v`를 실행합니다.
2. 필요하면 `tmp/` 아래 생성물을 삭제합니다.
3. DB volume을 완전히 지워 깨끗한 상태로 되돌립니다.

## Per-DB Scripts

각 DB만 따로 조작할 수 있어야 합니다. 개발 중 ClickHouse schema만 바꿨다면 전체를 밀지 않고 ClickHouse만 재생성할 수 있어야 합니다.

### Postgres

```bash
./scripts/postgres-init.sh local
./scripts/postgres-dummy.sh local
./scripts/postgres-drop.sh local
```

`postgres-init.sh` 동작:

1. `docker compose up -d postgres`
2. Postgres readiness 대기
3. `postgres/schema.sql` 적용

예상 구현:

```bash
docker compose exec -T postgres \
  psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --file /contract/postgres/schema.sql
```

`postgres-dummy.sh` 동작:

1. Postgres readiness 대기
2. `postgres/dummy.sql` 적용

`postgres-drop.sh` 동작:

1. Postgres container 중지
2. Postgres volume 삭제

주의:

- 초기에는 `sqldef`를 쓰지 않습니다.
- `schema.sql`은 전체 schema source of truth입니다.
- 정확한 상태가 필요하면 `postgres-drop -> postgres-init -> postgres-dummy`를 사용합니다.

### ClickHouse

```bash
./scripts/clickhouse-init.sh local
./scripts/clickhouse-dummy.sh local
./scripts/clickhouse-drop.sh local
```

`clickhouse-init.sh` 동작:

1. `docker compose up -d clickhouse`
2. ClickHouse readiness 대기
3. `clickhouse/schema.sql` 적용

예상 구현:

```bash
docker compose exec -T clickhouse \
  clickhouse-client \
  --multiquery \
  --queries-file /contract/clickhouse/schema.sql
```

`clickhouse-dummy.sh` 동작:

1. ClickHouse readiness 대기
2. `clickhouse/dummy.sql` 적용

`clickhouse-drop.sh` 동작:

1. ClickHouse container 중지
2. ClickHouse volume 삭제

주의:

- ClickHouse는 migration framework 없이 SQL 파일과 reset 흐름으로 관리합니다.
- schema 변경이 꼬이면 `clickhouse-drop -> clickhouse-init -> clickhouse-dummy`가 정답입니다.
- Materialized view나 aggregate table 변경도 초기에는 drop/recreate 기준으로 갑니다.

### Redis

```bash
./scripts/redis-init.sh local
./scripts/redis-dummy.sh local
./scripts/redis-drop.sh local
```

`redis-init.sh` 동작:

1. `docker compose up -d redis`
2. Redis readiness 대기
3. 필요 시 Redis base key namespace를 초기화합니다.

Redis는 schema가 없으므로 `redis-init.sh`는 보통 readiness 확인만 수행합니다.

`redis-dummy.sh` 동작:

1. Redis readiness 대기
2. `redis/dummy.redis` 적용

예상 구현:

```bash
docker compose exec -T redis \
  redis-cli < redis/dummy.redis
```

`redis-drop.sh` 동작:

1. Redis container 중지
2. Redis volume 삭제

주의:

- Redis의 핵심 계약은 `redis/contract.md`입니다.
- key namespace, value format, TTL, owner service, reader service를 반드시 문서화합니다.
- dummy data는 `dummy.redis`에 Redis CLI 명령 형태로 둡니다.

## Environment Files

`environments/local.env`는 data contract repo script가 사용하는 값입니다.

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=loopad
POSTGRES_USER=loopad
POSTGRES_PASSWORD=loopad

CLICKHOUSE_HOST=localhost
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_NATIVE_PORT=9000
CLICKHOUSE_DATABASE=loopad
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=

REDIS_HOST=localhost
REDIS_PORT=6379
```

각 서버 repo용 `.env.example`은 runtime contract의 env 이름을 사용합니다.

예: `env/advertisement-api.env.example`

```env
LOOPAD_ENV=local
LOOPAD_SERVICE_ID=advertisement-api
LOOPAD_RUNTIME=go
LOOPAD_COMPUTE_TARGET=local
PORT=8080

LOOPAD_AURORA_HOST=localhost
LOOPAD_AURORA_PORT=5432
LOOPAD_AURORA_DATABASE=loopad
LOOPAD_AURORA_USERNAME=loopad
LOOPAD_AURORA_PASSWORD=loopad

LOOPAD_REDIS_URL=redis://localhost:6379
```

## Docker Compose

초기 compose service는 세 개만 둡니다.

```text
postgres
clickhouse
redis
```

권장 port:

| Service | Host Port | Container Port |
|---|---:|---:|
| Postgres | `5432` | `5432` |
| ClickHouse HTTP | `8123` | `8123` |
| ClickHouse Native | `9000` | `9000` |
| Redis | `6379` | `6379` |

repo root를 container에 `/contract`로 mount합니다. 이렇게 하면 host에 DB client를 설치하지 않고 container 안에서 `/contract/postgres/schema.sql` 같은 파일을 읽을 수 있습니다.

## Data Files

### `postgres/schema.sql`

Postgres 전체 schema source of truth입니다.

작성 원칙:

- `CREATE TABLE IF NOT EXISTS`를 사용합니다.
- local reset 기준이므로 복잡한 migration은 만들지 않습니다.
- enum, index, foreign key도 이 파일에 포함합니다.

### `postgres/dummy.sql`

로컬 개발에 필요한 최소 dummy data입니다.

작성 원칙:

- id는 사람이 읽기 쉬운 deterministic 값으로 둡니다.
- seed를 여러 번 적용해도 깨지지 않도록 `ON CONFLICT DO NOTHING`을 우선 사용합니다.

### `clickhouse/schema.sql`

ClickHouse database, table, materialized view source of truth입니다.

작성 원칙:

- `CREATE DATABASE IF NOT EXISTS loopad`
- `CREATE TABLE IF NOT EXISTS`
- local/dev는 drop 후 재생성을 기본으로 합니다.
- event table, context table, aggregate table을 한 파일에 두되 주석으로 구획을 나눕니다.

### `clickhouse/dummy.sql`

raw event와 aggregate query 테스트용 dummy data입니다.

작성 원칙:

- 최소한 Event Collector, Projector, Dashboard API가 모두 조회 가능한 흐름을 포함합니다.
- 시간 값은 고정된 timestamp를 사용합니다.

### `redis/contract.md`

Redis key contract 문서입니다.

각 key는 아래 형식으로 적습니다.

```md
## ad-context:{user_id}

Owner: ad-context-projector
Readers: advertisement-api
Type: JSON
TTL: 300s

Example:
{
  "userId": "u_001",
  "segments": ["sports", "mobile"],
  "updatedAt": "2026-06-25T12:00:00Z"
}
```

### `redis/dummy.redis`

Redis CLI 명령 파일입니다.

```redis
SETEX ad-context:u_001 300 '{"userId":"u_001","segments":["sports","mobile"],"updatedAt":"2026-06-25T12:00:00Z"}'
SETEX advertisement-cache:u_001:slot_main 60 '{"campaignId":"c_001","creativeId":"cr_001"}'
```

## AI Patch Guide

`docs/ai-patch-guide.md`에 빠른 보완 원칙을 둡니다.

기본 원칙:

1. 확실한 방법은 항상 `drop -> init -> dummy`입니다.
2. drop이 느릴 때만 임시 patch를 만듭니다.
3. patch는 `tmp/patches/YYYYMMDD-description.sql` 또는 `tmp/patches/YYYYMMDD-description.redis`에 둡니다.
4. patch 적용 후 기준 파일도 반드시 수정합니다.
5. 상태가 꼬이면 patch를 버리고 다시 `drop -> init -> dummy`로 돌아갑니다.

AI에게 시킬 작업 예시:

```text
postgres/schema.sql에 새 컬럼을 추가했고, 로컬 DB를 drop하지 않고 맞추고 싶다.
현재 schema.sql 기준으로 필요한 ALTER 문을 tmp/patches에 만들고 적용해줘.
적용이 실패하면 drop/init/dummy로 돌아가도 된다.
```

## README에 넣을 기본 사용법

```bash
./scripts/init.sh local
./scripts/dummy.sh local
```

개별 DB만 다시 만들 때:

```bash
./scripts/clickhouse-drop.sh local
./scripts/clickhouse-init.sh local
./scripts/clickhouse-dummy.sh local
```

전체 초기화:

```bash
./scripts/drop.sh local
./scripts/init.sh local
./scripts/dummy.sh local
```

## 작업 순서

1. `loop-ad_data_contract` repo 생성
2. `docker-compose.yml` 작성
3. `environments/local.env` 작성
4. 공통 script helper 작성
   - `scripts/lib/env.sh`
   - `scripts/lib/docker.sh`
   - `scripts/lib/wait.sh`
5. Postgres script 작성
   - `postgres-init.sh`
   - `postgres-dummy.sh`
   - `postgres-drop.sh`
6. ClickHouse script 작성
   - `clickhouse-init.sh`
   - `clickhouse-dummy.sh`
   - `clickhouse-drop.sh`
7. Redis script 작성
   - `redis-init.sh`
   - `redis-dummy.sh`
   - `redis-drop.sh`
8. top-level script 작성
   - `init.sh`
   - `dummy.sh`
   - `drop.sh`
9. Postgres schema/dummy 작성
10. ClickHouse schema/dummy 작성
11. Redis contract/dummy 작성
12. 각 서버 repo용 `.env.example` 작성
13. README 작성
14. 인프라 repo의 `docs/app-repository-guide.md`와 env 이름 대조

## 완료 기준

아래 명령이 macOS에서 성공해야 합니다.

```bash
./scripts/drop.sh local
./scripts/init.sh local
./scripts/dummy.sh local

./scripts/postgres-drop.sh local
./scripts/postgres-init.sh local
./scripts/postgres-dummy.sh local

./scripts/clickhouse-drop.sh local
./scripts/clickhouse-init.sh local
./scripts/clickhouse-dummy.sh local

./scripts/redis-drop.sh local
./scripts/redis-init.sh local
./scripts/redis-dummy.sh local
```

그리고 각 DB에 대해 smoke query가 성공해야 합니다.

```text
Postgres: SELECT 1;
ClickHouse: SELECT 1;
Redis: PING
```
