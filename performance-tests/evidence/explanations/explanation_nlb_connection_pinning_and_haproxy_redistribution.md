# NLB 연결 고정과 HAProxy 요청 재분산의 근거

## 핵심 조건

NLB와 HAProxy의 분산 동작은 다음 조건에서 구분해야 한다.

> NLB는 TCP/HTTP/2 연결 단위로 타깃을 고정한다. HAProxy가 HTTP 모드에서 연결을 종료하고, stickiness 없이 `leastconn` 같은 알고리즘으로 백엔드를 선택하면 같은 프런트 연결의 요청과 스트림을 여러 collector로 다시 분산할 수 있다.

HAProxy가 `mode tcp`라면 이런 요청 단위 재분산은 발생하지 않는다.

## 조사 결론과 인용 방법

2026-07-15 기준으로 다음 다섯 조건을 한꺼번에 모두 만족하는 독립 기술 블로그나 개인 블로그는 찾지 못했다.

1. AWS NLB를 사용한다.
2. NLB 뒤에 HAProxy를 둔다.
3. 장기 TCP/HTTP/2 연결 고정을 문제로 특정한다.
4. HAProxy가 동일한 downstream 연결의 요청이나 HTTP/2 스트림을 백엔드에 다시 분산한다고 명시한다.
5. 이 변경의 전후 결과가 글의 주제이며 실측값이 있다.

가장 강한 근거는 역할을 나눠 결합해야 한다.

- TripleLift 사례로 `NLB -> HAProxy -> backends`, 장기 연결에 따른 NLB 불균형, 실운영 개선 결과를 근거로 삼는다.
- Achievers와 Kubernetes/Buoyant 실험으로 연결 단위 고정을 L7 프록시가 요청 단위 분산으로 바꾸는 작동과 개선 결과를 근거로 삼는다.
- HAProxy 설정 매뉴얼과 HTTP/2 기술 블로그로 해당 작동이 `mode http` 및 non-sticky 백엔드 선택에서 가능함을 확인한다.

TripleLift 글만으로 "HAProxy가 하나의 HTTP/2 연결에서 나온 각 요청을 다른 백엔드로 보내서 개선했다"고 단정하면 근거를 넘어선다. 해당 글은 HAProxy의 capacity-aware 가중치 제어와 AZ sharding을 핵심 개선으로 다루고, 백엔드 HTTP/2는 향후 과제로 남겨 둔다.

## AWS 공식 자료

### 1. [NLB 트래픽이 타깃 사이에서 불균등하게 분산되는 문제](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-troubleshooting.html)

가장 직접적인 근거다.

- NLB가 flow hash로 타깃을 선택한다.
- 하나의 클라이언트 연결은 본질적으로 sticky하다.
- HTTP keep-alive를 사용하면 기존 연결이 원래 타깃에 계속 남는다.
- AWS는 타깃별 요청 수가 아니라 VPC Flow Logs의 고유 연결 수를 비교하라고 권고한다.

즉, 적은 수의 장기 TCP/HTTP/2 연결로 많은 요청을 전송하면 collector 수를 늘려도 기존 연결은 새 collector로 옮겨지지 않는다.

### 2. [ECS 서비스에 NLB와 L7 ingress를 함께 사용하는 아키텍처](https://aws.amazon.com/blogs/containers/load-balancing-amazon-ecs-services-with-a-kubernetes-ingress-controller-style-approach/)

AWS가 공개한 가장 가까운 구조적 선례다.

- NLB는 TCP 전송 계층에서만 라우팅한다.
- NLB 뒤에 NGINX를 배치해 HTTP 요청을 L7에서 백엔드 ECS 서비스로 라우팅한다.
- 즉 `NLB -> L7 reverse proxy -> ECS backends` 구성을 AWS가 실제 아키텍처로 제시한다.

다만 이 문서는 connection skew 개선보다는 다중 ECS 서비스 라우팅이 주목적이다.

### 3. [NLB 앞단에서 HAProxy/NGINX와 Proxy Protocol v2 사용](https://aws.amazon.com/blogs/networking-and-content-delivery/preserving-client-ip-address-with-proxy-protocol-v2-and-network-load-balancer/)

AWS가 NLB의 타깃으로 HAProxy를 직접 배포하는 CloudFormation 예제를 제공한다.

- `NLB -> HAProxy`는 AWS가 문서화한 지원 구성이다.
- Envoy, Traefik 같은 프록시도 같은 방식으로 사용할 수 있다고 설명한다.
- 다만 이 문서의 목적은 성능 개선이 아니라 클라이언트 IP 보존이다.

