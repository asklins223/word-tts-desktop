'use strict';

const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const EventEmitter = require('node:events');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
    createInstallerService,
    cleanupScript,
    InstallerError,
    normalizeTargetPath,
    parseInstallerArguments,
    readRegistryInstallLocations,
    relocatedExecutableCleanupBatch,
    resolveCleanupLauncherPath,
    resolveRelocatedCleanupExecutable,
    resolveUninstallRelocation,
    validateInstallTargetPath,
    waitForCleanupReady,
} = require('../../installer-prototype/installer-service');

function execFileAsync(executable, args, options = {}) {
    return new Promise((resolve, reject) => {
        execFile(executable, args, options, (error, stdout, stderr) => {
            if (error) {
                error.stdout = stdout;
                error.stderr = stderr;
                reject(error);
                return;
            }
            resolve({ stdout, stderr });
        });
    });
}

test('Windows 卸载清理不会把临时 Electron 子进程误当成 portable 外壳', () => {
    const target = 'C:\\Apps\\小猪wordTTS';
    assert.equal(
        resolveCleanupLauncherPath(target, 'C:\\Temp\\小猪wordTTS-Setup.exe', {}),
        'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe',
    );
    assert.equal(
        resolveCleanupLauncherPath(target, 'C:\\Temp\\小猪wordTTS-Setup.exe', {
            PORTABLE_EXECUTABLE_FILE: 'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe',
        }),
        'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe',
    );
    assert.equal(
        resolveCleanupLauncherPath(target, 'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe', {}),
        'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe',
    );
});

test('TEMP 清理子进程快速退出时会最后复查持久就绪标记', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-cleanup-ready-race-'));
    const readyPath = path.join(root, 'cleanup.ready');
    const child = { exitCode: 0 };
    try {
        const published = new Promise((resolve, reject) => {
            setTimeout(() => {
                fsp.writeFile(readyPath, 'ready', 'utf8').then(resolve, reject);
            }, 20);
        });
        await waitForCleanupReady(child, readyPath, 500);
        await published;
        assert.equal(fs.existsSync(readyPath), true);
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 延迟卸载脚本会等待 portable 外壳退出后再删除目标目录', () => {
    const script = cleanupScript(
        'C:\\Apps\\小猪wordTTS',
        'C:\\Temp\\wordtts-uninstall.ps1',
        101,
        202,
        'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller.exe',
    );
    const launcherGuard = script.indexOf('if ($isLauncher) {');
    const processKill = script.indexOf('Stop-Process -Id $childProcessId');
    const launcherWait = script.indexOf('if ($launcherRunning) {');
    const directoryDelete = script.indexOf('rmdir /s /q');

    assert.match(script, /\$launcherProcessId = 202/);
    assert.match(script, /\$launcherPath = 'C:\\Apps\\小猪wordTTS\\小猪wordTTS-uninstaller\.exe'/);
    assert.ok(launcherGuard >= 0 && launcherGuard < processKill);
    assert.ok(launcherWait >= 0 && launcherWait < directoryDelete);
    assert.match(script, /Get-Process -Id \$launcherProcessId/);
    assert.match(script, /\$launcherGraceDeadline = \(Get-Date\)\.AddSeconds\(15\)/);
    assert.ok(script.indexOf('if ((Get-Date) -lt $launcherGraceDeadline)') < directoryDelete);
    assert.doesNotMatch(script, /Stop-Process -Id \$processId/);
    assert.match(script, /waiting for installer process/);
});

