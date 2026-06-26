import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as msk from 'aws-cdk-lib/aws-msk';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

export const LOOP_AD_REGION = 'ap-northeast-2';

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
const DEV_CLICKHOUSE_IMAGE = 'clickhouse/clickhouse-server:26.3.13.31';
const DEV_MSK_KAFKA_VERSION = '3.9.x';
const DEV_VALKEY_MAJOR_ENGINE_VERSION = '7';
const DEV_VALKEY_MAX_DATA_STORAGE_GB = 1;
const DEV_VALKEY_MAX_ECPU_PER_SECOND = 1000;
const DEV_VPC_AVAILABILITY_ZONES = ['ap-northeast-2a', 'ap-northeast-2c'];
const DEV_ECS_LOG_GROUP_PREFIX = '/loop-ad/dev/ecs';
const DEV_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS;
const AURORA_DATABASE_NAME = 'loopad';
const EVENT_TOPIC_NAME = 'loop-ad.events.raw';
const GENAI_GENERATED_ASSETS_PREFIX = 'genai/generated/';
const PUBLIC_API_RECORD_NAME = 'api.dev';
const PUBLIC_INGEST_RECORD_NAME = 'ingest.dev';
const GENAI_PUBLIC_ASSETS_RECORD_NAME = 'gen-ai.asset.dev';
const DASHBOARD_WEB_RECORD_NAME = 'dashboard.dev';
const DEMO_SHOPPINGMALL_WEB_RECORD_NAME = 'demo-shoppingmall.dev';
const OPENAI_API_KEY_PARAMETER_NAME = '/loop-ad/dev/external/openai/api-key';
const DEV_APPLICATION_REPOSITORIES = [
    { id: 'EventCollectorRepository', repositoryName: 'loop-ad/event-collector', outputId: 'EventCollectorRepositoryUri' },
    { id: 'AdContextProjectorRepository', repositoryName: 'loop-ad/ad-context-projector', outputId: 'AdContextProjectorRepositoryUri' },
    { id: 'AdvertisementApiRepository', repositoryName: 'loop-ad/advertisement-api', outputId: 'AdvertisementApiRepositoryUri' },
    { id: 'DashboardApiRepository', repositoryName: 'loop-ad/dashboard-api', outputId: 'DashboardApiRepositoryUri' },
    { id: 'DecisionApiRepository', repositoryName: 'loop-ad/decision-api', outputId: 'DecisionApiRepositoryUri' },
] as const;

type DevApplicationRepositories = [ecr.IRepository, ecr.IRepository, ecr.IRepository, ecr.IRepository, ecr.IRepository];

export interface PublicHostedZoneConfig {
    readonly hostedZoneId: string;
    readonly domainName: string;
}

export interface LoopAdDevCertificateStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
}

// Certificate stack의 출력값입니다.
// 서로 다른 region stack을 직접 참조하지 않고 ARN만 넘겨 data/runtime stack synth를 단순하게 유지합니다.
export interface LoopAdDevCertificateArns {
    readonly frontendSitesCertificateArn: string;
    readonly genAiGeneratedAssetsCertificateArn: string;
}

// CloudFront custom domain에 연결하는 ACM certificate는 us-east-1에 있어야 합니다.
// 인증서는 자주 바뀌지 않으므로 dev data/runtime stack과 분리해 먼저 배포합니다.
export class LoopAdDevCertificateStack extends Stack {
    public readonly frontendSitesCertificate: acm.ICertificate;
    public readonly genAiGeneratedAssetsCertificate: acm.ICertificate;

