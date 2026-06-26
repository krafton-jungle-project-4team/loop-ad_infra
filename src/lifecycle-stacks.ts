import { RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';
import {
    DASHBOARD_WEB_RECORD_NAME,
    DEMO_SHOPPINGMALL_WEB_RECORD_NAME,
    DEV_APPLICATION_REPOSITORIES,
    DEV_MONTHLY_BUDGET_CRITICAL_PERCENT,
    DEV_MONTHLY_BUDGET_LIMIT_USD,
    DEV_MONTHLY_BUDGET_WARNING_PERCENT,
    GENAI_PUBLIC_ASSETS_RECORD_NAME,
    type PublicHostedZoneConfig,
} from './dev-config';

export interface LoopAdDevCertificateStackProps extends StackProps {
    readonly publicHostedZone: PublicHostedZoneConfig;
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

export interface LoopAdDevCostGuardrailStackProps extends StackProps {
    readonly budgetAlertEmail: string;
}

// Budgets는 billing plane guardrail이므로 dev resource stack과 분리합니다.
// 실제 비용 알림 수신 확인은 배포 후 AWS Budgets email confirmation까지 완료되어야 합니다.
export class LoopAdDevCostGuardrailStack extends Stack {
    public constructor(scope: Construct, id: string, props: LoopAdDevCostGuardrailStackProps) {
        super(scope, id, props);

        const budget = new budgets.CfnBudget(this, 'MonthlyDevBudget', {
            budget: {
                budgetName: 'loop-ad-dev-monthly-budget',
                budgetType: 'COST',
                timeUnit: 'MONTHLY',
                budgetLimit: {
                    amount: DEV_MONTHLY_BUDGET_LIMIT_USD,
                    unit: 'USD',
                },
                costTypes: {
                    includeCredit: false,
                    includeRefund: false,
                    useBlended: false,
                },
            },
            notificationsWithSubscribers: [
                {
                    notification: {
                        notificationType: 'ACTUAL',
                        comparisonOperator: 'GREATER_THAN',
                        threshold: DEV_MONTHLY_BUDGET_WARNING_PERCENT,
                        thresholdType: 'PERCENTAGE',
                    },
                    subscribers: [
                        {
                            subscriptionType: 'EMAIL',
                            address: props.budgetAlertEmail,
                        },
                    ],
                },
                {
                    notification: {
                        notificationType: 'ACTUAL',
                        comparisonOperator: 'GREATER_THAN',
                        threshold: DEV_MONTHLY_BUDGET_CRITICAL_PERCENT,
                        thresholdType: 'PERCENTAGE',
                    },
                    subscribers: [
                        {
                            subscriptionType: 'EMAIL',
                            address: props.budgetAlertEmail,
                        },
                    ],
                },
                {
                    notification: {
                        notificationType: 'FORECASTED',
                        comparisonOperator: 'GREATER_THAN',
                        threshold: DEV_MONTHLY_BUDGET_CRITICAL_PERCENT,
                        thresholdType: 'PERCENTAGE',
                    },
                    subscribers: [
                        {
                            subscriptionType: 'EMAIL',
                            address: props.budgetAlertEmail,
                        },
                    ],
                },
            ],
            resourceTags: [
                { key: 'Project', value: 'loop-ad' },
                { key: 'Environment', value: 'dev' },
            ],
        });

        new cdk.CfnOutput(this, 'MonthlyDevBudgetName', {
            value: budget.ref,
        });
    }
}
