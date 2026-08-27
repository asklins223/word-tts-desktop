/**
 * Electron 主进程
 * =================
 * 1. 启动/管理 Python FastAPI 后端服务器（开发模式用系统 Python，打包模式用 PyInstaller 产物）
 * 2. 创建无边框窗口，加载 renderer/index.html
 * 3. 提供原生文件对话框（选择文件、保存文件）
 */

const {
    app,
    BrowserWindow,
    dialog,
    globalShortcut,
    ipcMain,
    shell,
} = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const net = require('net');
const { pathToFileURL } = require('url');
const { createNativeFileDialogs } = require('./file-dialogs');
const {
    createSettingsStore,
    DEFAULT_RECOVERY_ACCELERATOR,
} = require('./settings');

let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;
let serverPort = null;
let serverUrl = null;
let serverToken = null;
let serverInstance = null;
let desktopServicesReady = false;
let backendReady = false;
let backendProcessError = null;
let rendererReady = false;
let rendererFatalShown = false;
let pythonStopPromise = null;
let quitCleanupStarted = false;
let settingsStore = null;
let desktopSettings = null;
let settingsWriteTimer = null;
const pendingAppNotices = new Map();
const isSmokeTest = process.argv.includes('--smoke-test');
const PRODUCT_NAME = '小猪wordTTS';
// 必须和 word_tts_app.py 保持一致。启动时拒绝混用旧 PyInstaller 后端，
// 避免打包客户端表面启动成功、实际退回逐条生成的隐性性能问题。
const EXPECTED_BACKEND_CONTRACT_VERSION = 5;
const RENDERER_ENTRY_PATH = path.join(__dirname, 'renderer', 'index.html');
const RENDERER_ENTRY_URL = pathToFileURL(RENDERER_ENTRY_PATH).href;
const SMOKE_LOG_PATH = isSmokeTest
    ? path.join(os.tmpdir(), 'wordtts-electron-smoke.log')
    : null;
let smokeWatchdog = null;
let smokeExitRequested = false;

const WINDOW_MODES = new Set(['full', 'compact', 'hidden']);
const FULL_WINDOW_MIN_SIZE = Object.freeze({ width: 900, height: 600 });
const COMPACT_WINDOW_DEFAULT_SIZE = Object.freeze({ width: 520, height: 680 });
const COMPACT_WINDOW_MIN_SIZE = Object.freeze({ width: 420, height: 520 });
let appWindowMode = 'full';
let restoreWindowMode = 'full';
let fullWindowBounds = null;
let compactWindowBounds = null;
// 开发/故障排查可以通过 `--show` 请求已有实例恢复窗口；普通用户仍依赖
// 全局恢复快捷键和单实例唤醒，不把命令行当作唯一入口。
let pendingWindowRestore = process.argv.includes('--show');
let recoveryShortcutRegistered = false;
let configuredShortcutStatus = {};
let privacyWindowHidden = false;

// Electron 的全局快捷键只能报告本应用是否注册成功，不能可靠给出占用它的
// 其他程序名称。恢复快捷键因此是 POC 阶段的固定兜底，不允许被设置逻辑
// 完全移除；后续自定义设置也必须先注册新值再替换旧值。
let recoveryAccelerator = process.env.WORDTTS_RECOVERY_ACCELERATOR
    || DEFAULT_RECOVERY_ACCELERATOR;

function smokeLog(message) {
    if (!SMOKE_LOG_PATH) return;
    const line = `[${new Date().toISOString()}] ${message}\n`;
    try {
        fs.appendFileSync(SMOKE_LOG_PATH, line, 'utf8');
    } catch (_) { /* 日志失败不能影响应用启动 */ }
}

if (SMOKE_LOG_PATH) {
    try { fs.writeFileSync(SMOKE_LOG_PATH, '', 'utf8'); } catch (_) { /* ignore */ }
    process.on('uncaughtException', (error) => {
        smokeLog(`uncaughtException: ${error?.stack || error}`);
    });
    process.on('unhandledRejection', (reason) => {
        smokeLog(`unhandledRejection: ${reason?.stack || reason}`);
    });
}

// 冒烟测试只验证后端/渲染器启动，不需要 GPU 合成；Windows runner 的虚拟
// 显示驱动偶尔会让隐藏 BrowserWindow 的渲染探测不返回。禁用 GPU 只作用于
// --smoke-test，不影响用户正常运行时的硬件加速。
if (isSmokeTest) app.disableHardwareAcceleration();

// Branding changed in 2.0, but existing history and preferences must continue
// using the original on-disk directory instead of appearing to disappear.
try {
    const legacyUserDataPath = path.join(app.getPath('appData'), 'WordTTS');
    fs.mkdirSync(legacyUserDataPath, { recursive: true });
    app.setPath('userData', legacyUserDataPath);
} catch (error) {
    console.warn(`[main] 无法沿用旧用户数据目录，将使用系统默认目录: ${error.message}`);
}

// 桌面设置由主进程独占读写。启动时只把可恢复的窗口状态读入内存，
// 隐藏模式不会作为启动模式直接创建，避免再次触发“隐藏创建窗口”问题。
try {
    settingsStore = createSettingsStore(path.join(app.getPath('userData'), 'settings.json'));
    desktopSettings = settingsStore.load();
    const savedWindow = desktopSettings.window || {};
    fullWindowBounds = savedWindow.full_bounds || null;
    compactWindowBounds = savedWindow.compact_bounds || null;
    const savedMode = savedWindow.last_mode === 'compact' ? 'compact' : 'full';
    // startup_mode 是用户明确选择的启动偏好；last_mode 只作为旧版本
    // 设置的兼容回退，不能让一次临时的小窗切换永久改变启动偏好。
    appWindowMode = savedWindow.startup_mode === 'compact' ? 'compact' : 'full';
    restoreWindowMode = savedWindow.restore_mode === 'compact' ? 'compact' : (savedWindow.restore_mode === 'full' ? 'full' : (savedMode || appWindowMode));
    if (!process.env.WORDTTS_RECOVERY_ACCELERATOR) {
        recoveryAccelerator = desktopSettings.shortcuts?.recover || recoveryAccelerator;
    }
} catch (error) {
    console.warn(`[main] 无法加载桌面设置，将使用默认值: ${error.message}`);
}

// 隐藏窗口没有托盘图标时，第二次启动必须能唤醒已有实例；否则用户在快捷键
// 失效时没有可靠的普通用户恢复入口。锁必须在创建窗口和启动后端前取得。
const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
    app.quit();
} else {
    app.on('second-instance', () => {
        pendingWindowRestore = true;
        showMainWindow();
    });
}