    public constructor(scope: Construct, id: string, props: LoopAdDevCertificateStackProps) {
        super(scope, id, props);

        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        const dashboardWebDomainName = `${DASHBOARD_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const demoShoppingmallWebDomainName = `${DEMO_SHOPPINGMALL_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        this.frontendSitesCertificate = new acm.Certificate(this, 'FrontendSitesCertificate', {
            domainName: dashboardWebDomainName,
            subjectAlternativeNames: [demoShoppingmallWebDomainName],
            validation: acm.CertificateValidation.fromDns(publicHostedZone),
        });
        new cdk.CfnOutput(this, 'FrontendSitesCertificateArn', {
            value: this.frontendSitesCertificate.certificateArn,
        });

        const genAiPublicAssetsDomainName = `${GENAI_PUBLIC_ASSETS_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        this.genAiGeneratedAssetsCertificate = new acm.Certificate(this, 'GenAiGeneratedAssetsCertificate', {
            domainName: genAiPublicAssetsDomainName,
            validation: acm.CertificateValidation.fromDns(publicHostedZone),
        });
        new cdk.CfnOutput(this, 'GenAiGeneratedAssetsCertificateArn', {
            value: this.genAiGeneratedAssetsCertificate.certificateArn,
        });
    }
}

// ECR repository는 ECS보다 먼저 배포해야 합니다.
// 각 앱 repo가 image를 직접 push한 뒤 runtime stack을 배포하면, 첫 ECS 배포 시 image not found를 피할 수 있습니다.
export class LoopAdDevRepositoryStack extends Stack {
    public constructor(scope: Construct, id: string, props?: StackProps) {
        super(scope, id, props);

        for (const repositoryConfig of DEV_APPLICATION_REPOSITORIES) {
            const repository = new ecr.Repository(this, repositoryConfig.id, {
                repositoryName: repositoryConfig.repositoryName,
                imageScanOnPush: true,
                lifecycleRules: [{ maxImageCount: 20 }],
                removalPolicy: RemovalPolicy.RETAIN,
            });

            new cdk.CfnOutput(this, repositoryConfig.outputId, {
                value: repository.repositoryUri,
            });
        }
    }
}

// VPC, subnet, endpoint, security group은 애플리케이션보다 변경 주기가 깁니다.
// 최초 배포 전에 network stack으로 분리해 두면 이후 data/runtime 변경의 영향 범위를 줄일 수 있습니다.
export class LoopAdDevNetworkStack extends Stack {
    public readonly vpc: ec2.Vpc;
    public readonly privateSubnetSelection: ec2.SubnetSelection;
    public readonly privateSubnets: ec2.SelectedSubnets;
    public readonly albSecurityGroup: ec2.SecurityGroup;
    public readonly nlbSecurityGroup: ec2.SecurityGroup;
    public readonly serverSecurityGroup: ec2.SecurityGroup;
    public readonly dataStorageSecurityGroup: ec2.SecurityGroup;

    public constructor(scope: Construct, id: string, props?: StackProps) {
        super(scope, id, props);

        // Dev server는 NAT가 있는 private subnet을 씁니다.
        this.vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'dev-loop-ad-vpc',
            availabilityZones: DEV_VPC_AVAILABILITY_ZONES,
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
        this.privateSubnetSelection = { subnetGroupName: 'private' };
        this.privateSubnets = this.vpc.selectSubnets(this.privateSubnetSelection);

        // S3 Gateway Endpoint는 hourly 비용 없이 route table에 붙습니다.
        // ECR layer 다운로드와 S3 접근 비용을 NAT data processing으로 보내지 않기 위해 유지합니다.
        this.vpc.addGatewayEndpoint('S3GatewayEndpoint', {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [this.privateSubnets],
        });

        // public ingress는 load balancer 종류별로 나누고, private 서비스는
        // stack을 읽기 쉽게 유지하기 위해 넓은 내부 SG를 공유합니다.
        this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev ALB public HTTPS ingress.',
        });
        this.nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev NLB event ingestion TLS ingress.',
        });
        this.serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev ECS server SG shared by app services.',
        });
        this.dataStorageSecurityGroup = new ec2.SecurityGroup(this, 'DataStorageSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev data storage SG shared by internal data endpoints.',
        });

        // 인터넷 트래픽은 public load balancer의 443만 받습니다.
        // load balancer에서 TLS를 종료하고 private ECS container의 80 포트로 전달합니다.
        this.albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Public HTTPS to dev ALB.');
        this.nlbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Public TLS ingest to dev NLB.');

        // VPC 내부에서는 SG 경계를 기준으로 server와 DataStorage가 서로 신뢰합니다.
        this.albSecurityGroup.addEgressRule(this.serverSecurityGroup, ec2.Port.allTraffic(), 'ALB may reach dev servers.');
        this.nlbSecurityGroup.addEgressRule(this.serverSecurityGroup, ec2.Port.allTraffic(), 'NLB may reach dev servers.');
        this.serverSecurityGroup.addIngressRule(this.albSecurityGroup, ec2.Port.allTraffic(), 'ALB may enter dev servers.');
        this.serverSecurityGroup.addIngressRule(this.nlbSecurityGroup, ec2.Port.allTraffic(), 'NLB may enter dev servers.');
        this.serverSecurityGroup.addIngressRule(this.serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        this.serverSecurityGroup.addEgressRule(this.serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may call each other.');
        this.serverSecurityGroup.addEgressRule(this.dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may reach internal data storage.');
        this.dataStorageSecurityGroup.addIngressRule(this.serverSecurityGroup, ec2.Port.allTraffic(), 'Dev servers may enter internal data storage.');
        this.dataStorageSecurityGroup.addIngressRule(this.dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev data storage may call each other.');
        this.dataStorageSecurityGroup.addEgressRule(this.dataStorageSecurityGroup, ec2.Port.allTraffic(), 'Dev data storage may call each other.');

        // Dev server는 외부 SaaS/API 및 AWS public API를 NAT로 호출합니다.
        this.serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev servers may use external HTTPS through NAT.');
        // ClickHouse bootstrap과 data storage 관리 작업도 NAT를 통해 HTTPS를 사용할 수 있습니다.
        this.dataStorageSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Dev data storage may use external HTTPS through NAT.');
    }
}

export interface LoopAdDevDataStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
    readonly genAiGeneratedAssetsCertificateArn: string;
    readonly network: LoopAdDevNetworkStack;
}

// 데이터 저장소와 endpoint contract를 소유하는 스택입니다.
// Runtime보다 먼저 배포해 ECS task가 참조할 storage endpoint를 안정적으로 제공합니다.
export class LoopAdDevDataStack extends Stack {
    public readonly dataStorageBucket: s3.Bucket;
    public readonly auroraHost: string;
    public readonly auroraPort: string;
    public readonly auroraCredentialsSecret: secretsmanager.ISecret;
    public readonly redisUrl: string;
    public readonly clickHouseUrl: string;
    public readonly mskBootstrapBrokerString: string;

    public constructor(scope: Construct, id: string, props: LoopAdDevDataStackProps) {
        super(scope, id, props);

        // Network stack에서 만든 기반 리소스를 재사용합니다.
        // 아직 배포 전이라 stack 분리로 인한 기존 리소스 이동/import 문제는 고려하지 않아도 됩니다.
        const {
            vpc,
            privateSubnets,
            dataStorageSecurityGroup,
        } = props.network;

        // GenAI 생성물은 DataStorage S3 bucket의 전용 prefix에 저장합니다.
        // bucket은 직접 public으로 열지 않고 CloudFront OAC를 통해 필요한 prefix만 읽히게 합니다.
        this.dataStorageBucket = new s3.Bucket(this, 'DataStorageBucket', {
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
        const genAiPublicAssetsDomainName = `${GENAI_PUBLIC_ASSETS_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const genAiGeneratedAssetsPublicBaseUrl = `https://${genAiPublicAssetsDomainName}`;
        // GenAI asset용 certificate도 별도 ARN으로 받습니다.
        // domain 범위를 분리해 두면 나중에 frontend certificate를 바꿔도 asset 배포 경로 영향이 작습니다.
        const genAiGeneratedAssetsCertificate = acm.Certificate.fromCertificateArn(
            this,
            'GenAiGeneratedAssetsCertificate',
            props.genAiGeneratedAssetsCertificateArn,
        );
        const genAiGeneratedAssetsDistribution = new cloudfront.Distribution(this, 'GenAiGeneratedAssetsDistribution', {
            domainNames: [genAiPublicAssetsDomainName],
            certificate: genAiGeneratedAssetsCertificate,
            comment: `Dev GenAI generated assets for ${genAiPublicAssetsDomainName}`,
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            defaultBehavior: {
                // originPath로 generated prefix만 공개합니다.
                // bucket 전체를 static hosting으로 쓰지 않아도 asset URL contract를 안정적으로 제공합니다.
                origin: origins.S3BucketOrigin.withOriginAccessControl(this.dataStorageBucket, {
                    originPath: `/${GENAI_GENERATED_ASSETS_PREFIX.replace(/\/$/, '')}`,
                }),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
                cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress: true,
            },
        });
        new route53.ARecord(this, 'GenAiGeneratedAssetsDnsRecord', {
            zone: publicHostedZone,
            recordName: GENAI_PUBLIC_ASSETS_RECORD_NAME,
            target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(genAiGeneratedAssetsDistribution)),
        });

        // 비용을 낮게 유지하는 개발용 data storage입니다.
        // Aurora는 serverless v2 auto pause를 사용하고, 삭제 시에는 snapshot을 남겨 실수 복구 여지를 둡니다.
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

        // Redis 호환 cache는 ElastiCache Serverless for Valkey로 둡니다.
        // Valkey는 Redis OSS보다 최소 저장 과금이 낮아 dev의 idle 비용을 줄이기 좋습니다.
        // 앱 contract는 기존 Redis client 호환성을 위해 LOOPAD_REDIS_URL 이름을 유지합니다.
        const valkeyCache = new elasticache.CfnServerlessCache(this, 'ValkeyServerlessCache', {
            engine: 'valkey',
            majorEngineVersion: DEV_VALKEY_MAJOR_ENGINE_VERSION,
            serverlessCacheName: 'dev-loop-ad-valkey',
            description: 'Dev Redis-compatible Valkey serverless cache for loop-ad.',
            subnetIds: privateSubnets.subnetIds,
            securityGroupIds: [dataStorageSecurityGroup.securityGroupId],
            cacheUsageLimits: {
                dataStorage: {
                    maximum: DEV_VALKEY_MAX_DATA_STORAGE_GB,
                    unit: 'GB',
                },
                ecpuPerSecond: {
                    maximum: DEV_VALKEY_MAX_ECPU_PER_SECOND,
                },
            },
        });
        this.redisUrl = cdk.Fn.join('', ['rediss://', valkeyCache.attrEndpointAddress, ':', valkeyCache.attrEndpointPort]);

        // ClickHouse는 dev 분석/집계용 단일 EC2 인스턴스로 시작합니다.
        // 버전은 LTS patch tag로 고정해 재부팅/재배포 시에도 같은 바이너리를 사용하게 합니다.
        // managed cluster보다 단순하고 저렴하지만, prod 수준의 고가용성은 목표로 하지 않습니다.
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
            `docker run -d --restart unless-stopped --name clickhouse-server -p 8123:8123 -p 9000:9000 -v /var/lib/clickhouse:/var/lib/clickhouse ${DEV_CLICKHOUSE_IMAGE}`,
        );

        // MSK는 raw event stream의 중심입니다.
        // dev 비용을 낮게 유지하기 위해 Standard t3.small broker와 작은 EBS storage로 시작합니다.
        // Express broker는 운영 편의성은 좋지만 현재 dev 트래픽에서는 최소 구성이 훨씬 비쌉니다.
        const mskCluster = new msk.CfnCluster(this, 'MskCluster', {
            clusterName: 'dev-loop-ad-msk',
            kafkaVersion: DEV_MSK_KAFKA_VERSION,
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

        // CDK의 L1 MSK construct는 bootstrap broker string을 바로 속성으로 주지 않습니다.
        // Custom Resource로 배포 시점에 조회해 ECS task env와 SSM endpoint contract에 재사용합니다.
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
                retention: DEV_LOG_RETENTION,
                removalPolicy: RemovalPolicy.DESTROY,
            }),
            policy: cr.AwsCustomResourcePolicy.fromStatements([
                new iam.PolicyStatement({
                    actions: ['kafka:GetBootstrapBrokers'],
                    resources: [mskCluster.attrArn],
                }),
            ]),
        });

        this.auroraHost = auroraCluster.clusterEndpoint.hostname;
        this.auroraPort = '5432';
        this.clickHouseUrl = cdk.Fn.join('', ['http://', clickHouseInstance.instancePrivateDnsName, ':8123']);
        this.mskBootstrapBrokerString = mskBootstrapBrokers.getResponseField('BootstrapBrokerString');
        const auroraCredentialsSecret = auroraCluster.secret;
        if (!auroraCredentialsSecret) {
            throw new Error('Aurora generated credentials secret is required.');
        }
        this.auroraCredentialsSecret = auroraCredentialsSecret;

        // endpoint contract는 SSM에도 남깁니다. 앱 task에는 아래 값을 env로 직접 주입합니다.
        // 다른 repository의 CI/CD나 운영 스크립트가 같은 endpoint 이름을 보고 연결값을 찾을 수 있게 하기 위함입니다.
        for (const parameter of [
            {
                id: 'AuroraEndpointParameter',
                parameterName: '/loop-ad/dev/aurora/endpoint',
                stringValue: this.auroraHost,
                description: 'Dev Aurora PostgreSQL endpoint contract.',
            },
            {
                id: 'RedisEndpointParameter',
                parameterName: '/loop-ad/dev/redis/endpoint',
                stringValue: this.redisUrl,
                description: 'Dev Redis-compatible Valkey endpoint contract.',
            },
            {
                id: 'ClickHouseEndpointParameter',
                parameterName: '/loop-ad/dev/clickhouse/endpoint',
                stringValue: this.clickHouseUrl,
                description: 'Dev ClickHouse endpoint contract.',
            },
            {
                id: 'MskEndpointParameter',
                parameterName: '/loop-ad/dev/msk/bootstrap-brokers',
                stringValue: this.mskBootstrapBrokerString,
                description: 'Dev MSK bootstrap broker contract.',
            },
            {
                id: 'DataStorageBucketNameParameter',
                parameterName: '/loop-ad/dev/data-storage/bucket-name',
                stringValue: this.dataStorageBucket.bucketName,
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
    }
}

export interface LoopAdDevRuntimeStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
    readonly certificateArns: LoopAdDevCertificateArns;
    readonly network: LoopAdDevNetworkStack;
    readonly data: LoopAdDevDataStack;
}

