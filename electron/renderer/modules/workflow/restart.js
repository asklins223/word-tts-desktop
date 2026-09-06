/** Renderer module: workflow.restart */
(function attachRendererFeature_workflow_restart(root) {
    'use strict';

// ============================================================================
// 重新开始
// ============================================================================

function resetGenerateState() {
    clearGenerationStartupTimer();
    setProgressIndeterminate(false);
    isGenerating = false;
    if (!cancelWorkflowPromise) generationCancelRequested = false;
    syncRestartButtonState();
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = isRestarting || isParsing;
    updateGenerationCancelUI();
}

async function requestRestart() {
    if (isRestarting) return;
    if (currentSession) {
        let confirmation = {
            kicker: '当前任务',
            title: '更换当前文档？',
            message: '当前文档会话将结束，随后可以导入新的文档。',
            detail: '尚未保存到历史记录的临时结果会被清理。',
            tone: 'warning',
            confirmLabel: '更换文档',
        };
        if (isGenerating) {
            confirmation = {
                kicker: '生成任务进行中',
                title: '中止并新建任务？',
                message: '当前音频仍在生成，新建任务会立即中止本次处理。',
                detail: '本次尚未完成的结果会被清理，此操作无法撤销。',
                tone: 'danger',
                confirmLabel: '中止并新建',
            };
        } else if (generatedFiles.length > 0 || currentStep === 4) {
            confirmation = latestCurrentResultEvent?.workflow_id
                ? {
                    kicker: '结果已保存',
                    title: '开始一个新任务？',
                    message: '本次结果已经保存在历史记录中，可以安全开始新任务。',
                    tone: 'info',
                    confirmLabel: '开始新任务',
                }
                : {
                    kicker: '结果尚未保存',
                    title: '仍要开始新任务？',
                    message: '本次结果未能保存到历史记录，新建任务会清理当前结果。',
                    detail: '请先确认需要的音频已经下载到本机。',
                    tone: 'danger',
                    confirmLabel: '清理并新建',
                };
        }
        if (!await showConfirmDialog(confirmation)) return;
    }
    setRestartingUI(true);
    try {
        await restart();
    } finally {
        setRestartingUI(false);
        setAppInteractive($('service-state')?.classList.contains('is-ready') === true);
    }
}

async function restart({ notify = true } = {}) {
    destroyWaveSurfers();
    if (activeArtifactTransfer) {
        await cancelArtifactTransfer();
        activeArtifactTransfer = null;
        hideArtifactTransferProgress();
    }
    // 先让所有在途异步回调失效，避免清理请求期间旧任务重新接管页面。
    parseAttemptId++;
    const sourceImportToAbort = sourceImportId;
    const sourceUploadToAbort = sourceStagingUploadId;
    sourceImportController?.abort();
    sourceImportController = null;
    sourceImportInFlight = false;
    clearPendingServiceSourceFile();
    sourceImportId = null;
    sourceStagingUploadId = null;
    sourceTransportUploadId = null;
    if (sourceUploadToAbort && typeof window.electronAPI?.sourceUpload?.abort === 'function') {
        await window.electronAPI.sourceUpload.abort(sourceUploadToAbort).catch(() => {});
    }
    if (sourceImportToAbort) await abortSourceImportIfPossible(sourceImportToAbort, 'desktop-restart');
    if (parseAbortController) {
        parseAbortController.abort();
        parseAbortController = null;
    }
    isParsing = false;
    generationAttemptId++;
    if (generateAbortController) {
        generateAbortController.abort();
        generateAbortController = null;
    }
    if (resultNavigationTimer) {
        clearTimeout(resultNavigationTimer);
        resultNavigationTimer = null;
    }
    clearSSEReconnectTimer();
    clearGenerationStartupTimer();
    sseConnectionToken++;

    // 断开 SSE
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }

    const sessionToCleanup = currentSession;
    let cleanupConfirmed = true;
    // 取消接口现在会立即完成本地终态。先完成这一次本地取消，再清空
    // renderer 会话，避免“旧任务还在后台、页面却已经新建任务”的竞态。
    if (sessionToCleanup) {
        try {
            const snapshot = await cancelCurrentWorkflow(sessionToCleanup, {
                reason: 'desktop-restart',
            });
            cleanupConfirmed = isCancellationSettledSnapshot(snapshot);
        } catch (error) {
            console.error('任务取消失败:', error);
            cleanupConfirmed = false;
        }
        if (!cleanupConfirmed) {
            setServiceState('warning', '任务停止失败');
            $('retry-service-btn').hidden = false;
            $('status-text').textContent = '任务停止失败，请重试停止后再开始新任务';
            return false;
        }
    }
    currentSession = null;
    resetReviewNavigationState();
    isGenerating = false;

    // 重置状态
    generatedFiles = [];
    currentWorkspace = null;
    systemInputDeliveryModeDraft = '';
    systemInputUnitDrafts = new Map();
    systemInputSelectedUnitId = '';
    activeWorkspace = 'import';
    clearTimeout(workspaceRefreshTimer);
    workspaceRefreshTimer = null;
    clearSystemInputRefreshTimer();
    systemInputRefreshInFlight = false;
    activeResultContext = null;
    latestCurrentResultEvent = null;
    historyRequestToken++;
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    transientGenerationErrorMessage = '';
    lastGenerationConfig = null;
    resetTaskVoiceConfiguration();

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file', 'has-error', 'is-processing', 'is-queued', 'dragover');
    uploadZone.setAttribute('aria-busy', 'false');
    uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
    setUploadFeedback();
    updateSourceImportProgress();
    setUploadParsing(false);
    updateSessionLabels();

    // 刷新预设列表（可能在上一次操作中保存了新配置）
    refreshPresetUI();

    // 重置 Step 3
    setProgressBarPercent(0);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', '0% 处理中');
    setProgressReadoutMode(false);
    setProgressIndeterminate(false);
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = '—';
    $('progress-failed').textContent = '0';
    if ($('progress-cancelled')) $('progress-cancelled').textContent = '0';
    if ($('progress-skipped')) $('progress-skipped').textContent = '0';
    if ($('progress-deliverable')) $('progress-deliverable').textContent = '0 / 0';
    $('gen-title').textContent = '正在生成音频';
    setGenerationVisualState('running');
    hideGenerationRecovery();
    resetLogTimeline('任务开始后，这里会按阶段展示详细处理记录。');
    $('type-stats').innerHTML = '';

    // 重置 Step 4
    $('audio-list').innerHTML = '<div class="audio-empty">暂无音频文件</div>';
    $('audio-count').textContent = '0 个文件';
    prepareAudioFilters([]);
    $('audio-filter-empty').hidden = true;
    document.querySelector('.audio-list-section').hidden = false;
    $('result-summary').textContent = '';
    $('result-success-label').textContent = '已生成';
    $('result-success-count').textContent = '0';
    $('result-success-caption').textContent = '音频文件';
    $('result-secondary-label').textContent = '未完成';
    $('result-failed-count').textContent = '0';
    if ($('result-cancelled-count')) $('result-cancelled-count').textContent = '0';
    $('result-secondary-caption').textContent = '待处理内容';
    $('result-format-value').textContent = 'MP3';
    $('result-hero').classList.remove('has-no-package');
    $('generate-full-btn').hidden = true;
    $('rerun-task-btn')?.setAttribute('hidden', 'hidden');
    $('back-to-history-btn').hidden = true;
    $('result-warning').hidden = true;
    $('zip-card').style.removeProperty('display');
    $('delivery-scope').textContent = '等待交付范围核验';
    $('delivery-exclusion-note').hidden = true;
    $('delivery-exclusion-note').textContent = '';
    $('delivery-exclusion-list')?.replaceChildren();
    if ($('delivery-exclusion-list')) $('delivery-exclusion-list').hidden = true;
    $('artifact-transfer')?.setAttribute('hidden', 'hidden');
    if ($('artifact-transfer-bar')) $('artifact-transfer-bar').value = 0;
    if ($('artifact-transfer-value')) $('artifact-transfer-value').textContent = '0%';
    $('download-zip-btn').disabled = false;
    $('result-failure-list').innerHTML = '';
    $('retry-failed-btn').hidden = true;
    $('result-eyebrow').textContent = '任务已完成';
    $('result-title').textContent = '音频已经准备好了';
    document.querySelector('.result-success-icon')?.classList.remove('has-warning', 'has-error');

    // 重置状态栏
    $('status-text').textContent = cleanupConfirmed ? '就绪' : '请重新连接生成服务';
    $('stats-bar').innerHTML = '';

    // 回到首页
    goToStep(1);
    await refreshHistoryRecords({ showLoading: false });
    if (notify) {
        showToast(cleanupConfirmed
            ? '已重置，可以开始新任务'
            : '当前任务已关闭，请重新连接生成服务后继续');
    }
    return cleanupConfirmed;
}


registerRendererModule("workflow.restart", {
    resetGenerateState,
    requestRestart,
    restart,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

