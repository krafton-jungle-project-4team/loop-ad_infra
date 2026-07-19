# HAProxy를 기본 연결 경로로 유지하는 이유

## 결정

이 저장소의 connection-path 구성은 `NLB -> HAProxy -> collector`를 기본 이벤트 경로로 사용한다. 성능 테스트의 기본 프로파일은 정상 HTTP 202 로그를 1/1000로 샘플링하고 자동 스레드 선택을 쓰는 `sampled-202`다. collector 목록 관리는 ECS service discovery와 AWS Cloud Map SRV 레코드에 맡기고, HAProxy는 DNS 기반 `server-template`로 backend를 갱신한다.

50k RPS 채택 근거, 엄격한 최종 게이트와 설계 채택을 분리한 판정, 고정 topology는 [50k RPS connection path 기본 설계와 검증 근거](explanation_connection_path_50k_baseline.md)에 정리한다.

## 선택한 관리 모델

HAProxy 공식 문서는 `resolvers`와 `server-template`를 사용해 SRV 레코드에서 backend를 동적으로 발견하는 구성을 제공한다. `init-addr`를 사용하면 backend가 아직 등록되지 않은 시작 시점에도 HAProxy를 기동할 수 있다. 이 방식은 현재 구성의 `init-addr last,libc,none`과 일치한다.

AWS 공식 문서에 따르면 ECS의 `bridge` 또는 `host` network mode에서는 service discovery에 SRV 레코드를 사용해야 하며, ECS가 task 상태와 container health에 따라 Cloud Map 등록과 해제를 관리한다. 따라서 애플리케이션 배포와 backend 등록을 별도 운영 절차로 분리할 필요가 없다.

## 다른 관리 방식과 비교

### Runtime API

HAProxy Runtime API는 실행 중 backend를 추가하거나 수정할 수 있지만, 공식 문서상 동적 서버는 reload 후 복원되지 않는다. 별도 상태 저장과 재적용 절차가 필요하므로 기본 운영 경로로 사용하지 않는다.

### Data Plane API

Data Plane API는 영구 설정 변경에 적합하지만 별도 관리 프로세스, 인증, 상태 저장, 배포 책임을 추가한다. 현재 요구사항은 4대와 6대 사이의 ECS task 변경이므로 DNS service discovery보다 운영 비용이 크다.

### ECS Service Connect

Service Connect는 ECS 서비스 간 연결을 관리하지만 클라이언트 task 배포와 별도 프록시 계층을 전제로 한다. HAProxy 사용이 필수인 현재 경로에서는 기능이 중복되므로 Cloud Map을 직접 소비하는 편이 단순하다.

## `server-template` 슬롯을 8개로 고정한 이유

4대와 6대 스케일을 같은 HAProxy 설정으로 처리하기 위해 슬롯을 8개로 고정한다. AWS 공식 ECS service discovery 문서는 healthy 레코드가 8개 이하일 때 DNS 질의에 모든 healthy 레코드를 반환한다고 설명한다. 지원 범위를 8대 이하로 제한하면 일부 backend만 보이는 상태를 피하면서 HAProxy 재설정 없이 스케일할 수 있다.

## 운영상 남는 책임

- HAProxy 이미지 digest와 TLS secret 전달 계약을 고정한다.
- Cloud Map instance 수, HAProxy active backend 수, ECS desired/running 수를 함께 감시한다.
- HAProxy 두 노드의 Prometheus/stats 수집을 유지한다.
- 8대를 넘는 scale-out은 DNS 응답 특성과 HAProxy topology를 다시 검증한 뒤 별도 설계한다.

## 공식 참고 자료

- [HAProxy DNS resolution and SRV service discovery](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/proxying-essentials/dns-resolution/)
- [HAProxy `server-template` configuration reference](https://www.haproxy.com/documentation/haproxy-configuration-manual/new/2-1r1/)
- [HAProxy Runtime API dynamic server behavior](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/add-server/)
- [Amazon ECS service discovery](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-discovery.html)
- [Amazon ECS ServiceRegistry API](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ServiceRegistry.html)
- [Amazon ECS service connectivity options](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-connecting-services.html)
