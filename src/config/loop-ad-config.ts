export const LOOP_AD_REGION = 'ap-northeast-2';

export type EnvironmentName = 'dev' | 'perf';
export type ComputeTarget = 'fargate' | 'ecs-ec2';
export type DataAccessMode = 'read' | 'write' | 'publish' | 'consume';
export type LoadBalancerKind = 'alb' | 'nlb';
export type RuntimeKind = 'go' | 'node' | 'python';
export type ExternalProviderId = 'openai' | 'n8n' | 'discord';

export type ServiceId =
  | 'event-collector'
  | 'ad-context-projector'
  | 'ad-decision-api'
  | 'dashboard-api'
  | 'recommendation';

export type DataStoreId = 'aurora' | 'redis' | 'clickhouse' | 'msk';

export interface EnvironmentMode {
  readonly name: EnvironmentName;
  readonly maxAzs: number;
  readonly enableNatGatewayByDefault: boolean;
  readonly natGatewayCountWhenEnabled: number;
  readonly enableVpcEndpointsByDefault: boolean;
  readonly logRetentionDays: 3 | 7;
  readonly fargate: {
    readonly desiredCount: number;
    readonly minTasks: number;
    readonly maxTasks: number;
    readonly cpu: number;
    readonly memoryMiB: number;
  };
  readonly ecsOnEc2: {
    readonly instanceType: string;
    readonly minCapacity: number;
    readonly maxCapacity: number;
  };
}

export interface ComputePolicy {
  readonly dev: ComputeTarget;
  readonly perf: ComputeTarget;
  readonly rationale: string;
}

export interface DataAccessDefinition {
  readonly store: DataStoreId;
  readonly mode: DataAccessMode;
}

export interface IngressDefinition {
  readonly loadBalancer: LoadBalancerKind;
  readonly pathPatterns?: readonly string[];
  readonly priority?: number;
}

export interface ServiceDefinition {
  readonly id: ServiceId;
  readonly displayName: string;
  readonly sourceRepository: string;
  readonly ecrRepositoryName: string;
  readonly containerName: string;
  readonly runtime: RuntimeKind;
  readonly port: number;
  readonly includeIn: readonly EnvironmentName[];
  readonly computePolicy: ComputePolicy;
  readonly healthCheckPath?: string;
  readonly ingress?: IngressDefinition;
  readonly dataAccess?: readonly DataAccessDefinition[];
  readonly callsServices?: readonly ServiceId[];
  readonly externalEgressProviders?: readonly ExternalProviderId[];
}

export interface DataStoreDefinition {
  readonly id: DataStoreId;
  readonly displayName: string;
  readonly engine: string;
  readonly ports: readonly number[];
  readonly includeIn: readonly EnvironmentName[];
  readonly endpointParameterName: string;
}

export interface RouteDefinition {
  readonly id: string;
  readonly loadBalancer: LoadBalancerKind;
  readonly targetServiceId: ServiceId;
  readonly pathPatterns?: readonly string[];
  readonly priority?: number;
}

export interface ExternalProviderDefinition {
  readonly id: ExternalProviderId;
  readonly displayName: string;
  readonly secretParameterName: string;
}

export const ENVIRONMENT_MODES = {
  dev: {
    name: 'dev',
    maxAzs: 2,
    enableNatGatewayByDefault: false,
    natGatewayCountWhenEnabled: 1,
    enableVpcEndpointsByDefault: true,
    logRetentionDays: 3,
    fargate: {
      desiredCount: 1,
      minTasks: 0,
      maxTasks: 2,
      cpu: 256,
      memoryMiB: 512,
    },
    ecsOnEc2: {
      instanceType: 't4g.small',
      minCapacity: 0,
      maxCapacity: 1,
    },
  },
  perf: {
    name: 'perf',
    maxAzs: 2,
    enableNatGatewayByDefault: false,
    natGatewayCountWhenEnabled: 1,
    enableVpcEndpointsByDefault: true,
    logRetentionDays: 3,
    fargate: {
      desiredCount: 1,
      minTasks: 0,
      maxTasks: 2,
      cpu: 512,
      memoryMiB: 1024,
    },
    ecsOnEc2: {
      instanceType: 't4g.small',
      minCapacity: 0,
      maxCapacity: 2,
    },
  },
} as const satisfies Record<EnvironmentName, EnvironmentMode>;

