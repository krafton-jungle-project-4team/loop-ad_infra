import {
  DATA_STORE_DEFINITIONS,
  ENVIRONMENT_MODES,
  LOOP_AD_REGION,
  resolveComputeTarget,
  routesFor,
  serviceById,
  servicesFor,
} from '../src/config/loop-ad-config';

describe('loop-ad 선언 config', () => {
  it('리전을 한국 리전으로 고정한다', () => {
    expect(LOOP_AD_REGION).toBe('ap-northeast-2');
  });

  it('compute policy를 서비스별로 명시한다', () => {
    expect(resolveComputeTarget(serviceById('event-collector'), 'dev')).toBe('fargate');
    expect(resolveComputeTarget(serviceById('event-collector'), 'perf')).toBe('ecs-ec2');
    expect(resolveComputeTarget(serviceById('ad-context-projector'), 'perf')).toBe('ecs-ec2');
    expect(resolveComputeTarget(serviceById('ad-decision-api'), 'dev')).toBe('fargate');
    expect(resolveComputeTarget(serviceById('dashboard-api'), 'dev')).toBe('fargate');
    expect(resolveComputeTarget(serviceById('recommendation'), 'dev')).toBe('fargate');
  });

  it('perf는 적재/가공 시간 측정 경로에 필요한 서비스만 포함한다', () => {
    expect(servicesFor('perf').map((service) => service.id).sort()).toEqual(['ad-context-projector', 'event-collector']);
    expect(DATA_STORE_DEFINITIONS.filter((store) => store.includeIn.some((mode) => mode === 'perf')).map((store) => store.id).sort()).toEqual([
      'clickhouse',
      'msk',
      'redis',
    ]);
  });

  it('NLB와 ALB target 범위를 아키텍처 이미지 기준으로 제한한다', () => {
    for (const mode of Object.values(ENVIRONMENT_MODES)) {
      const routes = routesFor(mode.name);
      expect(routes.filter((route) => route.loadBalancer === 'nlb').map((route) => route.targetServiceId)).toEqual([
        'event-collector',
      ]);
      expect(routes.filter((route) => route.loadBalancer === 'alb').map((route) => route.targetServiceId).sort()).toEqual(
        mode.name === 'dev' ? ['ad-decision-api', 'dashboard-api'] : [],
      );
    }
  });
});