따라서 "NLB 뒤에 HAProxy를 두는 것이 공식적으로 가능한가?"에 대한 근거이지, "HAProxy가 connection skew의 공식 해법이다"라는 근거는 아니다.

### 4. [ALB Target Optimizer와 타깃 hot spot](https://aws.amazon.com/blogs/networking-and-content-delivery/drive-application-performance-with-application-load-balancer-target-optimizer/)

HAProxy 구성과 동일하지는 않지만, AWS가 문제 자체를 어떻게 보는지 보여준다.

- 특정 타깃에 동시 요청이 과도하게 몰리면 hot spot이 발생한다.
- 다른 타깃은 유휴 상태인데 일부 타깃만 큐와 오류를 만들 수 있다.
- 그 결과 재시도와 지연시간이 증가한다.
- AWS는 이를 요청 동시성 단위의 균등 분산으로 완화한다.

즉 "연결 수는 비슷해 보여도 진행 중인 요청 수가 불균형하면 p95가 악화된다"는 공식 근거다.

## HAProxy 공식 자료

### 5. [HAProxy 3.2 Configuration Manual](https://docs.haproxy.org/3.2/configuration.html)

현재 설명을 가장 직접적으로 뒷받침한다.

- HTTP 모드에서는 요청과 응답을 개별적으로 분석한다.
- `roundrobin`, `leastconn`은 비결정적 알고리즘이므로 연속된 요청이 같은 서버로 간다고 보장하지 않는다.
- `http-reuse`는 백엔드 연결 풀을 여러 요청이 재사용하도록 한다.
- 따라서 generator의 긴 연결과 collector 연결을 분리할 수 있다.

특히 `http-reuse always`는 새로운 세션의 첫 요청도 기존 백엔드 연결로 보낼 수 있다. 단, HAProxy 문서도 안정적인 내부 백엔드처럼 기존 연결이 갑자기 끊기지 않는 환경에서만 사용하라고 경고한다.

### 6. [Power of Two와 Least Connections 벤치마크](https://cdn.haproxy.com/blog/power-of-two-load-balancing)

HAProxy 창시자 Willy Tarreau가 작성한 비교 실험이다.

- `leastconn`은 느리거나 이미 바쁜 서버를 계속 선택할 가능성을 낮춘다.
- 해당 실험에서는 중간 이상의 자원 경합 상황에서 round-robin보다 요청 처리량과 응답시간이 약 4% 개선됐다.
- 중앙 집중식 로드밸런서는 분산된 여러 프록시보다 약 3% 더 좋았다.
- 핵심 이유는 중앙 프록시가 백엔드 전체의 현재 부하를 보고 선택할 수 있기 때문이다.

수치는 해당 벤치마크에만 해당하며 현재 시스템에 그대로 적용할 수는 없다.

### 7. [HTTP keep-alive, multiplexing, connection pooling](https://www.haproxy.com/blog/http-keep-alive-pipelining-multiplexing-and-connection-pooling)

HAProxy 프록시가 클라이언트 연결과 백엔드 연결을 분리해 다루는 방식을 설명하고 재현 가능한 벤치마크를 제시한다.

- HTTP/2에서 여러 요청은 하나의 지속 연결 안에서 병렬 스트림으로 전송된다.
- HAProxy는 백엔드별 연결 풀을 관리하고, `http-reuse`에 따라 후속 요청을 기존 백엔드 연결에 보낼 수 있다.
- 지속 HTTP/2 클라이언트 시나리오에서 `safe`는 1분간 59,000건과 최대 백엔드 연결 50개, `always`는 78,000건과 최대 연결 4개를 기록했다.

이 수치는 연결 재사용의 효과이지 백엔드 간 분산 개선 수치가 아니다. 다만 한 downstream 연결과 upstream 연결 풀이 1:1로 고정되지 않는다는 HAProxy 내부 모델을 보완한다.

## 실운영 기술 블로그와 개인 블로그

작성자와 운영 배경이 확인되고, 설정·재현 코드·전후 그래프 중 하나 이상을 공개하며, 주장의 한계를 확인할 수 있는 글만 채택했다. 출처가 불분명한 재가공 글과 실측이 없는 SEO 중심 글은 제외했다.

### 8. [TripleLift의 custom load balancing 운영 사례](https://aws.amazon.com/blogs/industries/how-triplelift-optimized-real-time-bidding-with-custom-load-balancing-spot-and-graviton/)

