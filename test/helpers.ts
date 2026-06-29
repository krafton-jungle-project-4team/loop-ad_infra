import * as cdk from 'aws-cdk-lib';
import {
    LOOP_AD_REGION,
    LoopAdDevCertificateStack,
    LoopAdDevDataStack,
    LoopAdDevNetworkStack,
    LoopAdDevRepositoryStack,
    LoopAdDevRuntimeStack,
    LoopAdDevSecretsStack,
    type DeveloperAllowlistConfig,
} from '../src/loop-ad-stack';

export const ROOT = `${__dirname}/..`;
export const EXPECTED_APP_INTERNAL_PORT = 8080;
export const testEnv = {
    account: '123456789012',
    region: LOOP_AD_REGION,
};
export const testPublicHostedZone = {
    hostedZoneId: 'ZTESTHOSTEDZONEID',
    domainName: 'example.test',
};
export const testCertificateArns = {
    frontendSitesCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/frontend-sites',
    genAiGeneratedAssetsCertificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/gen-ai-assets',
};
export const testSecretNames = {
    auroraCredentialsSecretName: '/loop-ad/dev/aurora/credentials',
    clickHouseCredentialsSecretName: '/loop-ad/dev/clickhouse/credentials',
    kafkaAppUserSecretName: '/loop-ad/dev/kafka/app-user',
    kafkaBrokerUserSecretName: '/loop-ad/dev/kafka/broker-user',
    openAiApiKeySecretName: '/loop-ad/dev/openai/api-key',
    internalApiKeySecretName: '/loop-ad/dev/internal/api-key',
};
export const emptyDeveloperAllowlist = {
    ipv4Cidrs: [],
    ipv6Cidrs: [],
};

export function synthNetwork(developerAllowlist: DeveloperAllowlistConfig): LoopAdDevNetworkStack {
    const app = new cdk.App();
    return new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
        developerAllowlist,
    });
}

export function synthData(): LoopAdDevDataStack {
    const app = new cdk.App();
    const network = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
        developerAllowlist: emptyDeveloperAllowlist,
    });
    return new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        network,
        genAiGeneratedAssetsCertificateArn: testCertificateArns.genAiGeneratedAssetsCertificateArn,
        secretNames: testSecretNames,
    });
}

export function synthRuntime(): LoopAdDevRuntimeStack {
    const app = new cdk.App();
    const network = new LoopAdDevNetworkStack(app, 'LoopAdDevNetworkStack', {
        env: testEnv,
        developerAllowlist: emptyDeveloperAllowlist,
    });
    const data = new LoopAdDevDataStack(app, 'LoopAdDevDataStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        network,
        genAiGeneratedAssetsCertificateArn: testCertificateArns.genAiGeneratedAssetsCertificateArn,
        secretNames: testSecretNames,
    });
    return new LoopAdDevRuntimeStack(app, 'LoopAdDevRuntimeStack', {
        env: testEnv,
        publicHostedZone: testPublicHostedZone,
        certificateArns: testCertificateArns,
        network,
        data,
        runtimeSecretNames: testSecretNames,
    });
}

export function synthSecrets(): LoopAdDevSecretsStack {
    const app = new cdk.App();
    return new LoopAdDevSecretsStack(app, 'LoopAdDevSecretsStack', {
        env: testEnv,
        secretNames: testSecretNames,
    });
}

export function synthRepositories(): LoopAdDevRepositoryStack {
    const app = new cdk.App();
    return new LoopAdDevRepositoryStack(app, 'LoopAdDevRepositoryStack', {
        env: testEnv,
    });
}

export function synthCertificate(): LoopAdDevCertificateStack {
    const app = new cdk.App();
    return new LoopAdDevCertificateStack(app, 'LoopAdDevCertificateStack', {
        env: {
            account: testEnv.account,
            region: 'us-east-1',
        },
        publicHostedZone: testPublicHostedZone,
    });
}

export function resourcesOf(template: { toJSON(): { Resources?: Record<string, unknown> } }): Record<string, { Type: string; Properties?: Record<string, unknown>; DeletionPolicy?: string; UpdateReplacePolicy?: string }> {
    return (template.toJSON().Resources ?? {}) as Record<string, { Type: string; Properties?: Record<string, unknown>; DeletionPolicy?: string; UpdateReplacePolicy?: string }>;
}

export function logicalIdBySecurityGroupDescription(resources: ReturnType<typeof resourcesOf>, description: string): string {
    const entry = Object.entries(resources).find(([, resource]) => (
        resource.Type === 'AWS::EC2::SecurityGroup' && resource.Properties?.GroupDescription === description
    ));
    if (!entry) {
        throw new Error(`Unable to find security group: ${description}`);
    }

    return entry[0];
}
