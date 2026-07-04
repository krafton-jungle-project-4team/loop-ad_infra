# Secrets Setup

Secrets stack 배포 후 `.env.secrets`에 실제 배포 대상 값을 넣고 다음 명령으로 Secrets Manager 값을 동기화합니다.

```bash
npm run secrets:sync -- --env-file .env.secrets
```

필요한 key와 region/prefix는 `.env.secrets.example`을 기준으로 모두 명시합니다. `.env.secrets`는 커밋하지 않습니다.

API key 계열 secret은 Secrets Manager에 `{ "api_key": "..." }` 형태로 저장합니다.

`LOOPAD_DEMO_DISPATCH_RECIPIENTS`는 JSON 배열 전체를 secret string으로 저장합니다. 각 항목은 `userId`, `email`, `phoneNumber` 문자열을 포함해야 하며, `phoneNumber`는 E.164 형식을 사용합니다.

```json
[
  {
    "userId": "10",
    "email": "user@example.com",
    "phoneNumber": "+821012345678"
  }
]
```
