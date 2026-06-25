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
    expect(workflow).toContain('default: ap-northeast-2');
  });

  it('frontend workflow는 S3 sync와 CloudFront invalidation만 담당한다', () => {
    const workflow = readFileSync(join(ROOT, '.github/workflows/frontend-deploy.yml'), 'utf8');

    expect(workflow).toContain('workflow_call:');
    expect(workflow).toContain('aws s3 sync');
    expect(workflow).toContain('aws cloudfront create-invalidation');
  });

  it('infra workflow는 deploy 없이 build/test/synth만 수행한다', () => {
    const workflow = readFileSync(join(ROOT, '.github/workflows/infra-check.yml'), 'utf8');

    expect(workflow).toContain('npm run build');
    expect(workflow).toContain('npm test');
    expect(workflow).toContain('cdk synth');
    expect(workflow).not.toContain('cdk deploy');
  });

  it('package script가 deploy/destroy를 차단한다', () => {
    const packageJson = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
      scripts: Record<string, string>;
    };
    const refusal = readFileSync(join(ROOT, 'scripts/refuse-deploy.mjs'), 'utf8');

    expect(packageJson.scripts.deploy).toBe('node scripts/refuse-deploy.mjs');
    expect(packageJson.scripts.destroy).toBe('node scripts/refuse-deploy.mjs');
    expect(refusal).toContain('intentionally blocked');
  });
});