function getWindowState() {
    const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
    return {
        mode: appWindowMode,
        restoreMode: restoreWindowMode,
        visible: Boolean(win && win.isVisible()),
        minimized: Boolean(win && win.isMinimized()),
        dockHidden: privacyWindowHidden,
        recoveryShortcut: {
            accelerator: recoveryAccelerator,
            registered: recoveryShortcutRegistered,
        },
        shortcuts: configuredShortcutStatus,
    };
}

function sendWindowState() {
    if (!rendererReady || !mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send('window:state', getWindowState());
}

function updateDesktopSettings(patch) {
    if (!settingsStore) return null;
    try {
        desktopSettings = settingsStore.update(patch);
        return desktopSettings;
    } catch (error) {
        console.error('[main] 保存桌面设置失败:', error.message);
        showInAppNotice('settings-write', {
            kicker: '设置保存',
            title: '设置未能保存',
            message: '本次设置只在当前运行期间生效，重启后可能恢复默认值。',
            detail: error.message,
            tone: 'warning',
        });
        return null;
    }
}

function persistWindowSettings() {
    if (!settingsStore) return;
    if (settingsWriteTimer) {
        clearTimeout(settingsWriteTimer);
        settingsWriteTimer = null;
    }
    const mode = appWindowMode === 'compact' ? 'compact' : restoreWindowMode;
    updateDesktopSettings({
        window: {
            startup_mode: desktopSettings?.window?.startup_mode === 'compact' ? 'compact' : 'full',
            last_mode: mode,
            restore_mode: restoreWindowMode,
            full_bounds: fullWindowBounds,
            compact_bounds: compactWindowBounds,
        },
    });
}

function scheduleWindowSettingsPersist() {
    if (!settingsStore || settingsWriteTimer) return;
    settingsWriteTimer = setTimeout(() => {
        settingsWriteTimer = null;
        persistWindowSettings();
    }, 350);
}

function saveCurrentWindowBounds() {
    if (!mainWindow || mainWindow.isDestroyed() || appWindowMode === 'hidden') return;
    const bounds = mainWindow.getBounds();
    if (appWindowMode === 'compact') compactWindowBounds = bounds;
    else if (appWindowMode === 'full') fullWindowBounds = bounds;
    scheduleWindowSettingsPersist();
}

function applyWindowModeBounds(mode) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mode === 'compact') {
        mainWindow.setMinimumSize(
            COMPACT_WINDOW_MIN_SIZE.width,
            COMPACT_WINDOW_MIN_SIZE.height,
        );
        if (compactWindowBounds) {
            mainWindow.setBounds(compactWindowBounds);
        } else {
            mainWindow.setSize(
                COMPACT_WINDOW_DEFAULT_SIZE.width,
                COMPACT_WINDOW_DEFAULT_SIZE.height,
            );
        }
        return;
    }
    mainWindow.setMinimumSize(FULL_WINDOW_MIN_SIZE.width, FULL_WINDOW_MIN_SIZE.height);
    if (fullWindowBounds) mainWindow.setBounds(fullWindowBounds);
}

function showMainWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) {
        pendingWindowRestore = true;
        return false;
    }
    let dockRestored = true;
    if (privacyWindowHidden && process.platform === 'darwin') {
        try {
            if (typeof app.dock?.show === 'function') app.dock.show();
            privacyWindowHidden = false;
        } catch (error) {
            dockRestored = false;
            console.error('[main] 恢复 macOS Dock 图标失败:', error.message);
        }
    }
    if (appWindowMode === 'hidden') {
        appWindowMode = restoreWindowMode;
        applyWindowModeBounds(appWindowMode);
        scheduleWindowSettingsPersist();
    }
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    pendingWindowRestore = false;
    mainWindow.focus();
    sendWindowState();
    return dockRestored;
}

function hideMainWindow(options = {}) {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    const privacy = options?.privacy === true;
    if (privacy && process.platform === 'darwin') {
        try {
            if (typeof app.dock?.hide === 'function') app.dock.hide();
            else throw new Error('当前 Electron 不支持隐藏 Dock 图标');
        } catch (error) {
            privacyWindowHidden = false;
            console.error('[main] 隐藏 macOS Dock 图标失败:', error.message);
            showInAppNotice('privacy-dock', {
                kicker: '防偷窥模式',
                title: '无法完成应用级隐藏',
                message: 'macOS 未允许隐藏 Dock 图标，防偷窥模式未开启。',
                detail: error.message,
                tone: 'warning',
            });
            return false;
        }
    }
    if (appWindowMode !== 'hidden') restoreWindowMode = appWindowMode;
    appWindowMode = 'hidden';
    privacyWindowHidden = privacy && process.platform === 'darwin';
    mainWindow.hide();
    scheduleWindowSettingsPersist();
    sendWindowState();
    return true;
}

function setWindowMode(mode) {
    if (!WINDOW_MODES.has(mode)) return { success: false, reason: 'invalid-mode' };
    if (!mainWindow || mainWindow.isDestroyed()) {
        pendingWindowRestore = mode !== 'hidden';
        return { success: false, reason: 'window-unavailable' };
    }

    if (mode === 'hidden') {
        const hidden = hideMainWindow();
        return { success: hidden, state: getWindowState() };
    }

    if (appWindowMode !== 'hidden') saveCurrentWindowBounds();
    appWindowMode = mode;
    restoreWindowMode = mode;
    applyWindowModeBounds(mode);
    scheduleWindowSettingsPersist();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
    sendWindowState();
    return { success: true, state: getWindowState() };
}

function registerRecoveryShortcut() {
    if (recoveryShortcutRegistered) return true;
    if (!singleInstanceLock) return false;
    try {
        recoveryShortcutRegistered = globalShortcut.register(
            recoveryAccelerator,
            () => { showMainWindow(); },
        );
    } catch (error) {
        recoveryShortcutRegistered = false;
        console.error('[main] 注册恢复快捷键失败:', error.message);
    }
    if (!recoveryShortcutRegistered) {
        showInAppNotice('recovery-shortcut', {
            kicker: '窗口恢复',
            title: '恢复快捷键未注册',
            message: `无法注册 ${recoveryAccelerator}，请使用再次启动应用的方式恢复窗口。`,
            detail: '系统通常不会提供占用该快捷键的其他程序名称。',
            tone: 'warning',
        });
    }
    sendWindowState();
    return recoveryShortcutRegistered;
}

const configuredShortcutAccelerators = new Map();
const CONFIGURED_SHORTCUT_ACTIONS = {
    privacy: 'privacy-toggle',
    pause_resume: 'task-pause-resume',
    terminate: 'task-terminate',
    compact: 'compact-toggle',
};

