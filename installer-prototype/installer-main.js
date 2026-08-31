'use strict';

const {
    app,
    BrowserWindow,
    dialog,
    ipcMain,
} = require('electron');
const fs = require('node:fs');
const crypto = require('node:crypto');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const {
    createInstallerService,
    decodeArgumentValue,
    encodePowerShellCommand,
    parseInstallerArguments,
} = require('./installer-service');

const PRODUCT_NAME = '小猪wordTTS';
const ELEVATION_PLAN_NAME = /^wordtts-installer-plan-\d+-\d+-[0-9a-f]+\.json$/i;
// Leave a slim canvas margin around the 1120 x 680 artwork. These dimensions
// are the content size of the frameless window, so the UI never gets clipped
// by the browser-like preview canvas.
const WINDOW_WIDTH = 1200;
const WINDOW_HEIGHT = 780;
const isSmokeTest = process.argv.includes('--smoke-test');

let mainWindow = null;
let installerService = null;
let installerConfig = null;
let activeOperation = null;
let cancellationRequested = false;
let operationPlan = null;

let parsedArguments = null;
let argumentError = null;
const headlessDiagnosticPath = String(process.env.WORDTTS_INSTALLER_LOG || '').trim();

function writeHeadlessDiagnostic(message) {
    if (!headlessDiagnosticPath) return;
    try {
        fs.appendFileSync(
            headlessDiagnosticPath,
            `${new Date().toISOString()} ${String(message || '')}\n`,
            'utf8',
        );
    } catch (_) {
        // Diagnostics must never change the installer result.
    }
}

try {
    parsedArguments = parseInstallerArguments(process.argv.slice(1));
} catch (error) {
    // Keep malformed command lines inside Electron's normal startup error
    // path. Throwing while the module is being evaluated skips the error
    // dialog and leaves callers with only a raw Node stack trace.
    argumentError = error;
    parsedArguments = { headless: process.argv.includes('--headless') };
}

function portableExecutablePath() {
    return process.env.PORTABLE_EXECUTABLE_FILE
        || process.execPath;
}

function isTrustedSender(event) {
    return Boolean(
        mainWindow
        && !mainWindow.isDestroyed()
        && event?.sender === mainWindow.webContents,
    );
}

function post(channel, payload) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send(channel, payload);
}

function psQuote(value) {
    return `'${String(value).replace(/'/g, "''")}'`;
}

function writeElevationPlan(plan) {
    const planPath = path.join(
        os.tmpdir(),
        `wordtts-installer-plan-${process.pid}-${Date.now()}-${crypto.randomBytes(8).toString('hex')}.json`,
    );
    fs.writeFileSync(planPath, JSON.stringify(plan), { encoding: 'utf8', mode: 0o600 });
    return planPath;
}

function isOwnedElevationPlanPath(planPath) {
    const resolved = path.resolve(String(planPath || ''));
    const tempRoot = path.resolve(os.tmpdir());
    const normalizeForComparison = value => process.platform === 'win32' ? value.toLowerCase() : value;
    return normalizeForComparison(path.dirname(resolved)) === normalizeForComparison(tempRoot)
        && ELEVATION_PLAN_NAME.test(path.basename(resolved));
}

function removeElevationPlan(planPath) {
    if (!isOwnedElevationPlanPath(planPath)) return;
    try { fs.rmSync(path.resolve(planPath), { force: true }); } catch (_) { /* best effort */ }
}

function waitForChildSpawn(child) {
    if (!child || typeof child.once !== 'function') return Promise.resolve(child);
    return new Promise((resolve, reject) => {
        let settled = false;
        const onSpawn = () => {
            if (settled) return;
            settled = true;
            resolve(child);
        };
        const onError = error => {
            if (settled) return;
            settled = true;
            child.removeListener?.('spawn', onSpawn);
            reject(error);
        };
        child.once('spawn', onSpawn);
        // Keep this listener attached after a successful spawn too. A later
        // child-process error must not become an uncaught Electron exception.
        child.once('error', onError);
    });
}

function waitForChildExit(child) {
    if (!child || typeof child.once !== 'function') return Promise.resolve();
    return new Promise((resolve, reject) => {
        const onError = error => {
            child.removeListener?.('close', onClose);
            reject(error);
        };
        const onClose = code => {
            child.removeListener?.('error', onError);
            // A process terminated by a signal reports code === null. Treat
            // that as a failed handoff rather than coercing null to success.
            if (code === 0) {
                resolve();
                return;
            }
            const error = new Error(`管理员安装程序启动失败（退出码 ${String(code ?? 'unknown')}）。`);
            error.code = 'ELEVATION_FAILED';
            error.exitCode = code;
            reject(error);
        };
        child.once('error', onError);
        child.once('close', onClose);
    });
}

