import { Duration, Fn, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
    AURORA_DATABASE_NAME,
    CLICKHOUSE_DATABASE_NAME,
    DASHBOARD_API_RECORD_NAME,
    DASHBOARD_WEB_RECORD_NAME,
    DEMO_SHOPPINGMALL_WEB_RECORD_NAME,
    DECISION_API_RECORD_NAME,
    DEV_APPLICATION_REPOSITORIES,
    DEV_AURORA_AUTO_PAUSE_MINUTES,
    DEV_AURORA_MAX_ACU,
    DEV_AURORA_MIN_ACU,
    DEV_AURORA_PORT,
    DEV_AL2023_ARM64_AMI_SSM_PARAMETER,
    DEV_CLICKHOUSE_HTTP_PORT,
    DEV_CLICKHOUSE_IMAGE,
    DEV_CLICKHOUSE_INSTANCE_TYPE,
    DEV_CLICKHOUSE_VOLUME_GIB,
    DEV_DASHBOARD_API_FARGATE_CAPACITY,
    DEV_DECISION_API_FARGATE_CAPACITY,
    DEV_EVENT_COLLECTOR_FARGATE_CAPACITY,
    DEV_KAFKA_INSTANCE_TYPE,
    DEV_KAFKA_SCRAM_PORT,
    DEV_KAFKA_SCALA_VERSION,
    DEV_KAFKA_VERSION,
    DEV_KAFKA_VOLUME_GIB,
    DEV_VPC_AVAILABILITY_ZONES,
    EVENT_COLLECTOR_API_RECORD_NAME,
    EVENT_TOPIC_NAME,
    GENAI_ASSETS_BASE_PREFIX,
    GENAI_PUBLIC_ASSETS_RECORD_NAME,
    LOOP_AD_REGION,
    type DeveloperAllowlistConfig,
    type LoopAdDevCertificateArns,
    type LoopAdDevDataSecretNames,
    type LoopAdDevRuntimeSecretNames,
    type PublicHostedZoneConfig,
} from './dev-config';
import {
    APP_CONTAINER_PORT,
    createFargateHttpService,
    createStaticFrontendSite,
} from './runtime-helpers';

export {
    LOOP_AD_REGION,
    buildDevSecretNames,
    type DeveloperAllowlistConfig,
    type LoopAdDevCertificateArns,
    type LoopAdDevDataSecretNames,
    type LoopAdDevRuntimeSecretNames,
    type LoopAdDevSecretNames,
    type PublicHostedZoneConfig,
} from './dev-config';
export {
    LoopAdDevCertificateStack,
    LoopAdDevRepositoryStack,
    LoopAdDevSecretsStack,
} from './lifecycle-stacks';

const APP_CONTAINER_PORT_TEXT = String(APP_CONTAINER_PORT);
const AURORA_PORT_TEXT = String(DEV_AURORA_PORT);
const CLICKHOUSE_HTTP_PORT_TEXT = String(DEV_CLICKHOUSE_HTTP_PORT);
const KAFKA_SCRAM_PORT_TEXT = String(DEV_KAFKA_SCRAM_PORT);
const KAFKA_SECURITY_PROTOCOL = 'SASL_PLAINTEXT';
const KAFKA_SASL_MECHANISM = 'SCRAM-SHA-512';
const KAFKA_HEAP_OPTS = '-Xms256m -Xmx1024m';
const USER_DATA_SCRIPT_DIR = join(__dirname, '..', 'assets', 'user-data');

type DevApplicationRepositories = [ecr.IRepository, ecr.IRepository, ecr.IRepository];

export interface LoopAdDevNetworkStackProps extends StackProps {
    readonly developerAllowlist: DeveloperAllowlistConfig;
}

export class LoopAdDevNetworkStack extends Stack {
    public readonly vpc: ec2.Vpc;
    public readonly publicSubnetSelection: ec2.SubnetSelection;
    public readonly publicSubnets: ec2.SelectedSubnets;
    public readonly albSecurityGroup: ec2.SecurityGroup;
    public readonly serverSecurityGroup: ec2.SecurityGroup;
    public readonly dataSourceSecurityGroup: ec2.SecurityGroup;

