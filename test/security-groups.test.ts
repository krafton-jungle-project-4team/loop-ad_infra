import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

describe('security group policy', () => {
  it('dev public ingress is only ALB/NLB port 80', () => {
    const template = Template.fromStack(synthDev());
    const publicIngressRules = publicIngressRulesFrom(template);

    expect(publicIngressRules).toHaveLength(2);
    for (const rule of publicIngressRules) {
      expect(rule).toMatchObject({
        FromPort: 80,
        ToPort: 80,
        IpProtocol: 'tcp',
      });
    }
  });

  it('perf public ingress is only the temporary NLB port 80', () => {
    const template = Template.fromStack(synthPerf());
    const publicIngressRules = publicIngressRulesFrom(template);

    expect(publicIngressRules).toHaveLength(1);
    expect(publicIngressRules[0]).toMatchObject({
      FromPort: 80,
      ToPort: 80,
      IpProtocol: 'tcp',
    });
  });

  it('dev and perf ECS-to-data rules use security group references', () => {
    for (const stack of [synthDev(), synthPerf()]) {
      const template = Template.fromStack(stack);

      template.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
        FromPort: 6379,
        ToPort: 6379,
        IpProtocol: 'tcp',
        DestinationSecurityGroupId: Match.anyValue(),
        GroupId: Match.anyValue(),
      });
      template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
        FromPort: 9098,
        ToPort: 9098,
        IpProtocol: 'tcp',
        SourceSecurityGroupId: Match.anyValue(),
        GroupId: Match.anyValue(),
      });
    }
  });
});

function publicIngressRulesFrom(template: Template): Record<string, unknown>[] {
  const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
  return Object.values(resources).flatMap((resource) => {
    if (resource.Type === 'AWS::EC2::SecurityGroupIngress') {
      return [resource.Properties ?? {}];
    }

    if (resource.Type !== 'AWS::EC2::SecurityGroup') {
      return [];
    }

    return ((resource.Properties?.SecurityGroupIngress as Record<string, unknown>[] | undefined) ?? []).map((rule) => rule);
  }).filter((rule) => rule.CidrIp === '0.0.0.0/0' || rule.CidrIpv6 === '::/0');
}

function synthDev(): LoopAdDevStack {
  const app = new cdk.App();
  return new LoopAdDevStack(app, 'LoopAdDevStack', {
    env: {
      account: '123456789012',
      region: LOOP_AD_REGION,
    },
    enableNatGateway: false,
  });
}

function synthPerf(): LoopAdPerfStack {
  const app = new cdk.App();
  return new LoopAdPerfStack(app, 'LoopAdPerfStack', {
    env: {
      account: '123456789012',
      region: LOOP_AD_REGION,
    },
  });
}
