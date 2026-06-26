import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as msk from 'aws-cdk-lib/aws-msk';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

export const LOOP_AD_REGION = 'ap-northeast-2';
export const LOOP_AD_MONTHLY_COST_TARGET_USD = 300;

const DEV_SERVICE_DESIRED_TASKS = 1;
const DEV_SERVICE_MIN_TASKS = 1;
const DEV_SERVICE_MAX_TASKS = 2;
const SERVICE_CPU_SCALE_TARGET_PERCENT = 70;
const DEV_AURORA_MIN_ACU = 0;
const DEV_AURORA_MAX_ACU = 2;
const DEV_AURORA_AUTO_PAUSE_MINUTES = 10;
const DEV_CLICKHOUSE_VOLUME_GIB = 50;
const DEV_MSK_BROKER_COUNT = 2;
const DEV_MSK_STORAGE_GIB_PER_BROKER = 20;
const AURORA_DATABASE_NAME = 'loopad';
const EVENT_TOPIC_NAME = 'loop-ad.events.raw';
const REDIS_URL_PLACEHOLDER = 'pending://dev/redis';
const GENAI_GENERATED_ASSETS_PREFIX = 'genai/generated/';
const GENAI_PUBLIC_ASSETS_RECORD_NAME = 'gen-ai.asset.dev';
const DASHBOARD_WEB_RECORD_NAME = 'dashboard.dev';
const DEMO_SHOPPINGMALL_WEB_RECORD_NAME = 'demo-shoppingmall.dev';
const OPENAI_API_KEY_PARAMETER_NAME = '/loop-ad/dev/external/openai/api-key';

export interface PublicHostedZoneConfig {
    readonly hostedZoneId: string;
    readonly domainName: string;
}

export interface LoopAdDevStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
}

// 상시 개발 스택입니다. 공유 VPC와 항상 떠 있는 개발 서버를 소유합니다.
export class LoopAdDevStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdDevStackProps) {
        super(scope, id, props);

