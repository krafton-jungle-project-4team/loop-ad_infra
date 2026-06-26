const originalWarn = console.warn;

console.warn = (...args: unknown[]) => {
    const message = args.map((arg) => String(arg)).join(' ');
    if (message.includes('aws-cdk-lib.aws_certificatemanager.DnsValidatedCertificate is deprecated')) {
        return;
    }

    originalWarn(...args);
};
