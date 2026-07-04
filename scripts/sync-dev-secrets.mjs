#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { parse as parseDotenv } from 'dotenv';

// Secret 리소스는 CDK가 소유하고, 이 스크립트는 로컬 구조 검증과 기존 값 업데이트만 담당합니다.
const options = parseArgs(process.argv.slice(2));
const envFilePath = resolve(options.envFile);
const envValues = readEnvFile(envFilePath);
const region = options.region ?? requiredValue(envValues, 'LOOP_AD_REGION');
const secretPrefix = normalizeSecretPrefix(requiredValue(envValues, 'LOOP_AD_SECRET_PREFIX'));

// CDK Secrets stack이 만든 이름 규칙과 같은 suffix를 사용합니다.
// 스크립트는 값을 생성하지 않고 .env.secrets의 현재 값을 기존 secret에 덮어쓰는 역할만 합니다.
const secretSpecs = [
    {
        name: `${secretPrefix}/aurora/credentials`,
        value: credentialsValue(envValues, 'LOOP_AD_AURORA_USERNAME', 'LOOP_AD_AURORA_PASSWORD'),
    },
    {
        name: `${secretPrefix}/clickhouse/credentials`,
        value: credentialsValue(envValues, 'LOOP_AD_CLICKHOUSE_USERNAME', 'LOOP_AD_CLICKHOUSE_PASSWORD'),
    },
    {
        name: `${secretPrefix}/kafka/app-user`,
        value: credentialsValue(envValues, 'LOOP_AD_KAFKA_APP_USERNAME', 'LOOP_AD_KAFKA_APP_PASSWORD'),
    },
    {
        name: `${secretPrefix}/kafka/broker-user`,
        value: credentialsValue(envValues, 'LOOP_AD_KAFKA_BROKER_USERNAME', 'LOOP_AD_KAFKA_BROKER_PASSWORD'),
    },
    {
        name: `${secretPrefix}/openai/api-key`,
        value: apiKeyValue(envValues, 'LOOP_AD_OPENAI_API_KEY'),
    },
    {
        name: `${secretPrefix}/gemini/api-key`,
        value: apiKeyValue(envValues, 'LOOP_AD_GEMINI_API_KEY'),
    },
    {
        name: `${secretPrefix}/internal/api-key`,
        value: apiKeyValue(envValues, 'LOOP_AD_INTERNAL_API_KEY'),
    },
    {
        name: `${secretPrefix}/dashboard-api/demo-dispatch-recipients`,
        value: demoDispatchRecipientsValue(envValues),
    },
];

if (options.dryRun) {
    // dry-run은 값 모양과 필수 키 존재 여부까지만 검증합니다.
    // AWS에 쓰기 전에 로컬 파일이 배포 계약을 만족하는지 확인하는 용도입니다.
    for (const spec of secretSpecs) {
        console.log(`[dry-run] validated ${spec.name}`);
    }
    process.exit(0);
}

for (const spec of secretSpecs) {
    syncSecret(spec, region);
}

function syncSecret(spec, regionName) {
    // Secrets 스택이 아직 리소스를 만들지 않았다면 바로 실패합니다.
    const existingSecret = runAws([
        'secretsmanager',
        'describe-secret',
        '--secret-id',
        spec.name,
        '--region',
        regionName,
    ], { allowMissingSecret: true });

    if (existingSecret.missing) {
        throw new Error(`${spec.name} does not exist. Deploy LoopAdDevSecretsStack before syncing secret values.`);
    }

    let tempDir;
    try {
        tempDir = mkdtempSync(join(tmpdir(), 'loop-ad-secrets-'));
        const secretStringPath = join(tempDir, 'secret.json');
        // JSON 값이 프로세스 인자 목록에 노출되지 않도록 file:// 입력을 사용합니다.
        writeFileSync(secretStringPath, `${JSON.stringify(spec.value)}\n`, { mode: 0o600 });

        runAws([
            'secretsmanager',
            'put-secret-value',
            '--secret-id',
            spec.name,
            '--secret-string',
            `file://${secretStringPath}`,
            '--region',
            regionName,
        ]);
        console.log(`[updated] ${spec.name}`);
    } finally {
        if (tempDir) {
            rmSync(tempDir, { recursive: true, force: true });
        }
    }
}

