'use strict';

const assert = require('node:assert/strict');
const EventEmitter = require('node:events');
const test = require('node:test');
const {
    buildUpdateArtifactUrl,
    compareVersions,
    createUpdateManager,
    deriveUpdatePolicy,
    parseVersion,
    probeUpdateArtifact,
    updateArtifactForPlatform,
} = require('../update-manager');

class FakeUpdater extends EventEmitter {
    autoDownload = true;
    autoInstallOnAppQuit = false;
    checkForUpdates() {
        return Promise.resolve(null);
    }
    downloadUpdate() {
        return Promise.resolve([]);
    }
    quitAndInstall() {}
}

function createTestManager(options = {}) {
    return createUpdateManager({
        // Unit tests use synthetic update metadata; keep them deterministic
        // while production uses the built-in remote asset probe.
        verifyArtifact: async () => true,
        ...options,
    });
}

function windowsArtifact(version) {
    return {
        url: `wordTTS-Setup-${version}-x64.exe`,
        sha512: 'synthetic-sha512',
        size: 123,
    };
}

test('更新版本比较遵循 SemVer 的预发布优先级', () => {
    assert.equal(compareVersions('1.2.3', '1.2.3'), 0);
    assert.equal(parseVersion('V1.2.3').major, 1);
    assert.equal(compareVersions('V1.2.4', '1.2.3'), 1);
    assert.equal(compareVersions('1.2.3', '1.2.4'), -1);
    assert.equal(compareVersions('1.2.3-alpha.2', '1.2.3-alpha.10'), -1);
    assert.equal(compareVersions('1.2.3-beta', '1.2.3-alpha'), 1);
    assert.equal(compareVersions('1.2.3', '1.2.3-rc.1'), 1);
    assert.equal(compareVersions('9007199254740992.0.0', '9007199254740993.0.0'), -1);
    assert.equal(compareVersions('1.2.3-9007199254740992', '1.2.3-9007199254740993'), -1);
    assert.equal(parseVersion('01.2.3'), null);
    assert.equal(parseVersion('1.2.3-alpha..1'), null);
});

test('更新策略能同时识别显式强更和最低支持版本', () => {
    assert.equal(
        deriveUpdatePolicy({ version: '2.0.0', updateMode: 'force' }, '1.0.0').isForced,
        true,
    );
    assert.equal(
        deriveUpdatePolicy({ version: '2.0.0', minimumSupportedVersion: '1.5.0' }, '1.0.0').isForced,
        true,
    );
    assert.equal(
        deriveUpdatePolicy({ version: '2.0.0', minimumSupportedVersion: '1.5.0' }, '1.5.0').isForced,
        false,
    );
});

test('更新资产只选择当前平台且与版本匹配的文件', () => {
    const info = {
        version: '2.0.0',
        files: [
            { url: 'wordTTS-1.9.9-arm64.zip', sha512: 'old', size: 10 },
            { url: 'wordTTS-2.0.0-arm64.zip', sha512: 'mac', size: 20 },
            { url: 'wordTTS-Setup-2.0.0-x64.exe', sha512: 'win', size: 30 },
        ],
    };
    assert.equal(updateArtifactForPlatform(info, 'win32').name, 'wordTTS-Setup-2.0.0-x64.exe');
    assert.equal(updateArtifactForPlatform(info, 'darwin').name, 'wordTTS-2.0.0-arm64.zip');
    assert.equal(updateArtifactForPlatform({ version: '2.0.0', files: [windowsArtifact('1.9.9')] }, 'win32'), null);
    assert.equal(updateArtifactForPlatform({
        version: '2.0.0',
        files: [{ url: 'other-app-2.0.0-x64.exe', sha512: 'wrong', size: 30 }],
    }, 'win32'), null);
    assert.equal(updateArtifactForPlatform({
        version: '2.0.0',
        files: [{ url: 'nested/wordTTS-Setup-2.0.0-x64.exe', sha512: 'wrong', size: 30 }],
    }, 'win32'), null);
    assert.equal(
        buildUpdateArtifactUrl('https://github.com/asklins223/word-tts-desktop/releases', 'v2.0.0', '../wordTTS-Setup-2.0.0-x64.exe'),
        null,
    );
});

