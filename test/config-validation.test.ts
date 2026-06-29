import { normalizeSecretPrefix, parseDeveloperCidrs } from '../src/config-validation';

describe('config validation helpers', () => {
    it('validates developer CIDR env values before stack construction', () => {
        expect(parseDeveloperCidrs('203.0.113.10/32,198.51.100.0/24', 'TEST_IPV4', 4)).toEqual([
            '203.0.113.10/32',
            '198.51.100.0/24',
        ]);
        expect(parseDeveloperCidrs('2001:db8::10/128', 'TEST_IPV6', 6)).toEqual(['2001:db8::10/128']);
        expect(() => parseDeveloperCidrs('0.0.0.0/0', 'TEST_IPV4', 4)).toThrow(/entire internet/);
        expect(() => parseDeveloperCidrs('::/0', 'TEST_IPV6', 6)).toThrow(/entire internet/);
        expect(() => parseDeveloperCidrs('2001:db8::10/128', 'TEST_IPV4', 4)).toThrow(/invalid address/);
        expect(() => parseDeveloperCidrs('203.0.113.10', 'TEST_IPV4', 4)).toThrow(/CIDR/);
        expect(() => parseDeveloperCidrs('203.0.113.10/24', 'TEST_IPV4', 4)).toThrow(/host bits/);
        expect(() => parseDeveloperCidrs('2001:db8::10/64', 'TEST_IPV6', 6)).toThrow(/host bits/);
    });

    it('normalizes Secrets Manager name prefixes', () => {
        expect(normalizeSecretPrefix('/loop-ad/dev/')).toBe('/loop-ad/dev');
        expect(() => normalizeSecretPrefix('loop-ad/dev')).toThrow(/must start/);
        expect(() => normalizeSecretPrefix('/loop ad/dev')).toThrow(/whitespace/);
        expect(() => normalizeSecretPrefix('arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:loop-ad')).toThrow(/not an ARN/);
    });
});