        // Dev가 유일한 VPC를 만듭니다. Dev server는 NAT가 있는 private subnet을 씁니다.
        const vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'dev-loop-ad-vpc',
            maxAzs: 2,
            natGateways: 1,
            subnetConfiguration: [
                {
                    name: 'public',
                    subnetType: ec2.SubnetType.PUBLIC,
                    cidrMask: 24,
                },
                {
                    name: 'private',
                    subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidrMask: 24,
                },
            ],
        });

        // 모든 ECS 서비스는 private subnet 그룹에서 실행됩니다.
        const privateSubnets = vpc.selectSubnets({ subnetGroupName: 'private' });

        // 비용 guardrail입니다. AWS Budget은 지출을 차단하지는 않지만 월간 목표를 계정에 명시합니다.
        new budgets.CfnBudget(this, 'MonthlyCostBudget', {
            budget: {
                budgetName: 'loop-ad-monthly-cost-target',
                budgetLimit: {
                    amount: LOOP_AD_MONTHLY_COST_TARGET_USD,
                    unit: 'USD',
                },
                budgetType: 'COST',
                timeUnit: 'MONTHLY',
            },
        });

        // S3 Gateway Endpoint는 hourly 비용 없이 route table에 붙습니다.
        // ECR layer 다운로드와 S3 접근 비용을 NAT data processing으로 보내지 않기 위해 유지합니다.
        vpc.addGatewayEndpoint('S3GatewayEndpoint', {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [privateSubnets],
        });

        // Dev는 상시 운영 환경이므로 Fargate cluster를 사용합니다.
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'dev-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'dev.loop-ad.local',
            },
        });

        // public ingress는 load balancer 종류별로 나누고, private 서비스는
        // stack을 읽기 쉽게 유지하기 위해 넓은 내부 SG를 공유합니다.
        const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev ALB public HTTP ingress only.',
        });
        const nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev NLB event ingestion ingress only.',
        });
        const serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev ECS server SG shared by app services.',
        });
        const dataStorageSecurityGroup = new ec2.SecurityGroup(this, 'DataStorageSecurityGroup', {
            vpc,
            allowAllOutbound: false,
            description: 'Dev data storage SG shared by internal data endpoints.',
        });

        // 인터넷 트래픽은 public load balancer만 받습니다.
        albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public HTTP to dev ALB.');
        nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Public ingest to dev NLB.');

        // VPC 내부에서는 SG 경계를 기준으로 server와 DataStorage가 서로 신뢰합니다.
        albSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'ALB may reach dev servers.');
        nlbSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach dev servers.');
        serverSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.allTraffic(), 'ALB may enter dev servers.');
        serverSecurityGroup.addIngressRule(nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter dev servers.');
        serverSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        serverSecurityGroup.addEgressRule(dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may reach internal data storage.');
        dataStorageSecurityGroup.addIngressRule(serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may enter internal data storage.');
        dataStorageSecurityGroup.addIngressRule(dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev data storage may call each other.');
        dataStorageSecurityGroup.addEgressRule(dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev data storage may call each other.');

        // Dev server는 외부 SaaS/API 및 AWS public API를 NAT로 호출합니다.
        serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev servers may use external HTTPS through NAT.');
        // ClickHouse bootstrap과 data storage 관리 작업도 NAT를 통해 HTTPS를 사용할 수 있습니다.
        dataStorageSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev data storage may use external HTTPS through NAT.');

        // ECR repository는 Dev 애플리케이션 이미지 storage를 소유합니다.
        const [
            eventCollectorRepository,
            projectorRepository,
            advertisementRepository,
            dashboardRepository,
            decisionRepository,
        ] = [
            { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector' },
            { id: 'AdContextProjectorRepository', repositoryName: 'loop-ad/ad-context-projector' },
            { id: 'AdvertisementApiRepository', repositoryName: 'loop-ad/advertisement-api' },
            { id: 'DashboardApiRepository', repositoryName: 'loop-ad/dashboard-api' },
            { id: 'DecisionRepository', repositoryName: 'loop-ad/decision' },
        ].map((repository) => new ecr.Repository(this, repository.id, {
            repositoryName: repository.repositoryName,
            imageScanOnPush: true,
            lifecycleRules: [{ maxImageCount: 20 }],
            removalPolicy: RemovalPolicy.RETAIN,
        }));

        // GenAI 생성물은 DataStorage S3 bucket의 전용 prefix에 저장합니다.
        const dataStorageBucket = new s3.Bucket(this, 'DataStorageBucket', {
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            versioned: true,
            objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removalPolicy: RemovalPolicy.RETAIN,
            lifecycleRules: [
                {
                    id: 'AbortIncompleteGenAiGeneratedUploads',
                    prefix: GENAI_GENERATED_ASSETS_PREFIX,
                    abortIncompleteMultipartUploadAfter: Duration.days(7),
                },
            ],
        });

        // .env에서 받은 public hosted zone을 import합니다.
        // fromHostedZoneAttributes는 synth 때 AWS lookup을 하지 않고 record template만 만듭니다.
        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        const dashboardWebDomainName = `${DASHBOARD_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const demoShoppingmallWebDomainName = `${DEMO_SHOPPINGMALL_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const frontendSitesCertificate = new acm.DnsValidatedCertificate(this, 'FrontendSitesCertificate', {
            domainName: dashboardWebDomainName,
            subjectAlternativeNames: [demoShoppingmallWebDomainName],
            hostedZone: publicHostedZone,
            region: 'us-east-1',
        });
        createStaticFrontendSite(this, {
            idPrefix: 'DashboardWeb',
            siteName: 'dashboard-web',
            bucketName: 'loop-ad-dev-dashboard-web',
            recordName: DASHBOARD_WEB_RECORD_NAME,
            domainName: dashboardWebDomainName,
            certificate: frontendSitesCertificate,
            publicHostedZone,
        });
        createStaticFrontendSite(this, {
            idPrefix: 'DemoShoppingmallWeb',
            siteName: 'demo-shoppingmall-web',
            bucketName: 'loop-ad-dev-demo-shoppingmall-web',
            recordName: DEMO_SHOPPINGMALL_WEB_RECORD_NAME,
            domainName: demoShoppingmallWebDomainName,
            certificate: frontendSitesCertificate,
            publicHostedZone,
        });
        const genAiPublicAssetsDomainName = `${GENAI_PUBLIC_ASSETS_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const genAiGeneratedAssetsPublicBaseUrl = `https://${genAiPublicAssetsDomainName}`;
        const genAiGeneratedAssetsCertificate = new acm.DnsValidatedCertificate(this, 'GenAiGeneratedAssetsCertificate', {
            domainName: genAiPublicAssetsDomainName,
            hostedZone: publicHostedZone,
            region: 'us-east-1',
        });
        const genAiGeneratedAssetsDistribution = new cloudfront.Distribution(this, 'GenAiGeneratedAssetsDistribution', {
            domainNames: [genAiPublicAssetsDomainName],
            certificate: genAiGeneratedAssetsCertificate,
            comment: `Dev GenAI generated assets for ${genAiPublicAssetsDomainName}`,
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(dataStorageBucket, {
                    originPath: `/${GENAI_GENERATED_ASSETS_PREFIX.replace(/\/$/, '')}`,
                }),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
                cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress: true,
            },
        });

        // 비용을 낮게 유지하는 개발용 data storage입니다.
        const auroraCluster = new rds.DatabaseCluster(this, 'AuroraPostgresCluster', {
            clusterIdentifier: 'dev-loop-ad-aurora-postgres',
            engine: rds.DatabaseClusterEngine.auroraPostgres({
                version: rds.AuroraPostgresEngineVersion.VER_16_13,
            }),
            writer: rds.ClusterInstance.serverlessV2('writer'),
            credentials: rds.Credentials.fromGeneratedSecret('loopad'),
            serverlessV2MinCapacity: DEV_AURORA_MIN_ACU,
            serverlessV2MaxCapacity: DEV_AURORA_MAX_ACU,
            serverlessV2AutoPauseDuration: Duration.minutes(DEV_AURORA_AUTO_PAUSE_MINUTES),
            defaultDatabaseName: AURORA_DATABASE_NAME,
            vpc,
            vpcSubnets: privateSubnets,
            securityGroups: [dataStorageSecurityGroup],
            backup: {
                retention: Duration.days(1),
            },
            deletionProtection: false,
            removalPolicy: RemovalPolicy.SNAPSHOT,
        });

        const clickHouseInstance = new ec2.Instance(this, 'ClickHouseInstance', {
            vpc,
            vpcSubnets: privateSubnets,
            securityGroup: dataStorageSecurityGroup,
            instanceName: 'dev-loop-ad-clickhouse',
            instanceType: new ec2.InstanceType('t4g.small'),
            machineImage: ec2.MachineImage.latestAmazonLinux2023({
                cpuType: ec2.AmazonLinuxCpuType.ARM_64,
            }),
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: ec2.BlockDeviceVolume.ebs(DEV_CLICKHOUSE_VOLUME_GIB, {
                        encrypted: true,
                        volumeType: ec2.EbsDeviceVolumeType.GP3,
                    }),
                },
            ],
            requireImdsv2: true,
        });
        clickHouseInstance.userData.addCommands(
            'set -eux',
            'dnf update -y',
            'dnf install -y docker',
            'systemctl enable --now docker',
            'mkdir -p /var/lib/clickhouse',
            'docker run -d --restart unless-stopped --name clickhouse-server -p 8123:8123 -p 9000:9000 -v /var/lib/clickhouse:/var/lib/clickhouse clickhouse/clickhouse-server:latest',
        );

        const mskCluster = new msk.CfnCluster(this, 'MskCluster', {
            clusterName: 'dev-loop-ad-msk',
            kafkaVersion: '3.6.0',
            numberOfBrokerNodes: DEV_MSK_BROKER_COUNT,
            brokerNodeGroupInfo: {
                clientSubnets: privateSubnets.subnetIds,
                instanceType: 'kafka.t3.small',
                securityGroups: [dataStorageSecurityGroup.securityGroupId],
                storageInfo: {
                    ebsStorageInfo: {
                        volumeSize: DEV_MSK_STORAGE_GIB_PER_BROKER,
                    },
                },
            },
            clientAuthentication: {
                unauthenticated: {
                    enabled: true,
                },
            },
            encryptionInfo: {
                encryptionInTransit: {
                    clientBroker: 'TLS_PLAINTEXT',
                    inCluster: true,
                },
            },
            enhancedMonitoring: 'DEFAULT',
        });

        const mskBootstrapBrokers = new cr.AwsCustomResource(this, 'MskBootstrapBrokers', {
            resourceType: 'Custom::LoopAdMskBootstrapBrokers',
            onUpdate: {
                service: 'kafka',
                action: 'GetBootstrapBrokers',
                parameters: {
                    ClusterArn: mskCluster.attrArn,
                },
                outputPaths: ['BootstrapBrokerString'],
                physicalResourceId: cr.PhysicalResourceId.of('dev-loop-ad-msk-bootstrap-brokers'),
            },
            installLatestAwsSdk: false,
            logGroup: new logs.LogGroup(this, 'MskBootstrapBrokersLogGroup', {
                retention: logs.RetentionDays.THREE_DAYS,
                removalPolicy: RemovalPolicy.DESTROY,
            }),
            policy: cr.AwsCustomResourcePolicy.fromStatements([
                new iam.PolicyStatement({
                    actions: ['kafka:GetBootstrapBrokers'],
                    resources: [mskCluster.attrArn],
                }),
            ]),
        });

        const auroraHost = auroraCluster.clusterEndpoint.hostname;
        const auroraPort = '5432';
        const clickHouseUrl = cdk.Fn.join('', ['http://', clickHouseInstance.instancePrivateDnsName, ':8123']);
        const mskBootstrapBrokerString = mskBootstrapBrokers.getResponseField('BootstrapBrokerString');
        const auroraCredentialsSecret = auroraCluster.secret;
        if (!auroraCredentialsSecret) {
            throw new Error('Aurora generated credentials secret is required.');
        }

        const openAiApiKeyParameter = ssm.StringParameter.fromSecureStringParameterAttributes(this, 'OpenAiApiKeyParameter', {
            parameterName: OPENAI_API_KEY_PARAMETER_NAME,
        });

        // endpoint contract는 SSM에도 남깁니다. 앱 task에는 아래 값을 env로 직접 주입합니다.
        for (const parameter of [
            {
                id: 'AuroraEndpointParameter',
                parameterName: '/loop-ad/dev/aurora/endpoint',
                stringValue: auroraHost,
                description: 'Dev Aurora PostgreSQL endpoint contract.',
            },
            {
                id: 'RedisEndpointParameter',
                parameterName: '/loop-ad/dev/redis/endpoint',
                stringValue: REDIS_URL_PLACEHOLDER,
                description: 'Dev Redis endpoint contract.',
            },
            {
                id: 'ClickHouseEndpointParameter',
                parameterName: '/loop-ad/dev/clickhouse/endpoint',
                stringValue: clickHouseUrl,
                description: 'Dev ClickHouse endpoint contract.',
            },
            {
                id: 'MskEndpointParameter',
                parameterName: '/loop-ad/dev/msk/bootstrap-brokers',
                stringValue: mskBootstrapBrokerString,
                description: 'Dev MSK bootstrap broker contract.',
            },
            {
                id: 'DataStorageBucketNameParameter',
                parameterName: '/loop-ad/dev/data-storage/bucket-name',
                stringValue: dataStorageBucket.bucketName,
                description: 'Dev DataStorage S3 bucket name contract.',
            },
            {
                id: 'GenAiGeneratedAssetsPrefixParameter',
                parameterName: '/loop-ad/dev/data-storage/genai-generated-prefix',
                stringValue: GENAI_GENERATED_ASSETS_PREFIX,
                description: 'Dev DataStorage GenAI generated assets prefix contract.',
            },
            {
                id: 'GenAiGeneratedAssetsPublicUrlParameter',
                parameterName: '/loop-ad/dev/data-storage/genai-generated-assets-public-base-url',
                stringValue: genAiGeneratedAssetsPublicBaseUrl,
                description: 'Dev DataStorage GenAI generated assets public base URL contract.',
            },
        ]) {
            new ssm.StringParameter(this, parameter.id, {
                parameterName: parameter.parameterName,
                stringValue: parameter.stringValue,
                description: parameter.description,
            });
        }

        // ALB는 API 경로를 열고, NLB는 raw event ingestion 경로를 엽니다.
        const alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroup: albSecurityGroup,
            vpcSubnets: { subnetGroupName: 'public' },
        });
        const albListener = alb.addListener('HttpListener', {
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'No loop-ad API route is registered.',
            }),
        });
        const nlb = new elbv2.NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroups: [nlbSecurityGroup],
            vpcSubnets: { subnetGroupName: 'public' },
        });

        for (const dnsRecord of [
            {
                id: 'DevApiDnsRecord',
                recordName: 'api.dev',
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(alb)),
            },
            {
                id: 'DevIngestDnsRecord',
                recordName: 'ingest.dev',
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(nlb)),
            },
            {
                id: 'GenAiGeneratedAssetsDnsRecord',
                recordName: GENAI_PUBLIC_ASSETS_RECORD_NAME,
                target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(genAiGeneratedAssetsDistribution)),
            },
        ] as const) {
            new route53.ARecord(this, dnsRecord.id, {
                zone: publicHostedZone,
                recordName: dnsRecord.recordName,
                target: dnsRecord.target,
            });
        }

        // Event Collector는 NLB 트래픽을 받고 event를 MSK로 발행합니다.
        const eventCollectorTask = new ecs.FargateTaskDefinition(this, 'EventCollectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const eventCollectorLogGroup = new logs.LogGroup(this, 'EventCollectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const eventCollectorContainer = eventCollectorTask.addContainer('EventCollectorContainer', {
            containerName: 'event-collector',
            image: ecs.ContainerImage.fromEcrRepository(eventCollectorRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'event-collector',
                logGroup: eventCollectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'event-collector',
                LOOPAD_RUNTIME: 'go',
                PORT: '80',
                LOOPAD_MSK_BOOTSTRAP_BROKERS: mskBootstrapBrokerString,
                LOOPAD_EVENT_TOPIC: EVENT_TOPIC_NAME,
            },
        });
        eventCollectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const eventCollectorService = new ecs.FargateService(this, 'EventCollectorService', {
            cluster,
            taskDefinition: eventCollectorTask,
            serviceName: 'dev-event-collector',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'event-collector' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        eventCollectorService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('EventCollectorCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

        // NLB는 TCP 80 포트를 collector service로 직접 전달합니다.
        const nlbListener = nlb.addListener('EventCollectorListener', {
            port: 80,
            protocol: elbv2.Protocol.TCP,
        });
        nlbListener.addTargets('EventCollectorTargets', {
            targets: [eventCollectorService],
            port: 80,
            protocol: elbv2.Protocol.TCP,
            healthCheck: {
                enabled: true,
                port: '80',
            },
        });

        // Projector는 MSK를 consume하고 가공된 context를 Redis/ClickHouse에 씁니다.
        const projectorTask = new ecs.FargateTaskDefinition(this, 'AdContextProjectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const projectorLogGroup = new logs.LogGroup(this, 'AdContextProjectorLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const projectorContainer = projectorTask.addContainer('AdContextProjectorContainer', {
            containerName: 'ad-context-projector',
            image: ecs.ContainerImage.fromEcrRepository(projectorRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'ad-context-projector',
                logGroup: projectorLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'ad-context-projector',
                LOOPAD_RUNTIME: 'go',
                PORT: '80',
                LOOPAD_MSK_BOOTSTRAP_BROKERS: mskBootstrapBrokerString,
                LOOPAD_EVENT_TOPIC: EVENT_TOPIC_NAME,
                LOOPAD_REDIS_URL: REDIS_URL_PLACEHOLDER,
                LOOPAD_CLICKHOUSE_URL: clickHouseUrl,
                LOOPAD_CLICKHOUSE_USERNAME: 'default',
            },
        });
        projectorContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const projectorService = new ecs.FargateService(this, 'AdContextProjectorService', {
            cluster,
            taskDefinition: projectorTask,
            serviceName: 'dev-ad-context-projector',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'ad-context-projector' },
        });
        projectorService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('AdContextProjectorCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

        // Advertisement API는 ALB를 통해 public 광고 조회 경로를 제공합니다.
        const advertisementTask = new ecs.FargateTaskDefinition(this, 'AdvertisementApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const advertisementLogGroup = new logs.LogGroup(this, 'AdvertisementApiLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const advertisementContainer = advertisementTask.addContainer('AdvertisementApiContainer', {
            containerName: 'advertisement-api',
            image: ecs.ContainerImage.fromEcrRepository(advertisementRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'advertisement-api',
                logGroup: advertisementLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'advertisement-api',
                LOOPAD_RUNTIME: 'go',
                PORT: '80',
                LOOPAD_REDIS_URL: REDIS_URL_PLACEHOLDER,
                LOOPAD_AURORA_HOST: auroraHost,
                LOOPAD_AURORA_PORT: auroraPort,
                LOOPAD_AURORA_DATABASE: AURORA_DATABASE_NAME,
            },
            secrets: {
                LOOPAD_AURORA_USERNAME: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'username'),
                LOOPAD_AURORA_PASSWORD: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'password'),
            },
        });
        advertisementContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const advertisementService = new ecs.FargateService(this, 'AdvertisementApiService', {
            cluster,
            taskDefinition: advertisementTask,
            serviceName: 'dev-advertisement-api',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'advertisement-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        advertisementService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('AdvertisementApiCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });
        albListener.addTargets('AdvertisementApiTargets', {
            targets: [advertisementService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 20,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/ads/*', '/advertisements/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200-399',
            },
        });

        // Dashboard API는 dashboard 경로를 제공하고 Cloud Map으로 Decision을 호출합니다.
        const dashboardTask = new ecs.FargateTaskDefinition(this, 'DashboardApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        dataStorageBucket.grantRead(dashboardTask.taskRole, `${GENAI_GENERATED_ASSETS_PREFIX}*`);
        const dashboardLogGroup = new logs.LogGroup(this, 'DashboardApiLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const dashboardContainer = dashboardTask.addContainer('DashboardApiContainer', {
            containerName: 'dashboard-api',
            image: ecs.ContainerImage.fromEcrRepository(dashboardRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'dashboard-api',
                logGroup: dashboardLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'dashboard-api',
                LOOPAD_RUNTIME: 'go',
                PORT: '80',
                LOOPAD_AURORA_HOST: auroraHost,
                LOOPAD_AURORA_PORT: auroraPort,
                LOOPAD_AURORA_DATABASE: AURORA_DATABASE_NAME,
                LOOPAD_CLICKHOUSE_URL: clickHouseUrl,
                LOOPAD_CLICKHOUSE_USERNAME: 'default',
                LOOPAD_DATA_STORAGE_BUCKET: dataStorageBucket.bucketName,
                LOOPAD_GENAI_GENERATED_ASSETS_PREFIX: GENAI_GENERATED_ASSETS_PREFIX,
            },
            secrets: {
                LOOPAD_AURORA_USERNAME: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'username'),
                LOOPAD_AURORA_PASSWORD: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'password'),
            },
        });
        dashboardContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const dashboardService = new ecs.FargateService(this, 'DashboardApiService', {
            cluster,
            taskDefinition: dashboardTask,
            serviceName: 'dev-dashboard-api',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'dashboard-api' },
            healthCheckGracePeriod: Duration.seconds(60),
        });
        dashboardService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('DashboardApiCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });
        albListener.addTargets('DashboardApiTargets', {
            targets: [dashboardService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 30,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/dashboard/*', '/dashboard/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200-399',
            },
        });

        // Decision은 private 전용이며 public ALB에 연결하지 않습니다.
        const decisionTask = new ecs.FargateTaskDefinition(this, 'DecisionTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        dataStorageBucket.grantReadWrite(decisionTask.taskRole, `${GENAI_GENERATED_ASSETS_PREFIX}*`);
        const decisionLogGroup = new logs.LogGroup(this, 'DecisionLogGroup', {
            retention: logs.RetentionDays.THREE_DAYS,
        });
        const decisionContainer = decisionTask.addContainer('DecisionContainer', {
            containerName: 'decision',
            image: ecs.ContainerImage.fromEcrRepository(decisionRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'decision',
                logGroup: decisionLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'decision',
                LOOPAD_RUNTIME: 'go',
                PORT: '80',
                LOOPAD_AURORA_HOST: auroraHost,
                LOOPAD_AURORA_PORT: auroraPort,
                LOOPAD_AURORA_DATABASE: AURORA_DATABASE_NAME,
                LOOPAD_CLICKHOUSE_URL: clickHouseUrl,
                LOOPAD_CLICKHOUSE_USERNAME: 'default',
                LOOPAD_DATA_STORAGE_BUCKET: dataStorageBucket.bucketName,
                LOOPAD_GENAI_GENERATED_ASSETS_PREFIX: GENAI_GENERATED_ASSETS_PREFIX,
            },
            secrets: {
                LOOPAD_AURORA_USERNAME: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'username'),
                LOOPAD_AURORA_PASSWORD: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'password'),
                LOOPAD_OPENAI_API_KEY: ecs.Secret.fromSsmParameter(openAiApiKeyParameter),
            },
        });
        decisionContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const decisionService = new ecs.FargateService(this, 'DecisionService', {
            cluster,
            taskDefinition: decisionTask,
            serviceName: 'dev-decision',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'decision' },
        });
        decisionService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('DecisionCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

    }
}

interface StaticFrontendSiteConfig {
    readonly idPrefix: string;
    readonly siteName: string;
    readonly bucketName: string;
    readonly recordName: string;
    readonly domainName: string;
    readonly certificate: acm.ICertificate;
    readonly publicHostedZone: route53.IHostedZone;
}

function createStaticFrontendSite(scope: Construct, config: StaticFrontendSiteConfig): void {
    const bucket = new s3.Bucket(scope, `${config.idPrefix}Bucket`, {
        bucketName: config.bucketName,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        versioned: true,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        removalPolicy: RemovalPolicy.RETAIN,
    });
    const distribution = new cloudfront.Distribution(scope, `${config.idPrefix}Distribution`, {
        domainNames: [config.domainName],
        certificate: config.certificate,
        comment: `Dev ${config.siteName} frontend for ${config.domainName}`,
        defaultRootObject: 'index.html',
        priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
        defaultBehavior: {
            origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
            viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
            cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
            compress: true,
        },
        errorResponses: [
            {
                httpStatus: 403,
                responseHttpStatus: 200,
                responsePagePath: '/index.html',
                ttl: Duration.seconds(0),
            },
            {
                httpStatus: 404,
                responseHttpStatus: 200,
                responsePagePath: '/index.html',
                ttl: Duration.seconds(0),
            },
        ],
    });
    new route53.ARecord(scope, `${config.idPrefix}DnsRecord`, {
        zone: config.publicHostedZone,
        recordName: config.recordName,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
    });

    for (const parameter of [
        {
            id: `${config.idPrefix}BucketNameParameter`,
            name: 'bucket-name',
            value: config.bucketName,
            description: `Dev ${config.siteName} frontend S3 bucket name.`,
        },
        {
            id: `${config.idPrefix}CloudFrontDistributionIdParameter`,
            name: 'cloudfront-distribution-id',
            value: distribution.distributionId,
            description: `Dev ${config.siteName} frontend CloudFront distribution ID.`,
        },
    ] as const) {
        new ssm.StringParameter(scope, parameter.id, {
            parameterName: `/loop-ad/dev/frontend/${config.siteName}/${parameter.name}`,
            stringValue: parameter.value,
            description: parameter.description,
        });
    }
}
