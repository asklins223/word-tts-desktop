'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const EventEmitter = require('node:events');
const { checkStartupUpdate, createStartupUpdateWindow, runStartupUpdate } = require('../startup-update');

function fixture({ forced = false, choices = [1], checkState, downloadStates, installState, release = {} } = {}) {
    const calls = [];
    const prompts = [];
    const available = {
        status: 'available', version: '1.3.0', currentVersion: '1.2.3',
        isForced: forced, canDownload: true, updateMessage: '版本说明',
    };
    const downloads = [...(downloadStates || [{ status: 'downloaded', canInstall: true }])];
    const responses = [...choices];
    return {
        calls,
        prompts,
        options: {
            app: { quit: () => calls.push('quit') },
            dialog: { showMessageBox: async options => {
                prompts.push(options);
                assert.ok(responses.length > 0, 'unexpected dialog');
                return { response: responses.shift() };
            } },
            manager: {
                check: async () => { calls.push('check'); return checkState || available; },
                download: async () => {
                    calls.push('download');
                    assert.ok(downloads.length > 0, 'unexpected download');
                    return downloads.shift();
                },
                install: async () => { calls.push('install'); return installState || { status: 'installing' }; },
            },
            release: { required: true, version: '1.3.0', updateAvailable: true, ...release },
            createProgressWindow: () => {
                calls.push('progress');
                return { close: () => calls.push('close-progress') };
            },
        },
    };
}

test('非强制更新可以选择稍后更新，随后允许进入业务界面', async () => {
    const f = fixture();
    assert.equal(await runStartupUpdate(f.options), true);
    assert.deepEqual(f.prompts[0].buttons, ['立即更新', '稍后更新']);
    assert.deepEqual(f.calls, ['check']);
});

test('强制更新选择退出或关闭提示都不能进入业务界面', async () => {
    const f = fixture({ forced: true });
    assert.equal(await runStartupUpdate(f.options), false);
    assert.deepEqual(f.prompts[0].buttons, ['立即更新', '退出软件']);
    assert.equal(f.prompts[0].cancelId, 1);
    assert.deepEqual(f.calls, ['check', 'quit']);
});

test('选择立即更新后只打开进度窗口，下载完成才安装且不放行业务启动', async () => {
    for (const forced of [false, true]) {
        const f = fixture({ forced, choices: [0] });
        assert.equal(await runStartupUpdate(f.options), false);
        assert.deepEqual(f.calls, ['check', 'progress', 'download', 'install', 'close-progress']);
    }
});

test('强更下载失败时只允许重试或退出，重试成功才安装', async () => {
    const f = fixture({
        forced: true,
        choices: [0, 0],
        downloadStates: [
            { status: 'error', error: { message: '下载失败' } },
            { status: 'downloaded', canInstall: true },
        ],
    });
    assert.equal(await runStartupUpdate(f.options), false);
    assert.deepEqual(f.prompts[1].buttons, ['重试', '退出软件']);
    assert.deepEqual(f.calls, ['check', 'progress', 'download', 'download', 'install', 'close-progress']);
});

test('强更安装失败时退出不能继续启动', async () => {
    const f = fixture({ forced: true, choices: [0, 1], installState: { status: 'error', error: { message: '安装失败' } } });
    assert.equal(await runStartupUpdate(f.options), false);
    assert.deepEqual(f.prompts[1].buttons, ['重试', '退出软件']);
    assert.ok(f.calls.includes('quit'));
});

test('非强更下载失败可选择稍后更新并进入业务界面', async () => {
    const f = fixture({ choices: [0, 1], downloadStates: [{ status: 'error' }] });
    assert.equal(await runStartupUpdate(f.options), true);
    assert.deepEqual(f.prompts[1].buttons, ['重试', '稍后更新', '退出软件']);
    assert.deepEqual(f.calls, ['check', 'progress', 'download', 'close-progress']);
});

test('启动门禁已经确认的强更策略不能被后到的可选更新降级', async () => {
    for (const policy of [{ updateMode: 'force' }, { minimumSupportedVersion: '1.3.0' }]) {
        const f = fixture({ release: { updateInfo: { version: '1.3.0', ...policy } } });
        assert.equal(await runStartupUpdate(f.options), false);
        assert.deepEqual(f.prompts[0].buttons, ['立即更新', '退出软件']);
    }
});

test('线上探测与更新器结果不一致或更新检查报错时阻止进入', async () => {
    for (const checkState of [
        { status: 'up-to-date' },
        { status: 'error', error: { message: '网络错误' } },
        { status: 'disabled' },
        { status: 'available', version: '1.2.4', canDownload: true },
    ]) {
        const f = fixture({ checkState });
        await assert.rejects(runStartupUpdate(f.options));
        assert.deepEqual(f.calls, ['check']);
    }
});

test('没有新版本时不弹更新提示，开发与 smoke 模式不调用更新器', async () => {
    const current = fixture({ checkState: { status: 'up-to-date' }, release: { updateAvailable: false } });
    assert.equal(await runStartupUpdate(current.options), true);
    assert.equal(current.prompts.length, 0);
    const development = fixture({ release: { required: false } });
    assert.equal(await runStartupUpdate(development.options), true);
    assert.deepEqual(development.calls, []);
});

test('下载期间用户退出软件，不再尝试安装或进入业务界面', async () => {
    const f = fixture({ choices: [0] });
    let quitting = false;
    f.options.isQuitting = () => quitting;
    f.options.manager.download = async () => { quitting = true; return { status: 'downloaded', canInstall: true }; };
    assert.equal(await runStartupUpdate(f.options), false);
    assert.deepEqual(f.calls, ['check', 'progress', 'close-progress']);
});

test('定时检查不能把正在下载或安装更新误判成服务失效', async () => {
    for (const status of ['downloading', 'downloaded', 'installing']) {
        const state = { status };
        assert.equal(await checkStartupUpdate({ check: async () => state }, { allowInProgress: true }), state);
    }
});

test('更新检查挂起时在限定时限内失败', async () => {
    await assert.rejects(checkStartupUpdate({ check: () => new Promise(() => {}) }, { timeoutMs: 20 }), /超时/);
});

test('进度窗口在服务检查前不能创建，服务检查后也只有受限的本地进度页面', () => {
    const calls = [];
    let window;
    class Window extends EventEmitter {
        constructor(options) {
            super();
            this.options = options;
            this.webContents = new EventEmitter();
            this.webContents.setWindowOpenHandler = handler => { this.openHandler = handler; };
            this.webContents.executeJavaScript = async () => {};
            window = this;
            calls.push('window');
        }
        loadFile(file) { this.file = file; return Promise.resolve(); }
        isDestroyed() { return false; }
        destroy() { this.emit('closed'); }
        show() {}
        setProgressBar() {}
    }
    const options = { app: { quit: () => calls.push('quit') }, BrowserWindow: Window };
    assert.throws(() => createStartupUpdateWindow({ ...options, canCreate: () => false }));
    assert.deepEqual(calls, []);
    const progress = createStartupUpdateWindow({ ...options, canCreate: () => true });
    assert.deepEqual(window.options.webPreferences, { contextIsolation: true, nodeIntegration: false, sandbox: true });
    assert.ok(window.file.endsWith('startup-update.html'));
    assert.equal(window.openHandler().action, 'deny');
    progress.close();
    assert.deepEqual(calls, ['window']);
    createStartupUpdateWindow({ ...options, canCreate: () => true });
    window.emit('closed');
    assert.deepEqual(calls, ['window', 'window', 'quit']);
});
