/**
 * Electron Preload 脚本
 * =======================
 * 通过 contextBridge 安全地暴露原生 API 给渲染进程。
 * 渲染进程通过 window.electronAPI 访问这些方法。
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');
const { createWorkflowApi } = require('./workflow-api');
const { createWorkflowEventTransport } = require('./workflow-event-transport');
const { createWorkflowArtifactEventTransport } = require('./workflow-artifact-transport');

const workflow = createWorkflowApi({
    request: (input) => ipcRenderer.invoke('workflow-request', input),
    // The progress callback is a renderer-only function and must not cross
    // Electron's structured-clone boundary.  The main process emits progress
    // on the dedicated channel exposed below.
    uploadSourceFile: ({ onProgress: _onProgress, ...input }) => ipcRenderer.invoke('workflow-source-upload', input),
    cancelSourceUpload: (input) => ipcRenderer.invoke('workflow-source-upload-cancel', input),
    openEvents: createWorkflowEventTransport(ipcRenderer),
    // Keep the native stream behind the bridge. Renderer-side code rebuilds a
    // ReadableStream from this contextBridge-safe event transport.
    openArtifactStream: createWorkflowArtifactEventTransport(ipcRenderer),
});

contextBridge.exposeInMainWorld('electronAPI', {
    // 文件操作
    // Native selection uses an opaque streaming handle; the byte-returning
    // method remains as a compatibility fallback for older renderer callers.
    selectFile: () => ipcRenderer.invoke('select-source-file'),
    selectFileStream: () => ipcRenderer.invoke('select-source-file-stream'),
    selectAudioFiles: () => ipcRenderer.invoke('select-audio-files-stream'),
    selectAudioFolder: () => ipcRenderer.invoke('select-audio-folder-stream'),
    openAudioDropFiles: (files) => {
        const selectedFiles = Array.from(files || []);
        const paths = selectedFiles.map((file) => {
            try { return typeof webUtils?.getPathForFile === 'function' ? webUtils.getPathForFile(file) : null; }
            catch (_) { return null; }
        });
        if (!paths.length || paths.some((pathname) => typeof pathname !== 'string' || pathname.length === 0)) {
            return Promise.resolve({
                success: false,
                reason: 'file-path-unavailable',
                error: '无法安全读取部分拖入文件，请使用“选择音频或 ZIP”或“选择文件夹”重新导入。',
            });
        }
        return ipcRenderer.invoke('open-audio-drop-files-stream', { paths });
    },
    releaseSourceFile: (sourceFileId) => ipcRenderer.invoke('release-source-file', { sourceFileId }),
    saveFile: (bytes, suggestedName) => ipcRenderer.invoke('save-artifact-file', bytes, suggestedName),
    saveArtifactStream: (artifactId, suggestedName) => ipcRenderer.invoke(
        'save-artifact-stream',
        { artifactId, suggestedName },
    ),
    startArtifactDownload: (artifactId, suggestedName, transferId) => ipcRenderer.invoke(
        'save-artifact-stream-start',
        { artifactId, suggestedName, transferId },
    ),
    cancelArtifactDownload: (transferId) => ipcRenderer.invoke(
        'cancel-artifact-download',
        { transferId },
    ),
    onArtifactDownloadProgress: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, progress) => callback(progress);
        ipcRenderer.on('artifact-download-progress', handler);
        return () => ipcRenderer.removeListener('artifact-download-progress', handler);
    },
    onSourceUploadProgress: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, progress) => callback(progress);
        ipcRenderer.on('source-upload-progress', handler);
        return () => ipcRenderer.removeListener('source-upload-progress', handler);
    },
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
    // Region choices are a bundled read-only snapshot.  The renderer uses it
    // only to populate searchable page-form suggestions; it never turns the
    // snapshot into an external write request.
    readRegionTree: () => ipcRenderer.invoke('read-region-tree'),
    serverReady: () => ipcRenderer.invoke('server-ready'),
    onAppNotice: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const handler = (_event, notice) => callback(notice);
        ipcRenderer.on('app-notice', handler);
        return () => ipcRenderer.removeListener('app-notice', handler);
    },
    // 更新下载、安装和 GitHub Release 页面均由主进程执行；renderer 只接收
    // 可序列化状态，不能自行拼接远程地址或调用 Node/Electron API。
    update: {
        getStatus: () => ipcRenderer.invoke('update-status'),
        check: () => ipcRenderer.invoke('update-check'),
        download: () => ipcRenderer.invoke('update-download'),
        install: () => ipcRenderer.invoke('update-install'),
        openReleasePage: () => ipcRenderer.invoke('open-update-release'),
        onStateChange: (callback) => {
            if (typeof callback !== 'function') return () => {};
            const handler = (_event, state) => callback(state);
            ipcRenderer.on('app-update', handler);
            return () => ipcRenderer.removeListener('app-update', handler);
        },
    },

    // 平台信息
    platform: process.platform,
});