AWS 블로그에 게시됐지만 TripleLift의 Principal Software Engineer와 Senior Engineering Manager가 공동 저자로 참여한 4년간의 실운영 회고다. 현재 구성과 가장 가깝고 실측 결과도 있어 최우선으로 인용할 수 있다.

- NLB가 HAProxy 인스턴스 그룹을 앞에서 L4로 분산하고 HAProxy가 Exchange 서버로 보내는 구조다.
- NLB의 AZ 간 트래픽 불균형 원인으로 round-robin DNS caching과 long-lived connections을 지목한다.
- HAProxy agent의 비례 제어와 AZ sharding 적용 후 서버 CPU 최댓값-최솟값 차이가 5% 미만으로 수렴했다.
- 느린 서버는 더 적은 RPS, 빠른 서버는 더 많은 RPS를 받게 됐고, 전체 아키텍처의 월간 비용은 23~40%, 연간 비용은 200만 달러 이상 절감됐다.

한계도 명확하다. 개선은 HAProxy 자체만의 단일 변수가 아니라 Spot, Graviton, custom agent, AZ sharding이 합쳐진 결과다. 또한 동일 HTTP/2 연결의 개별 요청을 HAProxy가 다른 백엔드로 보냈다고 명시하지 않는다.

### 9. [Achievers의 Kubernetes 부하 테스트와 Istio 분산 개선](https://newrelic.com/blog/log/load-testing-kubernetes-achievers)

Achievers의 Principal SRE가 작성한 기업 기술 블로그로, 이 문서의 핵심 가설과 가장 가까운 독립 실운영 사례다.

- gRPC의 장기 TCP 세션 때문에 scale-out 후에도 클라이언트가 기존 pod에 남아 불균형이 발생했다.
- 실제 대시보드에서 pod별 RPS가 불균형하고 트래픽을 받지 못한 pod도 있었다.
- Istio/Envoy의 `LEAST_REQUEST`를 적용한 후 pod별 요청이 고르게 분산됐고 전체 서비스 처리량이 크게 증가했다.

글 전체의 4배 처리량 개선은 NAT, 애플리케이션 코드, 데이터베이스 등 여러 병목을 같이 제거한 결과다. 이 4배를 L7 분산 하나의 효과로 인용하면 안 된다. 또한 NLB와 HAProxy가 아닌 Kubernetes Service와 Envoy를 사용했다.

### 10. [Kubernetes/Buoyant의 gRPC Load Balancing on Kubernetes without Tears](https://kubernetes.io/blog/2018/11/07/grpc-load-balancing-on-kubernetes-without-tears/)

Buoyant의 William Morgan이 작성한 기술 블로그로, 연결 단위 분산을 요청 단위 분산으로 바꾸는 것이 글의 메인 주제다.

- HTTP/2의 하나의 장기 TCP 연결이 모든 gRPC 요청을 하나의 pod에 고정하는 현상을 CPU 그래프로 보여 준다.
- 해결을 connection balancing에서 request balancing, 즉 L3/L4에서 L5/L7로의 이동으로 정의한다.
- Linkerd 프록시 적용 전에는 한 pod만 일했고, 적용 후에는 모든 pod가 트래픽을 받아 각각 약 5 RPS를 처리했다.

AWS NLB와 HAProxy 사례는 아니지만, "L4 연결 고정을 L7 프록시의 요청 단위 선택으로 해소한다"는 원리와 전후 차이를 가장 깔끔하게 입증한다.

### 11. [Homayoon Alimohammadi의 gRPC name resolution 재현 글](https://dev.to/homayoonalimohammadi/grpc-name-resolution-load-balancing-on-kubernetes-everything-you-need-to-know-and-probably-a-bit-more-3if6)

수백 개의 Python과 Go 마이크로서비스를 운영하던 중 발생한 장애에서 출발한 개인 기술 글이다.

- gRPC pod rolling update 때 1분 미만의 일시적 장애를 관찰했고, 게시한 그래프를 기준으로 실패율을 약 5%로 추정했다.
- connection-level Kubernetes 분산이 HTTP/2 장기 연결 이후에는 더 이상 작동하지 않는 과정을 설명한다.
- GitHub 예제 저장소를 제공하고, Linkerd를 주입하면 요청이 모든 서버 pod로 분산되는 실습 절차를 제공한다.