export const SERVICE_DEFINITIONS = [
  {
    id: 'event-collector',
    displayName: 'Event Collector',
    sourceRepository: 'loopad-event-collector',
    ecrRepositoryName: 'loopad/event-collector',
    containerName: 'event-collector',
    runtime: 'go',
    port: 80,
    includeIn: ['dev', 'perf'],
    computePolicy: {
      dev: 'fargate',
      perf: 'ecs-ec2',
      // NLB ingest와 Kafka producer 중심이라 perf부터 EC2 capacity provider 경로를 검증한다.
      rationale: 'NLB ingest, Kafka producer, high network/IO path.',
    },
    healthCheckPath: '/health',
    ingress: {
      loadBalancer: 'nlb',
    },
    dataAccess: [
      {
        store: 'msk',
        mode: 'publish',
      },
    ],
  },
  {
    id: 'ad-context-projector',
    displayName: 'Ad Context Projector',
    sourceRepository: 'loopad-ad-context-projector',
    ecrRepositoryName: 'loopad/ad-context-projector',
    containerName: 'ad-context-projector',
    runtime: 'go',
    port: 80,
    includeIn: ['dev', 'perf'],
    computePolicy: {
      dev: 'fargate',
      perf: 'ecs-ec2',
      // Kafka consumer, ClickHouse insert, Redis update 중심이라 perf부터 EC2를 우선한다.
      rationale: 'Kafka consumer, ClickHouse insert, Redis update path.',
    },
    dataAccess: [
      {
        store: 'msk',
        mode: 'consume',
      },
      {
        store: 'clickhouse',
        mode: 'write',
      },
      {
        store: 'redis',
        mode: 'write',
      },
    ],
  },
  {
    id: 'ad-decision-api',
    displayName: 'Ad Decision API Server',
    sourceRepository: 'loopad-ad-decision-api',
    ecrRepositoryName: 'loopad/ad-decision-api',
    containerName: 'ad-decision-api',
    runtime: 'go',
    port: 80,
    includeIn: ['dev'],
    computePolicy: {
      dev: 'fargate',
      perf: 'fargate',
      // HTTP API scale-out 성격이라 Fargate를 우선한다.
      rationale: 'HTTP API scale-out with Redis/Aurora reads.',
    },
    healthCheckPath: '/health',
    ingress: {
      loadBalancer: 'alb',
      pathPatterns: ['/api/ads/*', '/decision/*'],
      priority: 20,
    },
    dataAccess: [
      {
        store: 'redis',
        mode: 'read',
      },
      {
        store: 'aurora',
        mode: 'read',
      },
    ],
  },
  {
    id: 'dashboard-api',
    displayName: 'Dashboard API Server',
    sourceRepository: 'loopad-dashboard-api',
    ecrRepositoryName: 'loopad/dashboard-api',
    containerName: 'dashboard-api',
    runtime: 'go',
    port: 80,
    includeIn: ['dev'],
    computePolicy: {
      dev: 'fargate',
      perf: 'fargate',
      // 사용자/관리 API 성격이라 Fargate를 우선한다.
      rationale: 'User/admin API with Aurora/ClickHouse reads.',
    },
    healthCheckPath: '/health',
    ingress: {
      loadBalancer: 'alb',
      pathPatterns: ['/api/dashboard/*', '/dashboard/*'],
      priority: 30,
    },
    callsServices: ['recommendation'],
    dataAccess: [
      {
        store: 'aurora',
        mode: 'read',
      },
      {
        store: 'clickhouse',
        mode: 'read',
      },
    ],
    externalEgressProviders: ['n8n', 'discord'],
  },
  {
    id: 'recommendation',
    displayName: 'Recommendation Server',
    sourceRepository: 'loopad-recommendation',
    ecrRepositoryName: 'loopad/recommendation',
    containerName: 'recommendation',
    runtime: 'go',
    port: 80,
    includeIn: ['dev'],
    computePolicy: {
      dev: 'fargate',
      perf: 'fargate',
      // 초기에는 Fargate로 두고, 병목이 확인되면 이 정책만 EC2로 바꾼다.
      rationale: 'Start on Fargate; move to EC2 after CPU/memory/network bottleneck is proven.',
    },
    healthCheckPath: '/health',
    dataAccess: [
      {
        store: 'aurora',
        mode: 'read',
      },
      {
        store: 'clickhouse',
        mode: 'read',
      },
    ],
    externalEgressProviders: ['openai'],
  },
] as const satisfies readonly ServiceDefinition[];

