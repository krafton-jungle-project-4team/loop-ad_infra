import { readFileSync } from 'node:fs';
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

    it('각 앱 repository는 uses로 reusable workflow를 호출한다', () => {
        const ecsExample = readFileSync(join(ROOT, 'docs/github-actions/app-ecs-deploy.example.yml'), 'utf8');
        const frontendExample = readFileSync(join(ROOT, 'docs/github-actions/frontend-deploy.example.yml'), 'utf8');

        expect(ecsExample).toContain('uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/ecs-deploy.yml@v1');
        expect(ecsExample).toContain('ecr_repository: loop-ad/event-collector');
        expect(ecsExample).toContain('ecs_service: dev-event-collector');
        expect(ecsExample).toContain('image_tag: ${{ github.sha }}');

        expect(frontendExample).toContain('uses: krafton-jungle-project-4team/loop-ad_aws_cdk/.github/workflows/frontend-deploy.yml@v1');
        expect(frontendExample).toContain('build_output_dir: dist');
        expect(frontendExample).toContain('s3_bucket: loop-ad-dev-dashboard-web');
        expect(frontendExample).toContain('s3_bucket: loop-ad-dev-demo-shoppingmall-web');
        expect(frontendExample).toContain('cloudfront_distribution_id: E1234567890ABC');
    });

    it('app repository guide는 실제 dev deploy target contract를 문서화한다', () => {
        const guide = readFileSync(join(ROOT, 'docs/app-repository-guide.md'), 'utf8');

        expect(guide).toContain('dev-loop-ad-cluster');
        expect(guide).toContain('loop-ad/decision-api');
        expect(guide).toContain('dev-decision-api');
        expect(guide).toContain('container_name');
        expect(guide).toContain('최초 seed image');
        expect(guide).toContain('latest');
        expect(guide).toContain('/loop-ad/dev/ecs/decision-api');
        expect(guide).toContain('loop-ad-dev-dashboard-web');
        expect(guide).toContain('/loop-ad/dev/frontend/dashboard-web/cloudfront-distribution-id');
    });

    it('infra workflow는 deploy 없이 build/test/synth만 수행한다', () => {
        const workflow = readFileSync(join(ROOT, '.github/workflows/infra-check.yml'), 'utf8');

        expect(workflow).toContain('npm run build');
        expect(workflow).toContain('npm test');
        expect(workflow).toContain('npm run synth:${{ inputs.environment }}');
        expect(workflow).toContain('Validate required inputs');
        expect(workflow).toContain('cdk_default_account:');
        expect(workflow).toContain('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN');
        expect(workflow).toContain('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN');
        expect(workflow).not.toContain('cdk deploy');
        expect(workflow).not.toContain('default:');
    });

    it('CDK app은 required env/context 없이 기본값으로 fallback하지 않는다', () => {
        const cdkApp = readFileSync(join(ROOT, 'bin/loop-ad_aws_cdk.ts'), 'utf8');

        expect(cdkApp).toContain("readRequiredEnv('CDK_DEFAULT_ACCOUNT')");
        expect(cdkApp).toContain("readRequiredEnv('LOOP_AD_FRONTEND_SITES_CERTIFICATE_ARN')");
        expect(cdkApp).toContain("readRequiredEnv('LOOP_AD_GENAI_GENERATED_ASSETS_CERTIFICATE_ARN')");
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
        expect(packageJson.scripts['deploy:dev-network']).toContain('LoopAdDevNetworkStack');
        expect(packageJson.scripts['deploy:dev-data']).toContain('LoopAdDevDataStack');
        expect(packageJson.scripts['deploy:dev-runtime']).toContain('LoopAdDevRuntimeStack');
        expect(packageJson.scripts['synth:dev']).toContain('LoopAdDevDataStack');
        expect(packageJson.scripts['synth:dev']).toContain('LoopAdDevRuntimeStack');
        expect(packageJson.scripts['deploy:dev']).toContain('LoopAdDevDataStack');
        expect(packageJson.scripts['deploy:dev']).toContain('LoopAdDevRuntimeStack');
        expect(refusal).toContain('intentionally blocked');
        expect(refusal).toContain('deploy:dev');
    });
});
