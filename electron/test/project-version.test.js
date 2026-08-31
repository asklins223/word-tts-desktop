'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
    PROJECT_VERSION_FILE,
    normalizeProjectVersion,
    readProjectVersion,
    syncProjectVersion,
} = require('../../scripts/project_version');
const { parseArguments: parseWindowsInstallerArguments } = require('../../scripts/build_windows_installer');

test('项目版本只从 version.json 读取，并同步到 npm 所需元数据', () => {
    const source = JSON.parse(fs.readFileSync(PROJECT_VERSION_FILE, 'utf8'));
    const version = readProjectVersion();
    const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
    const lockJson = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package-lock.json'), 'utf8'));

    assert.equal(source.version, version);
    assert.equal(packageJson.version, version);
    assert.equal(lockJson.version, version);
    assert.equal(lockJson.packages[''].version, version);
});

test('版本同步器可以更新临时 package/package-lock，而不要求手工改多个版本', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-project-version-'));
    const packagePath = path.join(root, 'package.json');
    const lockPath = path.join(root, 'package-lock.json');
    try {
        await fsp.writeFile(packagePath, JSON.stringify({ name: 'fixture', version: '0.1.0' }));
        await fsp.writeFile(lockPath, JSON.stringify({ name: 'fixture', version: '0.1.0', packages: { '': { version: '0.1.0' } } }));
        assert.equal(syncProjectVersion({
            version: '4.5.6',
            packagePath,
            lockPath,
        }), '4.5.6');
        assert.equal(JSON.parse(await fsp.readFile(packagePath, 'utf8')).version, '4.5.6');
        const lock = JSON.parse(await fsp.readFile(lockPath, 'utf8'));
        assert.equal(lock.version, '4.5.6');
        assert.equal(lock.packages[''].version, '4.5.6');
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('安装器页面不再内置具体发布版本号', () => {
    const installerRoot = path.join(__dirname, '..', '..', 'installer-prototype');
    const html = fs.readFileSync(path.join(installerRoot, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(installerRoot, 'app.js'), 'utf8');
    const installerPackage = JSON.parse(fs.readFileSync(path.join(installerRoot, 'package.json'), 'utf8'));

    assert.equal(Object.hasOwn(installerPackage, 'version'), false);
    assert.doesNotMatch(html, /v\d+\.\d+\.\d+/);
    assert.doesNotMatch(app, /FALLBACK_VERSION/);
});

test('版本和 Windows 构建参数拒绝含糊或空值输入', () => {
    assert.equal(normalizeProjectVersion('v4.5.6-rc.1'), '4.5.6-rc.1');
    assert.throws(() => normalizeProjectVersion('4.5.6-01'), /不是合法 SemVer/);
    const parsed = parseWindowsInstallerArguments([
        '--payload=C:\\build\\win-unpacked',
        '--output-dir=C:\\build\\release',
    ]);
    assert.equal(parsed.payloadDir, path.resolve('C:\\build\\win-unpacked'));
    assert.equal(parsed.outputDir, path.resolve('C:\\build\\release'));
    assert.throws(
        () => parseWindowsInstallerArguments(['--payload=']),
        /必须提供参数值/,
    );
});