export const DATA_STORE_DEFINITIONS = [
  {
    id: 'aurora',
    displayName: 'Aurora PostgreSQL',
    engine: 'aurora-postgresql',
    ports: [5432],
    includeIn: ['dev'],
    endpointParameterName: '/loop-ad/${environment}/aurora/endpoint',
  },
  {
    id: 'redis',
    displayName: 'ElastiCache for Redis',
    engine: 'redis',
    ports: [6379],
    includeIn: ['dev', 'perf'],
    endpointParameterName: '/loop-ad/${environment}/redis/endpoint',
  },
  {
    id: 'clickhouse',
    displayName: 'ClickHouse',
    engine: 'clickhouse',
    ports: [8123, 9000],
    includeIn: ['dev', 'perf'],
    endpointParameterName: '/loop-ad/${environment}/clickhouse/endpoint',
  },
  {
    id: 'msk',
    displayName: 'MSK Express',
    engine: 'msk-express',
    ports: [9098],
    includeIn: ['dev', 'perf'],
    endpointParameterName: '/loop-ad/${environment}/msk/bootstrap-brokers',
  },
] as const satisfies readonly DataStoreDefinition[];

export const EXTERNAL_PROVIDER_DEFINITIONS = [
  {
    id: 'openai',
    displayName: 'OpenAI',
    secretParameterName: '/loop-ad/${environment}/external/openai/api-key',
  },
  {
    id: 'n8n',
    displayName: 'n8n',
    secretParameterName: '/loop-ad/${environment}/external/n8n/webhook',
  },
  {
    id: 'discord',
    displayName: 'Discord',
    secretParameterName: '/loop-ad/${environment}/external/discord/webhook',
  },
] as const satisfies readonly ExternalProviderDefinition[];

export function servicesFor(mode: EnvironmentName): readonly ServiceDefinition[] {
  return SERVICE_DEFINITIONS.filter((service) => includesEnvironment(service.includeIn, mode));
}

export function dataStoresFor(mode: EnvironmentName): readonly DataStoreDefinition[] {
  return DATA_STORE_DEFINITIONS.filter((store) => includesEnvironment(store.includeIn, mode));
}

export function dataStoresForStack(mode: EnvironmentName, stack: 'data' | 'stream'): readonly DataStoreDefinition[] {
  return dataStoresFor(mode).filter((store) => (stack === 'stream' ? store.id === 'msk' : store.id !== 'msk'));
}

export function routesFor(mode: EnvironmentName): readonly RouteDefinition[] {
  return servicesFor(mode)
    .filter((service) => service.ingress !== undefined)
    .map((service) => ({
      id: `${service.ingress?.loadBalancer}-${service.id}`,
      loadBalancer: service.ingress?.loadBalancer ?? 'alb',
      targetServiceId: service.id,
      pathPatterns: service.ingress?.pathPatterns,
      priority: service.ingress?.priority,
    }));
}

export function resolveComputeTarget(service: ServiceDefinition, mode: EnvironmentName): ComputeTarget {
  return service.computePolicy[mode];
}

export function endpointParameterName(definition: DataStoreDefinition, mode: EnvironmentName): string {
  return definition.endpointParameterName.replace('${environment}', mode);
}

export function externalSecretParameterName(definition: ExternalProviderDefinition, mode: EnvironmentName): string {
  return definition.secretParameterName.replace('${environment}', mode);
}

export function serviceById(id: ServiceId): ServiceDefinition {
  const service = SERVICE_DEFINITIONS.find((candidate) => candidate.id === id);
  if (service === undefined) {
    throw new Error(`Unknown service id: ${id}`);
  }

  return service;
}

export function dataStoreById(id: DataStoreId): DataStoreDefinition {
  const store = DATA_STORE_DEFINITIONS.find((candidate) => candidate.id === id);
  if (store === undefined) {
    throw new Error(`Unknown datastore id: ${id}`);
  }

  return store;
}

function includesEnvironment(values: readonly EnvironmentName[], mode: EnvironmentName): boolean {
  return values.includes(mode);
}