async function startElevatedInstaller(planPath) {
    const executable = portableExecutablePath();
    const script = [
        '$ErrorActionPreference = \'Stop\'',
        `$installer = ${psQuote(executable)}`,
        `$plan = ${psQuote(planPath)}`,
        'try {',
        "  $child = Start-Process -FilePath $installer -ArgumentList @('--elevated', ('--plan=' + $plan)) -Verb RunAs -PassThru",
        '  if (-not $child) { throw \'管理员安装程序没有返回进程。\' }',
        '  $child.Id | Out-Null',
        '} catch {',
        '  exit 1',
        '}',
    ].join('\n');
    const child = spawn(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encodePowerShellCommand(script)],
        { detached: true, stdio: 'ignore', windowsHide: true },
    );
    if (!child || typeof child !== 'object') {
        throw new Error('无法启动管理员权限安装程序。');
    }
    await waitForChildSpawn(child);
    await waitForChildExit(child);
}

async function delegateToElevatedInstance(plan) {
    const planPath = writeElevationPlan({
        ...plan,
        autoStart: true,
        planPath: null,
    });
    try {
        await startElevatedInstaller(planPath);
        return { delegated: true };
    } catch (error) {
        try { fs.rmSync(planPath, { force: true }); } catch (_) { /* best effort */ }
        throw error;
    }
}

function readOperationPlan() {
    const planPath = parsedArguments.planPath;
    if (!planPath) return null;
    try {
        const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'));
        if (!plan || typeof plan !== 'object' || Array.isArray(plan)) {
            throw new Error('安装操作计划格式不正确。');
        }
        const resolvedPlan = { ...plan, planPath };
        // The elevated child only needs the plan while bootstrapping. Remove
        // it immediately after reading so a completed UAC handoff does not
        // leave user-selected install paths and options in the temp folder.
        if (parsedArguments.elevated) {
            removeElevationPlan(planPath);
        }
        return resolvedPlan;
    } catch (error) {
        if (parsedArguments.elevated) removeElevationPlan(planPath);
        throw new Error(`无法读取安装操作计划：${error.message}`);
    }
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: WINDOW_WIDTH,
        height: WINDOW_HEIGHT,
        minWidth: 920,
        minHeight: 600,
        maxWidth: WINDOW_WIDTH,
        maxHeight: WINDOW_HEIGHT,
        frame: false,
        resizable: false,
        show: false,
        backgroundColor: '#151a22',
        title: `${PRODUCT_NAME} 安装程序`,
        webPreferences: {
            preload: path.join(__dirname, 'installer-preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: false,
        },
    });
    mainWindow.removeMenu();
    mainWindow.on('close', event => {
        // Alt+F4, the taskbar close button, and system shutdown do not go
        // through installer-close. Keep the window alive while a file swap or
        // uninstall is in flight; otherwise Electron can terminate the
        // process midway through the atomic-operation cleanup.
        if (activeOperation) event.preventDefault();
    });
    mainWindow.on('closed', () => { mainWindow = null; });
    mainWindow.loadFile(path.join(__dirname, 'index.html'));
    mainWindow.once('ready-to-show', () => mainWindow?.show());
    return mainWindow;
}

function normalizedPlan(input) {
    const candidate = input && typeof input === 'object' ? input : {};
    const mode = ['install', 'update', 'uninstall'].includes(candidate.mode)
        ? candidate.mode
        : installerConfig.mode;
    const targetPath = String(candidate.targetPath || installerConfig.targetPath || '').trim();
    return {
        mode,
        targetPath,
        version: installerConfig.version,
        targetVersion: installerConfig.targetVersion,
        scope: candidate.scope === 'per-machine' ? 'per-machine' : 'per-user',
        desktopShortcut: candidate.desktopShortcut !== false,
        startMenuShortcut: candidate.startMenuShortcut !== false,
        refreshShortcuts: candidate.refreshShortcuts !== false,
        keepUserData: candidate.keepUserData !== false,
        deleteCache: candidate.deleteCache === true,
    };
}

