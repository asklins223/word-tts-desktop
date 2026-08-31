'use strict';

const assert = require('node:assert/strict');
const EventEmitter = require('node:events');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
    createInstallerService,
    InstallerError,
    normalizeTargetPath,
    parseInstallerArguments,
    readRegistryInstallLocations,
    validateInstallTargetPath,
} = require('../../installer-prototype/installer-service');

test('自绘安装器参数解析支持普通、更新、卸载和无窗口冒烟参数', () => {
    assert.deepEqual(parseInstallerArguments([
        '--mode=update',
        '--target',
        'C:\\Apps\\小猪wordTTS',
        '--auto-start',
        '--headless',
    ]), {
        mode: 'update',
        targetPath: 'C:\\Apps\\小猪wordTTS',
        targetVersion: null,
        planPath: null,
        elevated: false,
        autoStart: true,
        headless: true,
    });
    assert.equal(parseInstallerArguments(['--mode=uninstall']).mode, 'uninstall');
    assert.throws(
        () => parseInstallerArguments(['--target']),
        error => error instanceof InstallerError && error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
        () => parseInstallerArguments(['--target=']),
        error => error instanceof InstallerError && error.code === 'INVALID_ARGUMENT',
    );
});

test('Windows 安装路径校验拒绝相对路径和磁盘根目录', () => {
    assert.equal(normalizeTargetPath('C:\\Apps\\小猪wordTTS', 'win32'), 'C:\\Apps\\小猪wordTTS');
    assert.throws(
        () => normalizeTargetPath('Apps\\小猪wordTTS', 'win32'),
        error => error instanceof InstallerError && error.code === 'INVALID_TARGET',
    );
    assert.throws(
        () => normalizeTargetPath('C:\\', 'win32'),
        error => error instanceof InstallerError && error.code === 'INVALID_TARGET',
    );
    const environment = {
        SystemRoot: 'C:\\Windows',
        USERPROFILE: 'C:\\Users\\Alice',
        PUBLIC: 'C:\\Users\\Public',
        ProgramData: 'C:\\ProgramData',
        ProgramFiles: 'C:\\Program Files',
    };
    assert.equal(
        validateInstallTargetPath('C:\\Apps\\小猪wordTTS', 'win32', environment),
        'C:\\Apps\\小猪wordTTS',
    );
    assert.throws(
        () => validateInstallTargetPath('C:\\Windows\\System32\\小猪wordTTS', 'win32', environment),
        error => error instanceof InstallerError && error.code === 'INVALID_TARGET',
    );
    assert.throws(
        () => validateInstallTargetPath('C:\\Program Files', 'win32', environment),
        error => error instanceof InstallerError && error.code === 'INVALID_TARGET',
    );
});

test('安装器会从当前用户和本机卸载注册表读取自定义安装位置', () => {
    const queried = [];
    const locations = readRegistryInstallLocations({}, (_executable, args) => {
        queried.push(args[1]);
        if (args[1] === 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WordTTS') {
            return '    InstallLocation    REG_SZ    D:\\Apps\\小猪wordTTS\r\n';
        }
        return '';
    });

    assert.ok(queried.length >= 2);
    assert.deepEqual(locations, ['D:\\Apps\\小猪wordTTS']);
});