function sendRendererShortcut(action) {
    if (!rendererReady || !mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send('global-shortcut', { action });
}

function unregisterConfiguredShortcuts() {
    configuredShortcutAccelerators.forEach((accelerator) => {
        try { globalShortcut.unregister(accelerator); } catch (_) { /* ignore */ }
    });
    configuredShortcutAccelerators.clear();
    configuredShortcutStatus = {};
}

function registerConfiguredShortcuts() {
    unregisterConfiguredShortcuts();
    const shortcuts = desktopSettings?.shortcuts || {};
    Object.entries(CONFIGURED_SHORTCUT_ACTIONS).forEach(([key, action]) => {
        const accelerator = String(shortcuts[key] || '').trim();
        if (!accelerator || accelerator === recoveryAccelerator) {
            configuredShortcutStatus[key] = {
                accelerator,
                registered: false,
                reason: accelerator === recoveryAccelerator ? '与恢复快捷键冲突' : '未设置',
            };
            return;
        }
        let registered = false;
        try {
            registered = globalShortcut.register(accelerator, () => sendRendererShortcut(action));
        } catch (error) {
            configuredShortcutStatus[key] = {
                accelerator,
                registered: false,
                reason: error.message,
            };
            return;
        }
        configuredShortcutStatus[key] = {
            accelerator,
            registered,
            reason: registered ? '' : '系统快捷键已被占用或不可用',
        };
        if (registered) configuredShortcutAccelerators.set(key, accelerator);
    });
    sendWindowState();
    return configuredShortcutStatus;
}

function replaceRecoveryShortcut(accelerator) {
    const next = String(accelerator || '').trim();
    if (!next) return { success: false, reason: '恢复快捷键不能为空' };
    const sameAccelerator = next === recoveryAccelerator;
    if (sameAccelerator && recoveryShortcutRegistered) {
        return { success: true, registered: true };
    }
    let registered = false;
    try {
        // 先注册新值，旧值仍然有效时即使失败也不会丢失恢复入口。
        registered = globalShortcut.register(next, () => { showMainWindow(); });
    } catch (error) {
        return { success: false, reason: error.message };
    }
    if (!registered) return { success: false, reason: '系统快捷键已被占用或不可用' };
    // 同一个 accelerator 可能是“状态丢失后重试注册”的路径；此时不能
    // 在注册成功后再 unregister 同一按键，否则会把刚恢复的兜底快捷键
    // 立即注销掉。
    if (!sameAccelerator) {
        try { globalShortcut.unregister(recoveryAccelerator); } catch (_) { /* ignore */ }
    }
    recoveryAccelerator = next;
    recoveryShortcutRegistered = true;
    registerConfiguredShortcuts();
    sendWindowState();
    return { success: true, registered: true };
}

function restoreRecoveryShortcutState(accelerator, shouldBeRegistered) {
    const target = String(accelerator || '').trim() || DEFAULT_RECOVERY_ACCELERATOR;
    if (recoveryShortcutRegistered) {
        try { globalShortcut.unregister(recoveryAccelerator); } catch (_) { /* ignore */ }
    }
    recoveryAccelerator = target;
    recoveryShortcutRegistered = false;
    if (shouldBeRegistered && singleInstanceLock) {
        try {
            recoveryShortcutRegistered = globalShortcut.register(
                recoveryAccelerator,
                () => { showMainWindow(); },
            );
        } catch (error) {
            console.error('[main] 回滚恢复快捷键失败:', error.message);
        }
    }
    registerConfiguredShortcuts();
    sendWindowState();
}

function getSettingsSnapshot() {
    return desktopSettings && typeof desktopSettings === 'object'
        ? JSON.parse(JSON.stringify(desktopSettings))
        : null;
}

function applySettingsPatch(patch) {
    if (!settingsStore || !patch || typeof patch !== 'object' || Array.isArray(patch)) {
        return { success: false, reason: 'invalid-settings' };
    }
    const current = getSettingsSnapshot() || {};
    const proposedShortcuts = settingsStore.normalize({
        shortcuts: {
            ...(current.shortcuts || {}),
            ...(
                patch.shortcuts && typeof patch.shortcuts === 'object' && !Array.isArray(patch.shortcuts)
                    ? patch.shortcuts
                    : {}
            ),
        },
    }).shortcuts;
    const nextRecovery = proposedShortcuts.recover;
    const previousRecovery = recoveryAccelerator;
    const previousRecoveryRegistered = recoveryShortcutRegistered;
    const needsRecoveryRegistration = (
        nextRecovery !== recoveryAccelerator || !recoveryShortcutRegistered
    );
    if (needsRecoveryRegistration) {
        const replaced = replaceRecoveryShortcut(nextRecovery);
        if (!replaced.success) return replaced;
    }
    const updated = updateDesktopSettings(patch);
    if (!updated) {
        if (needsRecoveryRegistration) {
            restoreRecoveryShortcutState(previousRecovery, previousRecoveryRegistered);
        }
        return { success: false, reason: 'settings-write-failed' };
    }
    registerConfiguredShortcuts();
    return { success: true, settings: getSettingsSnapshot() };
}

function resetDesktopSettings() {
    if (!settingsStore) return { success: false, reason: 'settings-unavailable' };
    const defaults = settingsStore.normalize({});
    const previousRecovery = recoveryAccelerator;
    const previousRecoveryRegistered = recoveryShortcutRegistered;
    const replaced = replaceRecoveryShortcut(defaults.shortcuts.recover);
    if (!replaced.success) return replaced;
    // “恢复默认”只重置偏好，不丢掉可用于页面恢复提示的运行时会话索引。
    // 生成中的任务和输出文件由后端生命周期管理，不能因为设置重置而失去入口。
    const activeSession = desktopSettings?.runtime?.active_session || null;
    try {
        desktopSettings = settingsStore.replace(activeSession
            ? { ...defaults, runtime: { active_session: activeSession } }
            : defaults);
    } catch (error) {
        if (defaults.shortcuts.recover !== previousRecovery || replaced.registered !== previousRecoveryRegistered) {
            restoreRecoveryShortcutState(previousRecovery, previousRecoveryRegistered);
        }
        return { success: false, reason: error.message };
    }
    fullWindowBounds = desktopSettings.window.full_bounds;
    compactWindowBounds = desktopSettings.window.compact_bounds;
    restoreWindowMode = desktopSettings.window.restore_mode || 'full';
    appWindowMode = 'full';
    if (mainWindow && !mainWindow.isDestroyed()) {
        applyWindowModeBounds('full');
        if (!mainWindow.isVisible()) mainWindow.show();
    }
    registerConfiguredShortcuts();
    sendWindowState();
    return { success: true, settings: getSettingsSnapshot() };
}

function showRendererFatalError(title, detail) {
    if (rendererFatalShown || isQuitting) return;
    rendererFatalShown = true;
    if (isSmokeTest) {
        console.error(`[main] ${PRODUCT_NAME} ${title}: ${detail}`);
        return;
    }
    dialog.showErrorBox(
        `${PRODUCT_NAME} ${title}`,
        `${detail}\n\n请重新启动应用；如果问题持续存在，请重新安装完整版本。`,
    );
}

function showInAppNotice(id, notice) {
    const payload = {
        kicker: String(notice?.kicker || '应用消息'),
        title: String(notice?.title || `${PRODUCT_NAME} 提示`),
        message: String(notice?.message || '应用遇到一个需要处理的问题。'),
        detail: String(notice?.detail || ''),
        tone: ['info', 'success', 'warning', 'danger'].includes(notice?.tone) ? notice.tone : 'danger',
        confirmLabel: String(notice?.confirmLabel || '知道了'),
    };
    if (rendererReady && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('app-notice', payload);
        return;
    }
    // Keep the earliest, most specific startup failure instead of replacing it
    // with a later generic readiness timeout that reports the same incident.
    if (!pendingAppNotices.has(id)) pendingAppNotices.set(id, payload);
}

function flushAppNotices() {
    if (!rendererReady || !mainWindow || mainWindow.isDestroyed()) return;
    pendingAppNotices.forEach(notice => mainWindow.webContents.send('app-notice', notice));
    pendingAppNotices.clear();
}

// ============================================================================
// Python 服务器管理
// ============================================================================

function findPython() {
    const isWin = process.platform === 'win32';
    const candidates = [
        process.env.PYTHON_CMD,
        // macOS / Linux
        '/usr/local/bin/python3',
        '/opt/homebrew/bin/python3',
        '/opt/homebrew/bin/python3.11',
        '/usr/bin/python3',
        // Windows (常见安装路径)
        ...(isWin ? [
            'C:\\Python311\\python.exe',
            'C:\\Python310\\python.exe',
            'C:\\Python39\\python.exe',
            `${process.env.LOCALAPPDATA}\\Programs\\Python\\Python311\\python.exe`,
            `${process.env.LOCALAPPDATA}\\Programs\\Python\\Python310\\python.exe`,
            `${process.env.LOCALAPPDATA}\\Programs\\Python\\Python39\\python.exe`,
        ] : []),
    ].filter(Boolean);

    for (const cmd of candidates) {
        if (cmd && (cmd.startsWith('/') || cmd.startsWith('C:') || cmd.includes('\\'))) {
            try {
                if (fs.existsSync(cmd)) {
                    console.log(`[main] 找到 Python: ${cmd}`);
                    return cmd;
                }
            } catch (e) { /* ignore */ }
        }
    }
    console.log(`[main] 未找到绝对路径的 Python，使用 ${isWin ? 'python' : 'python3'}`);
    return isWin ? 'python' : 'python3';
}

/**
 * 获取后端服务器可执行文件路径。
 * - 打包模式: process.resourcesPath/server_backend/server_backend (.exe on Windows)
 * - 开发模式: 使用系统 python + server.py
 */
function getServerCommand() {
    if (app.isPackaged) {
        const exeName = process.platform === 'win32' ? 'server_backend.exe' : 'server_backend';
        const serverExe = path.join(process.resourcesPath, 'server_backend', exeName);
        return { cmd: serverExe, args: [], cwd: path.dirname(serverExe) };
    }
    const projectRoot = path.resolve(__dirname, '..');
    const pythonCmd = findPython();
    return { cmd: pythonCmd, args: ['server.py'], cwd: projectRoot };
}

function allocateServerPort() {
    return new Promise((resolve, reject) => {
        const probe = net.createServer();
        probe.unref();
        probe.once('error', reject);
        probe.listen(0, '127.0.0.1', () => {
            const address = probe.address();
            const port = address && typeof address === 'object' ? address.port : null;
            probe.close((err) => {
                if (err) reject(err);
                else if (!port) reject(new Error('无法分配本地服务端口'));
                else resolve(port);
            });
        });
    });
}

function startPythonServer() {
    const { cmd, args, cwd } = getServerCommand();

    if (!serverPort || !serverToken) {
        throw new Error('后端端口或会话令牌尚未初始化');
    }

    backendReady = false;
    backendProcessError = null;

    console.log(`[main] 启动后端服务器: ${cmd} ${args.join(' ')} (cwd: ${cwd})`);
    smokeLog(`start backend: ${cmd} ${args.join(' ')}`);

    // 校验可执行文件存在（打包模式）
    if (app.isPackaged && !fs.existsSync(cmd)) {
        const msg = `后端可执行文件不存在:\n${cmd}\n\n应用可能已损坏，请重新安装。`;
        console.error(`[main] ${msg}`);
        throw new Error(`应用缺少生成服务组件：${cmd}\n请重新安装完整版本后再试。`);
    }

    // 根据平台设置 PATH（确保后端能找到所需工具）
    const extraPath = process.platform === 'win32'
        ? `${process.env.SystemRoot}\\System32;${process.env.SystemRoot};${process.env.PATH || ''}`
        : `/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${process.env.PATH || ''}`;

    pythonProcess = spawn(cmd, args, {
        cwd: cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
            ...process.env,
            PATH: extraPath,
            WORDTTS_PORT: String(serverPort),
            WORDTTS_API_TOKEN: serverToken,
            WORDTTS_VERSION: app.getVersion(),
            WORDTTS_DATA_DIR: app.getPath('userData'),
            // 打包后复用 Electron 自带的 Node 启动 Playwright driver，
            // 避免在 Python 后端中再携带一份约 106MB 的 node/node.exe。
            ...(app.isPackaged ? {
                PLAYWRIGHT_NODEJS_PATH: process.execPath,
                ELECTRON_RUN_AS_NODE: '1',
            } : {}),
        },
        // Windows 下隐藏控制台窗口（后端输出通过 pipe 捕获）
        windowsHide: true,
    });

    // 收集 stderr 输出，用于崩溃时显示错误详情
    let stderrBuffer = '';
    const MAX_ERR_LINES = 30;
    const forwardBackendStdout = process.argv.includes('--dev')
        || process.env.WORDTTS_DEBUG_LOGS === '1';

    pythonProcess.stdout.on('data', (data) => {
        // 始终消费 stdout 防止后端管道背压；普通运行不把每条讯飞日志
        // 再复制到 Electron 主进程控制台，降低长任务期间的日志开销。
        if (forwardBackendStdout) {
            console.log(`[python] ${data.toString().trim()}`);
        }
    });

    pythonProcess.stderr.on('data', (data) => {
        const text = data.toString();
        console.error(`[python:err] ${text.trim()}`);
        // 保留最后 MAX_ERR_LINES 行用于错误提示
        stderrBuffer = (stderrBuffer + text).split('\n').slice(-MAX_ERR_LINES).join('\n');
    });

    pythonProcess.on('error', (err) => {
        backendReady = false;
        if (!backendProcessError) backendProcessError = err;
        console.error(`[main] 无法启动后端进程: ${err.message}`);
        showInAppNotice('backend-start', {
            kicker: '生成服务',
            title: '后端启动失败',
            message: '无法启动本机生成服务，请确认应用安装完整后重试。',
            detail: `${err.message}\n可执行文件：${cmd}`,
            tone: 'danger',
        });
    });

    pythonProcess.on('exit', (code) => {
        const wasBackendReady = backendReady;
        backendReady = false;
        if (!wasBackendReady && !backendProcessError) {
            backendProcessError = new Error(`后端进程在服务就绪前退出（代码 ${code ?? '未知'}）`);
        }
        console.log(`[main] 后端进程退出，代码: ${code}`);
        smokeLog(`backend exit: ${code}`);
        pythonProcess = null;
        // 非正常退出且不是用户主动关闭时，提示用户
        if (code !== 0 && code !== null && !isQuitting) {
            const errDetail = stderrBuffer.trim()
                ? `\n\n--- 后端错误日志（最后 ${MAX_ERR_LINES} 行）---\n${stderrBuffer.trim()}`
                : '';
            showInAppNotice('backend-exit', {
                kicker: '生成服务',
                title: '后端异常退出',
                message: `生成服务已停止（代码 ${code}），当前任务无法继续。`,
                detail: errDetail
                    ? `请重新连接或重启应用。${errDetail}`
                    : '请尝试重新连接生成服务，或重启应用。',
                tone: 'danger',
            });
        }
    });
}

