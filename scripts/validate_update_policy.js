'use strict';

const path = require('node:path');
const {
    readUpdatePolicy,
    validateUpdatePolicy,
} = require('./update_policy');
const { readProjectVersion } = require('./project_version');

const rootDir = path.resolve(__dirname, '..');

function argumentValue(argv, name) {
    const index = argv.indexOf(name);
    if (index >= 0) {
        const value = argv[index + 1];
        if (!value || value.startsWith('--')) throw new Error(`${name} 后面必须提供参数值`);
        return value;
    }
    const prefix = `${name}=`;
    const inline = argv.find(value => value.startsWith(prefix));
    if (inline === undefined) return undefined;
    const value = inline.slice(prefix.length);
    if (!value) throw new Error(`${name} 后面必须提供参数值`);
    return value;
}

function main() {
    const argv = process.argv.slice(2);
    const version = argumentValue(argv, '--version') || process.env.UPDATE_VERSION || readProjectVersion();
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
