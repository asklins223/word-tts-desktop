'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {
    readUpdatePolicy,
    validateUpdatePolicy,
} = require('./update_policy');

const rootDir = path.resolve(__dirname, '..');

function argumentValue(argv, name) {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : undefined;
}

function main() {
    const argv = process.argv.slice(2);
    const packageJson = JSON.parse(fs.readFileSync(path.join(rootDir, 'electron/package.json'), 'utf8'));
    const version = argumentValue(argv, '--version') || process.env.UPDATE_VERSION || packageJson.version;
    const tag = argumentValue(argv, '--tag') || process.env.RELEASE_TAG || null;
    const policyPath = argumentValue(argv, '--policy') || path.join(rootDir, 'release/update-policy.json');
    const policy = validateUpdatePolicy(readUpdatePolicy(policyPath), { version, tag });
    console.log(`更新策略校验通过: v${policy.version} · ${policy.mode === 'force' ? '强制更新' : '可选更新'}`);
    if (policy.minimumSupportedVersion) {
        console.log(`最低支持版本: v${policy.minimumSupportedVersion}`);
    }
}

try {
    main();
} catch (error) {
    console.error(`更新策略校验失败: ${error.message}`);
    process.exitCode = 1;
}
