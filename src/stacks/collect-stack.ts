import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { serviceById, type EnvironmentMode } from '../config/loop-ad-config';
import { attachNlbTarget, createWorkload } from './service-stack-support';
import type { EdgeResources, EndpointParameterMap, NetworkResources, RepositoryMap } from './stack-interfaces';

export interface CollectStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
  readonly network: NetworkResources;
  readonly edge: EdgeResources;
  readonly repositories: RepositoryMap;
  readonly endpointParameters: EndpointParameterMap;
}

export class CollectStack extends cdk.Stack {
  public constructor(scope: Construct, id: string, props: CollectStackProps) {
    super(scope, id, props);

    const service = serviceById('event-collector');
    const workload = createWorkload(this, service.id, props);
    attachNlbTarget(this, props.edge, service, workload);
  }
}
