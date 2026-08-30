'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
    compareVersions,
    readUpdatePolicy,
    validateUpdatePolicy,
} = require('../../scripts/update_policy');
const { chooseArtifact, prepareMetadata } = require('../../scripts/prepare_update_metadata');

test('发布前更新策略校验会锁定构建版本与 tag', () => {
    const policy = validateUpdatePolicy({
        version: '2.7.46',
        mode: 'force',
        minimumSupportedVersion: '2.7.40',
        message: '请先更新到最新版本',
    }, { version: '2.7.46', tag: 'v2.7.46' });
    assert.deepEqual(policy, {
        version: '2.7.46',
        mode: 'force',
        minimumSupportedVersion: '2.7.40',
        message: '请先更新到最新版本',
    });
});

test('读取默认更新策略不依赖调用时的工作目录', () => {
    assert.equal(readUpdatePolicy().version, '2.7.45');
});

test('发布前更新策略校验拒绝版本错配和无效规则', () => {
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.45', mode: 'optional', minimumSupportedVersion: null, message: 'ok' }, { version: '2.7.46' }),
        /不一致/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.46', mode: 'optional', minimumSupportedVersion: '2.8.0', message: 'ok' }, { version: '2.7.46' }),
        /不能高于/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.46', mode: 'later', minimumSupportedVersion: null, message: 'ok' }, { version: '2.7.46' }),
        /optional 或 force/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.46', mode: 'optional', message: 'ok' }, { version: '2.7.46' }),
        /缺少字段.*minimumSupportedVersion/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.46', mode: 'optional', minimumSupportedVersion: null, message: 'ok', rollout: 50 }, { version: '2.7.46' }),
        /未知字段.*rollout/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '2.7.46', mode: 'optional', minimumSupportedVersion: null, message: 'ok' }, { version: '2.7.46', tag: '2.7.46' }),
        /Release tag 必须是 v<version> 格式/,
    );
    assert.throws(
        () => validateUpdatePolicy({ version: '02.7.46', mode: 'optional', minimumSupportedVersion: null, message: 'ok' }),
        /不是合法 SemVer/,
    );
});

test('发布版本比较同样处理预发布版本', () => {
    assert.equal(compareVersions('2.7.46-rc.1', '2.7.46'), -1);
    assert.equal(compareVersions('9007199254740992.0.0', '9007199254740993.0.0'), -1);
    assert.equal(compareVersions('1.0.0-9007199254740992', '1.0.0-9007199254740993'), -1);
});

test('发布元数据从实际 Windows 安装包计算大小和 SHA-512', () => {
    const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-update-metadata-'));
    const releaseDir = path.join(tempRoot, 'release');
    const policyPath = path.join(tempRoot, 'update-policy.json');
    const notesPath = path.join(tempRoot, 'release-notes.md');
    const artifactName = '小猪wordTTS-Setup-2.7.45-x64.exe';
    const artifactBytes = Buffer.from('test installer payload', 'utf8');

    try {
        fs.mkdirSync(releaseDir);
        fs.writeFileSync(path.join(releaseDir, artifactName), artifactBytes);
        fs.writeFileSync(policyPath, JSON.stringify({
            version: '2.7.45',
            mode: 'optional',
            minimumSupportedVersion: null,
            message: '可选更新',
        }));
        fs.writeFileSync(notesPath, '## v2.7.45\n\n修复更新流程。\n');

        const result = prepareMetadata({
            version: '2.7.45',
            tag: 'v2.7.45',
            platform: 'win32',
            releaseDir,
            policyPath,
            notesPath,
            releaseDate: '2026-08-30T00:00:00.000Z',
        });
        const yaml = require('js-yaml').load(fs.readFileSync(path.join(releaseDir, result.filename), 'utf8'));

        assert.equal(result.filename, 'latest.yml');
        assert.equal(yaml.version, '2.7.45');
        assert.equal(yaml.files[0].url, artifactName);
        assert.equal(yaml.files[0].size, artifactBytes.length);
        assert.equal(yaml.files[0].sha512, result.checksum);
        assert.equal(yaml.updateMode, 'optional');
        assert.match(yaml.releaseNotes, /修复更新流程/);
    } finally {
        fs.rmSync(tempRoot, { recursive: true, force: true });
    }
});

test('发布元数据为 macOS 自动更新选择架构 ZIP', () => {
    const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-update-mac-metadata-'));
    const releaseDir = path.join(tempRoot, 'release');
    const policyPath = path.join(tempRoot, 'update-policy.json');
    const notesPath = path.join(tempRoot, 'release-notes.md');
    const artifactName = '小猪wordTTS-2.7.45-arm64.zip';

    try {
        fs.mkdirSync(releaseDir);
        fs.writeFileSync(path.join(releaseDir, artifactName), Buffer.from('test mac update payload', 'utf8'));
        fs.writeFileSync(policyPath, JSON.stringify({
            version: '2.7.45',
            mode: 'force',
            minimumSupportedVersion: '2.7.40',
            message: '请先完成更新',
        }));
        fs.writeFileSync(notesPath, '## v2.7.45\n\n更新 macOS 安装包。\n');

        const result = prepareMetadata({
            version: '2.7.45',
            tag: 'v2.7.45',
            platform: 'darwin',
            architecture: 'arm64',
            releaseDir,
            policyPath,
            notesPath,
            releaseDate: '2026-08-30T00:00:00.000Z',
        });
        const yaml = require('js-yaml').load(fs.readFileSync(path.join(releaseDir, result.filename), 'utf8'));

        assert.equal(result.filename, 'latest-mac.yml');
        assert.equal(yaml.files[0].url, artifactName);
        assert.equal(yaml.updateMode, 'force');
        assert.equal(yaml.minimumSupportedVersion, '2.7.40');
    } finally {
        fs.rmSync(tempRoot, { recursive: true, force: true });
    }
});

test('发布元数据不会猜测错误平台或架构的安装包', () => {
    const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-update-artifact-guard-'));
    try {
        fs.writeFileSync(path.join(tempRoot, '小猪wordTTS-Setup-2.7.45-arm64.exe'), 'wrong windows arch');
        assert.throws(
            () => chooseArtifact(tempRoot, '2.7.45', 'win32'),
            /未找到 Windows 更新安装包/,
        );

        fs.writeFileSync(path.join(tempRoot, '小猪wordTTS-2.7.45-arm64.zip'), 'arm update');
        assert.throws(
            () => chooseArtifact(tempRoot, '2.7.45', 'darwin', 'x64'),
            /未找到 macOS x64 自动更新 ZIP/,
        );
    } finally {
        fs.rmSync(tempRoot, { recursive: true, force: true });
    }
});
