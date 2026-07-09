import { Template } from 'aws-cdk-lib/assertions';
import {
    EXPECTED_APP_INTERNAL_PORT,
    resourcesOf,
    synthRuntime,
    testSecretNames,
} from './helpers';

describe('runtime architecture', () => {
    it('uses one public ALB and no NLB', () => {
        const template = Template.fromStack(synthRuntime());

        template.resourcePropertiesCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', {
            Type: 'application',
            Scheme: 'internet-facing',
        }, 1);
        template.resourcePropertiesCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', {
            Type: 'network',
        }, 0);
        template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
            Port: 443,
            Protocol: 'HTTPS',
        });
        expect(JSON.stringify(template.toJSON())).not.toContain('ingest.dev');
    });

    it('keeps exactly the three public Fargate services with service-specific capacity', () => {
        const template = Template.fromStack(synthRuntime());

        template.resourceCountIs('AWS::ECS::Service', 3);
        template.resourceCountIs('AWS::Logs::LogGroup', 3);
        template.resourcePropertiesCountIs('AWS::ECS::TaskDefinition', {
            Cpu: '256',
            Memory: '512',
        }, 1);
        template.resourcePropertiesCountIs('AWS::ECS::TaskDefinition', {
            Cpu: '512',
            Memory: '1024',
        }, 1);
        template.resourcePropertiesCountIs('AWS::ECS::TaskDefinition', {
            Cpu: '1024',
            Memory: '2048',
        }, 1);
        template.resourcePropertiesCountIs('AWS::ECS::Service', {
            DesiredCount: 1,
            LaunchType: 'FARGATE',
            NetworkConfiguration: {
                AwsvpcConfiguration: {
                    AssignPublicIp: 'ENABLED',
                },
            },
        }, 3);
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 1,
            MaxCapacity: 1,
        }, 1);
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 1,
            MaxCapacity: 2,
        }, 1);
        template.resourcePropertiesCountIs('AWS::ApplicationAutoScaling::ScalableTarget', {
            MinCapacity: 1,
            MaxCapacity: 4,
        }, 1);
        for (const serviceId of ['event-collector', 'dashboard-api', 'decision-api']) {
            template.hasResourceProperties('AWS::ECS::Service', {
                ServiceName: `dev-${serviceId}`,
            });
            template.hasResourceProperties('AWS::Logs::LogGroup', {
                LogGroupName: `/loop-ad/dev/ecs/${serviceId}`,
                RetentionInDays: 90,
            });
        }
        expect(JSON.stringify(template.toJSON())).not.toContain('advertisement-api');
    });

    it('routes public API hosts to the expected service target groups', () => {
        const template = Template.fromStack(synthRuntime());

        for (const hostHeader of [
            'event.api.dev.example.test',
            'dashboard.api.dev.example.test',
            'decision.api.dev.example.test',
        ]) {
            template.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
                Conditions: [
                    {
                        Field: 'host-header',
                        HostHeaderConfig: {
                            Values: [hostHeader],
                        },
                    },
                ],
            });
        }
        template.resourcePropertiesCountIs('AWS::ElasticLoadBalancingV2::TargetGroup', {
            Port: EXPECTED_APP_INTERNAL_PORT,
            Protocol: 'HTTP',
            HealthCheckPath: '/health',
            HealthCheckPort: String(EXPECTED_APP_INTERNAL_PORT),
        }, 3);
        template.resourcePropertiesCountIs('AWS::Route53::RecordSet', {
            Type: 'A',
        }, 5);
    });

    it('injects Secrets Manager fields for app credentials and internal key verification', () => {
        const templateText = JSON.stringify(Template.fromStack(synthRuntime()).toJSON());

        expect(templateText).toContain('LOOPAD_INTERNAL_API_KEY');
        expect(templateText).toContain('LOOPAD_OPEN_PIXEL_SIGNING_SECRET');
        expect(templateText).toContain('LOOPAD_OPENAI_API_KEY');
        expect(templateText).toContain('LOOPAD_DEMO_DISPATCH_RECIPIENTS');
        expect(templateText).toContain(testSecretNames.openAiApiKeySecretName);
        expect(templateText).toContain(testSecretNames.internalApiKeySecretName);
        expect(templateText).toContain(testSecretNames.openPixelSigningSecretName);
        expect(templateText).toContain(testSecretNames.demoDispatchRecipientsSecretName);
        expect(templateText).toContain(testSecretNames.geminiApiKeySecretName);
        expect(templateText).toContain('api_key');
        expect(templateText).toContain('LOOPAD_GEMINI_API_KEY');
        expect(templateText).toContain('LOOPAD_KAFKA_USERNAME');
        expect(templateText).toContain('LOOPAD_AURORA_PASSWORD');
        expect(templateText).toContain('LOOPAD_CLICKHOUSE_PASSWORD');
        expect(templateText).not.toContain('LOOPAD_OPENAI_API_KEY_PARAMETER_NAME');
    });

    it('keeps runtime env contracts aligned with the new data model', () => {
        const template = Template.fromStack(synthRuntime());
        const resources = resourcesOf(template);
        const taskDefinitions = Object.values(resources).filter((resource) => resource.Type === 'AWS::ECS::TaskDefinition');

        expect(taskDefinitions).toHaveLength(3);
        for (const taskDefinition of taskDefinitions) {
            const containers = taskDefinition.Properties?.ContainerDefinitions as Array<Record<string, unknown>>;
            expect(containers).toHaveLength(1);
            const container = containers[0] as {
                Environment?: Array<{ Name?: string; Value?: string }>;
                PortMappings?: Array<{ ContainerPort?: number }>;
            };
            expect(container.PortMappings).toEqual(expect.arrayContaining([
                expect.objectContaining({ ContainerPort: EXPECTED_APP_INTERNAL_PORT }),
            ]));
            expect(container.Environment).toEqual(expect.arrayContaining([
                expect.objectContaining({ Name: 'PORT', Value: String(EXPECTED_APP_INTERNAL_PORT) }),
                expect.objectContaining({ Name: 'LOOPAD_ENV', Value: 'dev' }),
            ]));
        }

        const templateText = JSON.stringify(template.toJSON());
        expect(templateText).toContain('LOOPAD_EVENT_TOPIC');
        expect(templateText).toContain('loop-ad.events.raw');
        expect(templateText).toContain('LOOPAD_CLICKHOUSE_DATABASE');
        expect(templateText).toContain('loopad');
        expect(templateText).toContain('LOOPAD_GENAI_ASSETS_BASE_PREFIX');
        expect(templateText).toContain('genai/');
        expect(templateText).not.toContain('LOOPAD_GENAI_GENERATED_ASSETS_PREFIX');
        expect(templateText).not.toContain('genai/generated/');
        expect(templateText).not.toContain('LOOPAD_REDIS_URL');
        expect(templateText).not.toContain('EventBridge');
    });

    it('grants dashboard API only the dispatch provider actions it needs', () => {
        const templateText = JSON.stringify(Template.fromStack(synthRuntime()).toJSON());

        expect(templateText).toContain('ses:SendEmail');
        expect(templateText).toContain('ses:FromAddress');
        expect(templateText).toContain('noreply@loop-ad.org');
        expect(templateText).toContain('sms-voice:SendTextMessage');
        expect(templateText).not.toContain('sns:Publish');
    });

    it('destroys static frontend buckets while retaining app data buckets in the data stack only', () => {
        const template = Template.fromStack(synthRuntime());
        const resources = resourcesOf(template);
        const frontendBuckets = Object.values(resources).filter((resource) => resource.Type === 'AWS::S3::Bucket');

        expect(frontendBuckets).toHaveLength(2);
        expect(frontendBuckets.every((bucket) => bucket.DeletionPolicy === 'Delete')).toBe(true);
        expect(JSON.stringify(template.toJSON())).toContain('Custom::S3AutoDeleteObjects');
    });
});