function runAws(args, options = {}) {
    // AWS CLI를 직접 호출해 SDK credential 로직을 중복 구현하지 않습니다.
    // 실패 메시지는 어떤 AWS 명령 계열에서 실패했는지만 남기고 secret payload는 출력하지 않습니다.
    const result = spawnSync('aws', args, {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    if (result.status === 0) {
        return { missing: false };
    }

    const stderr = result.stderr ?? '';
    if (options.allowMissingSecret && stderr.includes('ResourceNotFoundException')) {
        return { missing: true };
    }

    const command = ['aws', ...args.slice(0, 2), '...'].join(' ');
    throw new Error(`${command} failed: ${stderr.trim() || `exit ${result.status}`}`);
}

function readEnvFile(path) {
    // .env.secrets는 실제 secret 값을 담으므로 CDK synth 경로에서 자동 로드하지 않습니다.
    // 사용자가 명시적으로 --env-file을 넘겼을 때만 읽어 업데이트 의도를 분명히 합니다.
    if (!existsSync(path)) {
        throw new Error(`Secret env file not found: ${path}`);
    }

    return parseDotenv(readFileSync(path, 'utf8'));
}

function credentialsValue(values, usernameKey, passwordKey) {
    // username/password 계열 secret은 앱과 인프라가 같은 JSON shape을 기대합니다.
    // shape을 여기서 고정해 앱별로 다른 키 이름이 퍼지지 않게 합니다.
    return {
        username: requiredValue(values, usernameKey),
        password: requiredValue(values, passwordKey),
    };
}

function apiKeyValue(values, apiKeyName) {
    // API key 계열 secret은 { api_key } 하나로 통일합니다.
    // ECS secret injection에서 JSON field 이름을 서비스마다 반복해도 값 구조는 같게 유지됩니다.
    return {
        api_key: requiredValue(values, apiKeyName),
    };
}

function demoDispatchRecipientsValue(values) {
    const source = requiredValue(values, 'LOOP_AD_DEMO_DISPATCH_RECIPIENTS');
    let parsed;

    try {
        parsed = JSON.parse(source);
    } catch {
        throw new Error('LOOP_AD_DEMO_DISPATCH_RECIPIENTS must be a valid JSON array.');
    }

    if (!Array.isArray(parsed)) {
        throw new Error('LOOP_AD_DEMO_DISPATCH_RECIPIENTS must be a JSON array.');
    }

    const seenUserIds = new Set();
    for (const [index, recipient] of parsed.entries()) {
        if (!recipient || typeof recipient !== 'object' || Array.isArray(recipient)) {
            throw new Error(`LOOP_AD_DEMO_DISPATCH_RECIPIENTS[${index}] must be an object.`);
        }

        const userId = requiredRecipientString(recipient, index, 'userId');
        const email = requiredRecipientString(recipient, index, 'email');
        const phoneNumber = requiredRecipientString(recipient, index, 'phoneNumber');

        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            throw new Error(`LOOP_AD_DEMO_DISPATCH_RECIPIENTS[${index}].email must be a valid email address.`);
        }

        if (!/^\+[1-9]\d{1,14}$/.test(phoneNumber)) {
            throw new Error(`LOOP_AD_DEMO_DISPATCH_RECIPIENTS[${index}].phoneNumber must be an E.164 phone number.`);
        }

        if (seenUserIds.has(userId)) {
            throw new Error(`LOOP_AD_DEMO_DISPATCH_RECIPIENTS has duplicated userId '${userId}'.`);
        }

        seenUserIds.add(userId);
    }

    return parsed;
}

function requiredRecipientString(recipient, index, key) {
    const value = typeof recipient[key] === 'string' ? recipient[key].trim() : '';
    if (!value) {
        throw new Error(`LOOP_AD_DEMO_DISPATCH_RECIPIENTS[${index}].${key} is required.`);
    }

    return value;
}

function requiredValue(values, key) {
    const value = values[key]?.trim();
    if (!value) {
        throw new Error(`${key} is required in the secret env file.`);
    }

    return value;
}

function normalizeSecretPrefix(secretPrefix) {
    // secret prefix는 CDK의 secret name 계약과 맞아야 하므로 CDK helper와 같은 규칙으로 검증합니다.
    // ARN을 받지 않는 이유는 이 스크립트가 prefix 아래의 여러 secret 이름을 조립해야 하기 때문입니다.
    const value = secretPrefix.trim().replace(/\/+$/g, '');
    if (!value) {
        throw new Error('LOOP_AD_SECRET_PREFIX must not be empty.');
    }

    if (value.includes(':')) {
        throw new Error('LOOP_AD_SECRET_PREFIX must be a Secrets Manager name prefix, not an ARN.');
    }

    if (!value.startsWith('/')) {
        throw new Error('LOOP_AD_SECRET_PREFIX must start with "/". Example: /loop-ad/dev');
    }

    if (/\s/.test(value)) {
        throw new Error('LOOP_AD_SECRET_PREFIX must not contain whitespace.');
    }

    return value;
}

function parseArgs(args) {
    // 배포 대상 파일을 암묵적으로 고르지 않기 위해 --env-file은 필수입니다.
    // region은 파일의 LOOP_AD_REGION을 기본으로 쓰되, 운영자가 일회성으로 명시 override할 수 있습니다.
    const parsed = {
        dryRun: false,
    };

    for (let index = 0; index < args.length; index += 1) {
        const arg = args[index];
        if (arg === '--dry-run') {
            parsed.dryRun = true;
            continue;
        }

        if (arg === '--env-file') {
            parsed.envFile = readArgValue(args, index, arg);
            index += 1;
            continue;
        }

        if (arg === '--region') {
            parsed.region = readArgValue(args, index, arg);
            index += 1;
            continue;
        }

        throw new Error(`Unknown argument: ${arg}`);
    }

    if (!parsed.envFile) {
        throw new Error('--env-file requires an explicit path.');
    }

    return parsed;
}

function readArgValue(args, index, optionName) {
    const value = args[index + 1]?.trim();
    if (!value) {
        throw new Error(`${optionName} requires a value.`);
    }

    return value;
}