    public constructor(scope: Construct, id: string, props: LoopAdDevNetworkStackProps) {
        super(scope, id, props);

        // 새 dev 구조는 NAT Gateway 없이 public subnet만 사용합니다.
        // Fargate task와 data node가 public IP를 받아 dev 비용을 낮추는 대신, Security Group이 접근 경계가 됩니다.
        this.vpc = new ec2.Vpc(this, 'Vpc', {
            vpcName: 'dev-loop-ad-vpc',
            availabilityZones: DEV_VPC_AVAILABILITY_ZONES,
            natGateways: 0,
            restrictDefaultSecurityGroup: false,
            subnetConfiguration: [
                {
                    name: 'public',
                    subnetType: ec2.SubnetType.PUBLIC,
                    cidrMask: 24,
                },
            ],
        });

        this.publicSubnetSelection = { subnetGroupName: 'public' };
        this.publicSubnets = this.vpc.selectSubnets(this.publicSubnetSelection);

        // Security Group은 ALB, 서버, 데이터소스 세 종류로만 둡니다.
        // 역할별로 ingress/egress를 분리해야 public subnet 구조에서도 허용 경로를 테스트로 검증하기 쉽습니다.
        this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev public ALB HTTPS ingress.',
        });
        this.serverSecurityGroup = new ec2.SecurityGroup(this, 'ServerSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev public Fargate services.',
        });
        this.dataSourceSecurityGroup = new ec2.SecurityGroup(this, 'DataSourceSecurityGroup', {
            vpc: this.vpc,
            allowAllOutbound: false,
            description: 'Dev Aurora, ClickHouse, and Kafka data sources.',
        });

        this.albSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Public HTTPS to dev ALB.');
        this.albSecurityGroup.addEgressRule(this.serverSecurityGroup, ec2.Port.tcp(APP_CONTAINER_PORT), 'ALB may reach app containers.');
        this.serverSecurityGroup.addIngressRule(this.albSecurityGroup, ec2.Port.tcp(APP_CONTAINER_PORT), 'ALB may reach app containers.');
        this.serverSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Servers may reach AWS APIs, ECR, CloudWatch Logs, and public HTTPS endpoints.');

        // 서버에서 데이터소스로 나가는 경로는 앱이 실제로 쓰는 포트만 엽니다.
        // allowAllOutbound를 끈 상태라 빠진 포트는 곧 연결 실패로 드러나며, 의도치 않은 외부 egress를 줄입니다.
        for (const dataSource of [
            { name: 'Aurora PostgreSQL', port: DEV_AURORA_PORT },
            { name: 'ClickHouse HTTP', port: DEV_CLICKHOUSE_HTTP_PORT },
            { name: 'Kafka SCRAM', port: DEV_KAFKA_SCRAM_PORT },
        ] as const) {
            this.serverSecurityGroup.addEgressRule(this.dataSourceSecurityGroup, ec2.Port.tcp(dataSource.port), `Servers may reach ${dataSource.name}.`);
            this.dataSourceSecurityGroup.addIngressRule(this.serverSecurityGroup, ec2.Port.tcp(dataSource.port), `Servers may enter ${dataSource.name}.`);
        }

        this.dataSourceSecurityGroup.addEgressRule(this.dataSourceSecurityGroup, ec2.Port.allTcp(), 'Data source nodes may reach each other.');
        this.dataSourceSecurityGroup.addIngressRule(this.dataSourceSecurityGroup, ec2.Port.allTcp(), 'Data source nodes may enter each other.');

        for (const cidr of props.developerAllowlist.ipv4Cidrs) {
            this.addDeveloperDataSourceIngress(ec2.Peer.ipv4(cidr), cidr);
        }
        for (const cidr of props.developerAllowlist.ipv6Cidrs) {
            this.addDeveloperDataSourceIngress(ec2.Peer.ipv6(cidr), cidr);
        }

        this.dataSourceSecurityGroup.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Data nodes may bootstrap from AWS and public package registries.');
    }

    private addDeveloperDataSourceIngress(peer: ec2.IPeer, cidr: string): void {
        // allowlist가 비어 있으면 이 함수는 호출되지 않아 개발자 직접 접근 rule도 생기지 않습니다.
        // CIDR 검증은 config 단계에서 끝났으므로 여기서는 동일 CIDR에 필요한 데이터 포트만 반복해 추가합니다.
        for (const dataSource of [
            { name: 'Aurora PostgreSQL', port: DEV_AURORA_PORT },
            { name: 'ClickHouse HTTP', port: DEV_CLICKHOUSE_HTTP_PORT },
            { name: 'Kafka SCRAM', port: DEV_KAFKA_SCRAM_PORT },
        ] as const) {
            this.dataSourceSecurityGroup.addIngressRule(peer, ec2.Port.tcp(dataSource.port), `Developer ${cidr} may reach ${dataSource.name}.`);
        }
    }
}