test('更新资产 URL 会固定到 Release 下载路径，并支持中文名编码', () => {
    assert.equal(
        buildUpdateArtifactUrl(
            'https://github.com/asklins223/word-tts-desktop/releases',
            'v2.0.0',
            '小猪wordTTS-Setup-2.0.0-x64.exe',
        ),
        'https://github.com/asklins223/word-tts-desktop/releases/download/v2.0.0/%E5%B0%8F%E7%8C%AAwordTTS-Setup-2.0.0-x64.exe',
    );
});

test('更新资产探测在 HEAD 不可用时使用单字节范围请求', async () => {
    const requests = [];
    const result = await probeUpdateArtifact('https://example.test/release.exe', {
        timeoutMs: 1000,
        fetchImpl: async (_url, request) => {
            requests.push(request);
            if (request.method === 'HEAD') return { status: 405, body: null };
            return { status: 206, body: null };
        },
    });
    assert.equal(result.available, true);
    assert.deepEqual(requests.map(request => request.method), ['HEAD', 'GET']);
    assert.equal(requests[1].headers.Range, 'bytes=0-0');
});

test('更新管理器按检查、下载、安装阶段发布可序列化状态', async () => {
    const updater = new FakeUpdater();
    const states = [];
    const info = {
        version: '2.0.0',
        files: [windowsArtifact('2.0.0')],
        updateMode: 'force',
        minimumSupportedVersion: '1.5.0',
        updateMessage: '必须先更新',
        releaseNotes: '修复稳定性问题',
    };
    updater.checkForUpdates = () => {
        updater.emit('update-available', info);
        return Promise.resolve({ isUpdateAvailable: true, updateInfo: info });
    };
    updater.downloadUpdate = () => {
        updater.emit('download-progress', {
            percent: 42,
            transferred: 42,
            total: 100,
            bytesPerSecond: 10,
        });
        updater.emit('update-downloaded');
        return Promise.resolve(['/tmp/update.exe']);
    };
    const manager = createTestManager({
        isPackaged: true,
        isSmokeTest: false,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
        now: () => '2026-08-30T00:00:00.000Z',
        send: state => states.push(state),
    });

    assert.equal(manager.getStatus().status, 'idle');
    const checked = await manager.check();
    assert.equal(checked.status, 'available');
    assert.equal(checked.isForced, true);
    assert.equal(checked.canDownload, true);

    const downloaded = await manager.download();
    assert.equal(downloaded.status, 'downloaded');
    assert.equal(downloaded.canInstall, true);
    assert.equal(downloaded.progress.percent, 100);

    const installing = await manager.install();
    assert.equal(installing.status, 'installing');
    assert.equal(updater.autoDownload, false);
    assert.equal(updater.autoInstallOnAppQuit, false);
    assert.ok(states.some(state => state.status === 'checking'));
    assert.ok(states.some(state => state.status === 'downloading'));
    manager.dispose();
});

test('Windows 生产路径使用自绘 Setup 客户端而不是 electron-updater', async () => {
    const calls = [];
    const info = {
        version: '2.0.0',
        tag: 'v2.0.0',
        files: [windowsArtifact('2.0.0')],
    };
    const windowsClient = {
        check: async () => {
            calls.push('check');
            return info;
        },
        download: async (_info, onProgress) => {
            calls.push('download');
            onProgress({ percent: 65, transferred: 65, total: 100, bytesPerSecond: 10 });
            return 'C:\\Temp\\wordtts-update.exe';
        },
        install: async () => {
            calls.push('install');
            return { success: true };
        },
        dispose: () => calls.push('dispose'),
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        windowsClient,
    });

    assert.equal((await manager.check()).status, 'available');
    assert.equal((await manager.download()).status, 'downloaded');
    assert.equal((await manager.install()).status, 'installing');
    assert.deepEqual(calls, ['check', 'download', 'install']);
    manager.dispose();
    assert.deepEqual(calls, ['check', 'download', 'install', 'dispose']);
});

