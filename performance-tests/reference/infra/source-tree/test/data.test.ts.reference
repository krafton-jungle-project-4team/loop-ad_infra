import { Match, Template } from 'aws-cdk-lib/assertions';
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

    it('keeps DataStorage and GenAI assets storage retained under the base prefix', () => {
        const template = Template.fromStack(synthData());
        const resources = resourcesOf(template);
        const buckets = Object.values(resources).filter((resource) => resource.Type === 'AWS::S3::Bucket');

        expect(buckets).toHaveLength(1);
        expect(buckets[0]?.DeletionPolicy).toBe('Retain');
        expect(buckets[0]?.UpdateReplacePolicy).toBe('Retain');
        template.hasResourceProperties('AWS::S3::Bucket', {
            LifecycleConfiguration: {
                Rules: Match.arrayWith([
                    Match.objectLike({
                        Prefix: 'genai/',
                    }),
                ]),
            },
        });
        template.resourceCountIs('AWS::CloudFront::Distribution', 1);
        expect(JSON.stringify(template.toJSON())).toContain('/genai');
    });

    it('runs ClickHouse and Kafka on public EC2 instances with non-retained gp3 EBS', () => {
        const template = Template.fromStack(synthData());

        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            InstanceType: 't4g.medium',
        }, 1);
        template.resourcePropertiesCountIs('AWS::EC2::Instance', {
            InstanceType: 't4g.small',
        }, 1);
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
        const template = Template.fromStack(synthData());
        const templateText = JSON.stringify(template.toJSON());
        const ec2UserDataText = Object.values(resourcesOf(template))
            .filter((resource) => resource.Type === 'AWS::EC2::Instance')
            .map((resource) => JSON.stringify(resource.Properties?.UserData))
            .join('\n');

        expect(templateText).toContain('SASL_PLAINTEXT');
        expect(templateText).toContain('SCRAM-SHA-512');
        expect(templateText).toContain('loop-ad.events.raw');
        expect(templateText).toContain('/opt/loop-ad/clickhouse.sh');
        expect(templateText).toContain('/opt/loop-ad/kafka.sh');
        expect(templateText).toContain('-Xms256m -Xmx1024m');
        expect(templateText).toContain(testSecretNames.kafkaAppUserSecretName);
        expect(templateText).toContain(testSecretNames.kafkaBrokerUserSecretName);
        expect(templateText).toContain('APP_USER_SECRET_NAME');
        expect(templateText).toContain('BROKER_USER_SECRET_NAME');
        expect(templateText).toContain('secretsmanager:GetSecretValue');
        expect(ec2UserDataText).not.toContain('{{resolve:secretsmanager');
        expect(templateText).not.toContain('auto.create.topics.enable=true');
    });

    it('builds ClickHouse credentials config without Docker entrypoint XML interpolation', () => {
        const templateText = JSON.stringify(Template.fromStack(synthData()).toJSON());

        expect(templateText).toContain('CLICKHOUSE_CREDENTIALS_SECRET_NAME');
        expect(templateText).toContain('get-secret-value');
        expect(templateText).toContain('password_sha256_hex');
        expect(templateText).toContain('named_collection_control');
        expect(templateText).toContain('CREATE DATABASE IF NOT EXISTS');
        expect(templateText).toContain('/etc/clickhouse-server/users.d/loopad-user.xml:ro');
        expect(templateText).not.toContain('-e CLICKHOUSE_PASSWORD=');
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
