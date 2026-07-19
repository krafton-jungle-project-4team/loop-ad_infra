import ipaddr from 'ipaddr.js';

// Secrets Manager secret 이름은 환경별 prefix와 코드에서 고정한 suffix를 합쳐 만듭니다.
// 여기서는 사용자가 ARN이나 파일 경로를 넣는 실수를 배포 전에 잡기 위해 prefix 모양만 검증합니다.
export function normalizeSecretPrefix(secretPrefix: string): string {
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

// 개발자 직접 접근 허용 목록은 env에서 문자열로 들어오지만, Security Group에는 정규화된 CIDR만 넘깁니다.
// 빈 문자열은 "직접 접근 없음"이라는 명시 설정으로 보고 빈 배열로 바꿉니다.
export function parseDeveloperCidrs(rawValue: string, envName: string, ipVersion: 4 | 6): string[] {
    const values = rawValue
        .split(/[,\s]+/)
        .map((value) => value.trim())
        .filter((value) => value.length > 0);

    return values.map((value) => validateCidr(value, envName, ipVersion));
}

// CIDR 검증은 ipaddr.js에 위임해 IPv4/IPv6 파싱 예외를 직접 구현하지 않습니다.
// 0-prefix와 host bit가 있는 값은 의도보다 넓은 접근을 열 수 있으므로 synth 전에 실패시킵니다.
function validateCidr(value: string, envName: string, ipVersion: 4 | 6): string {
    let address: ipaddr.IPv4 | ipaddr.IPv6;
    let prefix: number;
    try {
        [address, prefix] = ipaddr.parseCIDR(value);
    } catch {
        throw new Error(`${envName} must contain CIDR values. Invalid entry: ${value}`);
    }

    if (address.kind() !== (ipVersion === 4 ? 'ipv4' : 'ipv6')) {
        throw new Error(`${envName} contains an IPv${ipVersion === 4 ? 6 : 4} or invalid address: ${value}`);
    }

    if (prefix === 0) {
        throw new Error(`${envName} must not allow the entire internet: ${value}`);
    }

    const networkAddress = ipVersion === 4
        ? ipaddr.IPv4.networkAddressFromCIDR(value)
        : ipaddr.IPv6.networkAddressFromCIDR(value);
    if (address.toNormalizedString() !== networkAddress.toNormalizedString()) {
        throw new Error(`${envName} must contain network CIDR values without host bits. Invalid entry: ${value}`);
    }

    return `${networkAddress.toString()}/${prefix}`;
}
