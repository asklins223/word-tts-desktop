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

let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;
let serverPort = null;
let serverUrl = null;
let serverToken = null;
let serverInstance = null;
let desktopServicesReady = false;
let rendererReady = false;
let rendererFatalShown = false;
let pythonStopPromise = null;
let quitCleanupStarted = false;
const pendingAppNotices = new Map();
const isSmokeTest = process.argv.includes('--smoke-test');
const PRODUCT_NAME = '小猪wordTTS';
const RENDERER_ENTRY_PATH = path.join(__dirname, 'renderer', 'index.html');
const RENDERER_ENTRY_URL = pathToFileURL(RENDERER_ENTRY_PATH).href;
const SMOKE_LOG_PATH = isSmokeTest
    ? path.join(os.tmpdir(), 'wordtts-electron-smoke.log')
    : null;
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
            const req = http.get(`${serverUrl}/api/health`, {
                headers: { 'X-WordTTS-Token': serverToken },
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
        show: !isSmokeTest,
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

    // 选择文件
    ipcMain.handle('select-file', nativeFileDialogs.selectFile);

    // 通过文件路径直接复制（校验源路径合法性）
    ipcMain.handle('save-file-by-path', nativeFileDialogs.saveFileByPath);

    // 检查服务器是否就绪
    ipcMain.handle('server-ready', async (event) => {
        if (!isTrustedRendererEvent(event)) return false;
        if (!pythonProcess) return false;
        try {
            await waitForServer();
            return true;
        } catch {
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
    console.log('[main] Electron app ready');
    smokeLog('app ready');
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

    registerIpcHandlers();
    desktopServicesReady = true;
    startPythonServer();

    try {
        console.log('[main] 等待 Python 服务器就绪...');
        smokeLog('waiting for backend');
        await waitForServer();
        if (isSmokeTest) {
            smokeLog('backend ready; creating smoke window');
            const smokeWindow = createWindow();
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
        return;
    }

    createWindow();
});

app.on('window-all-closed', () => {
    app.quit();
});

// 在应用退出前确保 Python 进程被终止，防止僵尸进程
app.on('will-quit', (event) => {
    isQuitting = true;
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