test('Windows 安装目录内的卸载器必须先迁移到 TEMP', () => {
    const target = 'C:\\Apps\\小猪wordTTS';
    const installed = `${target}\\小猪wordTTS-uninstaller.exe`;
    const tempDirectory = 'C:\\Users\\Alice\\AppData\\Local\\Temp';
    const staged = `${tempDirectory}\\wordtts-uninstaller-stage-12-34-a1b2c3.exe`;

    assert.deepEqual(resolveUninstallRelocation({
        platform: 'win32',
        mode: 'uninstall',
        targetPath: target,
        executablePath: installed,
        tempDirectory,
        environment: {},
    }), { required: true, targetPath: target, staged: false });

    assert.deepEqual(resolveUninstallRelocation({
        platform: 'win32',
        mode: 'uninstall',
        targetPath: target,
        executablePath: staged,
        relocatedUninstall: true,
        tempDirectory,
        environment: { WORDTTS_RELOCATED_UNINSTALLER: staged },
    }), { required: false, targetPath: target, staged: true });

    assert.equal(resolveUninstallRelocation({
        platform: 'win32',
        mode: 'uninstall',
        targetPath: target,
        executablePath: installed,
        relocatedUninstall: true,
        tempDirectory,
        environment: {},
    }).required, true, '仅伪造内部参数不得跳过迁移');

    assert.equal(
        resolveRelocatedCleanupExecutable(
            tempDirectory,
            staged,
            { WORDTTS_RELOCATED_UNINSTALLER: staged },
        ),
        staged,
    );
});

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
        relocatedUninstall: false,
    });
    assert.equal(parseInstallerArguments(['--mode=uninstall']).mode, 'uninstall');
    assert.equal(parseInstallerArguments(['--uninstall-relocated']).relocatedUninstall, true);
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
    const spawnForTest = process.platform === 'win32'
        ? () => {
            const child = new EventEmitter();
            child.unref = () => {};
            process.nextTick(() => child.emit('spawn'));
            return child;
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
            spawn: spawnForTest,
            waitForCleanupReady: process.platform === 'win32' ? async () => {} : undefined,
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

test('Windows 已迁移卸载同步删除目标后只为 NSIS 外壳准备自清理批处理', {
    skip: process.platform !== 'win32',
}, async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-installer-portable-cleanup-'));
    const target = path.join(root, 'installed', '小猪wordTTS');
    const dataPath = path.join(root, 'user-data');
    const tempDirectory = path.join(root, 'temp');
    const stagedExecutable = path.join(tempDirectory, 'wordtts-uninstaller-stage-12-34-a1b2c3.exe');
    const environment = {
        ...process.env,
        SystemRoot: process.env.SystemRoot || 'C:\\Windows',
        USERPROFILE: process.env.USERPROFILE || path.join(root, 'profile'),
        PUBLIC: process.env.PUBLIC || path.join(root, 'public'),
        ProgramData: process.env.ProgramData || path.join(root, 'program-data'),
        ProgramFiles: process.env.ProgramFiles || path.join(root, 'program-files'),
        PORTABLE_EXECUTABLE_FILE: stagedExecutable,
        WORDTTS_RELOCATED_UNINSTALLER: stagedExecutable,
    };

    try {
        await fsp.mkdir(target, { recursive: true });
        await fsp.writeFile(path.join(target, '小猪wordTTS.exe'), 'installed application');
        const service = createInstallerService({
            platform: 'win32',
            dataPath,
            tempDirectory,
            setupExecutable: stagedExecutable,
            environment,
            // The test is about the portable wrapper handoff, not shell
            // integration. Avoid requiring a real registry or shortcut host.
            runPowerShell: async () => {},
        });

        const result = await service.run({
            mode: 'uninstall',
            targetPath: target,
            scope: 'per-user',
        });

        assert.equal(result.success, true);
        assert.equal(result.scheduledCleanup, true);
        assert.equal(fs.existsSync(target), false);
        const cleanupScriptPath = `${stagedExecutable}.cleanup.cmd`;
        const cleanupLogPath = `${cleanupScriptPath}.log`;
        const cleanupScript = fs.readFileSync(cleanupScriptPath, 'ascii');
        assert.match(cleanupScript, /set "target=%~1"/);
        assert.match(cleanupScript, /for \/l %%I in \(1,1,90\)/);
        assert.match(cleanupScript, /staged cleanup complete/);
        assert.match(cleanupScript, /del \/f \/q "%target%"/);
        assert.doesNotMatch(cleanupScript, /powershell/i);
        assert.match(fs.readFileSync(cleanupLogPath, 'utf8'), /staged cleanup prepared/);
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows cmd.exe 真实执行 TEMP 外壳自清理批处理', {
    skip: process.platform !== 'win32',
}, async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-stage-batch-'));
    const stagedExecutable = path.join(root, 'wordtts-uninstaller-stage-12-34-a1b2c3.exe');
    const logPath = path.join(root, 'cleanup.log');
    const scriptPath = path.join(root, 'cleanup.cmd');
    try {
        await fsp.writeFile(stagedExecutable, 'temporary wrapper');
        await fsp.writeFile(scriptPath, relocatedExecutableCleanupBatch(), 'ascii');
        await execFileAsync('cmd.exe', [
            '/d',
            '/q',
            '/c',
            scriptPath,
            stagedExecutable,
            logPath,
        ], { windowsHide: true });

        assert.equal(fs.existsSync(stagedExecutable), false);
        assert.equal(fs.existsSync(scriptPath), true);
        assert.match(fs.readFileSync(logPath, 'utf8'), /staged cleanup complete/);
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