현재 구성과 다른 Kubernetes/Linkerd 사례이고 수치가 엄격한 성능 벤치마크는 아니다. 그럼에도 운영 동기, 관찰 자료, 예제 코드가 모두 있어 개인 블로그 중에서는 비교적 신뢰도가 높다.

### 12. [Mark Vincze의 Envoy 운영과 least-request 벤치마크](https://blog.markvincze.com/how-to-use-envoy-as-a-load-balancer-in-kubernetes/)

개인 블로그이지만 실제 운영 규모, 설정, 벤치마크 방법과 결과 그래프를 공개한다.

- 현실의 CPU-intensive API에서 우연히 여러 요청이 한 node에 모이면 round-robin의 평균 응답 시간이 악화되는 문제를 동기로 제시한다.
- Envoy `LEAST_REQUEST`로 트래픽을 더 고르게 분산하고 고부하에서 평균 응답 시간을 낮춘 약 40분의 벤치마크를 제시한다.
- 실제 서비스에서 초당 약 1,000건, upstream pod 약 400개를 Envoy 3개와 약 10% CPU로 처리했다고 보고한다.

이 글은 장기 연결 고정을 주제로 하지 않는다. 따라서 `leastconn` 또는 `least-request`가 부하 편차를 줄이는 보조 근거로만 쓴다.

### 13. [Learnkube의 장기 연결 분산 해설](https://learnkube.com/kubernetes-long-lived-connections)

독립 Kubernetes 교육 사이트의 심층 글이다. Kubernetes Service가 장기 연결을 요청 단위로 다시 분산하지 못하며, HTTP/2와 gRPC에서는 client-side load balancing 또는 프록시를 고려하라고 설명한다.

그림과 단계별 설명이 명확해 원리 설명에는 좋지만, 프록시 적용 전후의 성능 수치는 없다.

### 14. [Datadog의 대규모 gRPC mesh 운영 회고](https://www.datadoghq.com/blog/grpc-at-datadog/)

2만 7천여 고객, 수만 개 pod, 초당 수천만 요청 규모의 실운영 기술 블로그다.

- Kubernetes의 TCP 단위 분산과 gRPC `pick_first`가 특정 pod에 요청, CPU, 메모리를 몰리게 하는 그래프를 제시한다.
- headless Service와 gRPC `round_robin`으로 바꾸자 pod별 요청이 균등하게 변한 전후 그래프를 제시한다.

프록시가 아닌 client-side load balancing을 선택한 사례라서 현재 구조의 직접 근거로는 쓸 수 없다. 다만 장기 연결 고정의 운영 영향과 "연결이 아닌 각 RPC를 분산해야 한다"는 비교 근거로 신뢰도가 높다.

## 다른 기업과 프로젝트의 유사 자료

### 15. [F5/NGINX의 AWS NLB + NGINX Plus 구성](https://docs.nginx.com/nginx/deployment-guides/amazon-web-services/high-availability-network-load-balancer/)

현재 구조와 매우 유사하다.

- AWS NLB는 L4 connection-level 분산을 담당한다.
- NGINX Plus는 L7 HTTP 요청 분산을 담당한다.
- 문서 자체가 두 계층을 결합하는 이유를 명시적으로 구분한다.

HAProxy 대신 NGINX를 사용했을 뿐, `NLB -> L7 proxy -> application servers`라는 원리는 같다.

### 16. [Envoy Connection Pooling](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/connection_pooling)

Envoy도 downstream 연결과 upstream 연결을 분리한다.

- HTTP/1 요청은 사용 가능한 백엔드 풀 연결에 배정된다.
- HTTP/2에서는 하나의 백엔드 연결에 여러 요청을 multiplex한다.
- 스트림 한도에 도달하면 필요한 만큼 추가 연결을 생성한다.

HAProxy의 HTTP connection reuse가 특이한 편법이 아니라 현대적인 L7 프록시의 일반적인 구조라는 근거다.

### 17. [Envoy Least Request 정책](https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/load_balancing_policies/least_request/v3/least_request.proto)

Envoy는 활성 요청이 적은 백엔드를 선택하는 `LEAST_REQUEST` 정책을 제공한다.

HAProxy `leastconn`과 측정 단위는 조금 다르지만, "현재 부하 정보를 이용해 이미 바쁜 타깃을 피한다"는 목적은 같다. HTTP/2에서는 연결 수보다 활성 요청과 스트림 수가 실제 부하를 더 잘 표현할 수 있다는 점도 중요하다.

