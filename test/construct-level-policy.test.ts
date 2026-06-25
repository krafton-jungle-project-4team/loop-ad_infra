import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SRC_DIR = join(__dirname, '..', 'src');
const ALLOWED_L1_CONSTRUCTS = new Set<string>();

describe('CDK construct level policy', () => {
  it('명시적 예외 없이 L1 Cfn* construct를 직접 생성하지 않는다', () => {
    const violations = sourceFiles(SRC_DIR).flatMap((file) => {
      const source = readFileSync(file, 'utf8');
      const matches = [...source.matchAll(/new\s+([a-zA-Z0-9_]+\.Cfn[A-Za-z0-9_]+)/g)];

      return matches
        .map((match) => match[1])
        .filter((constructName) => !ALLOWED_L1_CONSTRUCTS.has(constructName))
        .map((constructName) => `${file}: ${constructName}`);
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
