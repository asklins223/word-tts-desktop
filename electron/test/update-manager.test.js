'use strict';

const assert = require('node:assert/strict');
const EventEmitter = require('node:events');
const test = require('node:test');
const {
    compareVersions,
    createUpdateManager,
    deriveUpdatePolicy,
    parseVersion,
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

test('更新版本比较遵循 SemVer 的预发布优先级', () => {
    assert.equal(compareVersions('1.2.3', '1.2.3'), 0);
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

test('更新管理器按检查、下载、安装阶段发布可序列化状态', async () => {
    const updater = new FakeUpdater();
    const states = [];
    const info = {
        version: '2.0.0',
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
    const manager = createUpdateManager({
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

test('没有更新事件时也会清除上一版残留信息，并保留当前最新版本', async () => {
    const updater = new FakeUpdater();
    let shouldReportUpdate = true;
    const updateInfo = { version: '2.0.0', releaseNotes: '上一版说明' };
    updater.checkForUpdates = () => {
        if (shouldReportUpdate) return Promise.resolve({ updateInfo });
        return Promise.resolve(null);
    };
    const manager = createUpdateManager({
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
    const manager = createUpdateManager({
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
    const manager = createUpdateManager({
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
    const manager = createUpdateManager({
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

test('已下载更新不会被后续检查降级或清空', async () => {
    const updater = new FakeUpdater();
    let checks = 0;
    const updateInfo = { version: '2.0.0', releaseNotes: '可安装版本' };
    updater.checkForUpdates = () => {
        checks += 1;
        updater.emit('update-available', updateInfo);
        return Promise.resolve({ updateInfo });
    };
    updater.downloadUpdate = () => {
        updater.emit('update-downloaded');
        return Promise.resolve(['/tmp/update.exe']);
    };
    const manager = createUpdateManager({
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
    const manager = createUpdateManager({
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
    const manager = createUpdateManager({
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
