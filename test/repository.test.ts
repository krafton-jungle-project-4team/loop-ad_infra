import { Template } from 'aws-cdk-lib/assertions';
import { synthCertificate, synthRepositories, synthSecrets, testPublicHostedZone, testSecretNames } from './helpers';

describe('repository and certificate stacks', () => {
    it('keeps ECR repositories limited to the three runtime services', () => {
        const template = Template.fromStack(synthRepositories());

        template.resourceCountIs('AWS::ECR::Repository', 3);
        for (const repositoryName of ['loop-ad/event-collector', 'loop-ad/dashboard-api', 'loop-ad/decision-api']) {
            template.hasResourceProperties('AWS::ECR::Repository', {
                RepositoryName: repositoryName,
                ImageScanningConfiguration: {
                    ScanOnPush: true,
                },
            });
        }
        expect(JSON.stringify(template.toJSON())).not.toContain('advertisement-api');
    });

    it('keeps CloudFront certificates in the certificate stack', () => {
        const template = Template.fromStack(synthCertificate());

        template.resourceCountIs('AWS::CertificateManager::Certificate', 2);
        template.hasResourceProperties('AWS::CertificateManager::Certificate', {
            DomainName: `dashboard.dev.${testPublicHostedZone.domainName}`,
            SubjectAlternativeNames: [`demo-shoppingmall.dev.${testPublicHostedZone.domainName}`],
        });
        template.hasResourceProperties('AWS::CertificateManager::Certificate', {
            DomainName: `gen-ai.asset.dev.${testPublicHostedZone.domainName}`,
        });
    });

    it('manages dev Secrets Manager resources without embedding secret values', () => {
        const template = Template.fromStack(synthSecrets());

        template.resourceCountIs('AWS::SecretsManager::Secret', 8);
        for (const secretName of Object.values(testSecretNames)) {
            template.hasResourceProperties('AWS::SecretsManager::Secret', {
                Name: secretName,
            });
        }

        const templateText = JSON.stringify(template.toJSON());
        expect(templateText).not.toContain('SecretString');
        expect(templateText).not.toContain('GenerateSecretString');
        expect(templateText).not.toContain('password');
        expect(templateText).not.toContain('api_key');
    });
});
