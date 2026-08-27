/**
 * Electron Preload 脚本
 * =======================
 * 通过 contextBridge 安全地暴露原生 API 给渲染进程。
 * 渲染进程通过 window.electronAPI 访问这些方法。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
    // 文件操作
    selectFile: () => ipcRenderer.invoke('select-file'),
    saveFileByPath: (sourcePath, suggestedName) => ipcRenderer.invoke('save-file-by-path', sourcePath, suggestedName),
    showInFolder: (filePath) => ipcRenderer.invoke('show-in-folder', filePath),

    // 窗口 POC：主进程是窗口状态唯一来源，渲染层不直接调用 BrowserWindow。
    getWindowState: () => ipcRenderer.invoke('window:get-state'),
    setWindowMode: (mode) => ipcRenderer.invoke('window:set-mode', mode),
    hideWindow: (privacy = false) => ipcRenderer.invoke('window:hide', { privacy: Boolean(privacy) }),
    showWindow: () => ipcRenderer.invoke('window:show'),
    onWindowState: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, state) => callback(state);
        ipcRenderer.on('window:state', handler);
        return () => ipcRenderer.removeListener('window:state', handler);
    },
    onGlobalShortcut: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on('global-shortcut', handler);
        return () => ipcRenderer.removeListener('global-shortcut', handler);
    },

    // 桌面设置只经主进程读写；渲染层不直接访问 settings.json。
    settings: {
        get: () => ipcRenderer.invoke('settings:get'),
        update: (patch) => ipcRenderer.invoke('settings:update', patch),
        reset: () => ipcRenderer.invoke('settings:reset'),
        importLegacy: (legacy) => ipcRenderer.invoke('settings:import-legacy', legacy),
    },

    task: {
        pause: (sessionId) => ipcRenderer.invoke('task:pause', sessionId),
        resume: (sessionId) => ipcRenderer.invoke('task:resume', sessionId),
        terminate: (sessionId) => ipcRenderer.invoke('task:terminate', sessionId),
    },

    // 专用浏览器控制请求由主进程转发到 Python 控制器，避免渲染层自行
    // 拼接敏感后端地址或尝试操作用户自己的 Chrome。
    browser: {
        getState: (sessionId) => ipcRenderer.invoke('browser:get-state', sessionId),
        show: (sessionId, options = {}) => ipcRenderer.invoke(
            'browser:show',
            sessionId,
            { minimize: options?.minimize === true },
        ),
        hide: (sessionId) => ipcRenderer.invoke('browser:hide', sessionId),
    },

    // 服务器
    backend: ipcRenderer.sendSync('backend-config'),
    serverReady: () => ipcRenderer.invoke('server-ready'),
    onAppNotice: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, notice) => callback(notice);
        ipcRenderer.on('app-notice', handler);
        return () => ipcRenderer.removeListener('app-notice', handler);
    },

    // 平台信息
    platform: process.platform,
});
