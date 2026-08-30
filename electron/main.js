/**
 * Electron 主进程
 * =================
 * 1. 启动/管理 Python FastAPI 后端服务器（开发模式用系统 Python，打包模式用 PyInstaller 产物）
 * 2. 创建无边框窗口，加载 renderer/index.html
 * 3. 提供原生文件对话框（选择文件、保存文件）
 */

const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const net = require('net');
const { pathToFileURL } = require('url');
const { createNativeFileDialogs } = require('./file-dialogs');
const { createSourceUploadStaging } = require('./source-staging');
const { createUpdateManager } = require('./update-manager');
const {
    newStreamId,
    openWorkflowSse,
    requestWorkflow,
    requestWorkflowStream,
    requestWorkflowUpload,
} = require('./workflow-proxy');

let mainWindow = null;
let pythonProcess = null;
let hasSingleInstanceLock = true;
let isQuitting = false;
let serverPort = null;
let serverUrl = null;
let serverToken = null;
let serverInstance = null;
let desktopServicesReady = false;
let backendReady = false;
let rendererReady = false;
let rendererFatalShown = false;
let updateManager = null;
let latestUpdateState = null;
let pythonStopPromise = null;
let quitCleanupStarted = false;
let persistentUserDataPath = null;
let persistentUserDataError = null;
const pendingAppNotices = new Map();
const workflowSseStreams = new Map();
const workflowArtifactStreams = new Map();
let pendingWorkflowArtifactStreams = 0;
const workflowArtifactDownloads = new Map();
const workflowSourceFiles = new Map();
const workflowSourceUploads = new Map();
const cancelledWorkflowSourceUploads = new Set();
let sourceUploadStaging = null;
const SOURCE_FILE_TOKEN_TTL_MS = 5 * 60 * 1000;
const MAX_WORKFLOW_ARTIFACT_STREAMS = 4;
const MAX_WORKFLOW_ARTIFACT_DOWNLOADS = 4;
// A renderer acknowledgement can be delayed by a busy main/UI thread while
// the stream is still healthy.  Keep the watchdog bounded, but allow a long
// pause between chunks for large local playback/downloads.
const WORKFLOW_ARTIFACT_STREAM_IDLE_TIMEOUT_MS = 90_000;
const MAX_PENDING_WORKFLOW_EVENT_FRAMES = 512;
let rendererLifecycleEpoch = 0;
const isSmokeTest = process.argv.includes('--smoke-test');
const PRODUCT_NAME = '小猪wordTTS';
const RELEASES_URL = 'https://github.com/asklins223/word-tts-desktop/releases';
// 正式 App 默认启用真实讯飞 Provider；只有打包/启动 smoke 明确使用
// --smoke-test 时才强制离线。后端环境变量由这里统一写成 1，避免用户
// 直接双击安装包时因为没有额外 shell 环境变量而落入“Provider 未启用”。
const realProviderEnabled = !isSmokeTest;
// 必须和 wordtts 包（wordtts/config.py）保持一致。启动时拒绝混用旧
// PyInstaller 后端，避免打包客户端表面启动成功、实际退回逐条生成的隐性性能问题。
const EXPECTED_BACKEND_CONTRACT_VERSION = 5;
// The renderer has one supported entry point so every UI change follows the
// same code path and cannot drift between parallel shells.
const RENDERER_ENTRY_PATH = path.join(__dirname, 'renderer', 'index.html');
const RENDERER_ENTRY_URL = pathToFileURL(RENDERER_ENTRY_PATH).href;
const SMOKE_LOG_PATH = isSmokeTest
    ? path.join(os.tmpdir(), 'wordtts-electron-smoke.log')
    : null;
// Windows runners can spend several seconds creating the first Chromium
// renderer under load.  Keep the smoke test bounded, but do not turn a slow
// cold start into a false packaging failure.
const SMOKE_RENDERER_TIMEOUT_MS = process.platform === 'win32' ? 30_000 : 15_000;
let smokeWatchdog = null;
let smokeExitRequested = false;

function smokeLog(message) {
    if (!SMOKE_LOG_PATH) return;
    const line = `[${new Date().toISOString()}] ${message}\n`;
    try {
        fs.appendFileSync(SMOKE_LOG_PATH, line, 'utf8');
    } catch (_) { /* 日志失败不能影响应用启动 */ }
}

if (SMOKE_LOG_PATH) {
    try { fs.writeFileSync(SMOKE_LOG_PATH, '', 'utf8'); } catch (_) { /* ignore */ }
    // 冒烟模式下的未捕获异常必须让自检失败。此前这里只写日志继续运行，
    // 导致缺少打包文件的坏包也能通过构建冒烟（如 ./source-staging 缺失）。
    const failSmoke = (kind, detail) => {
        smokeLog(`${kind}: ${detail}`);
        if (!smokeExitRequested) exitSmokeTest(1);
    };
    process.on('uncaughtException', (error) => failSmoke('uncaughtException', error?.stack || error));
    process.on('unhandledRejection', (reason) => failSmoke('unhandledRejection', reason?.stack || reason));
}

// 冒烟测试只验证后端/渲染器启动，不需要 GPU 合成；Windows runner 的虚拟
// 显示驱动偶尔会让隐藏 BrowserWindow 的渲染探测不返回。禁用 GPU 只作用于
// --smoke-test，不影响用户正常运行时的硬件加速。
if (isSmokeTest) app.disableHardwareAcceleration();

// Branding changed in 2.0, but existing history and preferences must continue
// using the original on-disk directory instead of appearing to disappear.
try {
    const legacyUserDataPath = path.join(app.getPath('appData'), 'WordTTS');
    persistentUserDataPath = legacyUserDataPath;
    fs.mkdirSync(legacyUserDataPath, { recursive: true });
    app.setPath('userData', legacyUserDataPath);
} catch (error) {
    // Never silently fall back to Electron's app-id-derived directory here:
    // that directory is normally empty and would make a path/permission
    // problem look like a brand-new installation with lost data.
    persistentUserDataError = error;
    console.error(`[main] 无法打开固定用户数据目录，已阻止启动以避免创建新数据库: ${error.message}`);
}

