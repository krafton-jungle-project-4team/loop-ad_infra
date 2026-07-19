import * as cdk from 'aws-cdk-lib';
import {
    LoopAdDevCertificateStack,
    LoopAdDevDataStack,
    LoopAdDevNetworkStack,
    LoopAdDevRepositoryStack,
    LoopAdDevRuntimeStack,
    LoopAdDevSecretsStack,
} from './loop-ad-stack';
import { DEV_VPC_AVAILABILITY_ZONES } from './dev-config';
import {
    readCdkAppConfig,
    type CdkAppConfig,
    type EnvironmentName,
} from './cdk-app-config';
import { LoopAdPerfPhase0Stack } from './perf-phase0-stack';

type StackGroupFactory = (app: cdk.App, config: CdkAppConfig) => void;

interface DataAssembly {
    readonly secretsStack: LoopAdDevSecretsStack;
    readonly networkStack: LoopAdDevNetworkStack;
    readonly dataStack: LoopAdDevDataStack;
}

// CDK context의 environment 값이 곧 합성 단위입니다.
// bin에서 분기하지 않고 여기서 표로 관리하면 stack 조합이 늘어도 진입점은 얇게 유지됩니다.
const STACK_GROUP_FACTORIES: Record<EnvironmentName, StackGroupFactory> = {
    dev: createRuntimeAssembly,
    'dev-certificate': createCertificateStack,
    'dev-repositories': createRepositoryStack,
    'dev-secrets': createSecretsStack,
    'dev-network': createNetworkStack,
    'dev-data': createDataAssembly,
    'dev-runtime': createRuntimeAssembly,
    'perf-phase0': createPerfPhase0Stack,
};

export function main(): void {
    const app = new cdk.App();
    const config = readCdkAppConfig(app);

    seedAvailabilityZoneContext(app, config);
    STACK_GROUP_FACTORIES[config.environmentName](app, config);

    cdk.Tags.of(app).add('Project', 'loop-ad');
    cdk.Tags.of(app).add('CdkProject', 'loop-ad_aws_cdk');
    cdk.Tags.of(app).add('Environment', config.environmentName.startsWith('dev-') ? 'dev' : config.environmentName);
}

function seedAvailabilityZoneContext(app: cdk.App, config: CdkAppConfig): void {
    const account = requireConfig(config.stackEnv.account, 'stackEnv.account');
    const region = requireConfig(config.stackEnv.region, 'stackEnv.region');

    // VPC는 AZ 목록을 이미 코드로 명시하므로, synth 시 AWS 조회가 필요 없도록 같은 값을 context에도 고정합니다.
    app.node.setContext(`availability-zones:account=${account}:region=${region}`, DEV_VPC_AVAILABILITY_ZONES);
}

function createCertificateStack(app: cdk.App, config: CdkAppConfig): void {
    new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: config.certificateStackEnv,
        publicHostedZone: requireConfig(config.publicHostedZone, 'publicHostedZone'),
    });
}

function createRepositoryStack(app: cdk.App, config: CdkAppConfig): void {
    new LoopAdDevRepositoryStack(app, 'LoopAdDevRepositoryStack', {
        env: config.stackEnv,
    });
}

function createSecretsStack(app: cdk.App, config: CdkAppConfig): void {
    new LoopAdDevSecretsStack(app, 'LoopAdDevSecretsStack', {
        env: config.stackEnv,
        secretNames: requireConfig(config.secretNames, 'secretNames'),
    });
}

function createNetworkStack(app: cdk.App, config: CdkAppConfig): void {
    new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: config.stackEnv,
        developerAllowlist: requireConfig(config.developerAllowlist, 'developerAllowlist'),
    });
}

function createDataAssembly(app: cdk.App, config: CdkAppConfig): DataAssembly {
    const secretNames = requireConfig(config.secretNames, 'secretNames');
    // Data 리소스가 시크릿 이름을 가져오므로 소유자인 Secrets 스택을 같은 합성 범위에 둡니다.
    // 값은 sync 스크립트가 넣지만, CloudFormation dependency는 secret 리소스 생성 순서를 보장해야 합니다.
    const secretsStack = new LoopAdDevSecretsStack(app, 'LoopAdDevSecretsStack', {
        env: config.stackEnv,
        secretNames,
    });
    const networkStack = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: config.stackEnv,
        developerAllowlist: requireConfig(config.developerAllowlist, 'developerAllowlist'),
    });
    const dataStack = new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env: config.stackEnv,
        publicHostedZone: requireConfig(config.publicHostedZone, 'publicHostedZone'),
        network: networkStack,
        genAiGeneratedAssetsCertificateArn: requireConfig(config.genAiGeneratedAssetsCertificateArn, 'genAiGeneratedAssetsCertificateArn'),
        secretNames,
    });
    dataStack.addDependency(secretsStack);

    return {
        secretsStack,
        networkStack,
        dataStack,
    };
}

function createRuntimeAssembly(app: cdk.App, config: CdkAppConfig): LoopAdDevRuntimeStack {
    // 기본 dev/dev-runtime 합성 결과는 인증서/저장소 lifecycle 스택을 제외한 의존 스택을 모두 포함합니다.
    const {
        secretsStack,
        networkStack,
        dataStack,
    } = createDataAssembly(app, config);
    const runtimeStack = new LoopAdDevRuntimeStack(app, 'LoopAdDevRuntimeStack', {
        env: config.stackEnv,
        publicHostedZone: requireConfig(config.publicHostedZone, 'publicHostedZone'),
        network: networkStack,
        data: dataStack,
        runtimeSecretNames: requireConfig(config.secretNames, 'secretNames'),
        openPixelSigningSecretArn: secretsStack.openPixelSigningSecretArn,
        certificateArns: requireConfig(config.certificateArns, 'certificateArns'),
    });
    runtimeStack.addDependency(secretsStack);

    return runtimeStack;
}

function createPerfPhase0Stack(app: cdk.App, config: CdkAppConfig): void {
    new LoopAdPerfPhase0Stack(app, 'LoopAdPerfPhase0Stack', {
        env: config.stackEnv,
    });
}

function requireConfig<T>(value: T | undefined, name: string): T {
    // optional config를 호출부에서 바로 단언하지 않고 이 함수로 실패 메시지를 통일합니다.
    // 잘못된 environment/schema 조합은 synth 초기에 드러나는 편이 배포 실패보다 다루기 쉽습니다.
    if (value === undefined) {
        throw new Error(`Missing parsed CDK app config: ${name}.`);
    }

    return value;
}
