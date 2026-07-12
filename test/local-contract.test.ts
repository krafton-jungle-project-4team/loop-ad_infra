import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { ROOT } from './helpers';

const SRC_DIR = join(ROOT, 'src');
const DOCS_DIR = join(ROOT, 'docs');

describe('local repository contracts', () => {
    it('keeps npm scripts deploy-capable but removes secret-writing helpers', () => {
        const packageJson = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
            scripts: Record<string, string>;
        };

        expect(packageJson.scripts).toEqual({
            build: 'tsc --noEmit',
            test: 'jest --runInBand',
            verify: 'npm run build && npm test',
            synth: 'cdk -c environment=dev synth',
            deploy: 'cdk -c environment=dev deploy',
            destroy: 'cdk -c environment=dev destroy',
            'secrets:sync': 'node scripts/sync-dev-secrets.mjs',
            cdk: 'cdk',
        });
        expect(existsSync(join(ROOT, 'scripts/put-openai-api-key.mjs'))).toBe(false);
        expect(existsSync(join(ROOT, 'scripts/estimate-dev-monthly-cost.mjs'))).toBe(false);
        expect(existsSync(join(ROOT, 'scripts/sync-dev-secrets.mjs'))).toBe(true);
    });

    it('keeps the CDK bin entrypoint thin', () => {
        const binText = readFileSync(join(ROOT, 'bin/loop-ad_aws_cdk.ts'), 'utf8');

        expect(binText).toContain("import { main } from '../src/cdk-app';");
        expect(binText).toContain('main();');
        expect(binText).not.toContain('LoopAdDevRuntimeStack');
        expect(binText.split('\n').filter((line) => line.trim().length > 0)).toHaveLength(3);
    });

    it('keeps sensitive values in Secrets Manager names and the sync script contract only', () => {
        const sourceText = sourceFiles(SRC_DIR)
            .concat([join(ROOT, 'bin/loop-ad_aws_cdk.ts'), join(ROOT, 'scripts/sync-dev-secrets.mjs')])
            .map((file) => readFileSync(file, 'utf8'))
            .join('\n');

        expect(sourceText).toContain('LOOP_AD_SECRET_PREFIX');
        expect(sourceText).toContain('describe-secret');
        expect(sourceText).toContain('put-secret-value');
        expect(sourceText).not.toContain('DEFAULT_DEV_SECRET_PREFIX');
        expect(sourceText).not.toContain('DEFAULT_SECRET_PREFIX');
        expect(sourceText).not.toContain('DEFAULT_REGION');
        expect(sourceText).not.toContain('DEFAULT_ENV_FILE');
        expect(sourceText).not.toContain('create-secret');
        expect(sourceText).not.toContain('get-secret-value');
        expect(sourceText).not.toContain('batch-get-secret-value');
        expect(readFileSync(join(ROOT, '.env.example'), 'utf8')).not.toContain('_SECRET_ARN');
        expect(readFileSync(join(ROOT, '.env.example'), 'utf8')).toContain('CDK_DEFAULT_ACCOUNT');
        expect(readFileSync(join(ROOT, '.env.example'), 'utf8')).toContain('LOOP_AD_REGION');
        expect(readFileSync(join(ROOT, '.env.example'), 'utf8')).toContain('LOOP_AD_SECRET_PREFIX');
        expect(readFileSync(join(ROOT, '.gitignore'), 'utf8')).toContain('.env.secrets');
        expect(sourceText).not.toContain('fromGeneratedSecret');
        expect(sourceText).not.toContain('SecureString');
        expect(sourceText).not.toContain('valueForSecureStringParameter');
        expect(sourceText).not.toContain('fromSecureStringParameterAttributes');
    });

    it('keeps L1 constructs limited to documented outputs', () => {
        const allowed = new Set([
            'cdk.CfnOutput',
            'secretsmanager.CfnSecret',
        ]);
        const violations = sourceFiles(SRC_DIR).flatMap((file) => {
            const source = readFileSync(file, 'utf8');
            const matches = [...source.matchAll(/new\s+([a-zA-Z0-9_]+\.Cfn[A-Za-z0-9_]+)/g)];

            return matches.flatMap((match) => {
                const constructName = match[1];
                return constructName && !allowed.has(constructName) ? [`${file}: ${constructName}`] : [];
            });
        });

        expect(violations).toEqual([]);
    });

    it('removes stale docs and keeps the new document set focused', () => {
        expect(readdirSync(DOCS_DIR).sort()).toEqual([
            'app-repository-guide.md',
            'guide_aws_event_pipeline_performance_test.md',
            'process_aws_perf_test_result_recording.md',
            'secrets-setup.md',
            'template_aws_perf_test_run_report.md',
        ]);
        expect(readFileSync(join(DOCS_DIR, 'app-repository-guide.md'), 'utf8')).not.toContain('사용하는 서비스');
    });

    it('keeps reusable GitHub workflows OIDC-based and infra checks deploy-free', () => {
        const ecsWorkflow = readFileSync(join(ROOT, '.github/workflows/ecs-deploy.yml'), 'utf8');
        const frontendWorkflow = readFileSync(join(ROOT, '.github/workflows/frontend-deploy.yml'), 'utf8');
        const infraWorkflow = readFileSync(join(ROOT, '.github/workflows/infra-check.yml'), 'utf8');

        expect(ecsWorkflow).toContain('id-token: write');
        expect(ecsWorkflow).toContain('runs-on: ubuntu-24.04-arm');
        expect(ecsWorkflow).toContain('--platform linux/arm64');
        expect(frontendWorkflow).toContain('id-token: write');
        expect(infraWorkflow).toContain('npm run build');
        expect(infraWorkflow).toContain('npm test');
        expect(infraWorkflow).toContain('npm run cdk -- -c environment=${{ inputs.environment }} synth --quiet');
        expect(infraWorkflow).toContain('CDK_DEFAULT_ACCOUNT');
        expect(infraWorkflow).toContain('LOOP_AD_REGION');
        expect(infraWorkflow).toContain('LOOP_AD_SECRET_PREFIX');
        expect(infraWorkflow).not.toContain('default: /loop-ad/dev');
        expect(infraWorkflow).not.toContain('_SECRET_ARN');
        expect(infraWorkflow).not.toContain('cdk deploy');
        expect(infraWorkflow).not.toContain('cdk destroy');
    });
});

function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((entry) => {
        const path = join(dir, entry);
        if (statSync(path).isDirectory()) {
            return sourceFiles(path);
        }

        return path.endsWith('.ts') ? [path] : [];
    });
}
