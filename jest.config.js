module.exports = {
    testEnvironment: 'node',
    roots: ['<rootDir>/test'],
    testMatch: ['**/*.test.ts'],
    transform: {
        '^.+\\.tsx?$': 'ts-jest'
    },
    setupFiles: ['<rootDir>/test/setup-jsii-deprecations.ts'],
    setupFilesAfterEnv: ['aws-cdk-lib/testhelpers/jest-autoclean'],
};
