import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';
import { SERVICE_DEFINITIONS, type EnvironmentMode } from '../config/loop-ad-config';
import type { RepositoryMap } from './stack-interfaces';

export interface StorageStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
}

export class StorageStack extends cdk.Stack {
  public readonly repositories: RepositoryMap;

  public constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    this.repositories = Object.fromEntries(
      SERVICE_DEFINITIONS.map((service) => [
        service.id,
        new ecr.Repository(this, `${pascalCase(service.id)}Repository`, {
          repositoryName: service.ecrRepositoryName,
          imageScanOnPush: true,
          lifecycleRules: [
            {
              maxImageCount: 20,
              description: '개발/성능 테스트용 이미지 누적 비용을 제한한다.',
            },
          ],
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        }),
      ]),
    ) as RepositoryMap;
  }
}

function pascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}
