import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { dataStoresForStack, endpointParameterName, type EnvironmentMode } from '../config/loop-ad-config';
import type { EndpointParameterMap } from './stack-interfaces';

export interface StreamStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
}

export class StreamStack extends cdk.Stack {
  public readonly endpointParameters: EndpointParameterMap;

  public constructor(scope: Construct, id: string, props: StreamStackProps) {
    super(scope, id, props);

    this.endpointParameters = Object.fromEntries(
      dataStoresForStack(props.mode.name, 'stream').map((store) => [
        store.id,
        new ssm.StringParameter(this, `${pascalCase(store.id)}EndpointParameter`, {
          parameterName: endpointParameterName(store, props.mode.name),
          stringValue: `pending://${props.mode.name}/${store.id}`,
          description: `${props.mode.name} ${store.displayName} endpoint contract. Ports: ${store.ports.join(',')}.`,
        }),
      ]),
    ) as EndpointParameterMap;
  }
}

function pascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}
