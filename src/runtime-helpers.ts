import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import {
    DEV_ECS_LOG_GROUP_PREFIX,
    DEV_LOG_RETENTION,
    DEV_SERVICE_DESIRED_TASKS,
    DEV_SERVICE_MAX_TASKS,
    DEV_SERVICE_MIN_TASKS,
    SERVICE_CPU_SCALE_TARGET_PERCENT,
} from './dev-config';

export const APP_CONTAINER_PORT = 8080;

// ECS 서비스 로그 그룹은 서비스별 로그 이름과 보관 기간을 한곳에서 맞춥니다.
// 로그 보관 기간은 CloudWatch Logs 비용에 직접 영향을 주므로 서비스마다 흩어 두지 않습니다.
export function createEcsServiceLogGroup(scope: Construct, id: string, serviceId: string): logs.LogGroup {
    return new logs.LogGroup(scope, id, {
        logGroupName: `${DEV_ECS_LOG_GROUP_PREFIX}/${serviceId}`,
        retention: DEV_LOG_RETENTION,
    });
}

// 이 설정은 하나의 HTTP 기반 Fargate 서비스를 만들기 위한 입력입니다.
// 작업 정의, 컨테이너, 서비스, 스케일링 construct ID는 호출부에서 직접 넘겨 CloudFormation logical ID를 고정합니다.
// 헬퍼 내부 이름을 바꾸거나 책임을 조금 나누더라도 이미 배포된 ECS 리소스가 교체되지 않게 하기 위함입니다.
export interface FargateHttpServiceConfig {
    readonly taskDefinitionId: string;
    readonly logGroupId: string;
    readonly containerId: string;
    readonly serviceConstructId: string;
    readonly cpuScalingId: string;
    readonly serviceId: string;
    readonly image: ecs.ContainerImage;
    readonly cluster: ecs.ICluster;
    readonly securityGroup: ec2.ISecurityGroup;
    readonly vpcSubnets: ec2.SubnetSelection;
    readonly environment: Record<string, string>;
    readonly secrets?: Record<string, ecs.Secret>;
    readonly healthCheckGracePeriod?: Duration;
    readonly grantTaskRole?: (taskDefinition: ecs.FargateTaskDefinition) => void;
}

export interface FargateHttpService {
    readonly taskDefinition: ecs.FargateTaskDefinition;
    readonly container: ecs.ContainerDefinition;
    readonly service: ecs.FargateService;
}

