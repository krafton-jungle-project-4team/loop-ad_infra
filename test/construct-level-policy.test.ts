import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC_DIR = join(__dirname, '..', 'src');
const ALLOWED_L1_CONSTRUCTS = new Set<string>([
    'cdk.CfnOutput',
    // ElastiCache Serverless는 현재 CDK L2 construct가 없어 L1으로만 정의합니다.
    'elasticache.CfnServerlessCache',
]);

describe('CDK construct level policy', () => {
    it('명시적 예외 없이 L1 Cfn* construct를 직접 생성하지 않는다', () => {
        const violations = sourceFiles(SRC_DIR).flatMap((file) => {
            const source = readFileSync(file, 'utf8');
            const matches = [...source.matchAll(/new\s+([a-zA-Z0-9_]+\.Cfn[A-Za-z0-9_]+)/g)];

            return matches
                .flatMap((match) => {
                    const constructName = match[1];
                    if (!constructName || ALLOWED_L1_CONSTRUCTS.has(constructName)) {
                        return [];
                    }

                    return [`${file}: ${constructName}`];
                });
        });

        expect(violations).toEqual([]);
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
