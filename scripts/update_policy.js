'use strict';

const fs = require('node:fs');
const path = require('node:path');

const rootDir = path.resolve(__dirname, '..');

const NUMERIC_IDENTIFIER = '(?:0|[1-9]\\d*)';
const NON_NUMERIC_IDENTIFIER = '(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)';
const PRERELEASE_IDENTIFIER = `(?:${NUMERIC_IDENTIFIER}|${NON_NUMERIC_IDENTIFIER})`;
const BUILD_IDENTIFIER = '[0-9A-Za-z-]+';
const VERSION_PATTERN = new RegExp(
    `^[vV]?(${NUMERIC_IDENTIFIER})\\.(${NUMERIC_IDENTIFIER})\\.(${NUMERIC_IDENTIFIER})`
    + `(?:-(${PRERELEASE_IDENTIFIER}(?:\\.${PRERELEASE_IDENTIFIER})*))?`
    + `(?:\\+${BUILD_IDENTIFIER}(?:\\.${BUILD_IDENTIFIER})*)?$`,
);

function parseVersion(value) {
    const match = String(value || '').trim().match(VERSION_PATTERN);
    if (!match) return null;
    return {
        major: Number(match[1]),
        minor: Number(match[2]),
        patch: Number(match[3]),
        majorText: match[1],
        minorText: match[2],
        patchText: match[3],
        prerelease: match[4] ? match[4].split('.') : [],
    };
}

function normalizeVersion(value, label = '版本号') {
    const text = String(value || '').trim();
    if (!parseVersion(text)) throw new Error(`${label}不是合法 SemVer: ${text || '(空)'}`);
    return text.replace(/^v/i, '');
}

function compareVersions(left, right) {
    const a = parseVersion(left);
    const b = parseVersion(right);
    if (!a || !b) throw new Error(`无法比较版本号: ${left} / ${right}`);
    for (const key of ['major', 'minor', 'patch']) {
        const result = compareNumericIdentifiers(a[`${key}Text`], b[`${key}Text`]);
        if (result !== 0) return result;
    }
    if (a.prerelease.length === 0 && b.prerelease.length === 0) return 0;
    if (a.prerelease.length === 0) return 1;
    if (b.prerelease.length === 0) return -1;
    const length = Math.max(a.prerelease.length, b.prerelease.length);
    for (let index = 0; index < length; index += 1) {
        if (index >= a.prerelease.length) return -1;
        if (index >= b.prerelease.length) return 1;
        const leftPart = a.prerelease[index];
        const rightPart = b.prerelease[index];
        if (leftPart === rightPart) continue;
        const leftNumeric = /^\d+$/.test(leftPart);
        const rightNumeric = /^\d+$/.test(rightPart);
        if (leftNumeric && rightNumeric) return compareNumericIdentifiers(leftPart, rightPart);
        if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
        return leftPart > rightPart ? 1 : -1;
    }
    return 0;
}

function compareNumericIdentifiers(left, right) {
    const leftText = String(left);
    const rightText = String(right);
    if (leftText.length !== rightText.length) return leftText.length > rightText.length ? 1 : -1;
    if (leftText === rightText) return 0;
    return leftText > rightText ? 1 : -1;
}

function validateUpdatePolicy(policy, { version, tag } = {}) {
    if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
        throw new Error('更新策略必须是 JSON 对象');
    }
    const requiredKeys = ['mode', 'minimumSupportedVersion', 'message'];
    const allowedKeys = new Set(['$schema', ...requiredKeys]);
    const missingKeys = requiredKeys.filter(key => !Object.prototype.hasOwnProperty.call(policy, key));
    if (missingKeys.length > 0) {
        throw new Error(`更新策略缺少字段: ${missingKeys.join(', ')}`);
    }
    const unknownKeys = Object.keys(policy).filter(key => !allowedKeys.has(key));
    if (unknownKeys.length > 0) {
        throw new Error(`更新策略包含未知字段: ${unknownKeys.join(', ')}`);
    }
    if (policy.$schema !== undefined && typeof policy.$schema !== 'string') {
        throw new Error('更新策略 $schema 必须是字符串');
    }
    if (typeof policy.message !== 'string') {
        throw new Error('更新策略 message 必须是字符串');
    }
    if (policy.minimumSupportedVersion !== null && typeof policy.minimumSupportedVersion !== 'string') {
        throw new Error('minimumSupportedVersion 必须是字符串或 null');
    }
    const policyVersion = version == null ? null : normalizeVersion(version, '构建版本');
    if (!policyVersion) {
        throw new Error('校验更新策略时必须提供构建版本；版本统一从 version.json 读取');
    }
    if (tag) {
        const releaseTag = String(tag).trim();
        if (!releaseTag.startsWith('v') || !parseVersion(releaseTag)) {
            throw new Error(`Release tag 必须是 v<version> 格式: ${releaseTag || '(空)'}`);
        }
        const normalizedTag = normalizeVersion(releaseTag, 'Release tag');
        if (normalizedTag !== policyVersion) {
            throw new Error(`Release tag=${normalizedTag} 与构建版本=${policyVersion} 不一致`);
        }
    }

    const mode = policy.mode;
    if (mode !== 'optional' && mode !== 'force') {
        throw new Error(`更新策略 mode 必须是 optional 或 force，当前为: ${String(mode)}`);
    }

    let minimumSupportedVersion = null;
    if (policy.minimumSupportedVersion !== null) {
        minimumSupportedVersion = normalizeVersion(policy.minimumSupportedVersion, 'minimumSupportedVersion');
        if (compareVersions(minimumSupportedVersion, policyVersion) > 0) {
            throw new Error(`minimumSupportedVersion=${minimumSupportedVersion} 不能高于发布版本=${policyVersion}`);
        }
    }

    const message = String(policy.message || '').trim();
    if (!message) throw new Error('更新策略 message 不能为空');
    if (message.length > 500) throw new Error('更新策略 message 不能超过 500 个字符');

    return {
        version: policyVersion,
        mode,
        minimumSupportedVersion,
        message,
    };
}

function readUpdatePolicy(policyPath) {
    const resolvedPath = policyPath || path.join(rootDir, 'release', 'update-policy.json');
    return JSON.parse(fs.readFileSync(resolvedPath, 'utf8'));
}

module.exports = {
    compareVersions,
    normalizeVersion,
    parseVersion,
    readUpdatePolicy,
    validateUpdatePolicy,
};
