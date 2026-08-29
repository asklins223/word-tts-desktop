/**
 * Electron Preload 脚本
 * =======================
 * 通过 contextBridge 安全地暴露原生 API 给渲染进程。
 * 渲染进程通过 window.electronAPI 访问这些方法。
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');
const { createWorkflowApi } = require('./workflow-api');
const { createWorkflowEventTransport } = require('./workflow-event-transport');
const { createWorkflowArtifactTransport } = require('./workflow-artifact-transport');

const workflow = createWorkflowApi({
    request: (input) => ipcRenderer.invoke('workflow-request', input),
    uploadSourceFile: (input) => ipcRenderer.invoke('workflow-source-upload', input),
    openEvents: createWorkflowEventTransport(ipcRenderer),
    openArtifactStream: createWorkflowArtifactTransport(ipcRenderer),
});

contextBridge.exposeInMainWorld('electronAPI', {
    // 文件操作
    // Native selection uses an opaque streaming handle; the byte-returning
    // method remains as a compatibility fallback for older renderer callers.
    selectFile: () => ipcRenderer.invoke('select-source-file'),
    selectFileStream: () => ipcRenderer.invoke('select-source-file-stream'),
    saveFile: (bytes, suggestedName) => ipcRenderer.invoke('save-artifact-file', bytes, suggestedName),
    saveArtifactStream: (artifactId, suggestedName) => ipcRenderer.invoke(
        'save-artifact-stream',
        { artifactId, suggestedName },
    ),
    // 拖拽导入：渲染层只暴露 File -> 本地路径 的映射与分块暂存 API，
    // 文件内容按块经 IPC 进入主进程落盘，渲染进程不再整块持有文档。
    getPathForFile: (file) => {
        try {
            return typeof webUtils?.getPathForFile === 'function' ? webUtils.getPathForFile(file) : null;
        } catch (_) {
            return null;
        }
    },
    sourceUpload: {
        begin: (input) => ipcRenderer.invoke('source-upload-begin', input),
        write: (input) => ipcRenderer.invoke('source-upload-write', input),
        complete: (uploadId) => ipcRenderer.invoke('source-upload-complete', { uploadId }),
        abort: (uploadId) => ipcRenderer.invoke('source-upload-abort', { uploadId }),
    },

    // 服务器能力保留在主进程；renderer 只能通过下方的受限 workflow proxy 访问。
    workflow,
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