export interface LoopAdDevDataStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
    readonly genAiGeneratedAssetsCertificateArn: string;
    readonly network: LoopAdDevNetworkStack;
    readonly secretNames: LoopAdDevDataSecretNames;
}

export class LoopAdDevDataStack extends Stack {
    public readonly dataStorageBucket: s3.Bucket;
    public readonly auroraHost: string;
    public readonly auroraPort: string;
    public readonly auroraCredentialsSecret: secretsmanager.ISecret;
    public readonly clickHouseUrl: string;
    public readonly clickHouseCredentialsSecret: secretsmanager.ISecret;
    public readonly kafkaScramBootstrapBrokerString: string;
    public readonly kafkaAppUserSecret: secretsmanager.ISecret;

    public constructor(scope: Construct, id: string, props: LoopAdDevDataStackProps) {
        super(scope, id, props);

        const {
            vpc,
            publicSubnets,
            dataSourceSecurityGroup,
        } = props.network;

        // CDK가 관리하는 시크릿 이름에만 연결하고 시크릿 값은 읽거나 생성하지 않습니다.
        // fromSecretNameV2는 CloudFormation에 secret 값이 들어가지 않게 하고, ECS/RDS가 런타임에 참조하게 합니다.
        const auroraCredentialsSecret = secretsmanager.Secret.fromSecretNameV2(this, 'AuroraCredentialsSecret', props.secretNames.auroraCredentialsSecretName);
        const clickHouseCredentialsSecret = secretsmanager.Secret.fromSecretNameV2(this, 'ClickHouseCredentialsSecret', props.secretNames.clickHouseCredentialsSecretName);
        const kafkaAppUserSecret = secretsmanager.Secret.fromSecretNameV2(this, 'KafkaAppUserSecret', props.secretNames.kafkaAppUserSecretName);
        const kafkaBrokerUserSecret = secretsmanager.Secret.fromSecretNameV2(this, 'KafkaBrokerUserSecret', props.secretNames.kafkaBrokerUserSecretName);

        // DataStorage는 앱이 만든 원천/GenAI 산출물을 담는 장기 저장소입니다.
        // dev stack을 재생성해도 데이터 손실을 피하려고 RETAIN을 쓰며, multipart 미완료 업로드만 정리합니다.
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
                    prefix: GENAI_ASSETS_BASE_PREFIX,
                    abortIncompleteMultipartUploadAfter: Duration.days(7),
                },
            ],
        });

        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        const genAiPublicAssetsDomainName = `${GENAI_PUBLIC_ASSETS_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const genAiGeneratedAssetsCertificate = acm.Certificate.fromCertificateArn(
            this,
            'GenAiGeneratedAssetsCertificate',
            props.genAiGeneratedAssetsCertificateArn,
        );
        // GenAI asset은 DataStorage 안의 base prefix만 CloudFront로 공개합니다.
        // 버킷 전체를 origin으로 열지 않고 originPath를 고정해 앱 산출물 공개 범위를 좁힙니다.
        const genAiGeneratedAssetsDistribution = new cloudfront.Distribution(this, 'GenAiGeneratedAssetsDistribution', {
            domainNames: [genAiPublicAssetsDomainName],
            certificate: genAiGeneratedAssetsCertificate,
            comment: `Dev GenAI assets for ${genAiPublicAssetsDomainName}`,
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(this.dataStorageBucket, {
                    originPath: `/${GENAI_ASSETS_BASE_PREFIX.replace(/\/$/, '')}`,
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

        // Aurora Serverless v2는 dev 유휴 시간에 0 ACU까지 내려가도록 설정합니다.
        // public accessible은 public subnet only 구조의 trade-off이며, 접근은 DataSourceSecurityGroup으로 제한합니다.
        const auroraCluster = new rds.DatabaseCluster(this, 'AuroraPostgresCluster', {
            clusterIdentifier: 'dev-loop-ad-aurora-postgres',
            engine: rds.DatabaseClusterEngine.auroraPostgres({
                version: rds.AuroraPostgresEngineVersion.VER_16_13,
            }),
            writer: rds.ClusterInstance.serverlessV2('writer', {
                publiclyAccessible: true,
            }),
            credentials: rds.Credentials.fromSecret(auroraCredentialsSecret),
            serverlessV2MinCapacity: DEV_AURORA_MIN_ACU,
            serverlessV2MaxCapacity: DEV_AURORA_MAX_ACU,
            serverlessV2AutoPauseDuration: Duration.minutes(DEV_AURORA_AUTO_PAUSE_MINUTES),
            defaultDatabaseName: AURORA_DATABASE_NAME,
            vpc,
            vpcSubnets: publicSubnets,
            securityGroups: [dataSourceSecurityGroup],
            backup: {
                retention: Duration.days(1),
            },
            deletionProtection: false,
            removalPolicy: RemovalPolicy.SNAPSHOT,
        });

        // ClickHouse는 관리형 클러스터 대신 단일 ARM EC2와 gp3 볼륨으로 둡니다.
        // dev 분석 저장소 비용을 예측 가능하게 낮추고, user-data 스크립트로 재현 가능한 초기화를 수행합니다.
        const clickHouseInstance = new ec2.Instance(this, 'ClickHouseInstance', {
            vpc,
            vpcSubnets: publicSubnets,
            securityGroup: dataSourceSecurityGroup,
            instanceName: 'dev-loop-ad-clickhouse',
            instanceType: new ec2.InstanceType(DEV_CLICKHOUSE_INSTANCE_TYPE),
            machineImage: ec2.MachineImage.fromSsmParameter(DEV_AL2023_ARM64_AMI_SSM_PARAMETER),
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: ec2.BlockDeviceVolume.ebs(DEV_CLICKHOUSE_VOLUME_GIB, {
                        encrypted: true,
                        volumeType: ec2.EbsDeviceVolumeType.GP3,
                        deleteOnTermination: true,
                    }),
                },
            ],
            requireImdsv2: true,
            userDataCausesReplacement: true,
            associatePublicIpAddress: true,
        });
        clickHouseInstance.role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'));
        clickHouseCredentialsSecret.grantRead(clickHouseInstance.role);
        // EC2 UserData는 Secrets Manager dynamic reference를 해석하지 않으므로 secret name만 전달합니다.
        // 인스턴스 role이 런타임에 값을 읽고, CDK synth/deploy 결과에는 평문 secret을 남기지 않습니다.
        addUserDataScript(clickHouseInstance, 'clickhouse.sh', {
            CLICKHOUSE_DATABASE: CLICKHOUSE_DATABASE_NAME,
            CLICKHOUSE_CREDENTIALS_SECRET_NAME: props.secretNames.clickHouseCredentialsSecretName,
            CLICKHOUSE_HTTP_PORT: CLICKHOUSE_HTTP_PORT_TEXT,
            CLICKHOUSE_IMAGE: DEV_CLICKHOUSE_IMAGE,
            AWS_REGION: LOOP_AD_REGION,
        });

        // Kafka도 dev용 단일 broker로 고정합니다.
        // app user와 broker user를 분리해 앱 접속 정보와 broker 내부 인증 정보를 같은 방식으로 회전할 수 있게 합니다.
        const kafkaInstance = new ec2.Instance(this, 'KafkaInstance', {
            vpc,
            vpcSubnets: publicSubnets,
            securityGroup: dataSourceSecurityGroup,
            instanceName: 'dev-loop-ad-kafka',
            instanceType: new ec2.InstanceType(DEV_KAFKA_INSTANCE_TYPE),
            machineImage: ec2.MachineImage.fromSsmParameter(DEV_AL2023_ARM64_AMI_SSM_PARAMETER),
            blockDevices: [
                {
                    deviceName: '/dev/xvda',
                    volume: ec2.BlockDeviceVolume.ebs(DEV_KAFKA_VOLUME_GIB, {
                        encrypted: true,
                        volumeType: ec2.EbsDeviceVolumeType.GP3,
                        deleteOnTermination: true,
                    }),
                },
            ],
            requireImdsv2: true,
            userDataCausesReplacement: true,
            associatePublicIpAddress: true,
        });
        kafkaInstance.role.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'));
        kafkaAppUserSecret.grantRead(kafkaInstance.role);
        kafkaBrokerUserSecret.grantRead(kafkaInstance.role);
        addUserDataScript(kafkaInstance, 'kafka.sh', {
            APP_USER_SECRET_NAME: props.secretNames.kafkaAppUserSecretName,
            AWS_REGION: LOOP_AD_REGION,
            BROKER_USER_SECRET_NAME: props.secretNames.kafkaBrokerUserSecretName,
            EVENT_TOPIC_NAME,
            KAFKA_HEAP_OPTS,
            KAFKA_SASL_MECHANISM,
            KAFKA_SCALA_VERSION: DEV_KAFKA_SCALA_VERSION,
            KAFKA_SCRAM_PORT: KAFKA_SCRAM_PORT_TEXT,
            KAFKA_SECURITY_PROTOCOL,
            KAFKA_VERSION: DEV_KAFKA_VERSION,
        });

        // Runtime stack은 Data stack의 endpoint와 secret import만 알면 됩니다.
        // 이 public DNS 기반 값들은 앱 env 계약으로 전달되고, 민감값은 ECS secret injection으로 분리합니다.
        this.auroraHost = auroraCluster.clusterEndpoint.hostname;
        this.auroraPort = AURORA_PORT_TEXT;
        this.auroraCredentialsSecret = auroraCredentialsSecret;
        this.clickHouseUrl = Fn.join('', ['http://', clickHouseInstance.instancePublicDnsName, ':', CLICKHOUSE_HTTP_PORT_TEXT]);
        this.clickHouseCredentialsSecret = clickHouseCredentialsSecret;
        this.kafkaScramBootstrapBrokerString = Fn.join('', [kafkaInstance.instancePublicDnsName, ':', KAFKA_SCRAM_PORT_TEXT]);
        this.kafkaAppUserSecret = kafkaAppUserSecret;
    }
}

export interface LoopAdDevRuntimeStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
    readonly certificateArns: LoopAdDevCertificateArns;
    readonly network: LoopAdDevNetworkStack;
    readonly data: LoopAdDevDataStack;
    readonly runtimeSecretNames: LoopAdDevRuntimeSecretNames;
}

export class LoopAdDevRuntimeStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdDevRuntimeStackProps) {
        super(scope, id, props);

        const {
            vpc,
            publicSubnets,
            albSecurityGroup,
            serverSecurityGroup,
        } = props.network;
        const {
            dataStorageBucket,
            auroraHost,
            auroraPort,
            auroraCredentialsSecret,
            clickHouseUrl,
            clickHouseCredentialsSecret,
            kafkaScramBootstrapBrokerString,
            kafkaAppUserSecret,
        } = props.data;
        // 런타임 task는 평문 환경 변수가 아니라 ECS 시크릿 주입으로 시크릿 필드를 받습니다.
        // internal key는 /api/*/internal/* 같은 내부성 API를 앱 레벨에서 검증하기 위한 공유 키입니다.
        const openAiApiKeySecret = secretsmanager.Secret.fromSecretNameV2(this, 'OpenAiApiKeySecret', props.runtimeSecretNames.openAiApiKeySecretName);
        const internalApiKeySecret = secretsmanager.Secret.fromSecretNameV2(this, 'InternalApiKeySecret', props.runtimeSecretNames.internalApiKeySecretName);

        // 하나의 ECS cluster에 세 API 서비스를 모읍니다.
        // Container Insights는 dev 장애 분석에 필요한 최소 관측성을 제공하므로 켜 둡니다.
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc,
            clusterName: 'dev-loop-ad-cluster',
            containerInsightsV2: ecs.ContainerInsights.ENABLED,
        });

        // Repository stack이 만든 ECR 이름을 import합니다.
        // Runtime stack은 이미지를 빌드하지 않고, 각 앱 repository가 latest 태그를 배포 전에 push했다고 가정합니다.
        const repositories = DEV_APPLICATION_REPOSITORIES.map((repository) => (
            ecr.Repository.fromRepositoryName(this, `${repository.id}Import`, repository.repositoryName)
        )) as DevApplicationRepositories;
        const [
            eventCollectorRepository,
            dashboardRepository,
            decisionApiRepository,
        ] = repositories;

        const publicHostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'PublicHostedZone', {
            hostedZoneId: props.publicHostedZone.hostedZoneId,
            zoneName: props.publicHostedZone.domainName,
        });
        const dashboardWebDomainName = `${DASHBOARD_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const demoShoppingmallWebDomainName = `${DEMO_SHOPPINGMALL_WEB_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const eventCollectorApiDomainName = `${EVENT_COLLECTOR_API_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const dashboardApiDomainName = `${DASHBOARD_API_RECORD_NAME}.${props.publicHostedZone.domainName}`;
        const decisionApiDomainName = `${DECISION_API_RECORD_NAME}.${props.publicHostedZone.domainName}`;

        // 정적 프론트는 같은 ACM 인증서와 공통 헬퍼를 사용합니다.
        // dashboard와 demo-shoppingmall은 앱 배포 산출물만 다르고 CDN/S3 보안 기본값은 같아야 합니다.
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

        // API 진입점은 public ALB 하나로 통일하고, 서비스 경계는 host-header routing으로 나눕니다.
        // event.api.dev, dashboard.api.dev, decision.api.dev가 각각 같은 ALB의 대상 그룹으로 연결됩니다.
        const regionalIngressCertificate = new acm.Certificate(this, 'RegionalIngressCertificate', {
            domainName: eventCollectorApiDomainName,
            subjectAlternativeNames: [
                dashboardApiDomainName,
                decisionApiDomainName,
            ],
            validation: acm.CertificateValidation.fromDns(publicHostedZone),
        });
        const alb = new elbv2.ApplicationLoadBalancer(this, 'ApplicationLoadBalancer', {
            vpc,
            internetFacing: true,
            securityGroup: albSecurityGroup,
            vpcSubnets: publicSubnets,
        });
        const httpsAlbListener = alb.addListener('HttpsListener', {
            port: 443,
            protocol: elbv2.ApplicationProtocol.HTTPS,
            certificates: [elbv2.ListenerCertificate.fromCertificateManager(regionalIngressCertificate)],
            open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'No loop-ad API host is registered.',
            }),
        });
        for (const apiRecord of [
            { id: 'EventCollectorApiDnsRecord', recordName: EVENT_COLLECTOR_API_RECORD_NAME },
            { id: 'DashboardApiDnsRecord', recordName: DASHBOARD_API_RECORD_NAME },
            { id: 'DecisionApiDnsRecord', recordName: DECISION_API_RECORD_NAME },
        ] as const) {
            new route53.ARecord(this, apiRecord.id, {
                zone: publicHostedZone,
                recordName: apiRecord.recordName,
                target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(alb)),
            });
        }

        // event-collector는 이벤트 수집과 Kafka publish만 담당합니다.
        // 내부 API key도 함께 주입해 앱이 자체 /internal 경로를 검증할 수 있게 합니다.
        const eventCollector = createFargateHttpService(this, {
            taskDefinitionId: 'EventCollectorTaskDefinition',
            logGroupId: 'EventCollectorLogGroup',
            containerId: 'EventCollectorContainer',
            serviceConstructId: 'EventCollectorService',
            cpuScalingId: 'EventCollectorCpuScaling',
            serviceId: 'event-collector',
            image: ecs.ContainerImage.fromEcrRepository(eventCollectorRepository, 'latest'),
            cluster,
            securityGroup: serverSecurityGroup,
            vpcSubnets: publicSubnets,
            capacity: DEV_EVENT_COLLECTOR_FARGATE_CAPACITY,
            healthCheckGracePeriod: Duration.seconds(60),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'event-collector',
                PORT: APP_CONTAINER_PORT_TEXT,
                LOOPAD_KAFKA_BOOTSTRAP_BROKERS: kafkaScramBootstrapBrokerString,
                LOOPAD_KAFKA_SECURITY_PROTOCOL: KAFKA_SECURITY_PROTOCOL,
                LOOPAD_KAFKA_SASL_MECHANISM: KAFKA_SASL_MECHANISM,
                LOOPAD_EVENT_TOPIC: EVENT_TOPIC_NAME,
            },
            secrets: {
                LOOPAD_KAFKA_USERNAME: ecs.Secret.fromSecretsManager(kafkaAppUserSecret, 'username'),
                LOOPAD_KAFKA_PASSWORD: ecs.Secret.fromSecretsManager(kafkaAppUserSecret, 'password'),
                LOOPAD_INTERNAL_API_KEY: ecs.Secret.fromSecretsManager(internalApiKeySecret, 'api_key'),
            },
        });
        httpsAlbListener.addTargets('EventCollectorTargets', {
            targets: [eventCollector.service],
            port: APP_CONTAINER_PORT,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 10,
            conditions: [elbv2.ListenerCondition.hostHeaders([eventCollectorApiDomainName])],
            healthCheck: {
                enabled: true,
                port: APP_CONTAINER_PORT_TEXT,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });

        // dashboard-api는 Aurora/ClickHouse 조회와 GenAI asset 읽기만 필요합니다.
        // S3 권한도 GenAI asset base prefix read로 제한해 dashboard가 임의 객체를 쓰지 못하게 합니다.
        const dashboard = createFargateHttpService(this, {
            taskDefinitionId: 'DashboardApiTaskDefinition',
            logGroupId: 'DashboardApiLogGroup',
            containerId: 'DashboardApiContainer',
            serviceConstructId: 'DashboardApiService',
            cpuScalingId: 'DashboardApiCpuScaling',
            serviceId: 'dashboard-api',
            image: ecs.ContainerImage.fromEcrRepository(dashboardRepository, 'latest'),
            cluster,
            securityGroup: serverSecurityGroup,
            vpcSubnets: publicSubnets,
            capacity: DEV_DASHBOARD_API_FARGATE_CAPACITY,
            healthCheckGracePeriod: Duration.seconds(60),
            grantTaskRole: (taskDefinition) => dataStorageBucket.grantRead(taskDefinition.taskRole, `${GENAI_ASSETS_BASE_PREFIX}*`),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'dashboard-api',
                PORT: APP_CONTAINER_PORT_TEXT,
                LOOPAD_AURORA_HOST: auroraHost,
                LOOPAD_AURORA_PORT: auroraPort,
                LOOPAD_AURORA_DATABASE: AURORA_DATABASE_NAME,
                LOOPAD_CLICKHOUSE_URL: clickHouseUrl,
                LOOPAD_CLICKHOUSE_DATABASE: CLICKHOUSE_DATABASE_NAME,
                LOOPAD_DATA_STORAGE_BUCKET: dataStorageBucket.bucketName,
                LOOPAD_GENAI_ASSETS_BASE_PREFIX: GENAI_ASSETS_BASE_PREFIX,
                LOOPAD_DECISION_API_BASE_URL: `https://${decisionApiDomainName}`,
            },
            secrets: {
                LOOPAD_AURORA_USERNAME: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'username'),
                LOOPAD_AURORA_PASSWORD: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'password'),
                LOOPAD_CLICKHOUSE_USERNAME: ecs.Secret.fromSecretsManager(clickHouseCredentialsSecret, 'username'),
                LOOPAD_CLICKHOUSE_PASSWORD: ecs.Secret.fromSecretsManager(clickHouseCredentialsSecret, 'password'),
                LOOPAD_INTERNAL_API_KEY: ecs.Secret.fromSecretsManager(internalApiKeySecret, 'api_key'),
            },
        });
        httpsAlbListener.addTargets('DashboardApiTargets', {
            targets: [dashboard.service],
            port: APP_CONTAINER_PORT,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 20,
            conditions: [elbv2.ListenerCondition.hostHeaders([dashboardApiDomainName])],
            healthCheck: {
                enabled: true,
                port: APP_CONTAINER_PORT_TEXT,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });

        // decision-api는 판단 요청 처리와 GenAI asset 생성을 담당합니다.
        // OpenAI API key와 S3 read/write 권한은 이 서비스에만 주입해 blast radius를 줄입니다.
        const decision = createFargateHttpService(this, {
            taskDefinitionId: 'DecisionApiTaskDefinition',
            logGroupId: 'DecisionApiLogGroup',
            containerId: 'DecisionApiContainer',
            serviceConstructId: 'DecisionApiService',
            cpuScalingId: 'DecisionApiCpuScaling',
            serviceId: 'decision-api',
            image: ecs.ContainerImage.fromEcrRepository(decisionApiRepository, 'latest'),
            cluster,
            securityGroup: serverSecurityGroup,
            vpcSubnets: publicSubnets,
            capacity: DEV_DECISION_API_FARGATE_CAPACITY,
            healthCheckGracePeriod: Duration.seconds(60),
            grantTaskRole: (taskDefinition) => dataStorageBucket.grantReadWrite(taskDefinition.taskRole, `${GENAI_ASSETS_BASE_PREFIX}*`),
            environment: {
                LOOPAD_ENV: 'dev',
                LOOPAD_SERVICE_ID: 'decision-api',
                PORT: APP_CONTAINER_PORT_TEXT,
                LOOPAD_AURORA_HOST: auroraHost,
                LOOPAD_AURORA_PORT: auroraPort,
                LOOPAD_AURORA_DATABASE: AURORA_DATABASE_NAME,
                LOOPAD_CLICKHOUSE_URL: clickHouseUrl,
                LOOPAD_CLICKHOUSE_DATABASE: CLICKHOUSE_DATABASE_NAME,
                LOOPAD_DATA_STORAGE_BUCKET: dataStorageBucket.bucketName,
                LOOPAD_GENAI_ASSETS_BASE_PREFIX: GENAI_ASSETS_BASE_PREFIX,
            },
            secrets: {
                LOOPAD_AURORA_USERNAME: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'username'),
                LOOPAD_AURORA_PASSWORD: ecs.Secret.fromSecretsManager(auroraCredentialsSecret, 'password'),
                LOOPAD_CLICKHOUSE_USERNAME: ecs.Secret.fromSecretsManager(clickHouseCredentialsSecret, 'username'),
                LOOPAD_CLICKHOUSE_PASSWORD: ecs.Secret.fromSecretsManager(clickHouseCredentialsSecret, 'password'),
                LOOPAD_OPENAI_API_KEY: ecs.Secret.fromSecretsManager(openAiApiKeySecret, 'api_key'),
                LOOPAD_INTERNAL_API_KEY: ecs.Secret.fromSecretsManager(internalApiKeySecret, 'api_key'),
            },
        });
        httpsAlbListener.addTargets('DecisionApiTargets', {
            targets: [decision.service],
            port: APP_CONTAINER_PORT,
            protocol: elbv2.ApplicationProtocol.HTTP,
            priority: 30,
            conditions: [elbv2.ListenerCondition.hostHeaders([decisionApiDomainName])],
            healthCheck: {
                enabled: true,
                port: APP_CONTAINER_PORT_TEXT,
                path: '/health',
                healthyHttpCodes: '200',
            },
        });
    }
}

