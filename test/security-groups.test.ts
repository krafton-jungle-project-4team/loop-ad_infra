import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { LOOP_AD_REGION, LoopAdDevStack, LoopAdPerfStack } from '../src/loop-ad-stack';

const DATA_PORTS = new Set([5432, 6379, 8123, 9000, 9098]);

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

    it('dev and perf use shared internal security groups with broad internal traffic', () => {
        for (const { stack, securityGroupCount } of [
            { stack: synthDev(), securityGroupCount: 5 },
            { stack: synthPerf(), securityGroupCount: 4 },
        ]) {
            const template = Template.fromStack(stack);

            template.resourceCountIs('AWS::EC2::SecurityGroup', securityGroupCount);
            template.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
                IpProtocol: '-1',
                DestinationSecurityGroupId: Match.anyValue(),
                GroupId: Match.anyValue(),
            });
            template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
                IpProtocol: '-1',
                SourceSecurityGroupId: Match.anyValue(),
                GroupId: Match.anyValue(),
            });
            expect(dataPortSecurityGroupRulesFrom(template)).toEqual([]);
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

function dataPortSecurityGroupRulesFrom(template: Template): Record<string, unknown>[] {
    const resources = template.toJSON().Resources as Record<string, { Type: string; Properties?: Record<string, unknown> }>;
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type === 'AWS::EC2::SecurityGroupIngress' || resource.Type === 'AWS::EC2::SecurityGroupEgress') {
            return [resource.Properties ?? {}];
        }

        if (resource.Type !== 'AWS::EC2::SecurityGroup') {
            return [];
        }

        return [
            ...((resource.Properties?.SecurityGroupIngress as Record<string, unknown>[] | undefined) ?? []),
            ...((resource.Properties?.SecurityGroupEgress as Record<string, unknown>[] | undefined) ?? []),
        ];
    }).filter((rule) => DATA_PORTS.has(Number(rule.FromPort)) || DATA_PORTS.has(Number(rule.ToPort)));
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
