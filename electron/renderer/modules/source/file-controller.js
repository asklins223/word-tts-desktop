/** Renderer module: source.fileController */
(function attachRendererFeature_source_fileController(root) {
    'use strict';

// ============================================================================
// Step 1: 文件上传
// ============================================================================

async function selectFile() {
    if (isParsing || sourceImportInFlight || sourceFileDialogInFlight || isRestarting || isForcedUpdateBlocking() || $('upload-zone')?.getAttribute('aria-disabled') === 'true') {
        return;
    }
    if (isElectron) {
        sourceFileDialogInFlight = true;
        try {
            const result = typeof window.electronAPI.selectFileStream === 'function'
                ? await window.electronAPI.selectFileStream()
                : await window.electronAPI.selectFile();
            if (result?.success && result.sourceFileId && result.fileName) {
                void processSourceFileReference(result.sourceFileId, result.fileName, result.sizeBytes);
            } else if (result?.success && result.bytes && result.fileName) {
                // Compatibility fallback for an older preload that does not
                // expose the opaque native file stream handle.
                void processSourceBytes(result.bytes, result.fileName);
            } else if (result != null && result?.reason !== 'user-cancelled') {
                await showNativeFileDialogError('选择文档失败', result?.reason ? result : {
                reason: result?.success === true ? 'dialog-error' : 'ipc-error',
                error: '主进程未返回有效的文档流',
                });
            }
        } catch (error) {
            console.error('打开文件选择框失败:', error);
            await showNativeFileDialogError('选择文档失败', {
                reason: 'ipc-error',
                error: error?.message,
            });
        } finally {
            sourceFileDialogInFlight = false;
        }
    } else {
        $('hidden-file-input').click();
    }
}

function handleFileSelected(file) {
    if (isParsing || sourceImportInFlight || isRestarting || isForcedUpdateBlocking() || $('upload-zone')?.getAttribute('aria-disabled') === 'true') return;
    void ingestSourceFile(file);
}

function isSupportedSourceFile(file) {
    const filename = String(file?.name || '').split(/[\\/]/).pop().toLowerCase();
    return ['.docx', '.xlsx'].some(extension => filename.endsWith(extension));
}

function sourceFileDisplayName(file) {
    return String(file?.name || '文档').split(/[\\/]/).pop() || '文档';
}

function sourceImportServiceIsReady() {
    return sourceImportServiceState === 'ready';
}

function sourceImportIsBusy() {
    return Boolean(isParsing || sourceImportInFlight || isRestarting);
}

function pendingSourceFilePresentation(
    filename,
    serviceState = sourceImportServiceState,
    sourceBusy = sourceImportIsBusy(),
) {
    const safeFilename = String(filename || '文档');
    if (serviceState === 'unavailable') {
        return {
            hint: '生成服务暂不可用。重试连接成功后会自动开始导入。',
            feedback: `已保留 ${safeFilename}。生成服务暂不可用；点击“重试连接”后会自动导入。`,
            status: `等待服务重试：${safeFilename}`,
        };
    }
    if (serviceState === 'ready') {
        if (sourceBusy) {
            return {
                hint: '服务已连接，当前导入完成后会自动开始。',
                feedback: `已接收 ${safeFilename}，当前文档导入完成后会自动开始。`,
                status: `等待当前导入：${safeFilename}`,
            };
        }
        return {
            hint: '服务已连接，正在开始导入…',
            feedback: `已接收 ${safeFilename}，正在开始导入。`,
            status: `准备导入：${safeFilename}`,
        };
    }
    return {
        hint: '正在等待生成服务连接，连接后会自动开始导入。',
        feedback: `已接收 ${safeFilename}，正在等待生成服务连接；连接后会自动导入。`,
        status: `等待服务连接：${safeFilename}`,
    };
}

function globalFileDropPresentation(serviceState = sourceImportServiceState) {
    if (serviceState === 'unavailable') {
        return {
            title: '松开后保留文档',
            hint: '支持 .docx / .xlsx · 重试连接成功后会自动导入',
        };
    }
    if (serviceState !== 'ready') {
        return {
            title: '松开后等待服务连接',
            hint: '支持 .docx / .xlsx · 服务连接后会自动导入',
        };
    }
    return {
        title: '松开以导入文档',
        hint: '支持 .docx / .xlsx · 将在当前工作台创建新任务',
    };
}

function refreshPendingServiceSourceFilePresentation() {
    if (!pendingServiceSourceFile) return false;
    const filename = sourceFileDisplayName(pendingServiceSourceFile);
    const presentation = pendingSourceFilePresentation(filename);
    const uploadZone = $('upload-zone');
    uploadZone?.classList.add('has-file', 'is-queued');
    uploadZone?.classList.remove('has-error');
    const uploadTitle = uploadZone?.querySelector('.upload-text-large');
    const uploadHint = uploadZone?.querySelector('.upload-hint');
    if (uploadTitle) uploadTitle.textContent = filename;
    if (uploadHint) uploadHint.textContent = presentation.hint;
    setUploadFeedback('info', presentation.feedback);
    updateSourceImportProgress();
    setUploadParsing(false);
    if ($('status-text')) $('status-text').textContent = presentation.status;
    return true;
}

function clearPendingServiceSourceFile() {
    pendingServiceSourceFile = null;
    $('upload-zone')?.classList.remove('is-queued');
}

function queueSourceFileUntilServiceReady(file) {
    const previous = pendingServiceSourceFile;
    pendingServiceSourceFile = file;
    refreshPendingServiceSourceFilePresentation();
    if (previous && sourceFileDisplayName(previous) !== sourceFileDisplayName(file)) {
        showToast(`已将等待导入的文档替换为 ${sourceFileDisplayName(file)}`, 'info');
    }
}

function canImportPendingServiceSourceFile() {
    return Boolean(
        sourceImportServiceIsReady()
        && pendingServiceSourceFile
        && !sourceImportIsBusy()
        && !incomingFileDropInFlight
        && !isForcedUpdateBlocking()
    );
}

function schedulePendingServiceSourceFileImport() {
    if (pendingServiceSourceImportScheduled || !canImportPendingServiceSourceFile()) return false;
    pendingServiceSourceImportScheduled = true;
    // Let the import that just cleared its busy flags finish all cleanup
    // first; starting synchronously here would let that older finalizer reset
    // the new import's progress UI.
    Promise.resolve().then(() => {
        pendingServiceSourceImportScheduled = false;
        void importPendingServiceSourceFile();
    });
    return true;
}

async function importPendingServiceSourceFile() {
    if (!canImportPendingServiceSourceFile()) return false;
    const file = pendingServiceSourceFile;
    clearPendingServiceSourceFile();
    return handleIncomingSourceFile(file);
}

function setGlobalFileDropActive(active) {
    globalFileDragActive = Boolean(active) && !isForcedUpdateBlocking();
    const overlay = $('global-drop-overlay');
    if (overlay) {
        const presentation = globalFileDropPresentation();
        const title = $('global-drop-title');
        const hint = $('global-drop-hint');
        if (title) title.textContent = presentation.title;
        if (hint) hint.textContent = presentation.hint;
        overlay.hidden = !globalFileDragActive;
        overlay.setAttribute('aria-hidden', globalFileDragActive ? 'false' : 'true');
    }
    document.body.classList.toggle('has-global-file-drop', globalFileDragActive);
}

function isFileDragEvent(event) {
    return Array.from(event?.dataTransfer?.types || []).includes('Files');
}

function hasActiveTaskForIncomingFile() {
    if (!currentSession?.session_id) return false;
    const snapshot = currentWorkspace?.snapshot || currentWorkspace || currentSession;
    return !isTerminalWorkflowSnapshot(snapshot) && generationResult !== 'done';
}

async function handleIncomingSourceFile(file) {
    if (!file || incomingFileDropInFlight || isForcedUpdateBlocking()) return;
    if (!isSupportedSourceFile(file)) {
        setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 或 .xlsx 文档。');
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    if (isParsing || sourceImportInFlight || isRestarting) {
        showToast('当前文档仍在导入，请等待本次操作完成', 'warning');
        return;
    }
    if (!sourceImportServiceIsReady()) {
        queueSourceFileUntilServiceReady(file);
        return;
    }
    incomingFileDropInFlight = true;
    try {
        if (hasActiveTaskForIncomingFile()) {
            const filename = String(file.name || '新文档').split(/[\\/]/).pop();
            const confirmed = await showConfirmDialog({
                kicker: '检测到新文档',
                title: '使用这个文件新建任务？',
                message: '当前任务仍在进行中。确认后将切换到「' + filename + '」，当前任务会先完成清理。',
                detail: '当前任务不会从历史记录中删除；如果任务已经产生结果，可稍后从历史记录继续查看。',
                tone: 'warning',
                confirmLabel: '使用此文件',
            });
            if (!confirmed) return;
        }

        if (currentSession?.session_id) {
            setRestartingUI(true);
            let cleanupConfirmed = false;
            try {
                cleanupConfirmed = await restart({ notify: false });
            } finally {
                setRestartingUI(false);
                setAppInteractive($('service-state')?.classList.contains('is-ready') === true);
            }
            if (!cleanupConfirmed) {
                showToast('旧任务尚未完成清理，暂未导入新文件', 'warning');
                return;
            }
        }
        handleFileSelected(file);
    } finally {
        incomingFileDropInFlight = false;
        schedulePendingServiceSourceFileImport();
    }
}

async function readBoundedSourceFile(file, maxBytes, signal) {
    if (typeof file?.stream !== 'function') {
        const error = new Error('当前环境不支持流式读取源文档');
        error.code = 'STREAM_UNSUPPORTED';
        throw error;
    }
    const reader = file.stream().getReader();
    const chunks = [];
    let total = 0;
    try {
        while (true) {
            throwIfSourceImportAborted(signal);
            const part = await reader.read();
            if (part.done) break;
            const chunk = part.value instanceof Uint8Array
                ? part.value
                : new Uint8Array(part.value || []);
            if (chunk.byteLength === 0) continue;
            if (total + chunk.byteLength > maxBytes) {
                const error = new Error(`兼容导入仅支持不超过 ${formatSourceBytes(maxBytes)} 的文档`);
                error.code = 'SOURCE_SIZE_LIMIT';
                throw error;
            }
            chunks.push(chunk);
            total += chunk.byteLength;
            updateSourceImportProgress('正在读取兼容文档', total, Number(file.size) || total);
        }
    } catch (error) {
        await reader.cancel().catch(() => {});
        throw error;
    } finally {
        reader.releaseLock?.();
    }
    const expectedSize = Number(file.size);
    if (Number.isSafeInteger(expectedSize) && total !== expectedSize) {
        const error = new Error('文档流读取长度与文件大小不一致');
        error.code = 'SOURCE_SIZE_MISMATCH';
        throw error;
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    chunks.forEach(chunk => {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    });
    return bytes;
}

/**
 * 导入拖拽/选择的源文档。Electron 下通过主进程分块暂存：渲染层每次只
 * 持有一个分块（约 4MB），文件内容由主进程在允许目录内落盘后按一次性
 * 句柄流式上传，300MB 级文档不再整块进入渲染进程内存。暂存不可用时
 * 只允许明确标记的 <=16MiB 兼容路径，不能悄悄把大文档聚合进内存。
 */
async function ingestSourceFile(file) {
    if (!file || typeof file.slice !== 'function' || isForcedUpdateBlocking()) return;
    const filename = String(file.name || '').split(/[\\/]/).pop() || '';
    const extension = filename.toLowerCase().slice(filename.lastIndexOf('.'));
    const size = Number(file.size);
    if (!['.docx', '.xlsx'].includes(extension)) {
        setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 或 .xlsx 文档。');
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    if (!Number.isSafeInteger(size) || size <= 0) {
        setUploadFeedback('error', '文档大小无效，请重新选择文件。');
        showToast('文档大小无效，请重新选择', 'error');
        return;
    }
    const controller = new AbortController();
    sourceImportController = controller;
    sourceImportInFlight = true;
    const uploadZone = $('upload-zone');
    const uploadTitle = uploadZone?.querySelector('.upload-text-large');
    const uploadHint = uploadZone?.querySelector('.upload-hint');
    if (uploadTitle) uploadTitle.textContent = filename;
    if (uploadHint) uploadHint.textContent = '正在接收文档…';
    uploadZone?.classList.add('has-file');
    setUploadParsing(true);
    updateSourceImportProgress('准备读取文档', 0, size);
    setUploadFeedback('info', `正在准备导入 ${filename} · ${formatSourceBytes(size)}`);
    $('status-text').textContent = `正在导入: ${filename}`;
    const staging = isElectron ? window.electronAPI?.sourceUpload : null;
    if (staging && typeof staging.begin === 'function') {
        let uploadId = null;
        let sourceProcessingStarted = false;
        try {
            throwIfSourceImportAborted(controller.signal);
            const opened = await staging.begin({ fileName: filename, sizeBytes: size });
            uploadId = opened.uploadId;
            sourceStagingUploadId = uploadId;
            const chunkSize = Number(opened.chunkSize) || 4 * 1024 * 1024;
            let offset = 0;
            while (offset < size) {
                throwIfSourceImportAborted(controller.signal);
                updateSourceImportProgress('正在读取源文档', offset, size);
                const chunk = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
                throwIfSourceImportAborted(controller.signal);
                if (chunk.byteLength <= 0) throw new Error('文档分块为空，无法继续导入');
                await staging.write({ uploadId, offset, bytes: chunk });
                offset += chunk.byteLength;
                updateSourceImportProgress('正在上传源文档', offset, size);
                setUploadFeedback('info', `正在上传源文档 · ${formatSourceBytes(offset)} / ${formatSourceBytes(size)}`);
            }
            throwIfSourceImportAborted(controller.signal);
            const completed = await staging.complete(uploadId);
            sourceStagingUploadId = null;
            if (!completed?.success || !completed.sourceFileId) {
                throw new Error(completed?.reason ? `文档流式导入未通过校验：${completed.reason}` : '文档流式导入失败');
            }
            updateSourceImportProgress('正在解析文档结构');
            sourceProcessingStarted = true;
            await processSourceFileReference(completed.sourceFileId, completed.fileName, completed.sizeBytes, { controller });
            return;
        } catch (error) {
            if (uploadId) await staging.abort(uploadId).catch(() => {});
            sourceStagingUploadId = null;
            if (sourceProcessingStarted) return;
            if (error?.name === 'AbortError') {
                setUploadFeedback('info', '已停止导入，可重新选择文档。');
                updateSourceImportProgress();
                if (sourceImportController === controller) sourceImportController = null;
                sourceImportInFlight = false;
                sourceTransportUploadId = null;
                setUploadParsing(false);
                return;
            }
            if (size > MAX_BROWSER_SOURCE_BYTES) {
                console.error('文档流式导入失败，拒绝大文件兼容回退:', error);
                setUploadFeedback('error', '当前环境无法继续流式导入大文件，请在 Electron 中重试，或选择不超过 16MiB 的文档。');
                showToast('大文件流式导入失败，未继续整块读取', 'error');
                updateSourceImportProgress();
                if (sourceImportController === controller) sourceImportController = null;
                sourceImportInFlight = false;
                sourceTransportUploadId = null;
                setUploadParsing(false);
                return;
            }
            console.warn('文档流式导入失败，使用受限兼容读取:', error);
            showToast('流式导入不可用，改用不超过 16MiB 的兼容读取', 'warning');
        }
    }
    if (size > MAX_BROWSER_SOURCE_BYTES) {
        setUploadFeedback('error', '当前环境不支持超过 16MiB 的兼容导入，请使用 Electron 原生选择/拖拽，或选择较小文档。');
        updateSourceImportProgress();
        sourceImportInFlight = false;
        sourceImportController = null;
        sourceTransportUploadId = null;
        setUploadParsing(false);
        return;
    }
    if (!workflowApi) {
        setUploadFeedback('error', '当前页面没有连接桌面工作流服务，请使用小猪wordTTS桌面应用导入文档。');
        showToast('工作流服务未连接，请使用桌面应用重试', 'error');
        updateSourceImportProgress();
        sourceImportInFlight = false;
        sourceImportController = null;
        sourceTransportUploadId = null;
        setUploadParsing(false);
        return;
    }
    try {
        throwIfSourceImportAborted(controller.signal);
        updateSourceImportProgress('正在读取兼容文档', 0, size);
        const bytes = await readBoundedSourceFile(file, MAX_BROWSER_SOURCE_BYTES, controller.signal);
        throwIfSourceImportAborted(controller.signal);
        await processSourceBytes(bytes, filename, { controller });
    } catch (error) {
        if (error?.name !== 'AbortError') {
            console.error('兼容方式读取文档失败:', error);
            setUploadFeedback('error', `文档读取失败：${error.message || '请重新选择文件'}`);
        } else {
            setUploadFeedback('info', '已停止导入，可重新选择文档。');
        }
    } finally {
        if (sourceImportController === controller) sourceImportController = null;
        sourceImportInFlight = false;
        sourceTransportUploadId = null;
        updateSourceImportProgress();
        setUploadParsing(false);
    }
}


registerRendererModule("source.fileController", {
    selectFile,
    handleFileSelected,
    isSupportedSourceFile,
    sourceFileDisplayName,
    sourceImportServiceIsReady,
    sourceImportIsBusy,
    pendingSourceFilePresentation,
    globalFileDropPresentation,
    refreshPendingServiceSourceFilePresentation,
    clearPendingServiceSourceFile,
    queueSourceFileUntilServiceReady,
    canImportPendingServiceSourceFile,
    schedulePendingServiceSourceFileImport,
    importPendingServiceSourceFile,
    setGlobalFileDropActive,
    isFileDragEvent,
    hasActiveTaskForIncomingFile,
    handleIncomingSourceFile,
    readBoundedSourceFile,
    ingestSourceFile,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