/**
 * 异步停止 Python 服务器，确保进程完全退出后再 resolve。
 * 用于 will-quit 事件中阻塞应用退出，防止僵尸进程。
 */
function stopPythonServerAsync() {
    // will-quit、冒烟测试失败分支和窗口关闭可能同时触发清理；复用同一
    // Promise，避免 Windows 下重复 taskkill 后又被 will-quit 重新拦截。
    backendReady = false;
    if (pythonStopPromise) return pythonStopPromise;
    if (!pythonProcess) return Promise.resolve();

    const proc = pythonProcess;
    pythonStopPromise = new Promise((resolve) => {
        let resolved = false;
        let forceStarted = false;
        let forceTimer = null;
        let safetyTimer = null;

        const clearTimers = () => {
            if (forceTimer) clearTimeout(forceTimer);
            if (safetyTimer) clearTimeout(safetyTimer);
            forceTimer = null;
            safetyTimer = null;
        };

        const done = () => {
            if (resolved) return;
            resolved = true;
            clearTimers();
            // Windows 的 taskkill 可能先于 Node 的 exit 事件返回。先释放
            // 全局引用，防止 app.exit() 再次进入 will-quit 清理死循环。
            if (pythonProcess === proc) pythonProcess = null;
            resolve();
        };

        const forceTerminate = () => {
            if (resolved || forceStarted) return;
            forceStarted = true;

            if (process.platform === 'win32' && proc.pid) {
                // child.kill('SIGKILL') 在 Windows 对带有子进程的打包后端
                // 不一定能清理完整进程树；taskkill /T /F 可以同时结束后端
                // 及其可能残留的 Playwright/Chromium 子进程。
                try {
                    const killer = spawn(
                        'taskkill',
                        ['/PID', String(proc.pid), '/T', '/F'],
                        { windowsHide: true, stdio: 'ignore' },
                    );
                    killer.once('error', done);
                    killer.once('close', () => setTimeout(done, 250));
                } catch (_) {
                    done();
                }
                return;
            }

            try { proc.kill('SIGKILL'); } catch (_) { /* already stopped */ }
            setTimeout(done, 500);
        };

        proc.once('exit', done);
        proc.once('error', done);
        try {
            if (!proc.kill('SIGTERM')) forceTerminate();
        } catch (_) {
            forceTerminate();
        }

        // 正常退出给 3 秒；Windows 超时后按进程树强制结束。
        forceTimer = setTimeout(forceTerminate, 3000);
        // 即使系统没有及时派发 exit/close 事件，也不能让 Electron 永久
        // 卡在 will-quit；强杀请求已发出后最多再等 3 秒释放退出流程。
        safetyTimer = setTimeout(done, 6000);
    });
    return pythonStopPromise;
}

