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

const SERVER_PORT = 7863;
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;

let mainWindow = null;
let pythonProcess = null;
let isQuitting = false;

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

function startPythonServer() {
    const { cmd, args, cwd } = getServerCommand();

    console.log(`[main] 启动后端服务器: ${cmd} ${args.join(' ')} (cwd: ${cwd})`);

    // 校验可执行文件存在（打包模式）
    if (app.isPackaged && !fs.existsSync(cmd)) {
        const msg = `后端可执行文件不存在:\n${cmd}\n\n应用可能已损坏，请重新安装。`;
        console.error(`[main] ${msg}`);
        dialog.showErrorBox('后端启动失败', msg);
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
        },
        // Windows 下隐藏控制台窗口（后端输出通过 pipe 捕获）
        windowsHide: true,
    });

        // 收集 stderr 输出，用于崩溃时显示错误详情
    let stderrBuffer = '';
    const MAX_ERR_LINES = 30;

    pythonProcess.stdout.on('data', (data) => {
        console.log(`[python] ${data.toString().trim()}`);
    });

    pythonProcess.stderr.on('data', (data) => {
        const text = data.toString();
        console.error(`[python:err] ${text.trim()}`);
        // 保留最后 MAX_ERR_LINES 行用于错误提示
        stderrBuffer = (stderrBuffer + text).split('\n').slice(-MAX_ERR_LINES).join('\n');
    });

    pythonProcess.on('error', (err) => {
        console.error(`[main] 无法启动后端进程: ${err.message}`);
        dialog.showErrorBox('后端启动失败',
            `无法启动后端服务器:\n${err.message}\n\n` +
            `可执行文件: ${cmd}`);
    });

    pythonProcess.on('exit', (code) => {
        console.log(`[main] 后端进程退出，代码: ${code}`);
        pythonProcess = null;
        // 非正常退出且不是用户主动关闭时，提示用户
        if (code !== 0 && code !== null && !isQuitting) {
            const errDetail = stderrBuffer.trim()
                ? `\n\n--- 后端错误日志（最后 ${MAX_ERR_LINES} 行）---\n${stderrBuffer.trim()}`
                : '';
            dialog.showErrorBox('后端异常退出',
                `后端服务器异常退出（代码 ${code}）。\n` +
                `请查看日志获取更多信息，或重新启动应用。${errDetail}`);
        }
    });
}

/**
 * 异步停止 Python 服务器，确保进程完全退出后再 resolve。
 * 用于 will-quit 事件中阻塞应用退出，防止僵尸进程。
 */
function stopPythonServerAsync() {
    return new Promise((resolve) => {
        if (!pythonProcess) {
            resolve();
            return;
        }
        const proc = pythonProcess;
        let resolved = false;

        const done = () => {
            if (!resolved) {
                resolved = true;
                resolve();
            }
        };

        proc.once('exit', done);
        proc.kill('SIGTERM');

        // 3 秒后强制 SIGKILL
        setTimeout(() => {
            if (pythonProcess) {
                pythonProcess.kill('SIGKILL');
            }
            // 给 SIGKILL 一点时间生效
            setTimeout(done, 500);
        }, 3000);
    });
}

function waitForServer(maxRetries = 30) {
    return new Promise((resolve, reject) => {
        let retries = 0;
        const check = () => {
            const req = http.get(`${SERVER_URL}/api/health`, (res) => {
                if (res.statusCode === 200) resolve();
                else retry();
                res.resume();
            });
            req.on('error', () => retry());
            req.setTimeout(1500, () => { req.destroy(); retry(); });
        };
        const retry = () => {
            retries++;
            if (retries >= maxRetries) reject(new Error('服务器启动超时'));
            else setTimeout(check, 500);
        };
        check();
    });
}

// ============================================================================
// 窗口创建
// ============================================================================

