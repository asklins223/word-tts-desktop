'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const EventEmitter = require('node:events');
const test = require('node:test');

const mainPath = path.join(__dirname, '..', 'main.js');
const mainSource = fs.readFileSync(mainPath, 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function boot({ argv = [], env = { WORDTTS_DISABLE_AUTO_UPDATE: '1' } } = {}) {
    const calls = [];
    const ready = deferred();
    const release = deferred();
    const decision = deferred();
    const app = new EventEmitter();
    const windows = [];
    Object.assign(app, {
        isPackaged: true,
        getPath: () => '/synthetic-user-data',
        getVersion: () => '1.2.3',
        setPath() {},
        requestSingleInstanceLock: () => true,
        whenReady: () => ready.promise,
        quit: () => calls.push('quit'),
        exit: code => calls.push(`exit:${code}`),
    });
    const manager = {
        getStatus: () => ({ status: 'idle' }),
        dispose: () => calls.push('dispose-manager'),
    };
    class Window extends EventEmitter {
        static getAllWindows() { return windows.filter(win => !win.destroyed); }
        constructor() {
            super();
            calls.push('window');
            this.webContents = new EventEmitter();
            this.webContents.id = 1;
            this.webContents.setWindowOpenHandler = () => {};
            windows.push(this);
        }
        loadFile() { calls.push('load-renderer'); }
        isDestroyed() { return Boolean(this.destroyed); }
        destroy() {
            calls.push('destroy-window');
            this.destroyed = true;
            this.emit('closed');
            if (Window.getAllWindows().length === 0) app.emit('window-all-closed');
        }
    }
    const mocks = {
        electron: { app, BrowserWindow: Window, dialog: { showErrorBox: (title, detail) => calls.push({ title, detail }) }, ipcMain: {} },
        fs: { mkdirSync() {} },
        './file-dialogs': {},
        './source-staging': {},
        './workflow-proxy': {},
        './window-activation': {},
        './update-manager': { createUpdateManager: options => {
            assert.equal(options.enforceStartupUpdates, true);
            calls.push('update-manager');
            return manager;
        } },
        './startup-release-check': { createReleaseMonitor: () => {
            calls.push('monitor');
            return { dispose: () => calls.push('dispose-monitor') };
        }, verifyStartupRelease: options => {
            assert.equal(options.isPackaged, true);
            calls.push('verify-release');
            return release.promise;
        } },
        './startup-update': { runStartupUpdate: () => { calls.push('update-decision'); return decision.promise; } },
        './client-code-protection': { loadProtectedClientAssets: async () => {
            calls.push('fetch-client-key');
            return new Map([
                ['index.html.enc', '<html></html>'],
                ['styles.css.enc', 'body{}'],
                ['app.js.enc', 'protected source'],
            ]);
        } },
    };
    const context = vm.createContext({
        require: name => Object.hasOwn(mocks, name) ? mocks[name] : require(name),
        __dirname: path.dirname(mainPath),
        process: { argv, env, platform: 'darwin', arch: 'arm64' },
        console: { log() {}, warn() {}, error() {} },
        setTimeout, clearTimeout,
        calls,
    });
    vm.runInContext(`${mainSource}\n
        allocateServerPort = async () => { calls.push('port'); return 54321; };
        registerIpcHandlers = () => calls.push('ipc');
        startPythonServer = () => calls.push('backend');
        stopPythonServerAsync = async () => calls.push('stop-backend');
        waitForServer = async () => {};
        globalThis.testCreateWindow = createWindow;
        globalThis.testStopForServiceError = stopForServiceError;
        globalThis.testIsSmokeTest = isSmokeTest;
    `, context, { filename: mainPath });
    return { app, calls, ready, release, decision, context };
}

test('线上检查和更新选择完成前，主窗口及业务服务均不可创建', async () => {
    const f = boot();
    f.ready.resolve();
    await tick();
    assert.deepEqual(f.calls, ['verify-release']);
    f.app.emit('activate');
    f.app.emit('second-instance');
    assert.equal(f.context.testCreateWindow(), null);
    assert.deepEqual(f.calls, ['verify-release']);

    f.release.resolve({ required: true, version: '1.2.3' });
    await tick();
    assert.deepEqual(f.calls, ['verify-release', 'update-manager', 'update-decision']);
    f.app.emit('activate');
    f.app.emit('second-instance');
    assert.equal(f.context.testCreateWindow(), null);
    assert.ok(!f.calls.includes('backend'));
    assert.ok(!f.calls.includes('ipc'));

    f.decision.resolve(true);
    await tick();
    assert.ok(f.calls.includes('backend'));
    assert.ok(f.calls.includes('window'));
    assert.ok(f.calls.indexOf('update-decision') < f.calls.indexOf('backend'));
    assert.ok(f.calls.indexOf('backend') < f.calls.indexOf('window'));
});

test('包地址检查失败时显示服务错误并退出，activate 和重复启动不能绕过', async () => {
    const f = boot();
    f.ready.resolve();
    await tick();
    f.release.reject(new Error('安装包不可达'));
    await tick();
    f.app.emit('activate');
    f.app.emit('second-instance');
    assert.equal(f.context.testCreateWindow(), null);
    assert.deepEqual(f.calls, [
        'verify-release',
        { title: '小猪wordTTS 服务错误', detail: '安装包不可达' },
        'exit:1',
    ]);
});

test('正式包不能通过伪造 CI 环境变量和 smoke 参数绕过线上启动门禁', async () => {
    const f = boot({
        argv: ['wordtts.exe', '--smoke-test'],
        env: { GITHUB_ACTIONS: 'true', CI: 'true' },
    });
    assert.equal(f.context.testIsSmokeTest, false);
    f.ready.resolve();
    await tick();
    assert.deepEqual(f.calls, ['verify-release']);
});

test('强更未完成时不会分配端口、注册业务 IPC、启动后端或主窗口', async () => {
    const f = boot();
    f.ready.resolve();
    f.release.resolve({ required: true, version: '1.3.0' });
    f.decision.resolve(false);
    await tick();
    f.app.emit('activate');
    f.app.emit('second-instance');
    assert.equal(f.context.testCreateWindow(), null);
    assert.deepEqual(f.calls, ['verify-release', 'update-manager', 'update-decision']);
});

test('运行中服务失效会销毁全部窗口、停止后端并显示错误后退出', async () => {
    const f = boot();
    f.ready.resolve();
    f.release.resolve({ required: true, version: '1.2.3' });
    f.decision.resolve(true);
    await tick();
    assert.equal(f.calls.filter(call => call === 'monitor').length, 1);
    const beforeFailure = f.calls.length;
    await f.context.testStopForServiceError(new Error('线上地址不可达'));
    assert.deepEqual(f.calls.slice(beforeFailure), [
        'dispose-monitor', 'dispose-manager', 'destroy-window', 'stop-backend',
        { title: '小猪wordTTS 服务错误', detail: '线上地址不可达' }, 'exit:1',
    ]);
    f.app.emit('activate');
    f.app.emit('second-instance');
    assert.equal(f.context.testCreateWindow(), null);
    assert.equal(f.calls.filter(call => call === 'window').length, 1);
});