test('只有高版本 tag 或错误平台元数据时不会展示可用更新', async () => {
    const updater = new FakeUpdater();
    updater.checkForUpdates = () => Promise.resolve({
        isUpdateAvailable: true,
        updateInfo: {
            version: '2.0.0',
            // A macOS-only Release must not be offered to Windows.
            files: [{ url: 'wordTTS-2.0.0-arm64.zip', sha512: 'sha', size: 123 }],
        },
    });
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    const status = await manager.check();
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.version, null);
    assert.equal(status.canDownload, false);
    manager.dispose();
});

test('更新元数据存在但具体安装包不可访问时不会展示新版本', async () => {
    const updater = new FakeUpdater();
    let probedUrl = '';
    const info = {
        version: '2.0.0',
        tag: 'v2.0.0',
        files: [windowsArtifact('2.0.0')],
    };
    updater.checkForUpdates = () => Promise.resolve({ isUpdateAvailable: true, updateInfo: info });
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
        verifyArtifact: async ({ url }) => {
            probedUrl = url;
            return { available: false, status: 404 };
        },
    });

    const status = await manager.check();
    assert.equal(probedUrl, 'https://github.com/asklins223/word-tts-desktop/releases/download/v2.0.0/wordTTS-Setup-2.0.0-x64.exe');
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.version, null);
    assert.equal(status.latestVersion, '1.0.0');
    assert.equal(status.canDownload, false);
    manager.dispose();
});

test('客户端只接受带校验值和正大小的当前平台安装包元数据', async () => {
    const updater = new FakeUpdater();
    updater.checkForUpdates = () => Promise.resolve({
        isUpdateAvailable: true,
        updateInfo: {
            version: '2.0.0',
            files: [{ url: 'wordTTS-Setup-2.0.0-x64.exe', sha512: '', size: 0 }],
        },
    });
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    const status = await manager.check();
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.version, null);
    assert.equal(status.canDownload, false);
    manager.dispose();
});

test('没有更新事件时也会清除上一版残留信息，并保留当前最新版本', async () => {
    const updater = new FakeUpdater();
    let shouldReportUpdate = true;
    const updateInfo = { version: '2.0.0', files: [windowsArtifact('2.0.0')], releaseNotes: '上一版说明' };
    updater.checkForUpdates = () => {
        if (shouldReportUpdate) return Promise.resolve({ updateInfo });
        return Promise.resolve(null);
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    assert.equal((await manager.check()).status, 'available');
    assert.equal(manager.getStatus().version, '2.0.0');
    shouldReportUpdate = false;
    const latest = await manager.check();
    assert.equal(latest.status, 'up-to-date');
    assert.equal(latest.version, null);
    assert.equal(latest.releaseNotes, '');
    assert.equal(latest.latestVersion, '1.0.0');
    assert.equal(latest.canDownload, false);
    manager.dispose();
});

test('服务端返回低于当前版本时不会把旧版本展示成最新版本', async () => {
    const updater = new FakeUpdater();
    updater.checkForUpdates = () => Promise.resolve({
        updateInfo: { version: '0.9.0' },
    });
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    const status = await manager.check();
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.latestVersion, '1.0.0');
    manager.dispose();
});

test('延迟到达的旧版本或无效更新事件不会污染强更状态', async () => {
    const updater = new FakeUpdater();
    updater.checkForUpdates = () => {
        updater.emit('update-available', { version: '0.9.0', updateMode: 'force' });
        return Promise.resolve({
            isUpdateAvailable: true,
            updateInfo: { version: 'not-a-version', updateMode: 'force' },
        });
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    const status = await manager.check();
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.version, null);
    assert.equal(status.isForced, false);
    assert.equal(status.canDownload, false);
    manager.dispose();
});