function waitForServer(timeoutMs = 90000) {
    return new Promise((resolve, reject) => {
        const deadline = Date.now() + timeoutMs;
        let settled = false;
        const check = () => {
            if (backendProcessError) {
                settled = true;
                reject(backendProcessError);
                return;
            }
            if (!serverUrl || !serverToken || !pythonProcess) {
                retry();
                return;
            }
            let attemptDone = false;
            const completeAttempt = (ready) => {
                if (attemptDone || settled) return;
                attemptDone = true;
                if (ready) {
                    settled = true;
                    resolve();
                } else {
                    retry();
                }
            };
            const req = http.get(`${serverUrl}/api/health`, {
                headers: { 'X-WordTTS-Token': serverToken },
            }, (res) => {
                let body = '';
                res.setEncoding('utf8');
                res.on('data', (chunk) => { body += chunk; });
                res.on('error', () => completeAttempt(false));
                res.on('end', () => {
                    try {
                        const health = JSON.parse(body);
                        if (
                            res.statusCode === 200
                            && health.app === 'wordtts'
                            && health.instance === serverInstance
                        ) {
                            if (health.backend_contract_version !== EXPECTED_BACKEND_CONTRACT_VERSION) {
                                settled = true;
                                reject(new Error(
                                    `后端版本不匹配：期望协议 ${EXPECTED_BACKEND_CONTRACT_VERSION}，`
                                    + `实际 ${health.backend_contract_version ?? '未知'}。`
                                    + '请重新安装同一版本的完整客户端。',
                                ));
                                return;
                            }
                            completeAttempt(true);
                            return;
                        }
                    } catch (e) { /* 服务尚未就绪或响应并非小猪wordTTS */ }
                    completeAttempt(false);
                });
            });
            req.on('error', () => completeAttempt(false));
            req.setTimeout(1500, () => {
                req.destroy();
                completeAttempt(false);
            });
        };
        const retry = () => {
            if (settled) return;
            if (Date.now() >= deadline) {
                settled = true;
                reject(new Error('服务器启动超时'));
            }
            else setTimeout(check, 500);
        };
        check();
    });
}