### 18. [Linkerd/Buoyant의 Beyond Round Robin: Load Balancing for Latency](https://linkerd.io/2016/03/16/beyond-round-robin-load-balancing-for-latency/)

유명한 L7 로드밸런싱 글이다.

- round-robin, least-loaded, peak EWMA를 비교한다.
- 느린 백엔드 한 대가 섞이면 round-robin은 p95부터 크게 영향을 받았다.
- least-loaded는 p99까지, peak EWMA는 p99.9까지 영향을 더 잘 억제했다.
- Twitter의 대규모 운영 실험에서도 이런 방식이 검증됐다고 설명한다.

현재 `leastconn` 가설과 가장 가까운 외부 자료다.

### 19. [Cloudflare Pingora 프록시 아키텍처](https://blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/)

connection reuse가 실제 p95에 미치는 영향을 보여주는 강한 사례다.

- 기존 NGINX는 worker별로 연결 풀이 분리돼 연결 재사용이 불균형했다.
- Pingora는 스레드 전체가 백엔드 연결을 공유하도록 변경했다.
- Cloudflare 측정에서 median TTFB가 5ms, p95가 80ms 감소했다.
- 신규 백엔드 연결은 기존 대비 약 3분의 1로 줄었다.

NLB pinning 문제와 정확히 같지는 않지만, "프록시가 요청과 백엔드 연결을 분리하고 연결 풀을 공유하면 tail latency가 줄어든다"는 실운영 사례다.

## L4 connection pinning의 기본 원리를 설명하는 자료

### 20. [Meta Katran L4 Load Balancer](https://engineering.fb.com/2018/05/22/open-source/open-sourcing-katran-a-scalable-network-load-balancer/)

Meta의 Katran도 5-tuple consistent hash를 사용한다. 동일한 TCP 연결의 모든 패킷을 같은 백엔드로 보내는 것이 L4 로드밸런서의 의도된 동작이라고 설명한다.

### 21. [Google Maglev](https://research.google/pubs/maglev-a-fast-and-reliable-software-network-load-balancer/)

Google의 대표적인 L4 로드밸런서 논문이다. consistent hashing과 connection tracking으로 연결 지향 프로토콜의 백엔드 일관성을 유지한다.

둘 다 NLB의 동작이 AWS만의 특이한 제한이 아니라 L4 로드밸런서의 일반적인 설계라는 근거다.

### 22. [Google The Tail at Scale](https://research.google/pubs/the-tail-at-scale/)

직접적인 로드밸런서 문서는 아니지만, 일부 서버의 일시적인 큐와 지연이 전체 시스템의 p95와 p99를 지배하는 이유를 설명하는 대표 논문이다.

## 최종 판단

- **직접 확인됨:** NLB는 연결 단위로 sticky하며 장기 keep-alive는 불균등 분산을 지속시킬 수 있다.
- **직접 확인됨:** HTTP 모드 HAProxy는 연속 요청마다 다른 백엔드를 선택할 수 있고 백엔드 연결을 요청 사이에서 재사용할 수 있다.
- **공식 구조 선례 있음:** AWS와 NGINX 모두 `NLB -> L7 proxy -> backends` 구조를 문서화했다.
- **실운영 선례 있음:** TripleLift는 `NLB -> HAProxy -> backends`에서 장기 연결을 NLB 불균형의 원인 중 하나로 지목했고, capacity-aware HAProxy와 AZ sharding 후 CPU 편차와 비용을 줄인 결과를 공개했다.
- **독립 유사 사례 있음:** Achievers와 Buoyant의 기술 블로그는 HTTP/2/gRPC 연결 고정을 L7 프록시의 요청 단위 선택으로 완화한 전후 그래프를 제시한다.
- **공식 AWS 권고라고 할 수 없음:** AWS가 "NLB connection pinning 문제에는 HAProxy를 넣어라"라고 직접 권고한 문서는 찾기 어렵다.
- **단일 출처로 단정할 수 없음:** TripleLift 사례는 동일 HTTP/2 연결의 request-level fan-out을 직접 증명하지 않으며, 다른 기술 블로그는 NLB와 HAProxy를 사용하지 않는다. 따라서 여러 근거를 역할별로 결합해야 한다.
- **실험으로 별도 입증해야 함:** 실제 p95 개선이 HAProxy 재분산 때문인지, generator 수, 연결 수, collector 용량 변화 때문인지는 collector별 활성 요청, 큐, 요청 수 분포를 비교해야 확정할 수 있다.