function registerIpc() {
    ipcMain.handle('installer-config', (event) => {
        if (!isTrustedSender(event)) return installerConfig;
        return installerConfig;
    });

    ipcMain.handle('installer-choose-directory', async (event, defaultPath) => {
        if (!isTrustedSender(event)) return { canceled: true };
        const result = await dialog.showOpenDialog(mainWindow, {
            title: '选择小猪wordTTS安装位置',
            defaultPath: String(defaultPath || installerConfig.targetPath || ''),
            properties: ['openDirectory', 'createDirectory'],
        });
        return {
            canceled: Boolean(result.canceled),
            path: result.canceled ? '' : String(result.filePaths?.[0] || ''),
        };
    });

    ipcMain.handle('installer-start', async (event, input) => {
        if (!isTrustedSender(event)) throw new Error('untrusted installer sender');
        if (activeOperation) return { running: true };
        const plan = normalizedPlan(input);

        // Per-machine work needs a UAC token. The elevated instance reuses the
        // same HTML UI and receives the exact plan through a short-lived file.
        if (process.platform === 'win32'
            && plan.scope === 'per-machine'
            && !parsedArguments.elevated) {
            const delegated = await delegateToElevatedInstance(plan);
            post('installer-complete', delegated);
            setTimeout(() => app.quit(), 120);
            return delegated;
        }

        cancellationRequested = false;
        activeOperation = installerService.run(plan, {
            onProgress: progress => post('installer-progress', progress),
            isCancelled: () => cancellationRequested,
        });
        try {
            const result = await activeOperation;
            // The user may have selected a different install folder in the
            // current window. Keep that successful target for the optional
            // "launch after finish" action; the initial config only contains
            // the detected/default folder.
            if (result?.success) {
                installerConfig = {
                    ...installerConfig,
                    targetPath: plan.targetPath,
                    scope: plan.scope,
                };
            }
            post('installer-complete', result);
            return result;
        } catch (error) {
            post('installer-error', {
                code: String(error.code || 'INSTALLER_ERROR'),
                message: String(error.message || error),
            });
            throw error;
        } finally {
            activeOperation = null;
            cancellationRequested = false;
        }
    });

    ipcMain.handle('installer-cancel', (event) => {
        if (!isTrustedSender(event)) return { canceled: false };
        cancellationRequested = true;
        return { canceled: true };
    });

    ipcMain.handle('installer-launch', async (event) => {
        if (!isTrustedSender(event)) return { success: false };
        try {
            await installerService.launchInstalledApp(installerConfig.targetPath);
            return { success: true };
        } catch (error) {
            return { success: false, message: error.message };
        }
    });

    ipcMain.handle('installer-close', (event) => {
        if (!isTrustedSender(event)) return { success: false };
        if (activeOperation) return { success: false, reason: 'operation-running' };
        app.quit();
        return { success: true };
    });

    ipcMain.handle('installer-minimize', (event) => {
        if (!isTrustedSender(event)) return { success: false };
        mainWindow?.minimize();
        return { success: true };
    });
}

async function bootstrap() {
    if (argumentError) throw argumentError;
    writeHeadlessDiagnostic(`[bootstrap] argv=${JSON.stringify(process.argv.slice(1))}`);
    if (process.platform !== 'win32' && !isSmokeTest) {
        // The source preview remains usable in a browser. The packaged custom
        // setup is intentionally Windows-only until a platform-specific file
        // operation backend is added.
        console.warn('[installer] custom setup is intended for Windows');
    }
    operationPlan = readOperationPlan();
    installerService = createInstallerService({
        platform: process.platform,
        resourcesPath: process.resourcesPath,
        setupExecutable: portableExecutablePath(),
        version: app.getVersion(),
        environment: process.env,
        tempDirectory: os.tmpdir(),
    });
    installerConfig = installerService.getConfig({
        appVersion: app.getVersion(),
        arguments: parsedArguments,
        operationPlan,
    });
    writeHeadlessDiagnostic(`[config] ${JSON.stringify({
        mode: installerConfig.mode,
        targetPath: installerConfig.targetPath,
        scope: installerConfig.scope,
        version: installerConfig.version,
        payloadPath: installerConfig.payloadPath,
        setupExecutable: installerConfig.setupExecutable,
    })}`);
    if (parsedArguments.headless) {
        const plan = normalizedPlan({
            mode: parsedArguments.mode || installerConfig.mode,
            targetPath: parsedArguments.targetPath || installerConfig.targetPath,
            scope: installerConfig.scope,
        });
        writeHeadlessDiagnostic(`[plan] ${JSON.stringify(plan)}`);
        await installerService.run(plan, {
            onProgress: progress => {
                const line = `[installer] ${progress.percent}% ${progress.stage}`;
                console.log(line);
                writeHeadlessDiagnostic(line);
            },
        });
        writeHeadlessDiagnostic('[complete] installer operation succeeded');
        app.exit(0);
        return;
    }
    registerIpc();
    createWindow();
}

app.whenReady().then(bootstrap).catch(error => {
    console.error(`[installer] 启动失败: ${error.stack || error}`);
    writeHeadlessDiagnostic(`[error] ${error.stack || error}`);
    if (!parsedArguments?.headless) {
        dialog.showErrorBox(`${PRODUCT_NAME} 安装程序`, error.message || String(error));
    }
    app.exit(1);
});

app.on('window-all-closed', () => app.quit());
