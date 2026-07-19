import { Match, Template } from 'aws-cdk-lib/assertions';
import {
    emptyDeveloperAllowlist,
    logicalIdBySecurityGroupDescription,
    resourcesOf,
    synthNetwork,
} from './helpers';

describe('network architecture', () => {
    it('uses public subnets only and does not create NAT or VPC endpoints', () => {
        const template = Template.fromStack(synthNetwork(emptyDeveloperAllowlist));

        template.resourceCountIs('AWS::EC2::VPC', 1);
        template.resourceCountIs('AWS::EC2::NatGateway', 0);
        template.resourceCountIs('AWS::EC2::VPCEndpoint', 0);
        template.hasResourceProperties('AWS::EC2::Subnet', {
            MapPublicIpOnLaunch: true,
        });
    });

    it('keeps the security group set limited to ALB, server, and data source groups', () => {
        const template = Template.fromStack(synthNetwork(emptyDeveloperAllowlist));

        template.resourceCountIs('AWS::EC2::SecurityGroup', 3);
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
            GroupDescription: 'Dev public ALB HTTPS ingress.',
        });
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
            GroupDescription: 'Dev public Fargate services.',
        });
        template.hasResourceProperties('AWS::EC2::SecurityGroup', {
            GroupDescription: 'Dev Aurora, ClickHouse, and Kafka data sources.',
        });
    });

    it('opens public ingress only on ALB HTTPS and keeps server egress to data ports', () => {
        const template = Template.fromStack(synthNetwork(emptyDeveloperAllowlist));
        const resources = resourcesOf(template);
        const serverSecurityGroupId = logicalIdBySecurityGroupDescription(resources, 'Dev public Fargate services.');
        const dataSourceSecurityGroupId = logicalIdBySecurityGroupDescription(resources, 'Dev Aurora, ClickHouse, and Kafka data sources.');

        const publicIngressRules = ingressRules(resources).filter((rule) => (
            rule.CidrIp === '0.0.0.0/0' || rule.CidrIpv6 === '::/0'
        ));
        expect(publicIngressRules).toHaveLength(1);
        expect(publicIngressRules[0]).toEqual(expect.objectContaining({
            IpProtocol: 'tcp',
            FromPort: 443,
            ToPort: 443,
        }));

        const serverEgressRules = Object.values(resources).filter((resource) => (
            resource.Type === 'AWS::EC2::SecurityGroupEgress' &&
            JSON.stringify(resource.Properties?.GroupId ?? '').includes(serverSecurityGroupId)
        ));
        expect(serverEgressRules.map((rule) => rule.Properties?.FromPort).sort()).toEqual([5432, 8123, 9094]);
        expect(serverEgressRules.every((rule) => JSON.stringify(rule.Properties?.DestinationSecurityGroupId ?? '').includes(dataSourceSecurityGroupId))).toBe(true);
        expect(JSON.stringify(serverEgressRules)).not.toContain('0.0.0.0/0');

        const dataSourceIngressRules = ingressRules(resources).filter((rule) => (
            JSON.stringify(rule.GroupId ?? '').includes(dataSourceSecurityGroupId) &&
            JSON.stringify(rule.SourceSecurityGroupId ?? '').includes(dataSourceSecurityGroupId)
        ));
        expect(dataSourceIngressRules).toEqual(expect.arrayContaining([
            expect.objectContaining({ IpProtocol: 'tcp', FromPort: 0, ToPort: 65535 }),
        ]));

        const dataSourceEgressRules = egressRules(resources).filter((rule) => (
            JSON.stringify(rule.GroupId ?? '').includes(dataSourceSecurityGroupId) &&
            JSON.stringify(rule.DestinationSecurityGroupId ?? '').includes(dataSourceSecurityGroupId)
        ));
        expect(dataSourceEgressRules).toEqual(expect.arrayContaining([
            expect.objectContaining({ IpProtocol: 'tcp', FromPort: 0, ToPort: 65535 }),
        ]));
    });

    it('adds developer data source ingress only when CIDR allowlists are provided', () => {
        const emptyTemplate = Template.fromStack(synthNetwork(emptyDeveloperAllowlist));
        expect(JSON.stringify(emptyTemplate.toJSON())).not.toContain('203.0.113.10/32');
        expect(JSON.stringify(emptyTemplate.toJSON())).not.toContain('2001:db8::10/128');

        const allowlistedTemplate = Template.fromStack(synthNetwork({
            ipv4Cidrs: ['203.0.113.10/32'],
            ipv6Cidrs: ['2001:db8::10/128'],
        }));
        const allowlistedRules = ingressRules(resourcesOf(allowlistedTemplate));
        for (const port of [5432, 8123, 9094]) {
            expect(allowlistedRules).toEqual(expect.arrayContaining([
                expect.objectContaining({ CidrIp: '203.0.113.10/32', FromPort: port, ToPort: port }),
                expect.objectContaining({ CidrIpv6: '2001:db8::10/128', FromPort: port, ToPort: port }),
            ]));
        }
    });

    it('does not synthesize removed ingress components', () => {
        const template = Template.fromStack(synthNetwork(emptyDeveloperAllowlist));

        template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
        template.hasResourceProperties('AWS::EC2::VPC', Match.objectLike({}));
    });
});

function ingressRules(resources: ReturnType<typeof resourcesOf>): Record<string, unknown>[] {
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type === 'AWS::EC2::SecurityGroupIngress') {
            return [resource.Properties ?? {}];
        }

        if (resource.Type !== 'AWS::EC2::SecurityGroup') {
            return [];
        }

        return (resource.Properties?.SecurityGroupIngress as Record<string, unknown>[] | undefined) ?? [];
    });
}

function egressRules(resources: ReturnType<typeof resourcesOf>): Record<string, unknown>[] {
    return Object.values(resources).flatMap((resource) => {
        if (resource.Type === 'AWS::EC2::SecurityGroupEgress') {
            return [resource.Properties ?? {}];
        }

        if (resource.Type !== 'AWS::EC2::SecurityGroup') {
            return [];
        }

        return (resource.Properties?.SecurityGroupEgress as Record<string, unknown>[] | undefined) ?? [];
    });
}
