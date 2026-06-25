import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { LoopAdEcsService } from '../constructs/loop-ad-ecs-service';
import { serviceById, type EnvironmentMode, type ServiceDefinition, type ServiceId } from '../config/loop-ad-config';
import type { EdgeResources, EndpointParameterMap, NetworkResources, RepositoryMap } from './stack-interfaces';

export interface ServiceStackInputs {
  readonly mode: EnvironmentMode;
  readonly network: NetworkResources;
  readonly repositories: RepositoryMap;
  readonly endpointParameters: EndpointParameterMap;
}

export function createWorkload(scope: Construct, serviceId: ServiceId, inputs: ServiceStackInputs): LoopAdEcsService {
  const service = serviceById(serviceId);
  const repository = inputs.repositories[service.id];
  const securityGroup = inputs.network.serviceSecurityGroups[service.id];

  if (repository === undefined) {
    throw new Error(`Missing ECR repository for ${service.id}.`);
  }

  if (securityGroup === undefined) {
    throw new Error(`Missing security group for ${service.id}.`);
  }

  return new LoopAdEcsService(scope, `${pascalCase(service.id)}Workload`, {
    mode: inputs.mode,
    service,
    cluster: inputs.network.cluster,
    repository,
    securityGroup,
    appSubnets: inputs.network.appSubnets,
    endpointParameters: inputs.endpointParameters,
    ec2CapacityProvider: inputs.network.ec2CapacityProvider,
  });
}

export function attachAlbTarget(scope: Construct, edge: EdgeResources, service: ServiceDefinition, workload: LoopAdEcsService): void {
  if (service.ingress?.loadBalancer !== 'alb') {
    throw new Error(`${service.id} is not an ALB service.`);
  }

  edge.albListener.addTargets(`${pascalCase(service.id)}AlbTargets`, {
    targets: [workload.ecsService],
    port: service.port,
    protocol: elbv2.ApplicationProtocol.HTTP,
    priority: service.ingress.priority,
    conditions: [elbv2.ListenerCondition.pathPatterns([...(service.ingress.pathPatterns ?? [`/${service.id}/*`])])],
    healthCheck: {
      enabled: true,
      path: service.healthCheckPath ?? '/',
      healthyHttpCodes: '200-399',
    },
  });
}

export function attachNlbTarget(scope: Construct, edge: EdgeResources, service: ServiceDefinition, workload: LoopAdEcsService): void {
  if (service.ingress?.loadBalancer !== 'nlb') {
    throw new Error(`${service.id} is not an NLB service.`);
  }

  const listener = edge.nlb.addListener(`${pascalCase(service.id)}NlbListener`, {
    port: 80,
    protocol: elbv2.Protocol.TCP,
  });

  listener.addTargets(`${pascalCase(service.id)}NlbTargets`, {
    targets: [workload.ecsService],
    port: service.port,
    protocol: elbv2.Protocol.TCP,
    healthCheck: {
      enabled: true,
      port: String(service.port),
    },
  });

  scope.node.addMetadata('loop-ad:nlb-target', service.id);
}

function pascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}
