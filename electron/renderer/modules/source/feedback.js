/** Renderer module: source.feedback */
(function attachRendererFeature_source_feedback(root) {
    'use strict';

function syncRestartButtonState(sourceBusy = null) {
    const restartBtn = $('restart-btn');
    if (!restartBtn) return;
    const busy = sourceBusy === null
        ? Boolean(isParsing || sourceImportInFlight)
        : Boolean(sourceBusy);
    restartBtn.disabled = Boolean(isRestarting || busy);
}

function setUploadParsing(parsing) {
    const active = Boolean(parsing || isParsing || sourceImportInFlight);
    // `connectService` may have become ready while an earlier import was
    // still unwinding. Defer the waiting file until this transition reaches
    // idle, rather than leaving it permanently in the waiting state.
    if (!active) schedulePendingServiceSourceFileImport();

    const uploadZone = $('upload-zone');
    if (!uploadZone) return;
    const waitingForService = Boolean(pendingServiceSourceFile) && !active;
    uploadZone.classList.toggle('is-processing', active);
    uploadZone.setAttribute('aria-busy', active ? 'true' : 'false');
    syncRestartButtonState(active);
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = active || isGenerating || isRestarting;
    const cancelButton = $('cancel-import-btn');
    if (cancelButton) {
        cancelButton.hidden = !active && !waitingForService;
        cancelButton.disabled = !active && !waitingForService;
        cancelButton.setAttribute('aria-busy', active && sourceImportController ? 'true' : 'false');
        cancelButton.textContent = active
            ? (sourceImportId ? '停止等待' : '停止导入')
            : '取消等待';
    }
}

function formatSourceBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function updateSourceImportProgress(stage = '', completed = null, total = null) {
    const panel = $('upload-progress');
    const label = $('upload-progress-label');
    const value = $('upload-progress-value');
    const progress = $('upload-progress-bar');
    if (!panel || !label || !value || !progress) return;
    if (!stage) {
        panel.hidden = true;
        progress.value = 0;
        value.textContent = '0%';
        return;
    }
    panel.hidden = false;
    label.textContent = stage;
    const hasTotal = Number.isFinite(Number(total)) && Number(total) > 0;
    const percent = hasTotal
        ? Math.min(100, Math.max(0, Math.floor((Number(completed) || 0) * 100 / Number(total))))
        : null;
    if (percent === null) {
        progress.removeAttribute('value');
        value.textContent = '处理中';
    } else {
        progress.value = percent;
        value.textContent = `${percent}%`;
    }
}

function createAbortError(message = 'source import was cancelled') {
    const error = new Error(message);
    error.name = 'AbortError';
    error.code = 'USER_CANCELLED';
    return error;
}

function throwIfSourceImportAborted(signal) {
    if (signal?.aborted) throw createAbortError();
}

async function abortSourceImportIfPossible(importId, reason = 'desktop-user-cancel') {
    if (!workflowApi || !importId || typeof workflowApi.abortSourceImport !== 'function') return false;
    try {
        const current = await workflowApi.getSourceImport(importId);
        const status = String(current?.current_status || current?.status || '');
        if (['READY', 'ABORTED', 'FAILED', 'EXPIRED'].includes(status)) return false;
        const stateVersion = Number(current?.state_version);
        if (!Number.isInteger(stateVersion) || stateVersion < 0) return false;
        await workflowApi.abortSourceImport(importId, {
            expected_state_version: stateVersion,
            reason,
        }, { idempotencyKey: `renderer-abort-source-${importId}` });
        return true;
    } catch (error) {
        // A write may have crossed the READY fence while the user cancelled.
        // In that case the source remains immutable and the local parser wait
        // is still stopped; never report it as a successful server abort.
        if (error?.code !== 'STATE_CONFLICT') {
            console.warn('停止源文件导入未能完成服务端收尾:', error);
        }
        return false;
    }
}

async function cancelSourceImport() {
    const controller = sourceImportController;
    if (!controller) {
        if (!pendingServiceSourceFile) return;
        const filename = sourceFileDisplayName(pendingServiceSourceFile);
        clearPendingServiceSourceFile();
        const uploadZone = $('upload-zone');
        uploadZone?.classList.remove('has-file', 'has-error');
        const uploadTitle = uploadZone?.querySelector('.upload-text-large');
        const uploadHint = uploadZone?.querySelector('.upload-hint');
        if (uploadTitle) uploadTitle.textContent = '拖拽文档到这里，或点击选择';
        if (uploadHint) uploadHint.textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
        setUploadFeedback('info', `已取消等待 ${filename}，可在服务连接后重新拖入文档。`);
        updateSourceImportProgress();
        setUploadParsing(false);
        if ($('status-text')) $('status-text').textContent = '已取消等待导入';
        return;
    }
    const hadServerImport = Boolean(sourceImportId);
    const cancelButton = $('cancel-import-btn');
    if (cancelButton) {
        cancelButton.disabled = true;
        cancelButton.textContent = '正在停止…';
        cancelButton.setAttribute('aria-busy', 'true');
    }
    setUploadFeedback('info', sourceImportId ? '正在停止等待，并核对源文件状态…' : '正在停止导入…');
    updateSourceImportProgress('正在停止导入…');
    controller.abort();
    const stagingId = sourceStagingUploadId;
    sourceStagingUploadId = null;
    if (stagingId && typeof window.electronAPI?.sourceUpload?.abort === 'function') {
        await window.electronAPI.sourceUpload.abort(stagingId).catch(() => {});
    }
    if (sourceImportId) await abortSourceImportIfPossible(sourceImportId);
    sourceTransportUploadId = null;
    $('status-text').textContent = hadServerImport
        ? '已停止等待；请确认源文件状态后再重新导入'
        : '已停止导入，可重新选择文档';
}

function setUploadFeedback(state = '', message = '') {
    const feedback = $('upload-feedback');
    const uploadZone = $('upload-zone');
    if (!feedback || !uploadZone) return;
    feedback.classList.remove('is-info', 'is-success', 'is-error');
    uploadZone.classList.remove('has-error');
    if (!message) {
        feedback.hidden = true;
        feedback.textContent = '';
        feedback.setAttribute('role', 'status');
        return;
    }
    feedback.hidden = false;
    feedback.textContent = message;
    if (state) feedback.classList.add(`is-${state}`);
    if (state === 'error') {
        uploadZone.classList.add('has-error');
        feedback.setAttribute('role', 'alert');
    } else {
        feedback.setAttribute('role', 'status');
    }
}

function updateConfigSummary() {
    updateGenerationModeUI(selectedGenerationMode());
    const paramsTarget = $('summary-params');
    if (paramsTarget) {
        const params = activeVoiceParams();
        paramsTarget.textContent = `${params.rate} / ${params.pitch} / ${params.volume}`;
    }

    // 音色摘要：显示当前默认男女声，角色音色在音色工作区逐个配置。
    const voiceEl = $('summary-voice');
    if (voiceEl) {
        const roleCount = Math.max(0, voiceRoles.length - 2);
        voiceEl.textContent = `女 ${voiceDisplayName(selectedDefaultFemaleVoice)} · 男 ${voiceDisplayName(selectedDefaultMaleVoice)}${roleCount ? ` · ${roleCount} 个角色` : ''}`;
    }

    const output = $('summary-output');
    if (output) {
        const format = $('format') ? $('format').value.toUpperCase() : 'MP3';
        const quality = $('quality') ? $('quality').value : '128 kbps（标准）';
        const qualityShort = quality.match(/^(\d+\s*kbps)/)?.[1] || quality;
        output.textContent = `${format} · ${qualityShort}`;
    }

    const scope = $('summary-scope');
    if (scope) scope.textContent = $('preview')?.checked ? '试听前 3 条' : '完整文档';
    updateSessionLabels(currentSession?.source_filename || '', currentSession?.parse_results);
}

function enforceOutputCompatibility() {
    const format = $('format');
    if (!format) return;
    // 输出格式固定为 MP3；质量只代表 MP3 码率，不再驱动格式切换。
    // 不依赖旧页面是否存在 MP3 option，直接重建唯一选项，杜绝 WAV 视觉回退。
    if (format.tagName === 'SELECT') {
        const option = document.createElement('option');
        option.value = 'mp3';
        option.textContent = 'MP3 · 通用格式';
        option.selected = true;
        format.replaceChildren(option);
        format.value = 'mp3';
        format.disabled = true;
        window.WordTTSUI?.syncSelect(format);
    } else {
        format.textContent = 'MP3 · 通用格式';
        format.dataset.format = 'mp3';
    }
    updateConfigSummary();
}


registerRendererModule("source.feedback", {
    syncRestartButtonState,
    setUploadParsing,
    formatSourceBytes,
    updateSourceImportProgress,
    createAbortError,
    throwIfSourceImportAborted,
    abortSourceImportIfPossible,
    cancelSourceImport,
    setUploadFeedback,
    updateConfigSummary,
    enforceOutputCompatibility,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

