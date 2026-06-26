import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { config as loadDotenv } from 'dotenv';

const PARAMETER_NAME = '/loop-ad/dev/external/openai/api-key';
const AWS_REGION = 'ap-northeast-2';

if (existsSync('.env')) {
    const dotenvResult = loadDotenv({ path: '.env', quiet: true });
    if (dotenvResult.error) {
        throw new Error(`Failed to load .env: ${dotenvResult.error.message}`);
    }
}

const apiKey = process.env.LOOP_AD_OPENAI_API_KEY?.trim();
if (!apiKey) {
    throw new Error('Missing required environment variable LOOP_AD_OPENAI_API_KEY.');
}

const tempDir = mkdtempSync(join(tmpdir(), 'loop-ad-openai-'));
const payloadPath = join(tempDir, 'put-parameter.json');

try {
    // secret 값을 command line argument로 직접 넘기지 않기 위해 임시 JSON payload를 사용합니다.
    writeFileSync(payloadPath, JSON.stringify({
        Name: PARAMETER_NAME,
        Type: 'SecureString',
        Value: apiKey,
        Overwrite: true,
    }), { mode: 0o600 });

    const result = spawnSync('aws', [
        'ssm',
        'put-parameter',
        '--region',
        AWS_REGION,
        '--cli-input-json',
        `file://${payloadPath}`,
    ], { stdio: 'inherit' });

    if (result.error) {
        throw result.error;
    }

    process.exit(result.status ?? 1);
} finally {
    rmSync(tempDir, { recursive: true, force: true });
}