function requestBackendJson(route, method = 'GET', payload = undefined) {
    return new Promise((resolve, reject) => {
        if (!serverUrl || !serverToken || !pythonProcess) {
            reject(new Error('生成服务尚未就绪'));
            return;
        }
        let target;
        try {
            target = new URL(route, serverUrl);
            target.searchParams.set('token', serverToken);
        } catch (error) {
            reject(error);
            return;
        }
        const body = payload === undefined ? null : JSON.stringify(payload);
        let settled = false;
        const resolveOnce = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
        };
        const rejectOnce = (error) => {
            if (settled) return;
            settled = true;
            reject(error);
        };
        const request = http.request(target, {
            method,
            headers: {
                'X-WordTTS-Token': serverToken,
                ...(body ? {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(body),
                } : {}),
            },
        }, (response) => {
            let raw = '';
            response.setEncoding('utf8');
            response.on('data', chunk => { raw += chunk; });
            response.on('end', () => {
                let data = null;
                try { data = raw ? JSON.parse(raw) : null; } catch (_) { /* non-JSON error */ }
                resolveOnce({
                    ok: response.statusCode >= 200 && response.statusCode < 300,
                    status: response.statusCode || 0,
                    data,
                });
            });
            response.on('error', rejectOnce);
        });
        request.setTimeout(5000, () => request.destroy(new Error('生成服务响应超时')));
        request.on('error', rejectOnce);
        if (body) request.write(body);
        request.end();
    });
}

// ============================================================================
// 窗口创建
// ============================================================================

function createWindow() {
    if (mainWindow && !mainWindow.isDestroyed()) {
        showMainWindow();
        return mainWindow;
    }

    const isWin = process.platform === 'win32';
    const isMac = process.platform === 'darwin';

    const windowOptions = {
        width: 1280,
        height: 860,
        minWidth: 900,
        minHeight: 600,
        // Windows runner 中隐藏 BrowserWindow 偶发触发 Chromium renderer
        // 访问冲突（0xC0000005）；冒烟测试使用可见窗口，正常运行保持可见。
        show: !isSmokeTest || process.platform === 'win32',
        title: PRODUCT_NAME,
        transparent: false,
        backgroundColor: '#f8fafd',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    };

    if (isMac) {
        // macOS: 完全去除标题栏，保留原生交通灯按钮（关闭/最小化/全屏）
        windowOptions.frame = false;
        windowOptions.titleBarStyle = 'hidden';
        windowOptions.trafficLightPosition = { x: 16, y: 18 };
    } else if (isWin) {
        // Windows: 使用 hidden + overlay 保留原生窗口控件（最小化/最大化/关闭）
        // titleBarOverlay 让 Windows 渲染原生控件按钮在右上角
        windowOptions.titleBarStyle = 'hidden';
        windowOptions.titleBarOverlay = {
            color: '#f8fafd',
            symbolColor: '#172033',
            height: 40,
        };
        windowOptions.autoHideMenuBar = true;
    } else {
        // Linux: 使用原生框架
        windowOptions.frame = true;
    }

    const win = new BrowserWindow(windowOptions);
    mainWindow = win;
    // 应用创建后再套用上次保存的边界，避免把隐藏模式作为 BrowserWindow
    // 的初始 show:false；小窗只在这里切尺寸，仍然复用同一个 renderer。
    applyWindowModeBounds(appWindowMode);
    smokeLog(`window created: show=${windowOptions.show}`);
    rendererReady = false;
    rendererFatalShown = false;

    win.webContents.on('will-navigate', (event, navigationUrl) => {
        if (navigationUrl === RENDERER_ENTRY_URL) return;
        event.preventDefault();
        console.warn(`[main] 已阻止界面导航到非本地地址: ${navigationUrl}`);
    });
    win.webContents.setWindowOpenHandler(({ url }) => {
        console.warn(`[main] 已阻止界面打开新窗口: ${url}`);
        return { action: 'deny' };
    });
    win.loadFile(RENDERER_ENTRY_PATH);
    win.webContents.on('did-finish-load', () => {
        if (mainWindow !== win) return;
        smokeLog('renderer did-finish-load');
        rendererReady = true;
        flushAppNotices();
        sendWindowState();
        if (pendingWindowRestore) showMainWindow();
    });
    win.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
        if (!isMainFrame || code === -3) return;
        smokeLog(`renderer did-fail-load: ${code} ${description} ${url || ''}`);
        if (mainWindow === win) rendererReady = false;
        showRendererFatalError('界面加载失败', `${description}（错误代码 ${code}）\n${url || '本地界面'}`);
    });
    win.webContents.on('render-process-gone', (_event, details) => {
        smokeLog(`renderer process gone: ${JSON.stringify(details)}`);
        if (mainWindow === win) rendererReady = false;
        if (details.reason === 'clean-exit') return;
        showRendererFatalError('界面进程异常退出', `退出原因：${details.reason}`);
    });

    if (process.argv.includes('--dev')) {
        win.webContents.openDevTools({ mode: 'detach' });
    }

    win.on('closed', () => {
        if (mainWindow !== win) return;
        rendererReady = false;
        mainWindow = null;
        sendWindowState();
    });

    win.on('resize', () => {
        saveCurrentWindowBounds();
        sendWindowState();
    });
    win.on('show', sendWindowState);
    win.on('hide', sendWindowState);
    win.on('minimize', sendWindowState);
    win.on('restore', sendWindowState);

    return win;
}

async function verifyRendererSmokeTest(win, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let lastState = null;
    while (Date.now() < deadline) {
        if (!win || win.isDestroyed()) throw new Error('界面窗口在冒烟测试期间提前关闭');
        try {
            const statePromise = win.webContents.executeJavaScript(`(() => ({
                title: document.title,
                nativeApi: Boolean(window.electronAPI),
                backendUrl: window.electronAPI?.backend?.url || '',
                backendToken: Boolean(window.electronAPI?.backend?.token),
                uiComponents: Boolean(window.WordTTSUI),
                serviceState: document.getElementById('service-state')?.textContent?.trim() || ''
            }))()`);
            let probeTimer = null;
            const timeoutPromise = new Promise((_, reject) => {
                probeTimer = setTimeout(() => reject(new Error('渲染器状态探测超时')), 2500);
            });
            try {
                lastState = await Promise.race([statePromise, timeoutPromise]);
            } finally {
                if (probeTimer) clearTimeout(probeTimer);
            }
            smokeLog(`renderer state: ${JSON.stringify(lastState)}`);
            if (
                lastState.title.includes(PRODUCT_NAME)
                && lastState.nativeApi
                && lastState.backendUrl === serverUrl
                && lastState.backendToken
                && lastState.uiComponents
                && lastState.serviceState === '服务已连接'
            ) {
                return lastState;
            }
        } catch (error) {
            lastState = { error: error.message };
            smokeLog(`renderer probe failed: ${error.message}`);
        }
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`界面未在限定时间内就绪: ${JSON.stringify(lastState)}`);
}

