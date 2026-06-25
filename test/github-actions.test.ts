import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = join(__dirname, '..');

describe('GitHub Actions reusable workflows', () => {
    it('ECS deploy workflow는 workflow_call과 OIDC를 사용한다', () => {
        const workflow = readFileSync(join(ROOT, '.github/workflows/ecs-deploy.yml'), 'utf8');

        expect(workflow).toContain('workflow_call:');
        expect(workflow).toContain('id-token: write');
        expect(workflow).toContain('aws-actions/configure-aws-credentials@v4');
        expect(workflow).toContain('aws-actions/amazon-ecr-login@v2');
        expect(workflow).toContain('aws-actions/amazon-ecs-render-task-definition@v1');
        expect(workflow).toContain('aws-actions/amazon-ecs-deploy-task-definition@v2');
        expect(workflow).toContain('aws_region:');
        expect(workflow).toContain('Validate required inputs');
        expect(workflow).toContain('IMAGE_TAG \\');
        expect(workflow).toContain('${input_name} input is required.');
        expect(workflow).toContain('Unable to resolve task definition');
        expect(workflow).not.toContain('default:');
    });

    it('frontend deploy workflow는 S3 업로드와 CloudFront invalidation을 수행한다', () => {
        const workflow = readFileSync(join(ROOT, '.github/workflows/frontend-deploy.yml'), 'utf8');

        expect(workflow).toContain('workflow_call:');
        expect(workflow).toContain('id-token: write');
        expect(workflow).toContain('actions/setup-node@v4');
        expect(workflow).toContain('aws-actions/configure-aws-credentials@v4');
        expect(workflow).toContain('aws s3 sync');
        expect(workflow).toContain('aws s3 cp');
        expect(workflow).toContain('aws cloudfront create-invalidation');
        expect(workflow).toContain('ASSET_CACHE_CONTROL \\');
        expect(workflow).toContain('HTML_CACHE_CONTROL');
        expect(workflow).toContain('${input_name} input is required.');
        expect(workflow).not.toContain('default:');
    });

    it('각 앱 repository에서 복사할 caller workflow 템플릿을 제공한다', () => {
        const templateDir = join(ROOT, 'docs/github-actions/templates');
        const templates = readdirSync(templateDir).sort();

        expect(templates).toEqual(expect.arrayContaining([
            'ad-context-projector.ecs-deploy.yml',
            'ad-decision-api.ecs-deploy.yml',
            'dashboard-api.ecs-deploy.yml',
            'dashboard-web.frontend-deploy.yml',
            'event-collector.ecs-deploy.yml',
            'recommendation.ecs-deploy.yml',
        ]));

        const eventCollector = readFileSync(join(templateDir, 'event-collector.ecs-deploy.yml'), 'utf8');
        expect(eventCollector).toContain('uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/ecs-deploy.yml@v1');
        expect(eventCollector).toContain('ecr_repository: loop-ad/event-collector');
        expect(eventCollector).toContain('ecs_service: dev-event-collector');
        expect(eventCollector).toContain('image_tag: ${{ github.sha }}');

        const dashboardWeb = readFileSync(join(templateDir, 'dashboard-web.frontend-deploy.yml'), 'utf8');
        expect(dashboardWeb).toContain('uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/frontend-deploy.yml@v1');
        expect(dashboardWeb).toContain('build_output_dir: dist');
        expect(dashboardWeb).toContain('s3_bucket: loop-ad-dev-dashboard-web');
        expect(dashboardWeb).toContain('cloudfront_distribution_id: E1234567890ABC');
    });

    it('infra workflow는 deploy 없이 build/test/synth만 수행한다', () => {
        const workflow = readFileSync(join(ROOT, '.github/workflows/infra-check.yml'), 'utf8');

        expect(workflow).toContain('npm run build');
        expect(workflow).toContain('npm test');
        expect(workflow).toContain('npm run synth:${{ inputs.environment }}');
        expect(workflow).toContain('Validate required inputs');
        expect(workflow).not.toContain('cdk deploy');
        expect(workflow).not.toContain('default:');
    });

    it('CDK app은 required env/context 없이 기본값으로 fallback하지 않는다', () => {
        const cdkApp = readFileSync(join(ROOT, 'bin/loop-ad_aws_cdk.ts'), 'utf8');

        expect(cdkApp).toContain("readRequiredEnv('CDK_DEFAULT_ACCOUNT')");
        expect(cdkApp).toContain('Missing required CDK context "environment"');
        expect(cdkApp).not.toContain("?? 'dev'");
        expect(cdkApp).not.toContain('process.env.CDK_DEFAULT_ACCOUNT');
    });

    it('generic deploy/destroy는 막고 명시적 dev lifecycle script만 둔다', () => {
        const packageJson = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
            scripts: Record<string, string>;
        };
        const refusal = readFileSync(join(ROOT, 'scripts/refuse-deploy.mjs'), 'utf8');

        expect(packageJson.scripts.deploy).toBe('node scripts/refuse-deploy.mjs');
        expect(packageJson.scripts.destroy).toBe('node scripts/refuse-deploy.mjs');
        expect(packageJson.scripts['deploy:dev']).toContain('LoopAdDevStack');
        expect(refusal).toContain('intentionally blocked');
        expect(refusal).toContain('deploy:dev');
    });
});