test('更新器明确返回无更新时不会采纳其中携带的旧元数据', async () => {
    const updater = new FakeUpdater();
    updater.checkForUpdates = () => Promise.resolve({
        isUpdateAvailable: false,
        updateInfo: { version: '2.0.0', updateMode: 'force' },
    });
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    const status = await manager.check();
    assert.equal(status.status, 'up-to-date');
    assert.equal(status.version, null);
    assert.equal(status.isForced, false);
    manager.dispose();
});

test('更新探测完成前收到无更新结果时，不会被延迟事件重新污染', async () => {
    const updater = new FakeUpdater();
    const info = { version: '2.0.0', files: [windowsArtifact('2.0.0')] };
    let releaseProbe;
    const probe = new Promise(resolve => { releaseProbe = resolve; });
    updater.checkForUpdates = () => {
        updater.emit('update-available', info);
        updater.emit('update-not-available', { version: '1.0.0' });
        return Promise.resolve(null);
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
        verifyArtifact: async () => probe,
    });

    const checked = await manager.check();
    assert.equal(checked.status, 'up-to-date');
    releaseProbe({ available: true });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(manager.getStatus().status, 'up-to-date');
    assert.equal(manager.getStatus().version, null);
    manager.dispose();
});

test('陈旧更新事件不会清掉新一轮仍在检查中的状态', async () => {
    const updater = new FakeUpdater();
    const info = { version: '2.0.0', files: [windowsArtifact('2.0.0')] };
    let releaseProbe;
    const probe = new Promise(resolve => { releaseProbe = resolve; });
    updater.checkForUpdates = () => {
        updater.emit('update-available', info);
        updater.emit('update-not-available', { version: '1.0.0' });
        updater.emit('checking-for-update');
        return Promise.resolve(null);
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
        verifyArtifact: async () => probe,
    });

    const checked = await manager.check();
    assert.equal(checked.status, 'checking');
    releaseProbe({ available: false });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(manager.getStatus().status, 'checking');
    manager.dispose();
});

test('已下载更新不会被后续检查降级或清空', async () => {
    const updater = new FakeUpdater();
    let checks = 0;
    const updateInfo = { version: '2.0.0', files: [windowsArtifact('2.0.0')], releaseNotes: '可安装版本' };
    updater.checkForUpdates = () => {
        checks += 1;
        updater.emit('update-available', updateInfo);
        return Promise.resolve({ updateInfo });
    };
    updater.downloadUpdate = () => {
        updater.emit('update-downloaded');
        return Promise.resolve(['/tmp/update.exe']);
    };
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '1.0.0',
        platform: 'win32',
        autoUpdater: updater,
    });

    await manager.check();
    await manager.download();
    assert.equal(manager.getStatus().status, 'downloaded');

    const afterCheck = await manager.check();
    assert.equal(checks, 1);
    assert.equal(afterCheck.status, 'downloaded');
    assert.equal(afterCheck.version, '2.0.0');
    assert.equal(afterCheck.canInstall, true);

    updater.emit('checking-for-update');
    updater.emit('update-available', updateInfo);
    updater.emit('update-not-available', { version: '1.0.0' });
    const afterEvents = manager.getStatus();
    assert.equal(afterEvents.status, 'downloaded');
    assert.equal(afterEvents.version, '2.0.0');
    assert.equal(afterEvents.canInstall, true);
    manager.dispose();
});

test('开发或 smoke 模式不会加载或触发原生更新器', async () => {
    const manager = createTestManager({
        isPackaged: false,
        isSmokeTest: false,
        appVersion: '2.0.0',
    });
    assert.equal(manager.getStatus().status, 'disabled');
    assert.equal((await manager.check()).status, 'disabled');
    manager.dispose();
});

test('不支持自动更新的平台保持禁用', async () => {
    const updater = new FakeUpdater();
    const manager = createTestManager({
        isPackaged: true,
        appVersion: '2.0.0',
        platform: 'linux',
        autoUpdater: updater,
    });
    assert.equal(manager.isEnabled(), false);
    assert.equal(manager.getStatus().status, 'disabled');
    assert.equal((await manager.check()).status, 'disabled');
    manager.dispose();
});