function exitSmokeTest(code, win = null) {
    if (!isSmokeTest || smokeExitRequested) return;
    smokeExitRequested = true;
    if (smokeWatchdog) {
        clearTimeout(smokeWatchdog);
        smokeWatchdog = null;
    }
    smokeLog(`exit requested: ${code}`);
    stopPythonServerAsync().then(() => {
        if (win && !win.isDestroyed()) win.destroy();
        app.exit(code);
    });
}

// ============================================================================
// IPC 注册
// ============================================================================

function getAllowedFileRoots() {
    const userDataDir = app.getPath('userData');
    if (!app.isPackaged) return [path.resolve(__dirname, '..'), userDataDir];

    const home = app.getPath('home');
    let dataDir;
    if (process.platform === 'darwin') {
        dataDir = path.join(home, 'Library', 'Application Support', 'WordTTS');
    } else if (process.platform === 'win32') {
        dataDir = path.join(app.getPath('appData'), 'WordTTS');
    } else {
        dataDir = path.join(home, '.wordtts');
    }
    return [dataDir, userDataDir, path.join(process.resourcesPath, 'server_backend')];
}

function isAllowedFilePath(filePath) {
    if (typeof filePath !== 'string' || !filePath) return false;
    try {
        // realpath 同时封住“允许目录内的符号链接指向目录外文件”的绕过方式。
        const source = fs.realpathSync.native(filePath);
        return getAllowedFileRoots().some((root) => {
            const canonicalRoot = fs.existsSync(root) ? fs.realpathSync.native(root) : path.resolve(root);
            const normalize = (value) => process.platform === 'win32' ? value.toLowerCase() : value;
            const normalizedSource = normalize(source);
            const normalizedRoot = normalize(canonicalRoot);
            const relative = path.relative(normalizedRoot, normalizedSource);
            return relative === '' || (
                relative !== '..'
                && !relative.startsWith(`..${path.sep}`)
                && !path.isAbsolute(relative)
            );
        });
    } catch (err) {
        return false;
    }
}

function isTrustedRendererEvent(event) {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    if (!event?.sender || !event?.senderFrame) return false;
    if (event.sender !== mainWindow.webContents) return false;
    if (event.senderFrame !== mainWindow.webContents.mainFrame) return false;
    return event.senderFrame.url === RENDERER_ENTRY_URL;
}

function registerIpcHandlers() {
    const nativeFileDialogs = createNativeFileDialogs({
        app,
        BrowserWindow,
        dialog,
        fs,
        isAllowedFilePath,
        getMainWindow: () => mainWindow,
        isTrustedSender: isTrustedRendererEvent,
    });

    ipcMain.on('backend-config', (event) => {
        event.returnValue = isTrustedRendererEvent(event)
            ? { url: serverUrl, token: serverToken }
            : null;
    });

    ipcMain.handle('window:get-state', (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return { success: true, state: getWindowState() };
    });

    ipcMain.handle('window:set-mode', (event, mode) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return setWindowMode(mode);
    });

    ipcMain.handle('window:hide', (event, options = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return {
            success: hideMainWindow({ privacy: options?.privacy === true }),
            state: getWindowState(),
        };
    });

    ipcMain.handle('window:show', (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return { success: showMainWindow(), state: getWindowState() };
    });

    ipcMain.handle('settings:get', (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return { success: true, settings: getSettingsSnapshot() };
    });

    ipcMain.handle('settings:update', (event, patch) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return applySettingsPatch(patch);
    });

    ipcMain.handle('settings:reset', (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        return resetDesktopSettings();
    });

    ipcMain.handle('settings:import-legacy', (event, legacy) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        if (desktopSettings?.migrations?.legacy_local_storage) {
            return { success: true, migrated: false, settings: getSettingsSnapshot() };
        }
        const payload = legacy && typeof legacy === 'object' ? legacy : {};
        const result = applySettingsPatch({
            tts: {
                current_config: payload.current_config && typeof payload.current_config === 'object'
                    ? payload.current_config : null,
                presets: Array.isArray(payload.presets) ? payload.presets : [],
            },
            migrations: { legacy_local_storage: true },
        });
        if (!result.success) return result;
        return { ...result, migrated: true };
    });

    const browserSessionRoute = (sessionId, suffix = '') => {
        const id = String(sessionId || '').trim();
        if (!/^[A-Za-z0-9._-]{1,220}$/.test(id)) return null;
        return `/api/session/${encodeURIComponent(id)}/browser${suffix}`;
    };

    const taskSessionRoute = (sessionId, action) => {
        const id = String(sessionId || '').trim();
        if (!/^[A-Za-z0-9._-]{1,220}$/.test(id)) return null;
        if (!['pause', 'resume', 'terminate'].includes(action)) return null;
        return `/api/session/${encodeURIComponent(id)}/${action}`;
    };

    ['pause', 'resume', 'terminate'].forEach((action) => {
        ipcMain.handle(`task:${action}`, async (event, sessionId) => {
            if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
            const route = taskSessionRoute(sessionId, action);
            if (!route) return { success: false, reason: 'invalid-session-id' };
            try {
                return await requestBackendJson(route, 'POST');
            } catch (error) {
                return { ok: false, status: 0, data: { detail: error.message } };
            }
        });
    });

    ipcMain.handle('browser:get-state', async (event, sessionId) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const route = browserSessionRoute(sessionId);
        if (!route) return { success: false, reason: 'invalid-session-id' };
        try {
            return await requestBackendJson(route);
        } catch (error) {
            return { ok: false, status: 0, data: { detail: error.message } };
        }
    });

    ipcMain.handle('browser:show', async (event, sessionId, options = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const route = browserSessionRoute(sessionId, '/show');
        if (!route) return { success: false, reason: 'invalid-session-id' };
        try {
            return await requestBackendJson(route, 'POST', {
                minimize: options?.minimize === true,
            });
        } catch (error) {
            return { ok: false, status: 0, data: { detail: error.message } };
        }
    });

    ipcMain.handle('browser:hide', async (event, sessionId) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const route = browserSessionRoute(sessionId, '/hide');
        if (!route) return { success: false, reason: 'invalid-session-id' };
        try {
            return await requestBackendJson(route, 'POST');
        } catch (error) {
            return { ok: false, status: 0, data: { detail: error.message } };
        }
    });

    // 选择文件
    ipcMain.handle('select-file', nativeFileDialogs.selectFile);

    // 通过文件路径直接复制（校验源路径合法性）
    ipcMain.handle('save-file-by-path', nativeFileDialogs.saveFileByPath);

    // 检查服务器是否就绪
    ipcMain.handle('server-ready', async (event) => {
        if (!isTrustedRendererEvent(event)) return false;
        if (!pythonProcess) return false;
        // 启动阶段已经由主进程完成过一次带 token、instance 和协议版本
        // 校验的健康检查。渲染器随后再发起一次相同检查会制造无意义的
        // 竞态（尤其是 macOS CI 刚创建 renderer 的窗口时），直接复用
        // 已验证状态；后端进程退出时由 exit handler 将它清回 false。
        if (backendReady) return true;
        try {
            await waitForServer();
            backendReady = true;
            return true;
        } catch (error) {
            smokeLog(`server-ready failed: ${error.stack || error.message}`);
            return false;
        }
    });

    // 在 Finder 中显示
    ipcMain.handle('show-in-folder', async (event, filePath) => {
        if (!isTrustedRendererEvent(event)) return false;
        if (isAllowedFilePath(filePath)) {
            shell.showItemInFolder(filePath);
            return true;
        }
        return false;
    });

    console.log('[main] IPC handlers registered');
}

