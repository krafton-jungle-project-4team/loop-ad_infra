import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { servicesFor, type EnvironmentMode } from '../config/loop-ad-config';

export interface ObservabilityStackProps extends cdk.StackProps {
  readonly mode: EnvironmentMode;
}

export class ObservabilityStack extends cdk.Stack {
  public constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${props.mode.name}-loop-ad-overview`,
      widgets: [
        [
          new cloudwatch.TextWidget({
            markdown: [
              `# loop-ad ${props.mode.name}`,
              '',
              '초안 단계의 운영 대시보드입니다.',
              '',
              ...servicesFor(props.mode.name).map((service) => `- ${service.displayName}: ${service.computePolicy[props.mode.name]}`),
            ].join('\n'),
            width: 24,
            height: 6,
          }),
        ],
      ],
    });
  }
}