export function createFargateHttpService(scope: Construct, config: FargateHttpServiceConfig): FargateHttpService {
    // 작업 정의는 컨테이너가 사용할 CPU, 메모리, 런타임 아키텍처의 비용 단위입니다.
    // 개발 환경의 모든 서비스는 작은 ARM64 작업 크기를 공유해 월 비용 모델의 Fargate 상한을 예측 가능하게 둡니다.
    const taskDefinition = new ecs.FargateTaskDefinition(scope, config.taskDefinitionId, {
        cpu: 256,
        memoryLimitMiB: 512,
        runtimePlatform: {
            cpuArchitecture: ecs.CpuArchitecture.ARM64,
            operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
    });

    // 서비스별 IAM 권한은 작업 정의를 만든 뒤 호출부가 grant 메서드로 부여합니다.
    // 어떤 서비스가 어떤 S3 prefix나 시크릿을 쓰는지는 공통 헬퍼보다 스택의 서비스 연결부에서 보는 편이 안전합니다.
    config.grantTaskRole?.(taskDefinition);

    // 컨테이너는 ECR 이미지, 환경 변수, 시크릿, CloudWatch Logs 연결을 묶습니다.
    // 환경 변수/시크릿 계약은 애플리케이션 배포와 관리형 전환 때 영향 범위가 크므로 서비스별 호출부에서 명시합니다.
    const logGroup = createEcsServiceLogGroup(scope, config.logGroupId, config.serviceId);
    const container = taskDefinition.addContainer(config.containerId, {
        containerName: config.serviceId,
        image: config.image,
        logging: ecs.LogDrivers.awsLogs({
            streamPrefix: config.serviceId,
            logGroup,
        }),
        environment: config.environment,
        secrets: config.secrets,
    });
    // 모든 내부 HTTP 서비스는 컨테이너 포트 8080으로 통일합니다.
    // TLS 종료와 외부 리스너 구성은 로드 밸런서 쪽에 두어 컨테이너 이미지를 단순하게 유지합니다.
    container.addPortMappings({ containerPort: APP_CONTAINER_PORT, protocol: ecs.Protocol.TCP });

    // FargateService는 private subnet에 작업을 배치하고 Cloud Map 이름을 등록합니다.
    // 내부 서비스 호출은 Cloud Map을 쓰고, 외부 공개 여부는 스택에서 ALB/NLB 대상 연결로 따로 결정합니다.
    const service = new ecs.FargateService(scope, config.serviceConstructId, {
        cluster: config.cluster,
        taskDefinition,
        serviceName: `dev-${config.serviceId}`,
        desiredCount: DEV_SERVICE_DESIRED_TASKS,
        assignPublicIp: false,
        securityGroups: [config.securityGroup],
        vpcSubnets: config.vpcSubnets,
        circuitBreaker: { rollback: true },
        minHealthyPercent: 100,
        maxHealthyPercent: 200,
        cloudMapOptions: { name: config.serviceId },
        healthCheckGracePeriod: config.healthCheckGracePeriod,
    });

    // Auto Scaling 대상은 최소 1개, 최대 2개 작업으로 고정해 개발 환경의 가용성과 비용 상한을 함께 관리합니다.
    // CPU 목표치만 공통으로 걸어 두고, 더 복잡한 스케일링 정책은 실제 트래픽 검증 뒤 서비스별로 분리합니다.
    service.autoScaleTaskCount({ minCapacity: DEV_SERVICE_MIN_TASKS, maxCapacity: DEV_SERVICE_MAX_TASKS }).scaleOnCpuUtilization(config.cpuScalingId, {
        targetUtilizationPercent: SERVICE_CPU_SCALE_TARGET_PERCENT,
    });

    return {
        taskDefinition,
        container,
        service,
    };
}

export interface StaticFrontendSiteConfig {
    readonly idPrefix: string;
    readonly siteName: string;
    readonly bucketName: string;
    readonly recordName: string;
    readonly domainName: string;
    readonly certificate: acm.ICertificate;
    readonly publicHostedZone: route53.IHostedZone;
}

// 정적 프론트엔드 사이트는 S3 버킷, CloudFront 배포, Route 53 레코드, SSM 파라미터를 함께 만듭니다.
// dashboard와 demo-shoppingmall이 같은 보안 기본값을 쓰도록 공통화하고, 배포 파이프라인은 SSM 계약으로 산출물을 찾습니다.
export function createStaticFrontendSite(scope: Construct, config: StaticFrontendSiteConfig): void {
    // S3 버킷은 원본 저장소입니다. 직접 공개 호스팅을 켜지 않고 CloudFront OAC로만 읽게 합니다.
    // 버전 관리와 RETAIN은 잘못된 삭제나 배포 실수에서 정적 파일을 복구할 시간을 주기 위한 개발 안전장치입니다.
    const bucket = new s3.Bucket(scope, `${config.idPrefix}Bucket`, {
        bucketName: config.bucketName,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        versioned: true,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        removalPolicy: RemovalPolicy.RETAIN,
    });
    // CloudFront 배포는 HTTPS, 캐시, SPA fallback을 담당합니다.
    // PRICE_CLASS_100은 전 세계 edge를 모두 쓰지 않아 개발 환경의 전송 비용을 낮추기 위한 선택입니다.
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
    // Route 53 레코드는 사용자가 접근할 고정 도메인을 CloudFront에 연결합니다.
    // 버킷 이름이나 배포 도메인이 바뀌어도 외부 URL 계약은 레코드 이름으로 유지됩니다.
    new route53.ARecord(scope, `${config.idPrefix}DnsRecord`, {
        zone: config.publicHostedZone,
        recordName: config.recordName,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
    });

    // SSM 파라미터는 프론트엔드 배포 작업이 버킷과 배포 ID를 찾는 읽기 전용 계약입니다.
    // 다른 repository나 CI가 CDK construct 내부를 몰라도 같은 경로로 배포 대상을 찾을 수 있게 둡니다.
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
