import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { ROOT } from './helpers';

describe('secret sync script', () => {
    it('validates the git-safe example env file without printing secret values', () => {
        const result = spawnSync(process.execPath, [
            join(ROOT, 'scripts/sync-dev-secrets.mjs'),
            '--env-file',
            join(ROOT, '.env.secrets.example'),
            '--dry-run',
        ], {
            encoding: 'utf8',
        });

        expect(result.status).toBe(0);
        expect(result.stdout).toContain('/loop-ad/dev/aurora/credentials');
        expect(result.stdout).toContain('/loop-ad/dev/gemini/api-key');
        expect(result.stdout).toContain('/loop-ad/dev/internal/api-key');
        expect(result.stdout).not.toContain('replace-me');
        expect(result.stderr).toBe('');
    });

    it('requires the env file path to be explicit', () => {
        const result = spawnSync(process.execPath, [
            join(ROOT, 'scripts/sync-dev-secrets.mjs'),
            '--dry-run',
        ], {
            encoding: 'utf8',
        });

        expect(result.status).not.toBe(0);
        expect(result.stderr).toContain('--env-file requires an explicit path.');
    });
});
