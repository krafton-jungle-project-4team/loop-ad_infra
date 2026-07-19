# HAProxy 기본 연결 경로 운영 가이드

## 목표

이 가이드는 이벤트 수집 경로를 다음 한 가지 기본 경로로 운영하는 방법을 설명한다.

`generator/client -> NLB -> HAProxy -> collector -> Kinesis final-ACK`

HAProxy backend IP를 직접 입력하거나 스케일 변경 때 HAProxy 설정을 수동 편집하지 않는다.

## 고정 운영 계약

- 기본 경로는 `T3_NLB_HAPROXY`다.
- 성능 테스트의 기본 HAProxy 프로파일은 `sampled-202`다. `baseline-full`과 `sampled-202-capacity`는 명시적 실험 override다.
- 기본 HAProxy fleet은 2개 AZ의 `2 × c6in.xlarge`다.
- `sampled-202`는 `nbthread`를 지정하지 않는다. 배포 후 runtime thread 수가 인스턴스의 4 vCPU와 일치해야 한다.
- HAProxy 이미지는 digest로 고정한다.
- 정상 요청 access log는 1/1000만 기록하고 400~599 오류 로그는 모두 기록한다.
- collector는 ECS 서비스가 AWS Cloud Map SRV 레코드에 자동 등록한다.
- HAProxy는 `_collector._tcp.connection-path.internal`을 조회한다.
- `server-template collector 1-8`로 4대와 6대 구성을 같은 설정으로 처리한다.
- Route 53이 모든 healthy 레코드를 한 번에 반환하도록 collector 수를 8대 이하로 제한한다.
- HAProxy backend 프로토콜은 H2C, 알고리즘은 `leastconn`, 요청 재시도는 0이다.
- 50k baseline은 oha 8대/16 processes, 12,000 physical connections, Collector `6 × c6i.xlarge`, Kinesis 120 shards다.

전체 고정값과 검증 근거는 [50k RPS connection path 기본 설계와 검증 근거](../../../evidence/explanations/explanation_connection_path_50k_baseline.md) 및 [baseline manifest](../../tools/phase1-kinesis/connection-path-performance-baseline.json)를 따른다.

## collector 수 변경

1. 기존 run의 불변 항목인 session ID, run ID, 이미지 digest, 인증서 ARN, TLS secret ARN을 그대로 사용한다.
2. CDK context의 `phase1CollectorCount`만 `4` 또는 `6`으로 변경한다.
3. `LoopAdPerfPhase1KinesisConnectionPathStack`을 배포한다.
4. 배포 검증기에 같은 collector 수를 전달한다.

```bash
node performance-tests/phase1-kinesis/verify-connection-path-deployment.mjs \
  --session-id "$SESSION_ID" \
  --run-id "$RUN_ID" \
  --image-digest "$IMAGE_DIGEST" \
  --expected-collector-count 6 \
  --expected-load-generator-count 8 \
  --haproxy-profile sampled-202 \
  --ca-certificate "$PUBLIC_CA_FILE" \
  --output "$RUN_DIR/deployment-verification-6collector.json"
```

HAProxy 설정 파일 수정, backend IP 입력, Runtime API 호출은 필요하지 않다. ECS가 task를 Cloud Map에 등록·해제하고 HAProxy DNS resolver가 TTL에 따라 반영한다.

## 배포 후 확인

다음 항목이 모두 참이어야 부하를 시작한다.

- CloudFormation stack이 `CREATE_COMPLETE` 또는 `UPDATE_COMPLETE`다.
- collector ECS service의 desired/running 수가 요청한 값과 같다.
- T1/T2 target group에 collector가 모두 healthy다.
- T3 target group에 HAProxy 두 대가 healthy다.
- Cloud Map instance 수가 collector 수와 같다.
- 모든 generator에서 NLB, HAProxy, 모든 collector까지 TLS 검증 코드가 0이고 ALPN이 `h2`다.
- HAProxy `/metrics`와 `/stats`가 두 노드 모두 응답한다.
- HAProxy 두 노드에서 runtime thread count와 `nproc`가 모두 4이고 config SHA가 배포 출력과 일치한다.

## 장애 확인 순서

1. 클라이언트 물리 연결 수와 실제 RPS를 확인한다.
2. HTTP 429와 collector `admission.rejected_total`을 확인한다.
3. HAProxy stats의 active backend 수가 Cloud Map instance 수와 같은지 확인한다.
4. collector queue depth, outstanding records, Kinesis failures/timeouts를 확인한다.
5. Cloud Map 불일치가 있으면 ECS container health와 service registry 등록 상태를 먼저 확인한다.

연결 수와 실제 RPS가 유지되면서 429가 증가하면 NLB/HAProxy 연결 부족보다 collector 애플리케이션 처리량 한계를 먼저 의심한다.

## 설정 변경이 필요한 경우

다음 변경만 HAProxy task definition 재배포 대상으로 취급한다.

- TLS 정책이나 인증서 전달 방식 변경
- frontend/backend 프로토콜 변경
- 8대를 넘는 collector 구성
- timeout, retry, balancing 알고리즘 변경

일상적인 4대↔6대 스케일 변경에는 HAProxy 관리 작업을 추가하지 않는다.
