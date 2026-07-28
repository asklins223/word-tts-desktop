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

    // 服务器
    serverReady: () => ipcRenderer.invoke('server-ready'),

    // 平台信息
    platform: process.platform,
});
