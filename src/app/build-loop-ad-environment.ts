import * as cdk from 'aws-cdk-lib';
import {
  LOOP_AD_REGION,
  routesFor,
  servicesFor,
  type EnvironmentMode,
  type ServiceId,
} from '../config/loop-ad-config';
import { AnalyticsStack } from '../stacks/analytics-stack';
import { CollectStack } from '../stacks/collect-stack';
import { DashboardStack } from '../stacks/dashboard-stack';
import { DataStack } from '../stacks/data-stack';
import { DecisionStack } from '../stacks/decision-stack';
import { EdgeStack } from '../stacks/edge-stack';
import { FrontendStack } from '../stacks/frontend-stack';
import { NetworkStack } from '../stacks/network-stack';
import { ObservabilityStack } from '../stacks/observability-stack';
import { StorageStack } from '../stacks/storage-stack';
import { StreamStack } from '../stacks/stream-stack';
import type { EndpointParameterMap } from '../stacks/stack-interfaces';

export interface BuildLoopAdEnvironmentProps {
  readonly mode: EnvironmentMode;
  readonly env: cdk.Environment;
  readonly enableNatGateway: boolean;
  readonly enableVpcEndpoints: boolean;
}

export interface LoopAdEnvironmentStacks {
  readonly network: NetworkStack;
  readonly frontend: FrontendStack;
  readonly edge: EdgeStack;
  readonly storage: StorageStack;
  readonly stream: StreamStack;
  readonly data: DataStack;
  readonly collect: CollectStack;
  readonly analytics: AnalyticsStack;
  readonly decision?: DecisionStack;
  readonly dashboard?: DashboardStack;
  readonly observability: ObservabilityStack;
}

export function buildLoopAdEnvironment(scope: cdk.App, props: BuildLoopAdEnvironmentProps): LoopAdEnvironmentStacks {
  validateRegion(props.env);
  validateRoutes(props.mode);

  const env = {
    account: props.env.account,
    region: LOOP_AD_REGION,
  };
  const prefix = `LoopAd${pascalCase(props.mode.name)}`;

  const network = new NetworkStack(scope, `${prefix}NetworkStack`, {
    env,
    mode: props.mode,
    enableNatGateway: props.enableNatGateway,
    enableVpcEndpoints: props.enableVpcEndpoints,
  });
  const frontend = new FrontendStack(scope, `${prefix}FrontendStack`, {
    env,
    mode: props.mode,
  });
  const edge = new EdgeStack(scope, `${prefix}EdgeStack`, {
    env,
    mode: props.mode,
    network,
  });
  const storage = new StorageStack(scope, `${prefix}StorageStack`, {
    env,
    mode: props.mode,
  });
  const stream = new StreamStack(scope, `${prefix}StreamStack`, {
    env,
    mode: props.mode,
  });
  const data = new DataStack(scope, `${prefix}DataStack`, {
    env,
    mode: props.mode,
  });

  const endpointParameters: EndpointParameterMap = {
    ...data.endpointParameters,
    ...stream.endpointParameters,
  };

  const collect = new CollectStack(scope, `${prefix}CollectStack`, {
    env,
    mode: props.mode,
    network,
    edge,
    repositories: storage.repositories,
    endpointParameters,
  });
  const analytics = new AnalyticsStack(scope, `${prefix}AnalyticsStack`, {
    env,
    mode: props.mode,
    network,
    repositories: storage.repositories,
    endpointParameters,
  });

  const decision = isIncluded('ad-decision-api', props.mode)
    ? new DecisionStack(scope, `${prefix}DecisionStack`, {
        env,
        mode: props.mode,
        network,
        edge,
        repositories: storage.repositories,
        endpointParameters,
      })
    : undefined;

  const dashboard = isIncluded('dashboard-api', props.mode)
    ? new DashboardStack(scope, `${prefix}DashboardStack`, {
        env,
        mode: props.mode,
        network,
        edge,
        repositories: storage.repositories,
        endpointParameters,
      })
    : undefined;

  if (dashboard !== undefined) {
    dashboard.addDependency(analytics);
  }

  const observability = new ObservabilityStack(scope, `${prefix}ObservabilityStack`, {
    env,
    mode: props.mode,
  });

  return {
    network,
    frontend,
    edge,
    storage,
    stream,
    data,
    collect,
    analytics,
    decision,
    dashboard,
    observability,
  };
}

function validateRegion(env: cdk.Environment): void {
  if (env.region !== undefined && env.region !== LOOP_AD_REGION) {
    throw new Error(`loop-ad CDK region is fixed to ${LOOP_AD_REGION}. Received: ${env.region}`);
  }
}

function validateRoutes(mode: EnvironmentMode): void {
  for (const route of routesFor(mode.name)) {
    if (route.loadBalancer === 'nlb' && route.targetServiceId !== 'event-collector') {
      throw new Error('NLB can target only Event Collector.');
    }

    if (route.loadBalancer === 'alb' && !(['ad-decision-api', 'dashboard-api'] as readonly ServiceId[]).includes(route.targetServiceId)) {
      throw new Error('ALB can target only Ad Decision API and Dashboard API.');
    }
  }
}

function isIncluded(serviceId: ServiceId, mode: EnvironmentMode): boolean {
  return servicesFor(mode.name).some((service) => service.id === serviceId);
}

function pascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}