// Request the instance lock only after userData has been pinned.  Electron
// otherwise creates/uses the app-id-derived lock directory before the data
// path override, leaving instance ownership and SQLite data in different
// locations.
hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
    app.quit();
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
    if (latestUpdateState) mainWindow.webContents.send('app-update', latestUpdateState);
}

function sendUpdateState(state) {
    latestUpdateState = state;
    if (rendererReady && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('app-update', state);
    }
}

function initializeUpdateManager() {
    updateManager = createUpdateManager({
        app,
        appVersion: app.getVersion(),
        isPackaged: app.isPackaged,
        isSmokeTest,
        platform: process.platform,
        releaseUrl: RELEASES_URL,
        send: sendUpdateState,
    });
    latestUpdateState = updateManager.getStatus();
}

function safeUpdateStatus() {
    return updateManager?.getStatus?.() || {
        status: 'disabled',
        currentVersion: app.getVersion(),
        platform: process.platform,
        releaseUrl: RELEASES_URL,
        canDownload: false,
        canInstall: false,
    };
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
    const providerArgs = realProviderEnabled ? ['--enable-real-provider'] : [];
    if (app.isPackaged) {
        const exeName = process.platform === 'win32' ? 'server_backend.exe' : 'server_backend';
        const serverExe = path.join(process.resourcesPath, 'server_backend', exeName);
        return { cmd: serverExe, args: providerArgs, cwd: path.dirname(serverExe) };
    }
    const projectRoot = path.resolve(__dirname, '..');
    const pythonCmd = findPython();
    return { cmd: pythonCmd, args: ['server.py', ...providerArgs], cwd: projectRoot };
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

    console.log(`[main] 启动后端服务器: ${cmd} ${args.join(' ')} (cwd: ${cwd})`);
    smokeLog(`start backend: ${cmd} ${args.join(' ')}`);

    // 校验可执行文件存在（打包模式）
    if (app.isPackaged && !fs.existsSync(cmd)) {
        const msg = `后端可执行文件不存在:\n${cmd}\n\n应用可能已损坏，请重新安装。`;
        console.error(`[main] ${msg}`);
        showInAppNotice('backend-start', {
            kicker: '生成服务',
            title: '后端启动失败',
            message: '应用缺少生成服务组件，暂时无法处理 Word 文档。',
            detail: `${cmd}\n请重新安装完整版本后再试。`,
            tone: 'danger',
        });
        return;
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
            WORDTTS_ENABLE_REAL_PROVIDER: realProviderEnabled ? '1' : '0',
            WORDTTS_AUTO_RETRY: realProviderEnabled ? '1' : '0',
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
        backendReady = false;
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
            const req = http.get(`${serverUrl}/api/v1/health`, {
                headers: { 'X-Desktop-Capability': serverToken },
            }, (res) => {
                let body = '';
                res.setEncoding('utf8');
                res.on('data', (chunk) => { body += chunk; });
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

// ============================================================================
// 窗口创建
// ============================================================================

function createWindow() {
    if (mainWindow && !mainWindow.isDestroyed()) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        if (!mainWindow.isVisible()) mainWindow.show();
        mainWindow.focus();
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
        backgroundColor: '#f4f9ff',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            // Electron 20+ sandboxes CommonJS preload scripts by default. The
            // preload is trusted, application-owned code and intentionally
            // composes the local workflow-api module; without this explicit
            // opt-out Electron rejects require('./workflow-api') before the
            // contextBridge can expose window.electronAPI. Renderer code still
            // has no Node integration and remains isolated from the preload.
            sandbox: false,
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
    // The `closed` event fires after the BrowserWindow and its webContents
    // have been destroyed. Keep the sender id as a plain value for cleanup.
    const windowWebContentsId = win.webContents.id;
    mainWindow = win;
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
    if (process.env.WORDTTS_DEBUG_LOGS === '1' || process.argv.includes('--dev')) {
        win.webContents.on('console-message', (details) => {
            console.log(`[renderer:${details.level}] ${details.message} (${details.sourceId}:${details.lineNumber})`);
        });
    }
    win.webContents.on('did-finish-load', () => {
        if (mainWindow !== win) return;
        smokeLog('renderer did-finish-load');
        rendererReady = true;
        flushAppNotices();
    });
    win.webContents.on('did-start-navigation', (_event, _url, _isInPlace, isMainFrame) => {
        if (!isMainFrame) return;
        rendererLifecycleEpoch += 1;
        closeWorkflowStreamsForSender(win.webContents.id, 'renderer-reload');
    });
    win.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
        if (!isMainFrame || code === -3) return;
        smokeLog(`renderer did-fail-load: ${code} ${description} ${url || ''}`);
        if (mainWindow === win) rendererReady = false;
        showRendererFatalError('界面加载失败', `${description}（错误代码 ${code}）\n${url || '本地界面'}`);
    });
    win.webContents.on('render-process-gone', (_event, details) => {
        smokeLog(`renderer process gone: ${JSON.stringify(details)}`);
        closeWorkflowStreamsForSender(win.webContents.id, 'renderer-process-gone');
        if (mainWindow === win) rendererReady = false;
        if (details.reason === 'clean-exit') return;
        showRendererFatalError('界面进程异常退出', `退出原因：${details.reason}`);
    });

    if (process.argv.includes('--dev')) {
        win.webContents.openDevTools({ mode: 'detach' });
    }

    win.on('closed', () => {
        closeWorkflowStreamsForSender(windowWebContentsId, 'window-closed');
        if (mainWindow !== win) return;
        rendererReady = false;
        mainWindow = null;
    });

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
                workflowApi: Boolean(window.electronAPI?.workflow),
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
                && lastState.workflowApi
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
    // 先销毁渲染器，停止它在启动 hydrate 阶段发起的新请求；再停后端。
    // 如果顺序反过来，活动任务列表等后台请求会在后端关闭时收到
    // ECONNRESET，虽然不影响 smoke 退出码，却会把一次正常收尾污染成故障日志。
    if (win && !win.isDestroyed()) win.destroy();
    stopPythonServerAsync().then(() => {
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

function closeWorkflowSourceFile(state) {
    if (!state) return;
    if (state.expiryTimer) clearTimeout(state.expiryTimer);
    try { state.handle?.close?.().catch?.(() => {}); } catch (_) { /* best effort cleanup */ }
    if (state.stagingPath) {
        // 分块暂存文件在上传消费、超时或退出时都必须物理删除。
        fs.promises.unlink(state.stagingPath).catch(() => {});
        state.stagingPath = null;
    }
}

function closeWorkflowSourceFiles() {
    workflowSourceFiles.forEach(closeWorkflowSourceFile);
    workflowSourceFiles.clear();
}

function cancelWorkflowSourceUploads() {
    workflowSourceUploads.forEach((state) => state.controller?.abort?.());
    workflowSourceUploads.clear();
    cancelledWorkflowSourceUploads.clear();
}

function closeWorkflowStreamsForSender(senderId, reason = 'renderer-reloaded') {
    for (const [streamId, state] of workflowSseStreams) {
        if (state.senderId !== senderId) continue;
        workflowSseStreams.delete(streamId);
        state.pendingFrames?.splice(0);
        state.pendingErrors?.splice(0);
        state.stream?.close();
    }
    for (const [streamId, state] of workflowArtifactStreams) {
        if (state.senderId !== senderId) continue;
        workflowArtifactStreams.delete(streamId);
        state.closed = true;
        state.waitingForAck = false;
        if (state.idleTimer) clearTimeout(state.idleTimer);
        state.idleTimer = null;
        state.stream?.destroy(new Error(`workflow artifact stream closed: ${reason}`));
    }
    for (const [transferId, state] of workflowArtifactDownloads) {
        if (state.senderId !== senderId) continue;
        workflowArtifactDownloads.delete(transferId);
        state.controller?.abort?.();
    }
    for (const [uploadId, state] of workflowSourceUploads) {
        if (state.senderId !== senderId) continue;
        workflowSourceUploads.delete(uploadId);
        state.controller?.abort?.();
    }
    for (const [sourceFileId, state] of workflowSourceFiles) {
        if (state.senderId !== senderId) continue;
        workflowSourceFiles.delete(sourceFileId);
        closeWorkflowSourceFile(state);
    }
    void sourceUploadStaging?.disposeSender?.(senderId, reason);
}

function sendArtifactDownloadProgress(event, transferId, payload) {
    if (event?.sender?.isDestroyed?.()) return;
    event.sender.send('artifact-download-progress', {
        transferId,
        ...payload,
    });
}

function sendSourceUploadProgress(event, uploadId, payload) {
    if (event?.sender?.isDestroyed?.()) return;
    event.sender.send('source-upload-progress', {
        uploadId,
        ...payload,
    });
}

function artifactDownloadError(error) {
    return {
        message: String(error?.message || 'Artifact 下载失败').slice(0, 500),
        code: String(error?.code || 'DOWNLOAD_ERROR').slice(0, 128),
    };
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

    // 更新能力只接受本地 renderer 的请求。下载和安装仍由
    // electron-updater 在主进程完成，避免把 GitHub Release 元数据或文件
    // 路径暴露给页面脚本。
    ipcMain.handle('update-status', async (event) => {
        if (!isTrustedRendererEvent(event)) return safeUpdateStatus();
        return safeUpdateStatus();
    });

    ipcMain.handle('update-check', async (event) => {
        if (!isTrustedRendererEvent(event)) return safeUpdateStatus();
        return updateManager?.check?.() || safeUpdateStatus();
    });

    ipcMain.handle('update-download', async (event) => {
        if (!isTrustedRendererEvent(event)) return safeUpdateStatus();
        return updateManager?.download?.() || safeUpdateStatus();
    });

    ipcMain.handle('update-install', async (event) => {
        if (!isTrustedRendererEvent(event)) return safeUpdateStatus();
        return updateManager?.install?.() || safeUpdateStatus();
    });

    ipcMain.handle('open-update-release', async (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        try {
            await shell.openExternal(RELEASES_URL);
            return { success: true };
        } catch (error) {
            console.error('[main] 打开 GitHub Releases 失败:', error.message);
            return { success: false, reason: 'open-failed', message: error.message };
        }
    });

    // 拖拽导入的分块暂存区：渲染层只持有单个分块，主进程在允许目录内
    // 落盘后再交给 workflow-source-upload 的流式管道。
    sourceUploadStaging = createSourceUploadStaging({
        fs,
        path,
        stagingDir: path.join(app.getPath('userData'), 'source-staging'),
        logger: console,
    });

    ipcMain.handle('source-upload-begin', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted source upload sender');
        try {
            return await sourceUploadStaging.begin({
                fileName: input.fileName,
                sizeBytes: input.sizeBytes,
                senderId: event.sender.id,
            });
        } catch (error) {
            console.error('[main] source upload begin failed:', error.message);
            throw error;
        }
    });

    ipcMain.handle('source-upload-write', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted source upload sender');
        return sourceUploadStaging.write({
            uploadId: input.uploadId,
            offset: input.offset,
            bytes: input.bytes,
        }, event.sender.id);
    });

    ipcMain.handle('source-upload-complete', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted source upload sender');
        return sourceUploadStaging.complete({ uploadId: input.uploadId }, event.sender.id, {
            openStagedHandle: async ({ filePath, fileName, sizeBytes, fileHandle }) => {
                // 暂存文件位于 userData 允许目录内，但仍走 realpath 允许列表
                // 与一次性句柄注册，和原生对话框路径保持同一安全边界。
                if (!isAllowedFilePath(filePath)) return { success: false, reason: 'untrusted-path' };
                const sourceFileId = newStreamId();
                const state = {
                    handle: fileHandle,
                    senderId: event.sender.id,
                    fileName,
                    sizeBytes,
                    expiryTimer: null,
                    stagingPath: filePath,
                };
                state.expiryTimer = setTimeout(() => {
                    if (workflowSourceFiles.get(sourceFileId) !== state) return;
                    workflowSourceFiles.delete(sourceFileId);
                    closeWorkflowSourceFile(state);
                }, SOURCE_FILE_TOKEN_TTL_MS);
                state.expiryTimer.unref?.();
                workflowSourceFiles.set(sourceFileId, state);
                return { success: true, sourceFileId, fileName, sizeBytes };
            },
        });
    });

    ipcMain.handle('source-upload-abort', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted source upload sender');
        return sourceUploadStaging.abort({ uploadId: input.uploadId });
    });


    // New workflow API transport.  The renderer only receives structured
    // responses; the long-lived capability stays in this main-process scope.
    ipcMain.handle('workflow-request', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted workflow request sender');
        let response;
        try {
            response = await requestWorkflow({
                http,
                baseUrl: serverUrl,
                capability: serverToken,
                method: input.method,
                pathname: input.pathname,
                body: input.body,
                headers: input.headers || {},
            });
        } catch (error) {
            if (isSmokeTest && smokeExitRequested && ['ECONNRESET', 'ECONNREFUSED', 'EPIPE'].includes(error?.code)) {
                // 渲染器在销毁过程中可能完成一个已经发出的请求；冒烟收尾
                // 已经明确停止后端，这类连接错误不是被测功能的失败，也不应
                // 以 rejected IPC handler 的形式污染测试日志。
                return {
                    status: 499,
                    body: { code: 'SMOKE_SHUTDOWN', message: 'smoke test is shutting down' },
                };
            }
            if (process.env.WORDTTS_DEBUG_LOGS === '1' || process.argv.includes('--dev')) {
                console.error(`[main] workflow request failed ${input.method || 'GET'} ${input.pathname || ''}: ${error.stack || error.message}`);
            }
            throw error;
        }
        if (process.env.WORDTTS_DEBUG_LOGS === '1' || process.argv.includes('--dev')) {
            const body = input.body;
            const bodyShape = body == null
                ? 'none'
                : (body instanceof Uint8Array
                    ? `bytes:${body.byteLength}`
                    : `keys:${Object.keys(body).sort().join(',')}`);
            const headerShape = Object.keys(input.headers || {}).sort().join(',') || 'none';
            const responseBody = response.body;
            const responseShape = responseBody == null
                ? 'none'
                : (responseBody instanceof Uint8Array
                    ? `bytes:${responseBody.byteLength}`
                    : (typeof responseBody === 'object'
                        ? `keys:${Object.keys(responseBody).sort().join(',')}`
                        : typeof responseBody));
            console.log(
                `[main] workflow request ${input.method || 'GET'} ${input.pathname || ''}`
                + ` body=${bodyShape} headers=${headerShape}`
                + ` response=${responseShape} -> ${response.status}`,
            );
        }
        if (Buffer.isBuffer(response.body)) response.body = new Uint8Array(response.body);
        return response;
    });

    // Large native source documents stay behind the main-process boundary.
    // The renderer receives only an opaque one-shot handle and metadata; the
    // file descriptor is streamed directly into the loopback API.
    ipcMain.handle('select-source-file-stream', async (event) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const selected = await nativeFileDialogs.selectFileSource(event);
        if (!selected?.success || !selected.handle) return selected;
        const sourceFileId = newStreamId();
        const state = {
            handle: selected.handle,
            senderId: event.sender.id,
            fileName: selected.fileName,
            sizeBytes: Number(selected.sizeBytes),
            expiryTimer: null,
        };
        state.expiryTimer = setTimeout(() => {
            if (workflowSourceFiles.get(sourceFileId) !== state) return;
            workflowSourceFiles.delete(sourceFileId);
            closeWorkflowSourceFile(state);
        }, SOURCE_FILE_TOKEN_TTL_MS);
        state.expiryTimer.unref?.();
        workflowSourceFiles.set(sourceFileId, state);
        return {
            success: true,
            sourceFileId,
            fileName: state.fileName,
            sizeBytes: state.sizeBytes,
        };
    });

    ipcMain.handle('release-source-file', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const sourceFileId = String(input.sourceFileId || '');
        if (!sourceFileId) return { success: false, reason: 'content-invalid' };
        const state = workflowSourceFiles.get(sourceFileId);
        if (!state) return { success: true, alreadyReleased: true };
        if (state.senderId !== event.sender.id) return { success: false, reason: 'untrusted-sender' };
        workflowSourceFiles.delete(sourceFileId);
        closeWorkflowSourceFile(state);
        return { success: true };
    });

    ipcMain.handle('workflow-source-upload', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted workflow source sender');
        const sourceFileId = String(input.sourceFileId || '');
        const state = workflowSourceFiles.get(sourceFileId);
        if (!sourceFileId || !state || state.senderId !== event.sender.id) {
            throw new Error('source file handle is missing or expired');
        }
        if (!Number.isSafeInteger(state.sizeBytes) || state.sizeBytes <= 0) {
            workflowSourceFiles.delete(sourceFileId);
            closeWorkflowSourceFile(state);
            throw new Error('source file size is invalid');
        }
        const pathname = String(input.pathname || '');
        if (!/^\/api\/v1\/source-imports\/[^/]+\/content$/.test(pathname)) {
            throw new Error('workflow source upload path is not allowed');
        }
        const incomingHeaders = input.headers && typeof input.headers === 'object' ? input.headers : {};
        const headers = {};
        [
            'X-Idempotency-Key',
            'X-Staging-Generation',
            'X-Source-Write-Grant',
            'X-Artifact-Format',
            'Content-Type',
        ].forEach((name) => {
            if (incomingHeaders[name] != null) headers[name] = String(incomingHeaders[name]);
        });
        if (!headers['X-Idempotency-Key'] || !headers['X-Staging-Generation'] || !headers['X-Source-Write-Grant']) {
            throw new Error('source upload grant headers are incomplete');
        }

        // Consume the opaque handle before opening the request so a duplicate
        // IPC call cannot reuse the same descriptor or submit the body twice.
        workflowSourceFiles.delete(sourceFileId);
        if (state.expiryTimer) clearTimeout(state.expiryTimer);
        const uploadId = String(input.uploadId || '');
        if (!uploadId) {
            closeWorkflowSourceFile(state);
            throw new Error('source upload id is required');
        }
        if (cancelledWorkflowSourceUploads.delete(uploadId)) {
            closeWorkflowSourceFile(state);
            const error = new Error('workflow source upload was cancelled');
            error.name = 'AbortError';
            error.code = 'USER_CANCELLED';
            throw error;
        }
        const uploadController = new AbortController();
        workflowSourceUploads.set(uploadId, {
            controller: uploadController,
            senderId: event.sender.id,
        });
        let stream = null;
        try {
            sendSourceUploadProgress(event, uploadId, {
                state: 'starting',
                receivedBytes: 0,
                totalBytes: state.sizeBytes,
            });
            const handle = state.handle;
            const streamOptions = {
                autoClose: false,
                start: 0,
                end: state.sizeBytes - 1,
            };
            if (typeof handle.createReadStream === 'function') {
                stream = handle.createReadStream(streamOptions);
            } else if (handle && handle.fd != null) {
                stream = fs.createReadStream(null, { ...streamOptions, fd: handle.fd });
            } else {
                throw new Error('source file stream is unavailable');
            }
            const response = await requestWorkflowUpload({
                http,
                baseUrl: serverUrl,
                capability: serverToken,
                method: 'PUT',
                pathname,
                headers,
                bodyStream: stream,
                contentLength: state.sizeBytes,
                signal: uploadController.signal,
                onProgress: ({ receivedBytes, totalBytes }) => sendSourceUploadProgress(
                    event,
                    uploadId,
                    { state: 'transferring', receivedBytes, totalBytes },
                ),
            });
            sendSourceUploadProgress(event, uploadId, {
                state: 'completed',
                receivedBytes: state.sizeBytes,
                totalBytes: state.sizeBytes,
            });
            return response;
        } catch (error) {
            sendSourceUploadProgress(event, uploadId, {
                state: uploadController.signal.aborted || error?.name === 'AbortError' ? 'cancelled' : 'failed',
                error: artifactDownloadError(error),
            });
            throw error;
        } finally {
            workflowSourceUploads.delete(uploadId);
            stream?.destroy?.();
            try { await state.handle?.close?.(); } catch (_) { /* best effort cleanup */ }
        }
    });

    ipcMain.handle('workflow-source-upload-cancel', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted workflow source cancel sender');
        const uploadId = String(input.uploadId || '');
        const state = workflowSourceUploads.get(uploadId);
        if (!uploadId || !state || state.senderId !== event.sender.id) {
            if (uploadId) {
                cancelledWorkflowSourceUploads.add(uploadId);
                const timer = setTimeout(() => cancelledWorkflowSourceUploads.delete(uploadId), 60_000);
                timer.unref?.();
            }
            return { success: true, alreadyFinished: true };
        }
        state.controller.abort();
        return { success: true };
    });

    ipcMain.handle('workflow-events-open', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted workflow stream sender');
        const senderEpoch = rendererLifecycleEpoch;
        const workflowId = String(input.workflowId || '');
        if (!workflowId) throw new Error('workflow id is required');
        const ticketResponse = await requestWorkflow({
            http,
            baseUrl: serverUrl,
            capability: serverToken,
            method: 'POST',
            pathname: `/api/v1/workflows/${encodeURIComponent(workflowId)}/event-tickets`,
            body: { last_event_id: input.lastEventId || null },
        });
        const ticket = ticketResponse.body?.ticket;
        if (!ticket) throw new Error('workflow event ticket was not issued');
        if (senderEpoch !== rendererLifecycleEpoch || event.sender.isDestroyed?.()) {
            throw new Error('renderer was reloaded while opening workflow event stream');
        }
        const streamId = newStreamId();
        const pendingFrames = [];
        const pendingErrors = [];
        const streamState = {
            stream: null,
            senderId: event.sender.id,
            ready: false,
            pendingFrames,
            pendingErrors,
            pendingOverflow: false,
        };
        workflowSseStreams.set(streamId, streamState);
        try {
            streamState.stream = openWorkflowSse({
                http,
                baseUrl: serverUrl,
                capability: serverToken,
                pathname: `/api/v1/workflows/${encodeURIComponent(workflowId)}/events`,
                headers: {
                    'X-SSE-Ticket': ticket,
                    ...(input.lastEventId ? { 'Last-Event-ID': String(input.lastEventId) } : {}),
                },
                onFrame: (frame) => {
                    if (event.sender.isDestroyed?.()) return;
                    const item = workflowSseStreams.get(streamId);
                    if (!item) return;
                    if (!item.ready) {
                        if (item.pendingOverflow) return;
                        if (item.pendingFrames.length >= MAX_PENDING_WORKFLOW_EVENT_FRAMES) {
                            item.pendingFrames.splice(0);
                            item.pendingOverflow = true;
                            return;
                        }
                        pendingFrames.push(frame);
                        return;
                    }
                    event.sender.send('workflow-event', { streamId, frame });
                },
                onError: (error) => {
                    if (event.sender.isDestroyed?.()) return;
                    const payload = {
                        streamId,
                        error: {
                            message: error?.message || 'workflow SSE failed',
                            code: error?.code || null,
                            status: error?.status || null,
                            closed: Boolean(error?.closed),
                        },
                    };
                    const item = workflowSseStreams.get(streamId);
                    if (!item) return;
                    if (!item.ready) {
                        if (pendingErrors.length === 0) pendingErrors.push(payload);
                        return;
                    }
                    workflowSseStreams.delete(streamId);
                    event.sender.send('workflow-event-error', payload);
                },
            });
        } catch (error) {
            workflowSseStreams.delete(streamId);
            throw error;
        }
        return streamId;
    });

    ipcMain.handle('workflow-events-ready', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return false;
        const streamId = String(input.streamId || '');
        const item = workflowSseStreams.get(streamId);
        if (!item || item.senderId !== event.sender.id) return false;
        item.ready = true;
        if (item.pendingOverflow) {
            workflowSseStreams.delete(streamId);
            item.pendingFrames.splice(0);
            item.pendingErrors.splice(0);
            item.stream?.close();
            if (!event.sender.isDestroyed?.()) {
                event.sender.send('workflow-event-error', {
                    streamId,
                    error: {
                        message: 'workflow event buffer exceeded its limit; a fresh snapshot is required',
                        code: 'EVENT_GAP',
                        status: 409,
                        closed: true,
                    },
                });
            }
            return true;
        }
        item.pendingFrames.splice(0).forEach((frame) => {
            if (!event.sender.isDestroyed?.()) event.sender.send('workflow-event', { streamId, frame });
        });
        item.pendingErrors.splice(0).forEach((payload) => {
            workflowSseStreams.delete(streamId);
            item.stream?.close();
            if (!event.sender.isDestroyed?.()) event.sender.send('workflow-event-error', payload);
        });
        return true;
    });

    ipcMain.handle('workflow-events-close', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return false;
        const item = workflowSseStreams.get(String(input.streamId || ''));
        if (!item || item.senderId !== event.sender.id) return false;
        workflowSseStreams.delete(String(input.streamId));
        item.pendingFrames?.splice(0);
        item.pendingErrors?.splice(0);
        item.stream?.close();
        return true;
    });

    // Artifact playback uses the same one-time ticket policy as artifact
    // saving, but streams chunks to the renderer instead of buffering the
    // entire Blob through the 16 MiB structured-response limit.
    ipcMain.handle('workflow-artifact-open', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) throw new Error('untrusted workflow artifact sender');
        const senderEpoch = rendererLifecycleEpoch;
        const artifactId = String(input.artifactId || '');
        const requestId = String(input.requestId || '');
        if (!artifactId || !requestId) throw new Error('artifact id and request id are required');
        if (workflowArtifactStreams.size + pendingWorkflowArtifactStreams >= MAX_WORKFLOW_ARTIFACT_STREAMS) {
            const error = new Error('too many artifact streams are active');
            error.code = 'RESOURCE_EXHAUSTED';
            throw error;
        }
        pendingWorkflowArtifactStreams += 1;
        try {
            const ticketResponse = await requestWorkflow({
            http,
            baseUrl: serverUrl,
            capability: serverToken,
            method: 'POST',
            pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content-tickets`,
            });
            const ticket = ticketResponse.body?.ticket;
            if (ticketResponse.status < 200 || ticketResponse.status >= 300 || !ticket) {
                const error = new Error(ticketResponse.body?.message || `artifact ticket failed: HTTP ${ticketResponse.status}`);
                error.status = ticketResponse.status;
                throw error;
            }
            const streamId = newStreamId();
            const stream = await requestWorkflowStream({
            http,
            baseUrl: serverUrl,
            capability: serverToken,
            method: 'GET',
            pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
            headers: { 'X-Artifact-Ticket': ticket },
            });
            if (senderEpoch !== rendererLifecycleEpoch || event.sender.isDestroyed?.()) {
                stream.destroy(new Error('renderer was reloaded while opening artifact stream'));
                throw new Error('renderer was reloaded while opening artifact stream');
            }
            const state = {
            stream,
            senderId: event.sender.id,
            requestId,
            waitingForAck: false,
            closed: false,
            idleTimer: null,
            touch: null,
            };
            workflowArtifactStreams.set(streamId, state);
            const remove = () => {
            if (workflowArtifactStreams.get(streamId) === state) workflowArtifactStreams.delete(streamId);
            state.closed = true;
            if (state.idleTimer) clearTimeout(state.idleTimer);
            state.idleTimer = null;
            };
            const touch = () => {
            if (state.closed) return;
            if (state.idleTimer) clearTimeout(state.idleTimer);
            state.idleTimer = setTimeout(() => {
                if (workflowArtifactStreams.get(streamId) !== state || state.closed) return;
                if (!event.sender.isDestroyed?.()) {
                    event.sender.send('workflow-artifact-error', {
                        streamId,
                        requestId,
                        error: { code: 'STREAM_IDLE_TIMEOUT', message: '试听流长时间未被消费，已自动关闭' },
                    });
                }
                remove();
                state.stream?.destroy(new Error('workflow artifact stream idle timeout'));
            }, WORKFLOW_ARTIFACT_STREAM_IDLE_TIMEOUT_MS);
            };
            state.touch = touch;
            touch();
            const headers = stream.headers || {};
            const rawDisposition = String(headers['content-disposition'] || '');
            const encodedFilename = rawDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1] || '';
            let filename = String(headers['x-artifact-filename'] || '').trim();
            if (filename) {
                try { filename = decodeURIComponent(filename); } catch (_) { filename = ''; }
            }
            if (!filename && encodedFilename) {
                try { filename = decodeURIComponent(encodedFilename); } catch (_) { filename = ''; }
            }
            const rawSha256 = String(headers['x-artifact-sha256'] || headers.etag || '')
            .replace(/^W\//i, '').replace(/^"|"$/g, '').trim();
            const rawLength = Number(headers['content-length']);
            if (!event.sender.isDestroyed?.()) {
            event.sender.send('workflow-artifact-meta', {
                streamId,
                requestId,
                metadata: {
                    content_type: String(headers['content-type'] || '').split(';', 1)[0] || null,
                    content_length: Number.isSafeInteger(rawLength) && rawLength >= 0 ? rawLength : null,
                    sha256: /^[0-9a-f]{64}$/i.test(rawSha256) ? rawSha256.toLowerCase() : null,
                    filename: filename || null,
                },
            });
            }
            stream.on('data', (chunk) => {
            if (
                workflowArtifactStreams.get(streamId) !== state
                || state.closed
                || event.sender.isDestroyed?.()
            ) return;
            // IPC has no built-in backpressure.  Pause after each chunk and
            // resume only when the renderer's ReadableStream asks for more;
            // this bounds the cross-process queue to one artifact chunk.
            state.waitingForAck = true;
            touch();
            stream.pause();
            try {
                event.sender.send('workflow-artifact-data', {
                    streamId,
                    requestId,
                    data: new Uint8Array(Buffer.from(chunk)),
                });
            } catch (error) {
                remove();
                stream.destroy(error);
            }
            });
            stream.once('end', () => {
            if (workflowArtifactStreams.get(streamId) !== state || state.closed) return;
            remove();
            if (!event.sender.isDestroyed?.()) event.sender.send('workflow-artifact-end', { streamId, requestId });
            });
            stream.once('error', (error) => {
            if (workflowArtifactStreams.get(streamId) !== state || state.closed) return;
            remove();
            if (!event.sender.isDestroyed?.()) {
                event.sender.send('workflow-artifact-error', {
                    streamId,
                    requestId,
                    error: { message: error?.message || 'workflow artifact stream failed' },
                });
            }
            });
            return streamId;
        } finally {
            pendingWorkflowArtifactStreams -= 1;
        }
    });

    ipcMain.handle('workflow-artifact-ack', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return false;
        const streamId = String(input.streamId || '');
        const state = workflowArtifactStreams.get(streamId);
        if (!state || state.senderId !== event.sender.id || state.closed) return false;
        if (state.waitingForAck) {
            state.waitingForAck = false;
            // An acknowledgement is proof that the renderer is alive and
            // consuming the stream, so it also resets the idle watchdog.
            state.stream?.resume();
        }
        state.touch?.();
        return true;
    });

    ipcMain.handle('workflow-artifact-close', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return false;
        const streamId = String(input.streamId || '');
        const state = workflowArtifactStreams.get(streamId);
        if (!state || state.senderId !== event.sender.id) return false;
        workflowArtifactStreams.delete(streamId);
        state.closed = true;
        state.waitingForAck = false;
        state.stream?.destroy();
        return true;
    });

    // Renderer receives document/artifact bytes only. Absolute paths remain
    // inside this process and never enter the preload surface.
    ipcMain.handle('select-source-file', nativeFileDialogs.selectFileContent);
    ipcMain.handle('save-artifact-file', (event, bytes, suggestedName) => (
        nativeFileDialogs.saveFileContent(event, bytes, suggestedName)
    ));
    ipcMain.handle('save-artifact-stream', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const artifactId = String(input.artifactId || '');
        if (!artifactId) return { success: false, reason: 'content-invalid', error: 'Artifact 标识缺失' };
        return nativeFileDialogs.saveFileStream(event, async () => {
            const ticketResponse = await requestWorkflow({
                http,
                baseUrl: serverUrl,
                capability: serverToken,
                method: 'POST',
                pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content-tickets`,
            });
            const ticket = ticketResponse.body?.ticket;
            if (ticketResponse.status < 200 || ticketResponse.status >= 300 || !ticket) {
                const error = new Error(ticketResponse.body?.message || `artifact ticket failed: HTTP ${ticketResponse.status}`);
                error.status = ticketResponse.status;
                throw error;
            }
            return requestWorkflowStream({
                http,
                baseUrl: serverUrl,
                capability: serverToken,
                method: 'GET',
                pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
                headers: { 'X-Artifact-Ticket': ticket },
            });
        }, input.suggestedName);
    });

    ipcMain.handle('save-artifact-stream-start', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const artifactId = String(input.artifactId || '');
        const transferId = String(input.transferId || newStreamId());
        if (!artifactId) return { success: false, reason: 'content-invalid', error: 'Artifact 标识缺失' };
        if (workflowArtifactDownloads.has(transferId)) {
            return { success: false, reason: 'transfer-already-running' };
        }
        if (workflowArtifactDownloads.size >= MAX_WORKFLOW_ARTIFACT_DOWNLOADS) {
            return {
                success: false,
                reason: 'resource-exhausted',
                code: 'RESOURCE_EXHAUSTED',
                error: '同时进行的 Artifact 下载数量已达到上限',
            };
        }
        const state = {
            controller: new AbortController(),
            senderId: event.sender.id,
            receivedBytes: 0,
            totalBytes: null,
        };
        workflowArtifactDownloads.set(transferId, state);
        void (async () => {
            sendArtifactDownloadProgress(event, transferId, { state: 'starting', receivedBytes: 0, totalBytes: null });
            try {
                const result = await nativeFileDialogs.saveFileStream(
                    event,
                    async ({ signal } = {}) => {
                        const requestSignal = signal || state.controller.signal;
                        const ticketResponse = await requestWorkflow({
                            http,
                            baseUrl: serverUrl,
                            capability: serverToken,
                            method: 'POST',
                            pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content-tickets`,
                            signal: requestSignal,
                        });
                        const ticket = ticketResponse.body?.ticket;
                        if (ticketResponse.status < 200 || ticketResponse.status >= 300 || !ticket) {
                            const error = new Error(ticketResponse.body?.message || `artifact ticket failed: HTTP ${ticketResponse.status}`);
                            error.status = ticketResponse.status;
                            throw error;
                        }
                        return requestWorkflowStream({
                            http,
                            baseUrl: serverUrl,
                            capability: serverToken,
                            method: 'GET',
                            pathname: `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
                            headers: { 'X-Artifact-Ticket': ticket },
                            signal: requestSignal,
                        });
                    },
                    input.suggestedName,
                    {
                        signal: state.controller.signal,
                        onProgress: ({ receivedBytes, totalBytes }) => sendArtifactDownloadProgress(
                            event,
                            transferId,
                            {
                                state: 'transferring',
                                receivedBytes: state.receivedBytes = Number(receivedBytes) || 0,
                                totalBytes: state.totalBytes = Number.isFinite(Number(totalBytes))
                                    ? Number(totalBytes)
                                    : null,
                            },
                        ),
                    },
                );
                const cancelled = result?.reason === 'user-cancelled' || state.controller.signal.aborted;
                sendArtifactDownloadProgress(event, transferId, cancelled
                    ? { state: 'cancelled', receivedBytes: state.receivedBytes, totalBytes: state.totalBytes }
                    : {
                        state: result?.success ? 'completed' : 'failed',
                        receivedBytes: state.receivedBytes,
                        totalBytes: state.totalBytes,
                        result: result || null,
                    });
            } catch (error) {
                sendArtifactDownloadProgress(event, transferId, state.controller.signal.aborted
                    ? {
                        state: 'cancelled',
                        receivedBytes: state.receivedBytes,
                        totalBytes: state.totalBytes,
                        error: artifactDownloadError(error),
                    }
                    : {
                        state: 'failed',
                        receivedBytes: state.receivedBytes,
                        totalBytes: state.totalBytes,
                        error: artifactDownloadError(error),
                    });
            } finally {
                workflowArtifactDownloads.delete(transferId);
            }
        })();
        return { success: true, transferId };
    });

    ipcMain.handle('cancel-artifact-download', async (event, input = {}) => {
        if (!isTrustedRendererEvent(event)) return { success: false, reason: 'untrusted-sender' };
        const transferId = String(input.transferId || '');
        const state = workflowArtifactDownloads.get(transferId);
        if (!state || state.senderId !== event.sender.id) {
            return { success: true, alreadyFinished: true };
        }
        state.controller.abort();
        return { success: true };
    });

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

    console.log('[main] IPC handlers registered');
}

