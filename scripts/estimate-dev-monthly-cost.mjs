#!/usr/bin/env node

const HOURS_PER_MONTH = 730;
const DEV_BUDGET_LIMIT_USD = 300;

const assumptions = [
    {
        id: 'nat-gateway-hourly',
        component: 'Network',
        description: 'One NAT Gateway hourly charge',
        quantity: 1,
        unitPriceUsd: 0.059,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption; refresh with AWS Pricing Calculator or Price List API for ap-northeast-2.',
    },
    {
        id: 'nat-gateway-data',
        component: 'Network',
        description: 'NAT data processing for external SaaS/API and AWS public API traffic',
        quantity: 50,
        unitPriceUsd: 0.059,
        unitsPerMonth: 1,
        source: 'Explicit planning assumption: 50 GB/month after S3 Gateway Endpoint offload.',
    },
    {
        id: 'fargate-arm64-vcpu',
        component: 'Runtime',
        description: 'Five steady dev Fargate tasks, 0.25 vCPU each',
        quantity: 5 * 0.25,
        unitPriceUsd: 0.04656,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption for Linux/ARM Fargate vCPU-hour.',
    },
    {
        id: 'fargate-arm64-memory',
        component: 'Runtime',
        description: 'Five steady dev Fargate tasks, 0.5 GiB each',
        quantity: 5 * 0.5,
        unitPriceUsd: 0.00511,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption for Linux/ARM Fargate GB-hour.',
    },
    {
        id: 'load-balancers',
        component: 'Ingress',
        description: 'One ALB and one NLB with low dev LCU/NLCU usage',
        quantity: 1,
        unitPriceUsd: 46.36,
        unitsPerMonth: 1,
        source: 'Explicit planning assumption combining hourly and one low-utilization capacity unit for ALB/NLB.',
    },
    {
        id: 'aurora-serverless-v2-average-acu',
        component: 'Database',
        description: 'Aurora Serverless v2 expected average after auto-pause',
        quantity: 0.15,
        unitPriceUsd: 0.14,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption: dev idle-heavy workload with min 0 ACU and 10 minute auto-pause.',
    },
    {
        id: 'aurora-storage-io',
        component: 'Database',
        description: 'Aurora storage, backup, and low dev I/O allowance',
        quantity: 1,
        unitPriceUsd: 7,
        unitsPerMonth: 1,
        source: 'Explicit planning assumption for low dev data volume; validate with Cost Explorer after deployment.',
    },
    {
        id: 'valkey-serverless',
        component: 'Cache',
        description: 'ElastiCache Serverless for Valkey capped at 1 GB and low ECPU usage',
        quantity: 1,
        unitPriceUsd: 19.52,
        unitsPerMonth: 1,
        source: 'Explicit planning assumption based on 1 GB storage cap plus low ECPU usage.',
    },
    {
        id: 'clickhouse-ec2',
        component: 'Analytics',
        description: 'ClickHouse t4g.small dev instance',
        quantity: 1,
        unitPriceUsd: 0.0208,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption for t4g.small Linux on-demand.',
    },
    {
        id: 'kafka-ec2',
        component: 'Streaming',
        description: 'Kafka t4g.small single broker dev instance',
        quantity: 1,
        unitPriceUsd: 0.0208,
        unitsPerMonth: HOURS_PER_MONTH,
        source: 'Explicit planning assumption for t4g.small Linux on-demand.',
    },
    {
        id: 'data-node-ebs-gp3',
        component: 'Storage',
        description: '70 GiB gp3 EBS for ClickHouse and Kafka',
        quantity: 70,
        unitPriceUsd: 0.096,
        unitsPerMonth: 1,
        source: 'Explicit planning assumption for gp3 GB-month.',
    },
    {
        id: 'static-and-observability',
        component: 'Shared',
        description: 'S3, CloudFront, ECR, CloudWatch Logs, Route 53, ACM, SSM low dev usage',
        quantity: 1,
        unitPriceUsd: 18,
        unitsPerMonth: 1,
        source: 'Explicit planning allowance for low dev static hosting, logs, DNS, images, and parameters.',
    },
];

const lineItems = assumptions.map((item) => ({
    ...item,
    monthlyUsd: roundCurrency(item.quantity * item.unitPriceUsd * item.unitsPerMonth),
}));

const totalMonthlyUsd = roundCurrency(lineItems.reduce((sum, item) => sum + item.monthlyUsd, 0));
const headroomUsd = roundCurrency(DEV_BUDGET_LIMIT_USD - totalMonthlyUsd);
const budgetUtilizationPercent = roundCurrency((totalMonthlyUsd / DEV_BUDGET_LIMIT_USD) * 100);

const result = {
    budgetLimitUsd: DEV_BUDGET_LIMIT_USD,
    hoursPerMonth: HOURS_PER_MONTH,
    totalMonthlyUsd,
    headroomUsd,
    budgetUtilizationPercent,
    lineItems,
    notes: [
        'This is a deterministic planning model, not a live bill.',
        'Refresh unit prices with AWS Pricing Calculator or Price List API before deployment approval.',
        'After deployment, compare this model with Cost Explorer and AWS Budgets actual/forecasted alerts.',
    ],
};

if (process.argv.includes('--json')) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} else {
    printHumanReport(result);
}

function roundCurrency(value) {
    return Math.round((value + Number.EPSILON) * 100) / 100;
}

function printHumanReport(model) {
    console.log('loop-ad dev monthly cost planning model');
    console.log(`Budget limit: $${model.budgetLimitUsd.toFixed(2)}`);
    console.log(`Estimated monthly total: $${model.totalMonthlyUsd.toFixed(2)}`);
    console.log(`Budget headroom: $${model.headroomUsd.toFixed(2)} (${model.budgetUtilizationPercent.toFixed(2)}% used)`);
    console.log('');
    console.log('| Component | Description | Monthly USD |');
    console.log('|---|---|---:|');
    for (const item of model.lineItems) {
        console.log(`| ${item.component} | ${item.description} | $${item.monthlyUsd.toFixed(2)} |`);
    }
    console.log('');
    for (const note of model.notes) {
        console.log(`- ${note}`);
    }
}
