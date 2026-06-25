import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { serviceById, type EnvironmentMode } from '../config/loop-ad-config';
import { attachAlbTarget, createWorkload } from './service-stack-support';
import type { EdgeResources, EndpointParameterMap, NetworkResources, RepositoryMap } from './stack-interfaces';

export interface DashboardStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
  readonly network: NetworkResources;
  readonly edge: EdgeResources;
  readonly repositories: RepositoryMap;
  readonly endpointParameters: EndpointParameterMap;
}

export class DashboardStack extends cdk.Stack {
  public constructor(scope: Construct, id: string, props: DashboardStackProps) {
    super(scope, id, props);

    const service = serviceById('dashboard-api');
    const workload = createWorkload(this, service.id, props);
    attachAlbTarget(this, props.edge, service, workload);
  }
}
