'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {
    readUpdatePolicy,
    validateUpdatePolicy,
} = require('./update_policy');
const { readProjectVersion } = require('./project_version');

const rootDir = path.resolve(__dirname, '..');
const productName = '小猪wordTTS';
const githubAssetPrefix = 'wordTTS';

// The release workflow uploads a canonical ASCII asset name even though the
// local builder keeps the Chinese product name. The updater follows the URL
// from latest*.yml, so metadata must use that release asset name rather than
// the local build filename. Windows uses latest-win.json; macOS keeps the
// latest-mac.yml contract used by electron-updater.
function githubAssetName(name) {
    const localName = String(name);
    const productPrefix = `${productName}-`;
    return localName.startsWith(productPrefix)
        ? `${githubAssetPrefix}-${localName.slice(productPrefix.length)}`
        : localName;
}

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
    const assetName = githubAssetName(artifact.name);
    return {
        version,
        files: [{
            url: assetName,
            sha512: digest.sha512,
            size: digest.size,
        }],
        // Keep the shared fields stable: macOS electron-updater consumes the
        // legacy top-level path, while Windows also exposes this shape inside
        // latest-win.json for the custom client and release diagnostics.
        path: assetName,
        sha512: digest.sha512,
        releaseDate,
        releaseName: `${productName} ${tag || `v${version}`}`,
        updateMode: policy.mode,
        minimumSupportedVersion: policy.minimumSupportedVersion,
        updateMessage: policy.message,
        releaseNotes: notes,
    };
}

function customWindowsMetadataForArtifact({ version, tag, policy, notes, artifact, releaseDir, releaseDate }) {
    const metadata = metadataForArtifact({
        version,
        tag,
        policy,
        notes,
        artifact,
        releaseDir,
        releaseDate,
    });
    const file = metadata.files[0];
    const blockmapName = `${artifact.name}.blockmap`;
    const blockmapPath = path.join(releaseDir, blockmapName);
    const blockmap = fs.existsSync(blockmapPath)
        ? {
            url: githubAssetName(blockmapName),
            ...checksum(blockmapPath),
        }
        : null;
    return {
        schemaVersion: 1,
        platform: 'win32',
        version: metadata.version,
        tag: tag || `v${metadata.version}`,
        artifact: {
            url: file.url,
            sha512: file.sha512,
            size: file.size,
            ...(blockmap ? { blockmap: blockmap.url } : {}),
        },
        ...(blockmap ? { blockmap } : {}),
        ...metadata,
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
    const resolvedVersion = version || readProjectVersion();
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
    let filename;
    if (artifact.kind === 'windows') {
        // Windows does not consume electron-updater metadata. It downloads
        // this self-contained Setup.exe and lets that program perform the
        // visible update. JSON keeps that contract explicit.
        filename = 'latest-win.json';
        const customMetadata = customWindowsMetadataForArtifact({
            version: policy.version,
            tag,
            policy,
            notes,
            artifact,
            releaseDir,
            releaseDate,
        });
        fs.writeFileSync(path.join(releaseDir, filename), `${JSON.stringify(customMetadata, null, 2)}\n`, 'utf8');
    } else {
        filename = 'latest-mac.yml';
        // Explicitly preserve a stable key order so reviewers can diff release
        // metadata and spot a wrong artifact/checksum without YAML noise.
        const yaml = loadYaml().dump(metadata, {
            noRefs: true,
            lineWidth: -1,
            sortKeys: false,
        });
        fs.writeFileSync(path.join(releaseDir, filename), yaml, 'utf8');
    }
    return {
        filename,
        artifact: artifact.name,
        asset: githubAssetName(artifact.name),
        checksum: digest.sha512,
        size: digest.size,
        policy,
    };
}

function main() {
    const argv = process.argv.slice(2);
    const version = argumentValue(argv, '--version') || process.env.UPDATE_VERSION || readProjectVersion();
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
    console.log(`已生成 ${result.filename}: ${result.artifact} -> ${result.asset} · ${result.size} bytes`);
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
    githubAssetName,
    customWindowsMetadataForArtifact,
    metadataForArtifact,
    prepareMetadata,
};
