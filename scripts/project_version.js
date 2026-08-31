'use strict';

/**
 * The only hand-maintained application release version.
 *
 * electron-builder and npm still need a version in electron/package.json and
 * electron/package-lock.json. Those files are generated metadata and are
 * synchronized from version.json by this module before every build.
 */

const fs = require('node:fs');
const path = require('node:path');

const rootDir = path.resolve(__dirname, '..');
const versionFile = path.join(rootDir, 'version.json');
const electronPackageFile = path.join(rootDir, 'electron', 'package.json');
const electronLockFile = path.join(rootDir, 'electron', 'package-lock.json');
const NUMERIC_IDENTIFIER = '(?:0|[1-9]\\d*)';
const NON_NUMERIC_IDENTIFIER = '(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)';
const PRERELEASE_IDENTIFIER = `(?:${NUMERIC_IDENTIFIER}|${NON_NUMERIC_IDENTIFIER})`;
const BUILD_IDENTIFIER = '[0-9A-Za-z-]+';
const VERSION_PATTERN = new RegExp(
    `^${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}`
    + `(?:-${PRERELEASE_IDENTIFIER}(?:\\.${PRERELEASE_IDENTIFIER})*)?`
    + `(?:\\+${BUILD_IDENTIFIER}(?:\\.${BUILD_IDENTIFIER})*)?$`,
);

function normalizeProjectVersion(value, label = '项目版本') {
    const version = String(value ?? '').trim().replace(/^v/i, '');
    if (!VERSION_PATTERN.test(version)) {
        throw new Error(`${label}不是合法 SemVer: ${version || '(空)'}`);
    }
    return version;
}

function readJson(filePath, label) {
    try {
        return JSON.parse(fs.readFileSync(filePath, 'utf8'));
    } catch (error) {
        throw new Error(`${label}读取失败: ${error.message}`);
    }
}

function readProjectVersion(filePath = versionFile) {
    const source = readJson(filePath, 'version.json');
    const rawVersion = typeof source === 'string' ? source : source?.version;
    return normalizeProjectVersion(rawVersion);
}

function writeJson(filePath, value) {
    fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function writeProjectVersion(version, filePath = versionFile) {
    const normalized = normalizeProjectVersion(version);
    writeJson(filePath, { version: normalized });
    return normalized;
}

function syncProjectVersion({ version = readProjectVersion(), packagePath = electronPackageFile, lockPath = electronLockFile } = {}) {
    const normalized = normalizeProjectVersion(version);
    const packageJson = readJson(packagePath, 'electron/package.json');
    packageJson.version = normalized;
    writeJson(packagePath, packageJson);

    if (fs.existsSync(lockPath)) {
        const lockJson = readJson(lockPath, 'electron/package-lock.json');
        lockJson.version = normalized;
        if (lockJson.packages && lockJson.packages['']) {
            lockJson.packages[''].version = normalized;
        }
        writeJson(lockPath, lockJson);
    }
    return normalized;
}

function argumentValue(argv, name) {
    const index = argv.indexOf(name);
    if (index >= 0) {
        const value = argv[index + 1];
        if (!value || value.startsWith('--')) {
            throw new Error(`${name} 后面必须提供版本号`);
        }
        return value;
    }
    const prefix = `${name}=`;
    const inline = argv.find(value => value.startsWith(prefix));
    if (inline === undefined) return undefined;
    const value = inline.slice(prefix.length);
    if (!value) throw new Error(`${name} 后面必须提供版本号`);
    return value;
}

function main(argv = process.argv.slice(2)) {
    const requested = argumentValue(argv, '--set');
    if (requested !== undefined) {
        const version = writeProjectVersion(requested);
        syncProjectVersion({ version });
        console.log(`项目版本已统一为 ${version}`);
        return;
    }
    if (argv.includes('--sync')) {
        const version = syncProjectVersion();
        console.log(`项目版本同步完成: ${version}`);
        return;
    }
    console.log(readProjectVersion());
}

if (require.main === module) {
    try {
        main();
    } catch (error) {
        console.error(`项目版本处理失败: ${error.message}`);
        process.exitCode = 1;
    }
}

module.exports = {
    ELECTRON_LOCK_FILE: electronLockFile,
    ELECTRON_PACKAGE_FILE: electronPackageFile,
    PROJECT_VERSION_FILE: versionFile,
    normalizeProjectVersion,
    readProjectVersion,
    syncProjectVersion,
    writeProjectVersion,
};
