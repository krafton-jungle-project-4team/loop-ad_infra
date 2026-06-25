import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { buildLoopAdEnvironment } from '../src/app/build-loop-ad-environment';
import { ENVIRONMENT_MODES, LOOP_AD_REGION } from '../src/config/loop-ad-config';

describe('security group policy', () => {
  it('public ingress는 ALB/NLB 80 포트에만 열린다', () => {
    const stacks = synthDev();
    const template = Template.fromStack(stacks.network);
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    const publicIngressRules = Object.values(resources).flatMap((resource) => {
      if (resource.Type === 'AWS::EC2::SecurityGroupIngress') {
        return [resource.Properties ?? {}];
      }

      if (resource.Type !== 'AWS::EC2::SecurityGroup') {
        return [];
      }

      return ((resource.Properties?.SecurityGroupIngress as Record<string, unknown>[] | undefined) ?? []).map((rule) => rule);
    }).filter((rule) => rule.CidrIp === '0.0.0.0/0' || rule.CidrIpv6 === '::/0');

    expect(publicIngressRules).toHaveLength(2);
    for (const rule of publicIngressRules) {
      expect(rule).toMatchObject({
        FromPort: 80,
        ToPort: 80,
        IpProtocol: 'tcp',
      });
    }
  });

  it('ALB/NLB에서 ECS로 들어가는 규칙은 SG 참조 기반이다', () => {
    const stacks = synthDev();
    const template = Template.fromStack(stacks.network);

    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 80,
      ToPort: 80,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: Match.anyValue(),
      GroupId: Match.anyValue(),
    });
  });

  it('ECS에서 datastore로 가는 규칙도 CIDR가 아니라 SG 관계로 만든다', () => {
    const stacks = synthDev();
    const template = Template.fromStack(stacks.network);

    template.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
      FromPort: 6379,
      ToPort: 6379,
      IpProtocol: 'tcp',
      DestinationSecurityGroupId: Match.anyValue(),
      GroupId: Match.anyValue(),
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 5432,
      ToPort: 5432,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: Match.anyValue(),
      GroupId: Match.anyValue(),
    });
  });
});

function synthDev() {
  const app = new cdk.App();
  return buildLoopAdEnvironment(app, {
    mode: ENVIRONMENT_MODES.dev,
    env: {
      account: '123456789012',
      region: LOOP_AD_REGION,
    },
    enableNatGateway: false,
    enableVpcEndpoints: true,
  });
}