function addUserDataScript(instance: ec2.Instance, scriptName: string, environment: Record<string, string>): void {
    // 긴 shell 명령을 TypeScript 문자열로 관리하지 않고 assets/user-data/*.sh 파일로 둡니다.
    // CDK는 파일을 인스턴스에 올리고, 인스턴스별 설정만 env로 주입해 스크립트를 재사용합니다.
    const remotePath = `/opt/loop-ad/${scriptName}`;
    const heredocMarker = `LOOP_AD_${scriptName.replace(/[^A-Za-z0-9]/g, '_').toUpperCase()}_EOF`;
    const scriptBody = readFileSync(join(USER_DATA_SCRIPT_DIR, scriptName), 'utf8').trimEnd();

    if (scriptBody.split('\n').includes(heredocMarker)) {
        throw new Error(`${scriptName} must not contain the generated heredoc marker ${heredocMarker}.`);
    }

    // 커밋된 스크립트 본문을 올리고 인스턴스별 설정은 실행 시점 환경 변수로 전달합니다.
    instance.userData.addCommands(
        'set -euo pipefail',
        'mkdir -p /opt/loop-ad',
        `cat > ${shellQuote(remotePath)} <<'${heredocMarker}'\n${scriptBody}\n${heredocMarker}`,
        `chmod 700 ${shellQuote(remotePath)}`,
        renderScriptExecutionCommand(remotePath, environment),
    );
}

function renderScriptExecutionCommand(remotePath: string, environment: Record<string, string>): string {
    // 각 env 값은 shellQuote를 거쳐 전달해 공백, 콜론 같은 문자가 shell에서 깨지지 않게 합니다.
    return [
        'env \\',
        ...Object.entries(environment).map(([key, value]) => `  ${key}=${shellQuote(value)} \\`),
        `  ${shellQuote(remotePath)}`,
    ].join('\n');
}

function shellQuote(value: string): string {
    // POSIX single quote escaping을 한곳에 모아 user-data 명령 생성 시 인자 주입 위험을 줄입니다.
    return `'${value.replace(/'/g, String.raw`'\''`)}'`;
}