// ============================================================================
// 应用生命周期
// ============================================================================

if (hasSingleInstanceLock) {
    app.on('second-instance', () => {
        if (!mainWindow || mainWindow.isDestroyed()) return;
        if (mainWindow.isMinimized()) mainWindow.restore();
        if (!mainWindow.isVisible()) mainWindow.show();
        mainWindow.focus();
    });
}

app.whenReady().then(async () => {
    if (!hasSingleInstanceLock) return;
    if (persistentUserDataError) {
        const detail = `无法访问固定数据目录 ${persistentUserDataPath || path.join(app.getPath('appData'), 'WordTTS')}\n\n${persistentUserDataError.message}`;
        if (isSmokeTest) {
            console.error(`[main] ${PRODUCT_NAME} 数据目录错误: ${detail}`);
        } else {
            dialog.showErrorBox(
                `${PRODUCT_NAME} 无法启动`,
                `${detail}\n\n请检查目录权限或磁盘空间；为避免覆盖/初始化新数据库，应用已停止。`,
            );
        }
        app.exit(1);
        return;
    }
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

    serverPort = await allocateServerPort();
    serverUrl = `http://127.0.0.1:${serverPort}`;
    serverToken = crypto.randomBytes(32).toString('hex');
    serverInstance = crypto.createHash('sha256').update(serverToken).digest('hex').slice(0, 16);
    console.log(`[main] 已分配独立后端地址: ${serverUrl}`);

    initializeUpdateManager();
    registerIpcHandlers();
    desktopServicesReady = true;
    startPythonServer();

    try {
        console.log('[main] 等待 Python 服务器就绪...');
        smokeLog('waiting for backend');
        await waitForServer();
        backendReady = true;
        if (isSmokeTest) {
            smokeLog('backend ready; creating smoke window');
            const smokeWindow = createWindow();
            try {
                await verifyRendererSmokeTest(smokeWindow, SMOKE_RENDERER_TIMEOUT_MS);
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
        updateManager?.start?.();
        return;
    }

    createWindow();
    updateManager?.start?.();
});

app.on('window-all-closed', () => {
    app.quit();
});

// 在应用退出前确保 Python 进程被终止，防止僵尸进程
app.on('will-quit', (event) => {
    isQuitting = true;
    updateManager?.dispose?.();
    workflowSseStreams.forEach(({ stream }) => stream.close());
    workflowSseStreams.clear();
    workflowArtifactStreams.forEach(({ stream }) => stream.destroy());
    workflowArtifactStreams.clear();
    workflowArtifactDownloads.forEach(({ controller }) => controller.abort());
    workflowArtifactDownloads.clear();
    cancelWorkflowSourceUploads();
    closeWorkflowSourceFiles();
    void sourceUploadStaging?.disposeAll('app-quit');
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
    if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
    }
});
