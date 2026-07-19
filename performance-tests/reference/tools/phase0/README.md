# Phase 0: ALB Fixed Response

Phase 0은 collector와 Kafka 없이 load generator와 ALB 한계만 본다.

구성:

- `perf-phase0` CDK stack이 internal ALB와 ECS on EC2 load generator capacity를 만든다.
- 로드 생성기는 public subnet의 EC2 인스턴스 4대로 시작한다.
- 기본 인스턴스 타입은 `c6in.xlarge`다. 4대를 쓰면 16 vCPU이며, 먼저 이 구성 안에서 부하 변수를 소진한다.
- `c6in.2xlarge` 같은 더 큰 구성은 16 vCPU 생성기 한계가 실측되고 다른 변수를 소진한 뒤에만 사용하며, 전체 EC2 On-Demand Standard vCPU quota 32를 넘지 않는다.
- 필요하면 `phase0LoadGeneratorInstanceType`, `phase0LoadGeneratorDesiredCapacity`, `phase0LoadGeneratorSpotPrice` CDK context로 바꾼다.
- Artillery는 EC2 인스턴스에 SSM으로 접속한 뒤 sweep script로 실행한다.
- EC2 host에 Node/npm이 있으면 `npx artillery@latest`를 쓰고, 없으면 Docker image `artilleryio/artillery:latest`를 사용한다.
- sweep script는 각 step 뒤 `worker_*.json`을 `jq`로 집계하고, 204가 아닌 결과나 socket timeout이 있으면 실패로 처리한다.
- 기본 `c6in.xlarge` churn sweep은 Artillery 프로세스를 최대 4개로 제한한다. 더 큰 host sweep은 `MAX_PROCESSES`와 `SWEEP_STEPS`를 함께 명시해야 한다.
- 실제 사용자에 가까운 고부하 모델은 `run-ec2-artillery-keepalive-multiprocess-worker.sh`를 사용한다.
- 각 EC2 host는 기본 5개 Artillery process를 띄우고, process당 `625` VU가 VU당 `0.15`초 간격으로 POST를 반복한다.
- Artillery payload는 `loop-ad_event_sdk`의 `hotel_rec_promo.v1` envelope를 따른다.
- SDK envelope 필드의 근거 데이터는 `payloads/sdk-compatible-events.tsv`에 보존한다.
- `generate-sdk-compatible-event-bodies.mjs`가 완성된 HTTP JSON body를 `payloads/sdk-compatible-event-bodies.tsv`로 오프라인 직렬화한다.
- 요청 시점에는 Artillery가 pre-serialized body row를 순환하며 가벼운 문자열 치환만 수행한다.
- Payload 크기는 `compact`, `standard`, `expanded` profile row를 섞어 대략 1.0~1.5 KiB 범위를 만든다.
- oha 비교 실행은 `generate-oha-body-pool.mjs`가 만든 12줄 NDJSON pool을 `-Z`로 읽어 요청마다 한 줄을 무작위 선택한다.
- `run-ec2-oha-worker.sh`는 `QUERY_PER_SECOND`가 0보다 크면 oha `-q`와 `--latency-correction`을 적용한다. 0이면 무제한으로 실행한다.
- multi-process worker는 5초 간격으로 host CPU counter, NIC byte counter, memory, `/proc/net/sockstat`, `ss -s`, Artillery process CPU/RSS를 run 폴더에 기록한다.

근거 payload를 변경한 경우 body pool을 다시 만든 뒤 Phase 0 테스트로 전 행의 schema와 byte 수를 검증한다.

```bash
node performance-tests/phase0/generate-sdk-compatible-event-bodies.mjs
node performance-tests/phase0/generate-oha-body-pool.mjs
npm test -- --runTestsByPath test/perf-phase0.test.ts
```

Artillery CLI는 전역 설치를 전제하지 않는다.

공식 문서:

- [Set up Artillery CLI](https://www.artillery.io/docs/get-started/get-artillery)
- [Run Your First Test](https://www.artillery.io/docs/get-started/first-test)

배포:

```bash
npm run cdk -- -c environment=perf-phase0 deploy LoopAdPerfPhase0Stack
```

Spot 인스턴스를 쓰고 싶으면 다음처럼 `phase0LoadGeneratorSpotPrice`를 넘긴다.

```bash
npm run cdk -- \
  -c environment=perf-phase0 \
  -c phase0LoadGeneratorInstanceType=c6in.xlarge \
  -c phase0LoadGeneratorDesiredCapacity=4 \
  -c phase0LoadGeneratorSpotPrice=0.80 \
  deploy LoopAdPerfPhase0Stack
```

배포 출력에서 다음 값을 확인한다.

- `Phase0LoadGeneratorTargetBaseUrl`
- `Phase0LoadGeneratorClusterName`
- `Phase0LoadGeneratorAutoScalingGroupName`
- `Phase0LoadGeneratorSecurityGroupId`
- `Phase0LoadGeneratorDescribeInstancesCommand`
- `Phase0LoadGeneratorSsmStartSessionCommand`
- `Phase0Ec2KeepAlivePlan`
- `Phase0Ec2KeepAliveWorkerCommand`
- `Phase0Ec2ArtilleryRunCommand`
- `Phase0Ec2ArtillerySweepPlan`
- `Phase0Ec2ArtillerySweepCommand`

인스턴스 ID를 찾는다.

```bash
aws ec2 describe-instances \
  --region ap-northeast-2 \
  --filters Name=tag:Name,Values=perf-phase0-loop-ad-load-generator Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' \
  --output text
```

SSM으로 접속한다.

```bash
aws ssm start-session --region ap-northeast-2 --target "<instance-id>"
```

EC2 인스턴스 안에서 repo를 준비한 뒤 run 폴더를 만든다.

```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)_phase0_alb_ec2"
mkdir -p "performance-tests/$RUN_ID"
```

50k HTTP rps 확인용 multi-process keep-alive worker를 실행한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
HOST_LABEL="$(hostname -s)" \
PROCESSES_PER_HOST=5 \
VUS_PER_PROCESS=625 \
REQUESTS_PER_VU=750 \
VU_THINK_SECONDS=0.15 \
HTTP_TIMEOUT_SECONDS=10 \
TELEMETRY_INTERVAL_SECONDS=5 \
./performance-tests/phase0/run-ec2-artillery-keepalive-multiprocess-worker.sh
```

이 설정은 host 1개 기준 대략 `5 process * 625 VU * 약 5 req/sec = 약 15,625 HTTP req/sec`를 의도한다. 4개 EC2에서 동시에 실행하면 대략 `62,500 HTTP req/sec`다. 요청 1개는 SDK envelope 이벤트 1개다.

각 EC2에서 같은 명령을 동시에 실행한다. `Phase0Ec2KeepAliveWorkerCommand` 출력값을 그대로 사용해도 된다.

oha로 payload pool과 고정 QPS를 검증하려면 다음처럼 실행한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
HOST_LABEL="$(hostname -s)" \
CONNECTIONS_PER_HOST=10000 \
DURATION_SECONDS=150 \
QUERY_PER_SECOND=55000 \
BODY_POOL_FILE=performance-tests/phase0/payloads/oha-body-pool-12.ndjson \
HTTP_TIMEOUT_SECONDS=10 \
TELEMETRY_INTERVAL_SECONDS=5 \
./performance-tests/phase0/run-ec2-oha-worker.sh
```

이 worker는 pool의 각 줄이 유효한 JSON인지, body가 1,024~1,536바이트인지, pool에 최소 두 줄이 있는지 확인한다. 실행 결과의 유효 성공률이 99.9%보다 낮거나 204 외 응답이 있으면 실패한다.

결과를 회수한 뒤 host telemetry를 집계한다.

```bash
node performance-tests/phase0/summarize-host-telemetry.mjs \
  "performance-tests/$RUN_ID" \
  "performance-tests/$RUN_ID/host-telemetry-summary.json"
```

짧은 단일 step을 실행한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
SWEEP_STEPS="p1_500:1:500:60" \
./performance-tests/phase0/run-ec2-artillery-sweep.sh
```

고정 rps를 직접 확인할 때도 sweep step을 지정한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
SWEEP_STEPS="p4_1000:4:1000:90" \
./performance-tests/phase0/run-ec2-artillery-sweep.sh
```

기본 `c6in.xlarge` 한 대에서 실행하는 churn 진단 sweep은 다음과 같다.

```text
1 x 500 rps   = 500 rps
2 x 750 rps   = 1,500 rps
3 x 1,000 rps = 3,000 rps
4 x 1,250 rps = 5,000 rps
```

이 sweep은 connection churn 진단용이다. 실제 사용자에 가까운 50k events/sec 확인은 keep-alive worker를 우선 사용한다.

CDK 출력의 `Phase0Ec2ArtillerySweepCommand` 또는 아래 명령으로 실행한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
./performance-tests/phase0/run-ec2-artillery-sweep.sh
```

필요하면 sweep만 바꿔 실행한다.

```bash
TARGET_BASE_URL="<Phase0LoadGeneratorTargetBaseUrl>" \
RUN_ID="$RUN_ID" \
MAX_PROCESSES=14 \
SWEEP_STEPS="p8_2000:8:2000:120 p12_2500:12:2500:180 p14_3000:14:3000:180" \
./performance-tests/phase0/run-ec2-artillery-sweep.sh
```

종료:

```bash
npm run cdk -- -c environment=perf-phase0 destroy LoopAdPerfPhase0Stack
```

주의:

- `destroy` 전에 EC2 인스턴스 안의 결과 파일을 이 repo의 `performance-tests/run_<id>/`에 남긴다.
- `destroy`는 Phase 0 CDK stack만 제거한다.
- 실패한 실행도 `report.md`에 실패 지점과 에러를 기록한다.

기록:

- `performance-tests/run_<id>/artillery-report.json`는 커밋 대상이다.
- EC2 sweep은 각 step 아래 `worker_*.json`, `worker_*.log`, `step-summary.json`를 남긴다.
- CloudWatch, S3, Artillery Cloud 링크가 있으면 `artifacts.md`에 남긴다.
- 실패한 실행도 `report.md`에 실패 지점과 에러를 기록한다.