function createWindow() {
    const isWin = process.platform === 'win32';
    const isMac = process.platform === 'darwin';

    const windowOptions = {
        width: 1280,
        height: 860,
        minWidth: 900,
        minHeight: 600,
        transparent: false,
        backgroundColor: '#faf9f7',
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
        // Windows: 隐藏标题栏但保留原生窗口控件（最小化/最大化/关闭）
        // 不使用 frame: false，否则 Windows 上没有窗口控件按钮
        windowOptions.titleBarStyle = 'hidden';
        windowOptions.autoHideMenuBar = true;
    } else {
        // Linux: 使用原生框架
        windowOptions.frame = true;
    }

    mainWindow = new BrowserWindow(windowOptions);

    mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

    if (process.argv.includes('--dev')) {
        mainWindow.webContents.openDevTools({ mode: 'detach' });
    }

    mainWindow.on('closed', () => { mainWindow = null; });
}

// ============================================================================
// IPC 注册
// ============================================================================

function registerIpcHandlers() {
    // 选择文件
    ipcMain.handle('select-file', async () => {
        if (!mainWindow) return null;
        const result = await dialog.showOpenDialog(mainWindow, {
            title: '选择 Word 文档',
            filters: [{ name: 'Word 文档', extensions: ['docx'] }],
            properties: ['openFile'],
        });
        if (result.canceled || result.filePaths.length === 0) return null;
        return result.filePaths[0];
    });

    // 通过文件路径直接复制（校验源路径合法性）
    ipcMain.handle('save-file-by-path', async (event, sourcePath, suggestedName) => {
        if (!mainWindow) return false;

        // 安全校验：源路径必须在允许的目录内，防止复制系统敏感文件
        const allowedRoots = [];
        if (app.isPackaged) {
            // 打包模式：后端输出文件在用户数据目录（与 server.py 的 BASE_DIR 一致）
            const home = app.getPath('home');
            let dataDir;
            if (process.platform === 'darwin') {
                dataDir = path.join(home, 'Library', 'Application Support', 'WordTTS');
            } else if (process.platform === 'win32') {
                dataDir = path.join(app.getPath('appData'), 'WordTTS');
            } else {
                dataDir = path.join(home, '.wordtts');
            }
            allowedRoots.push(dataDir);
            // 打包资源目录（读取资源文件时可能需要）
            allowedRoots.push(path.join(process.resourcesPath, 'server_backend'));
        } else {
            // 开发模式：项目根目录
            allowedRoots.push(path.resolve(__dirname, '..'));
        }
        const resolvedSource = path.resolve(sourcePath);
        const isAllowed = allowedRoots.some(root =>
            resolvedSource === root || resolvedSource.startsWith(root + path.sep)
        );
        if (!isAllowed) {
            console.error('[main] 拒绝复制允许目录外的文件:', sourcePath);
            return false;
        }

        // 安全校验：源文件必须存在
        try {
            if (!fs.existsSync(sourcePath)) {
                console.error('[main] 源文件不存在:', sourcePath);
                return false;
            }
        } catch (err) {
            console.error('[main] 检查源文件失败:', err);
            return false;
        }

        const result = await dialog.showSaveDialog(mainWindow, {
            title: '保存文件',
            defaultPath: suggestedName,
        });
        if (result.canceled || !result.filePath) return false;
        try {
            fs.copyFileSync(sourcePath, result.filePath);
            return true;
        } catch (err) {
            console.error('[main] 复制文件失败:', err);
            return false;
        }
    });

    // 检查服务器是否就绪
    ipcMain.handle('server-ready', async () => {
        try {
            await waitForServer(30);
            return true;
        } catch {
            return false;
        }
    });

    // 在 Finder 中显示
    ipcMain.handle('show-in-folder', async (event, filePath) => {
        if (filePath && fs.existsSync(filePath)) {
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

    registerIpcHandlers();
    startPythonServer();

    try {
        console.log('[main] 等待 Python 服务器就绪...');
        await waitForServer(40);
        console.log('[main] 服务器就绪，创建窗口');
    } catch (err) {
        console.error('[main] 服务器启动失败:', err.message);
        // 服务器启动失败时仍然创建窗口，让用户能看到错误提示
        createWindow();
        if (mainWindow) {
            mainWindow.webContents.on('did-finish-load', () => {
                mainWindow.webContents.executeJavaScript(
                    `showToast('后端服务启动失败，请检查应用是否完整');`
                );
            });
        }
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
    if (pythonProcess) {
        event.preventDefault();
        stopPythonServerAsync().then(() => {
            // 进程已终止，真正退出
            app.exit(0);
        });
    }
});

app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
    }
});