// ============================================================================
// 应用生命周期
// ============================================================================

app.whenReady().then(async () => {
    if (!singleInstanceLock) return;
    console.log('[main] Electron app ready');
    smokeLog('app ready');
    backendReady = false;
    if (isSmokeTest) {
        // CI 中若渲染器或 Electron 子进程完全不响应，也必须在有限时间内
        // 输出诊断并退出，不能让 PowerShell 永久等待。
        smokeWatchdog = setTimeout(() => {
            smokeLog('smoke watchdog timeout');
            console.error('[main] 桌面界面端到端冒烟测试 watchdog 超时');
            exitSmokeTest(2, mainWindow);
        }, 45000);
    }

    try {
        serverPort = await allocateServerPort();
        serverUrl = `http://127.0.0.1:${serverPort}`;
        serverToken = crypto.randomBytes(32).toString('hex');
        serverInstance = crypto.createHash('sha256').update(serverToken).digest('hex').slice(0, 16);
        console.log(`[main] 已分配独立后端地址: ${serverUrl}`);

        registerIpcHandlers();
        desktopServicesReady = true;
        startPythonServer();
    } catch (error) {
        console.error('[main] 初始化桌面服务失败:', error.stack || error.message);
        if (isSmokeTest) {
            smokeLog(`desktop service initialization failed: ${error.stack || error.message}`);
            exitSmokeTest(1);
            return;
        }
        // 端口、IPC 或后端进程初始化失败时仍创建界面，让用户看到可
        // 恢复的错误信息，而不是留下一个没有任何窗口的后台进程。
        showInAppNotice('desktop-start', {
            kicker: '应用启动',
            title: '桌面服务初始化失败',
            message: `${PRODUCT_NAME} 已打开，但生成服务暂时不可用。`,
            detail: error.message,
            tone: 'danger',
        });
        createWindow();
        registerRecoveryShortcut();
        registerConfiguredShortcuts();
        return;
    }

    try {
        console.log('[main] 等待 Python 服务器就绪...');
        smokeLog('waiting for backend');
        await waitForServer();
        backendReady = true;
        if (isSmokeTest) {
            smokeLog('backend ready; creating smoke window');
            const smokeWindow = createWindow();
            registerRecoveryShortcut();
            registerConfiguredShortcuts();
            try {
                await verifyRendererSmokeTest(smokeWindow);
                console.log('[main] 桌面界面端到端冒烟测试通过');
                smokeLog('renderer smoke passed');
                exitSmokeTest(0, smokeWindow);
            } catch (error) {
                console.error('[main] 桌面界面端到端冒烟测试失败:', error.message);
                smokeLog(`renderer smoke failed: ${error.stack || error.message}`);
                exitSmokeTest(1, smokeWindow);
            }
            return;
        }
        console.log('[main] 服务器就绪，创建窗口');
    } catch (err) {
        console.error('[main] 服务器启动失败:', err.message);
        if (isSmokeTest) {
            smokeLog(`backend startup failed: ${err.stack || err.message}`);
            exitSmokeTest(1);
            return;
        }
        // 服务器启动失败时仍然创建窗口，让用户能看到错误提示
        showInAppNotice('backend-start', {
            kicker: '生成服务',
            title: '生成服务未能启动',
            message: `${PRODUCT_NAME} 已打开，但本机生成服务暂时不可用。`,
            detail: err.message,
            tone: 'danger',
        });
        createWindow();
        registerRecoveryShortcut();
        registerConfiguredShortcuts();
        return;
    }

    createWindow();
    registerRecoveryShortcut();
    registerConfiguredShortcuts();
}).catch((error) => {
    // 启动阶段的局部 try/catch 覆盖了后端失败；这里兜底处理窗口创建、
    // 快捷键和 Electron API 版本差异等未预期异常，避免留下无窗口后台进程。
    console.error('[main] 应用启动流程异常:', error.stack || error.message);
    smokeLog(`startup promise failed: ${error.stack || error.message}`);
    if (isSmokeTest) {
        exitSmokeTest(1);
        return;
    }
    showInAppNotice('desktop-start-unexpected', {
        kicker: '应用启动',
        title: '应用启动遇到异常',
        message: `${PRODUCT_NAME} 已打开，但部分桌面功能不可用。`,
        detail: error.message,
        tone: 'danger',
    });
    try {
        createWindow();
        registerRecoveryShortcut();
        registerConfiguredShortcuts();
    } catch (fallbackError) {
        console.error('[main] 创建错误提示窗口失败:', fallbackError.stack || fallbackError.message);
        if (!isQuitting) app.quit();
    }
});

app.on('window-all-closed', () => {
    app.quit();
});

// 在应用退出前确保 Python 进程被终止，防止僵尸进程
app.on('will-quit', (event) => {
    isQuitting = true;
    globalShortcut.unregisterAll();
    recoveryShortcutRegistered = false;
    unregisterConfiguredShortcuts();
    persistWindowSettings();
    if (!pythonProcess || quitCleanupStarted) return;
    quitCleanupStarted = true;
    event.preventDefault();
    stopPythonServerAsync().then(() => {
        // 进程已终止，真正退出；quitCleanupStarted 防止重复 will-quit
        // 事件再次创建清理循环。
        app.exit(0);
    });
});

app.on('activate', () => {
    // macOS 可能在端口分配和 IPC 注册完成前发出 activate；此时由启动流程稍后建窗。
    if (!desktopServicesReady) return;
    if (mainWindow && !mainWindow.isDestroyed()) {
        showMainWindow();
    } else if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
        registerRecoveryShortcut();
        registerConfiguredShortcuts();
    }
});