test('安装器启动时会根据目标目录自动识别安装或更新', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-detect-'));
    const target = path.join(root, 'installed', '小猪wordTTS');
    try {
        const service = createInstallerService({
            platform: process.platform,
            targetPath: target,
            dataPath: path.join(root, 'user-data'),
        });

        const firstRun = service.getConfig({ appVersion: '3.0.1', arguments: {} });
        assert.equal(firstRun.mode, 'install');
        assert.equal(firstRun.installed, false);
        assert.deepEqual(firstRun.allowedModes, ['install']);

        await fsp.mkdir(target, { recursive: true });
        await fsp.writeFile(path.join(target, '小猪wordTTS.exe'), 'installed application');

        const existingInstall = service.getConfig({ appVersion: '3.0.2', arguments: {} });
        assert.equal(existingInstall.mode, 'update');
        assert.equal(existingInstall.installed, true);
        assert.equal(existingInstall.fixedMode, true);
        assert.deepEqual(existingInstall.allowedModes, ['update']);

        const metadataTarget = service.getConfig({
            appVersion: '3.0.2',
            arguments: { mode: 'update', targetVersion: 'v3.0.2' },
        });
        assert.equal(metadataTarget.version, '3.0.2');
        assert.equal(metadataTarget.targetVersion, '3.0.2');
        assert.throws(
            () => service.getConfig({
                appVersion: '3.0.1',
                arguments: { mode: 'update', targetVersion: '3.0.2' },
            }),
            error => error.code === 'TARGET_VERSION_MISMATCH',
        );
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('安装器启动已安装应用失败时不会误报成功', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-launch-error-'));
    const target = path.join(root, 'installed', '小猪wordTTS');
    const child = new EventEmitter();
    child.unref = () => {};
    try {
        await fsp.mkdir(target, { recursive: true });
        await fsp.writeFile(path.join(target, '小猪wordTTS.exe'), 'installed application');
        const service = createInstallerService({
            platform: process.platform,
            spawn: () => {
                process.nextTick(() => child.emit('error', Object.assign(new Error('permission denied'), { code: 'EACCES' })));
                return child;
            },
        });
        await assert.rejects(
            () => service.launchInstalledApp(target),
            error => error instanceof InstallerError && error.code === 'APP_LAUNCH_FAILED',
        );
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('安装状态文件中的路径字段不会重定向安装器的恢复或注册表操作', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-state-guard-'));
    const target = path.join(root, 'installed', '小猪wordTTS');
    const dataPath = path.join(root, 'user-data');
    try {
        await fsp.mkdir(target, { recursive: true });
        await fsp.writeFile(path.join(target, 'install-state.json'), JSON.stringify({
            format: 1,
            product: '小猪wordTTS',
            version: 'v3.0.1',
            scope: 'per-user',
            installPath: target,
            executable: path.join(root, 'not-the-app.exe'),
            uninstaller: path.join(root, 'not-the-uninstaller.exe'),
            dataPath: path.join(root, 'not-user-data'),
            shortcuts: { desktop: 'false', startMenu: false },
        }));
        const service = createInstallerService({
            platform: process.platform,
            dataPath,
        });

        assert.deepEqual(service.readState(target), {
            format: 1,
            product: '小猪wordTTS',
            version: '3.0.1',
            scope: 'per-user',
            installPath: path.normalize(target),
            executable: path.join(path.normalize(target), '小猪wordTTS.exe'),
            uninstaller: path.join(path.normalize(target), '小猪wordTTS-uninstaller.exe'),
            dataPath,
            shortcuts: { desktop: true, startMenu: false },
        });
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('安装器服务可以真实完成安装、更新、保留缓存卸载和删除数据卸载', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-service-'));
    const payload = path.join(root, 'payload');
    const target = path.join(root, 'installed', '小猪wordTTS');
    const dataPath = path.join(root, 'user-data');
    const setup = path.join(root, '小猪wordTTS-Setup.exe');
    const progress = [];
    const shellEnvironment = process.platform === 'win32'
        ? {
            USERPROFILE: path.join(root, 'user-profile'),
            APPDATA: path.join(root, 'app-data'),
            PUBLIC: path.join(root, 'public'),
            ProgramData: path.join(root, 'program-data'),
            ProgramFiles: path.join(root, 'program-files'),
        }
        : undefined;

    try {
        await fsp.mkdir(path.join(payload, 'resources'), { recursive: true });
        // WScript.Shell validates shortcut targets on Windows. Use a real PE
        // executable there; a text file merely renamed to .exe makes the
        // integration test fail before the installer logic is exercised.
        if (process.platform === 'win32') {
            await fsp.copyFile(process.execPath, path.join(payload, '小猪wordTTS.exe'));
        } else {
            await fsp.writeFile(path.join(payload, '小猪wordTTS.exe'), 'application v1');
        }
        await fsp.writeFile(path.join(payload, 'resources', 'runtime.txt'), 'runtime');
        await fsp.writeFile(setup, 'self-drawing setup');

        const service = createInstallerService({
            platform: process.platform,
            payloadPath: payload,
            setupExecutable: setup,
            tempDirectory: path.join(root, 'temp'),
            dataPath,
            environment: shellEnvironment,
        });
        const install = await service.run({
            mode: 'install',
            targetPath: target,
            scope: 'per-user',
            version: '3.0.1',
            desktopShortcut: true,
            startMenuShortcut: true,
        }, {
            onProgress: value => progress.push(value),
        });

        assert.equal(install.success, true);
        if (process.platform === 'win32') {
            // Windows shortcut creation requires a valid PE target.  The
            // fixture therefore copies Node's executable instead of a text
            // file renamed to .exe; only verify the PE signature here.
            assert.equal(
                fs.readFileSync(path.join(target, '小猪wordTTS.exe')).subarray(0, 2).toString('ascii'),
                'MZ',
            );
        } else {
            assert.equal(fs.readFileSync(path.join(target, '小猪wordTTS.exe'), 'utf8'), 'application v1');
        }
        assert.equal(fs.readFileSync(path.join(target, '小猪wordTTS-uninstaller.exe'), 'utf8'), 'self-drawing setup');
        assert.equal(service.readState(target).version, '3.0.1');
        assert.equal(progress.at(-1).percent, 100);
        if (process.platform === 'win32') {
            assert.equal(
                fs.existsSync(path.win32.join(shellEnvironment.USERPROFILE, 'Desktop', '小猪wordTTS.lnk')),
                true,
            );
            assert.equal(
                fs.existsSync(path.win32.join(shellEnvironment.APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs', '小猪wordTTS.lnk')),
                true,
            );
        }

        await fsp.writeFile(path.join(payload, '小猪wordTTS.exe'), 'application v2');
        const update = await service.run({
            mode: 'update',
            targetPath: target,
            scope: 'per-user',
            version: '3.0.2',
        });
        assert.equal(update.previousState.version, '3.0.1');
        assert.equal(fs.readFileSync(path.join(target, '小猪wordTTS.exe'), 'utf8'), 'application v2');
        assert.equal(service.readState(target).version, '3.0.2');

        await assert.rejects(
            () => service.run({
                mode: 'update',
                targetPath: target,
                scope: 'per-user',
                version: '3.0.1',
            }),
            error => error.code === 'NOT_NEWER_VERSION',
        );
        assert.equal(fs.readFileSync(path.join(target, '小猪wordTTS.exe'), 'utf8'), 'application v2');
        assert.equal(service.readState(target).version, '3.0.2');

        await fsp.mkdir(path.join(dataPath, 'cache'), { recursive: true });
        await fsp.writeFile(path.join(dataPath, 'settings.json'), '{}');
        await fsp.writeFile(path.join(dataPath, 'cache', 'audio.tmp'), 'cache');
        const kept = await service.run({
            mode: 'uninstall',
            targetPath: target,
            scope: 'per-user',
            keepUserData: true,
            deleteCache: true,
        });
        assert.equal(kept.success, true);
        assert.equal(fs.existsSync(target), false);
        assert.equal(fs.existsSync(path.join(dataPath, 'settings.json')), true);
        assert.equal(fs.existsSync(path.join(dataPath, 'cache')), false);

        await fsp.mkdir(path.join(payload, 'resources'), { recursive: true });
        await service.run({
            mode: 'install',
            targetPath: target,
            scope: 'per-user',
            version: '3.0.2',
        });
        const deleted = await service.run({
            mode: 'uninstall',
            targetPath: target,
            scope: 'per-user',
            keepUserData: false,
        });
        assert.equal(deleted.success, true);
        assert.equal(fs.existsSync(target), false);
        assert.equal(fs.existsSync(dataPath), false);
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('取消安装不会留下半成品目录', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-cancel-'));
    try {
        const payload = path.join(root, 'payload');
        const target = path.join(root, 'installed', '小猪wordTTS');
        await fsp.mkdir(payload, { recursive: true });
        await fsp.writeFile(path.join(payload, '小猪wordTTS.exe'), 'application');
        const service = createInstallerService({
            platform: process.platform,
            payloadPath: payload,
            tempDirectory: path.join(root, 'temp'),
        });
        await assert.rejects(
            () => service.run({ mode: 'install', targetPath: target, version: '3.0.1' }, { isCancelled: () => true }),
            error => error.code === 'CANCELLED',
        );
        assert.equal(fs.existsSync(target), false);
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('卸载不会删除没有小猪安装标记的任意目录，安装版本不能为空', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-safety-'));
    const target = path.join(root, 'unrelated');
    try {
        await fsp.mkdir(target, { recursive: true });
        await fsp.writeFile(path.join(target, 'notes.txt'), 'keep me');
        const service = createInstallerService({
            platform: process.platform,
            payloadPath: path.join(root, 'payload'),
        });

        await assert.rejects(
            () => service.run({ mode: 'uninstall', targetPath: target, keepUserData: false }),
            error => error.code === 'NOT_INSTALLED',
        );
        const uninstallerOnlyTarget = path.join(root, 'uninstaller-only');
        await fsp.mkdir(uninstallerOnlyTarget, { recursive: true });
        await fsp.writeFile(path.join(uninstallerOnlyTarget, '小猪wordTTS-uninstaller.exe'), 'not enough');
        await assert.rejects(
            () => service.run({ mode: 'uninstall', targetPath: uninstallerOnlyTarget, keepUserData: false }),
            error => error.code === 'NOT_INSTALLED',
        );
        const missingUpdateTarget = path.join(root, 'missing-update');
        await assert.rejects(
            () => service.run({ mode: 'update', targetPath: missingUpdateTarget, version: '3.0.1' }),
            error => error.code === 'NOT_INSTALLED',
        );
        const cancellableUninstallTarget = path.join(root, 'cancellable-uninstall');
        await fsp.mkdir(cancellableUninstallTarget, { recursive: true });
        await fsp.writeFile(path.join(cancellableUninstallTarget, '小猪wordTTS.exe'), 'installed application');
        await assert.rejects(
            () => service.run({ mode: 'uninstall', targetPath: cancellableUninstallTarget }, { isCancelled: () => true }),
            error => error.code === 'CANCELLED',
        );
        assert.equal(fs.existsSync(path.join(cancellableUninstallTarget, '小猪wordTTS.exe')), true);
        const fileTarget = path.join(root, 'not-a-folder');
        await fsp.writeFile(fileTarget, 'do not replace');
        await assert.rejects(
            () => service.run({ mode: 'install', targetPath: fileTarget, version: '3.0.1' }),
            error => error.code === 'INVALID_TARGET',
        );
        assert.equal(fs.readFileSync(fileTarget, 'utf8'), 'do not replace');
        await assert.rejects(
            () => service.run({ mode: 'install', targetPath: path.join(root, 'new-install'), version: '' }),
            error => error.code === 'INVALID_VERSION',
        );
        assert.equal(fs.readFileSync(path.join(target, 'notes.txt'), 'utf8'), 'keep me');
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('安装器拒绝把程序目录放进个人数据目录或其父目录', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-data-boundary-'));
    const dataPath = path.join(root, 'user-data');
    try {
        const service = createInstallerService({
            platform: process.platform,
            dataPath,
            payloadPath: path.join(root, 'payload'),
        });
        await assert.rejects(
            () => service.run({ mode: 'install', targetPath: dataPath, version: '3.0.1' }),
            error => error.code === 'INVALID_TARGET',
        );
        await assert.rejects(
            () => service.run({ mode: 'uninstall', targetPath: root, keepUserData: false }),
            error => error.code === 'INVALID_TARGET',
        );
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});