// 상시 개발 런타임 스택입니다.
// FE static hosting, public ingress, ECS cluster/service를 소유하고 데이터 저장소는 DataStack에서 받습니다.
export class LoopAdDevRuntimeStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdDevRuntimeStackProps) {
        super(scope, id, props);

        // Network/Data stack에서 만든 기반 리소스를 재사용합니다.
        // 아직 배포 전이라 stack 분리로 인한 기존 리소스 이동/import 문제는 고려하지 않아도 됩니다.
        const {
            vpc,
            privateSubnets,
            albSecurityGroup,
            nlbSecurityGroup,
            serverSecurityGroup,
        } = props.network;
        const {
            dataStorageBucket,
            auroraHost,
            auroraPort,
            auroraCredentialsSecret,
            redisUrl,
            clickHouseUrl,
            mskBootstrapBrokerString,
        } = props.data;

        // Dev는 상시 운영 환경이므로 Fargate cluster를 사용합니다.
        // Cloud Map namespace를 같이 열어 private 서비스끼리는 public DNS 없이 이름으로 호출할 수 있게 합니다.
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'dev-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
            defaultCloudMapNamespace: {
                name: 'dev.loop-ad.local',
            },
        });

        // ECR repository는 repository stack에서 먼저 만듭니다.
        // runtime stack은 이름 contract만 import해서 ECS가 이미 push된 image를 참조하게 합니다.
        const repositories = DEV_APPLICATION_REPOSITORIES.map((repository) => (
            ecr.Repository.fromRepositoryName(this, `${repository.id}Import`, repository.repositoryName)
        )) as DevApplicationRepositories;
        const [
            eventCollectorRepository,
            projectorRepository,
            advertisementRepository,
            dashboardRepository,
            decisionApiRepository,
        ] = repositories;

        // .env에서 받은 public hosted zone을 import합니다.
        // fromHostedZoneAttributes는 synth 때 AWS lookup을 하지 않고 record template만 만듭니다.
        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        const dashboardWebDomainName = `${DASHBOARD_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const demoShoppingmallWebDomainName = `${DEMO_SHOPPINGMALL_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const publicApiDomainName = `${PUBLIC_API_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const publicIngestDomainName = `${PUBLIC_INGEST_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        // Certificate stack에서 만든 ACM ARN을 명시적으로 import합니다.
        // deprecated된 DnsValidatedCertificate를 쓰지 않고, CloudFront용 us-east-1 인증서 요구사항도 유지합니다.
        const frontendSitesCertificate = acm.Certificate.fromCertificateArn(
            this,
            'FrontendSitesCertificate',
            props.certificateArns.frontendSitesCertificateArn,
        );
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

        // 외부 SaaS credential은 infra code에 직접 넣지 않고 SSM SecureString 이름만 참조합니다.
        // 실제 secret 값은 배포 전 AWS 계정에 별도로 만들어 둡니다.
        const openAiApiKeyParameter = ssm.StringParameter.fromSecureStringParameterAttributes(this, 'OpenAiApiKeyParameter', {
            parameterName: OPENAI_API_KEY_PARAMETER_NAME,
        });

        // ALB는 API 경로를 열고, NLB는 raw event ingestion 경로를 엽니다.
        // HTTP API와 ingestion traffic의 성격이 달라 listener/target group을 분리해 장애 범위를 줄입니다.
        // ALB/NLB에 붙는 인증서는 같은 region(ap-northeast-2)에 있어야 하므로 runtime stack에서 별도로 만듭니다.
        const regionalIngressCertificate = new acm.Certificate(this, 'RegionalIngressCertificate', {
            domainName: publicApiDomainName,
            subjectAlternativeNames: [publicIngestDomainName],
            validation: acm.CertificateValidation.fromDns(publicHostedZone),
        });
        const regionalIngressListenerCertificate = elbv2.ListenerCertificate.fromCertificateManager(regionalIngressCertificate);
        const alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroup: albSecurityGroup,
            vpcSubnets: { subnetGroupName: 'public' },
        });
        const httpsAlbListener = alb.addListener('HttpsListener', {
            port: 443,
            protocol: elbv2.ApplicationProtocol.HTTPS,
            certificates: [regionalIngressListenerCertificate],
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

        // 외부에서 접근해야 하는 주소만 Route 53 record로 노출합니다.
        // 내부 서비스 간 호출은 Cloud Map 이름을 쓰므로 public DNS record를 추가하지 않습니다.
        for (const dnsRecord of [
            {
                id: 'DevApiDnsRecord',
                recordName: PUBLIC_API_RECORD_NAME,
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(alb)),
            },
            {
                id: 'DevIngestDnsRecord',
                recordName: PUBLIC_INGEST_RECORD_NAME,
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(nlb)),
            },
        ] as const) {
            new route53.ARecord(this, dnsRecord.id, {
                zone: publicHostedZone,
                recordName: dnsRecord.recordName,
                target: dnsRecord.target,
            });
        }

        // Event Collector는 NLB 트래픽을 받고 event를 MSK로 발행합니다.
        // public ingestion 진입점이지만 task 자체는 private subnet에서 실행되고 NLB만 앞에 둡니다.
        const eventCollectorTask = new ecs.FargateTaskDefinition(this, 'EventCollectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const eventCollectorLogGroup = createEcsServiceLogGroup(this, 'EventCollectorLogGroup', 'event-collector');
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

        // NLB는 443에서 TLS를 종료하고 collector service의 80 포트로 전달합니다.
        const tlsNlbListener = nlb.addListener('TlsEventCollectorListener', {
            port: 443,
            protocol: elbv2.Protocol.TLS,
            certificates: [regionalIngressListenerCertificate],
        });
        tlsNlbListener.addTargets('TlsEventCollectorTargets', {
            targets: [eventCollectorService],
            port: 80,
            protocol: elbv2.Protocol.TCP,
            healthCheck: {
                enabled: true,
                port: '80',
                protocol: elbv2.Protocol.HTTP,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });

        // Projector는 MSK를 consume하고 가공된 context를 Valkey/ClickHouse에 씁니다.
        // 앱에서는 Redis client를 그대로 쓸 수 있게 LOOPAD_REDIS_URL에 rediss:// Valkey endpoint를 주입합니다.
        const projectorTask = new ecs.FargateTaskDefinition(this, 'AdContextProjectorTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const projectorLogGroup = createEcsServiceLogGroup(this, 'AdContextProjectorLogGroup', 'ad-context-projector');
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
                PORT: '80',
                LOOPAD_MSK_BOOTSTRAP_BROKERS: mskBootstrapBrokerString,
                LOOPAD_EVENT_TOPIC: EVENT_TOPIC_NAME,
                LOOPAD_REDIS_URL: redisUrl,
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
        // Aurora credential은 Secrets Manager에서 주입하고, 조회 성능을 위한 cache 계층은 Valkey contract로 연결합니다.
        const advertisementTask = new ecs.FargateTaskDefinition(this, 'AdvertisementApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        const advertisementLogGroup = createEcsServiceLogGroup(this, 'AdvertisementApiLogGroup', 'advertisement-api');
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
                PORT: '80',
                LOOPAD_REDIS_URL: redisUrl,
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
        httpsAlbListener.addTargets('AdvertisementApiTargets', {
            targets: [advertisementService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 20,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/ads/*', '/advertisements/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });

        // Dashboard API는 dashboard 경로를 제공하고 Cloud Map으로 Decision API를 호출합니다.
        // 생성된 asset 목록/메타데이터를 조회해야 하므로 DataStorage bucket의 generated prefix 읽기 권한만 부여합니다.
        const dashboardTask = new ecs.FargateTaskDefinition(this, 'DashboardApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        dataStorageBucket.grantRead(dashboardTask.taskRole, `${GENAI_GENERATED_ASSETS_PREFIX}*`);
        const dashboardLogGroup = createEcsServiceLogGroup(this, 'DashboardApiLogGroup', 'dashboard-api');
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
        httpsAlbListener.addTargets('DashboardApiTargets', {
            targets: [dashboardService],
            port: 80,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 30,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/dashboard/*', '/dashboard/*'])],
            healthCheck: {
                enabled: true,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });

        // Decision API는 private 전용이며 public ALB에 연결하지 않습니다.
        // OpenAI 호출과 GenAI asset 생성을 담당하므로 SecureString과 DataStorage write 권한을 이 task에만 줍니다.
        const decisionApiTask = new ecs.FargateTaskDefinition(this, 'DecisionApiTaskDefinition', {
            cpu: 256,
            memoryLimitMiB: 512,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.ARM64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        dataStorageBucket.grantReadWrite(decisionApiTask.taskRole, `${GENAI_GENERATED_ASSETS_PREFIX}*`);
        const decisionApiLogGroup = createEcsServiceLogGroup(this, 'DecisionApiLogGroup', 'decision-api');
        const decisionApiContainer = decisionApiTask.addContainer('DecisionApiContainer', {
            containerName: 'decision-api',
            image: ecs.ContainerImage.fromEcrRepository(decisionApiRepository, 'latest'),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: 'decision-api',
                logGroup: decisionApiLogGroup,
            }),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'decision-api',
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
        decisionApiContainer.addPortMappings({ containerPort: 80, protocol: ecs.Protocol.TCP });
        const decisionApiService = new ecs.FargateService(this, 'DecisionApiService', {
            cluster,
            taskDefinition: decisionApiTask,
            serviceName: 'dev-decision-api',
            desiredCount: DEV_SERVICE_DESIRED_TASKS,
            assignPublicIp: false,
            securityGroups: [serverSecurityGroup],
            vpcSubnets: privateSubnets,
            circuitBreaker: { rollback: true },
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            cloudMapOptions: { name: 'decision-api' },
        });
        decisionApiService.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization('DecisionApiCpuScaling', {
            targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
        });

    }
}

function createEcsServiceLogGroup(scope: Construct, id: string, serviceId: string): logs.LogGroup {
    return new logs.LogGroup(scope, id, {
        logGroupName: `${DEV_ECS_LOG_GROUP_PREFIX}/${serviceId}`,
        retention: DEV_LOG_RETENTION,
    });
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
