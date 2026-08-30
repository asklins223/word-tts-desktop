'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {
    readUpdatePolicy,
    validateUpdatePolicy,
} = require('./update_policy');

const rootDir = path.resolve(__dirname, '..');
const productName = '小猪wordTTS';

function loadYaml() {
    try {
        return require('js-yaml');
    } catch (_) {
        // The Electron package owns the dependency because it is also used by
        // electron-updater at runtime. Root-level release scripts run after
        // `npm ci` in electron/, so use that deterministic copy here too.
        return require(path.join(rootDir, 'electron', 'node_modules', 'js-yaml'));
    }
}

function argumentValue(argv, name) {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : undefined;
}

function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function filesInDirectory(directory) {
    return fs.readdirSync(directory, { withFileTypes: true })
        .filter(entry => entry.isFile())
        .map(entry => entry.name);
}

function chooseArtifact(releaseDir, version, platform, architecture) {
    const names = filesInDirectory(releaseDir);
    if (platform === 'win32') {
        const exact = `${productName}-Setup-${version}-x64.exe`;
        if (!names.includes(exact)) {
            throw new Error(`未找到 Windows 更新安装包，应为 ${exact}`);
        }
        return { name: exact, kind: 'windows' };
    }

    const wantedArchitecture = architecture || process.arch;
    const escapedVersion = escapeRegExp(version);
    const candidates = names.filter(name => new RegExp(
        `^${escapeRegExp(productName)}-${escapedVersion}-(?:arm64|x64|universal)\\.zip$`,
    ).test(name));
    if (candidates.length === 0) {
        throw new Error(`未找到 macOS 自动更新 ZIP，应为 ${productName}-${version}-<arch>.zip`);
    }
    const selected = candidates.find(name => name.includes(`-${wantedArchitecture}.`))
        || candidates.find(name => name.includes('-universal.'));
    if (!selected) {
        throw new Error(`未找到 macOS ${wantedArchitecture} 自动更新 ZIP；已有产物: ${candidates.join(', ')}`);
    }
    return { name: selected, kind: 'mac' };
}

function checksum(filePath) {
    const data = fs.readFileSync(filePath);
    return {
        sha512: crypto.createHash('sha512').update(data).digest('base64'),
        size: data.length,
    };
}

function metadataForArtifact({ version, tag, policy, notes, artifact, releaseDir, releaseDate }) {
    const artifactPath = path.join(releaseDir || path.join(rootDir, 'electron', 'release'), artifact.name);
    const digest = checksum(artifactPath);
    return {
        version,
        files: [{
            url: artifact.name,
            sha512: digest.sha512,
            size: digest.size,
        }],
        // Keep these fields for older electron-updater clients. New clients
        // use `files`, while both formats point to the same checksum.
        path: artifact.name,
        sha512: digest.sha512,
        releaseDate,
        releaseName: `${productName} ${tag || `v${version}`}`,
        updateMode: policy.mode,
        minimumSupportedVersion: policy.minimumSupportedVersion,
        updateMessage: policy.message,
        releaseNotes: notes,
    };
}

function prepareMetadata({
    version,
    tag,
    releaseDir = path.join(rootDir, 'electron', 'release'),
    policyPath = path.join(rootDir, 'release', 'update-policy.json'),
    notesPath = path.join(rootDir, 'release-notes.md'),
    platform = process.platform,
    architecture = process.env.UPDATE_MAC_ARCH || null,
    releaseDate = new Date().toISOString(),
}) {
    const packageJson = JSON.parse(fs.readFileSync(path.join(rootDir, 'electron', 'package.json'), 'utf8'));
    const resolvedVersion = version || packageJson.version;
    const policy = validateUpdatePolicy(readUpdatePolicy(policyPath), {
        version: resolvedVersion,
        tag,
    });
    if (!fs.existsSync(notesPath)) {
        throw new Error(`缺少更新日志 ${notesPath}，请先运行 scripts/extract_release_notes.js`);
    }
    const notes = fs.readFileSync(notesPath, 'utf8').trim();
    if (!notes) throw new Error('release-notes.md 不能为空');
    if (!fs.existsSync(releaseDir)) throw new Error(`产物目录不存在: ${releaseDir}`);

    const artifact = chooseArtifact(releaseDir, policy.version, platform, architecture);
    const metadata = metadataForArtifact({
        version: policy.version,
        tag,
        policy,
        notes,
        artifact,
        releaseDir,
        releaseDate,
    });
    const digest = checksum(path.join(releaseDir, artifact.name));
    const filename = artifact.kind === 'windows' ? 'latest.yml' : 'latest-mac.yml';
    // Explicitly preserve a stable key order so reviewers can diff release
    // metadata and spot a wrong artifact/checksum without YAML noise.
    const yaml = loadYaml().dump(metadata, {
        noRefs: true,
        lineWidth: -1,
        sortKeys: false,
    });
    fs.writeFileSync(path.join(releaseDir, filename), yaml, 'utf8');
    return {
        filename,
        artifact: artifact.name,
        checksum: digest.sha512,
        size: digest.size,
        policy,
    };
}

function main() {
    const argv = process.argv.slice(2);
    const packageJson = JSON.parse(fs.readFileSync(path.join(rootDir, 'electron', 'package.json'), 'utf8'));
    const version = argumentValue(argv, '--version') || process.env.UPDATE_VERSION || packageJson.version;
    const tag = argumentValue(argv, '--tag') || process.env.RELEASE_TAG || null;
    if (argv.includes('--validate-only')) {
        const policy = validateUpdatePolicy(readUpdatePolicy(argumentValue(argv, '--policy') || path.join(rootDir, 'release/update-policy.json')), { version, tag });
        console.log(`更新策略校验通过: v${policy.version}`);
        return;
    }
    const platform = argumentValue(argv, '--platform') || process.platform;
    const result = prepareMetadata({
        version,
        tag,
        platform,
        architecture: argumentValue(argv, '--arch') || process.env.UPDATE_MAC_ARCH || null,
        releaseDir: argumentValue(argv, '--release-dir') || path.join(rootDir, 'electron/release'),
        notesPath: argumentValue(argv, '--notes') || path.join(rootDir, 'release-notes.md'),
        policyPath: argumentValue(argv, '--policy') || path.join(rootDir, 'release/update-policy.json'),
    });
    console.log(`已生成 ${result.filename}: ${result.artifact} · ${result.size} bytes`);
}

if (require.main === module) {
    try {
        main();
    } catch (error) {
        console.error(`更新元数据生成失败: ${error.message}`);
        process.exitCode = 1;
    }
}

module.exports = {
    chooseArtifact,
    checksum,
    metadataForArtifact,
    prepareMetadata,
};
