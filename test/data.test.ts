import { Template } from 'aws-cdk-lib/assertions';
import { resourcesOf, synthData, testSecretNames } from './helpers';

describe('data architecture', () => {
    it('creates Aurora Serverless v2 as a public single-writer cluster using imported credentials', () => {
        const template = Template.fromStack(synthData());

        template.resourceCountIs('AWS::RDS::DBCluster', 1);
        template.resourceCountIs('AWS::RDS::DBInstance', 1);
        template.resourceCountIs('AWS::SecretsManager::Secret', 0);
        template.hasResourceProperties('AWS::RDS::DBCluster', {
            Engine: 'aurora-postgresql',
            EngineVersion: '16.13',
            DatabaseName: 'loopad',
            ServerlessV2ScalingConfiguration: {
                MinCapacity: 0,
                MaxCapacity: 4,
                SecondsUntilAutoPause: 600,
            },
        });
        template.hasResourceProperties('AWS::RDS::DBInstance', {
            DBInstanceClass: 'db.serverless',
            PubliclyAccessible: true,
        });
        expect(JSON.stringify(template.toJSON())).toContain(testSecretNames.auroraCredentialsSecretName);
    });

    it('keeps DataStorage and GenAI generated assets storage retained', () => {
        const template = Template.fromStack(synthData());
        const resources = resourcesOf(template);
        const buckets = Object.values(resources).filter((resource) => resource.Type === 'AWS::S3::Bucket');

        expect(buckets).toHaveLength(1);
        expect(buckets[0]?.DeletionPolicy).toBe('Retain');
        expect(buckets[0]?.UpdateReplacePolicy).toBe('Retain');
        template.resourceCountIs('AWS::CloudFront::Distribution', 1);
    });

    it('runs ClickHouse and Kafka on public t4g.medium EC2 instances with non-retained gp3 EBS', () => {
        const template = Template.fromStack(synthData());

        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            InstanceType: 't4g.medium',
        }, 2);
        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            NetworkInterfaces: [
                {
                    AssociatePublicIpAddress: true,
                },
            ],
        }, 2);
        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            BlockDeviceMappings: [
                {
                    DeviceName: '/dev/xvda',
                    Ebs: {
                        DeleteOnTermination: true,
                        Encrypted: true,
                        VolumeType: 'gp3',
                    },
                },
            ],
        }, 2);
        template.hasResourceProperties('AWS::EC2::Instance', {
            BlockDeviceMappings: [
                {
                    Ebs: {
                        VolumeSize: 100,
                    },
                },
            ],
        });
        template.hasResourceProperties('AWS::EC2::Instance', {
            BlockDeviceMappings: [
                {
                    Ebs: {
                        VolumeSize: 40,
                    },
                },
            ],
        });
    });

    it('configures Kafka auth and the single infra-owned topic without storing auth method in secrets', () => {
        const templateText = JSON.stringify(Template.fromStack(synthData()).toJSON());

        expect(templateText).toContain('SASL_PLAINTEXT');
        expect(templateText).toContain('SCRAM-SHA-512');
        expect(templateText).toContain('loop-ad.events.raw');
        expect(templateText).toContain('/opt/loop-ad/clickhouse.sh');
        expect(templateText).toContain('/opt/loop-ad/kafka.sh');
        expect(templateText).toContain(testSecretNames.kafkaAppUserSecretName);
        expect(templateText).toContain(testSecretNames.kafkaBrokerUserSecretName);
        expect(templateText).not.toContain('get-secret-value');
        expect(templateText).not.toContain('auto.create.topics.enable=true');
    });

    it('does not synthesize removed managed data resources or endpoint SSM parameters', () => {
        const template = Template.fromStack(synthData());

        template.resourceCountIs('AWS::ElastiCache::ServerlessCache', 0);
        template.resourceCountIs('AWS::MSK::Cluster', 0);
        template.resourceCountIs('AWS::SSM::Parameter', 0);
        expect(JSON.stringify(template.toJSON())).not.toContain('redis');
        expect(JSON.stringify(template.toJSON())).not.toContain('Valkey');
    });
});
