import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { serviceById, servicesFor, type EnvironmentMode, type ServiceId } from '../config/loop-ad-config';
import { createWorkload } from './service-stack-support';
import type { EndpointParameterMap, NetworkResources, RepositoryMap } from './stack-interfaces';

export interface AnalyticsStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
  readonly network: NetworkResources;
  readonly repositories: RepositoryMap;
  readonly endpointParameters: EndpointParameterMap;
}

export class AnalyticsStack extends cdk.Stack {
  public constructor(scope: Construct, id: string, props: AnalyticsStackProps) {
    super(scope, id, props);

    for (const serviceId of ['ad-context-projector', 'recommendation'] as const satisfies readonly ServiceId[]) {
      if (!isIncluded(serviceId, props.mode.name)) {
        continue;
      }

      const service = serviceById(serviceId);
      createWorkload(this, service.id, props);
    }
  }
}

function isIncluded(serviceId: ServiceId, environmentName: EnvironmentMode['name']): boolean {
  return servicesFor(environmentName).some((service) => service.id === serviceId);
}
