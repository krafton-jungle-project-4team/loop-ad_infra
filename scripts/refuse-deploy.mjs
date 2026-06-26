#!/usr/bin/env node

console.error('Generic deploy/destroy is intentionally blocked.');
console.error('Use deploy:dev-certificate, deploy:dev-repositories, deploy:dev-network, or deploy:dev so the target lifecycle is explicit.');
process.exit(1);
