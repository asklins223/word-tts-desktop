/** Renderer module: generation.runtimeView */
(function attachRendererFeature_generation_runtimeView(root) {
    'use strict';

function clearSSEReconnectTimer() {
    if (sseReconnectTimer) {
        clearTimeout(sseReconnectTimer);
        sseReconnectTimer = null;
    }
    if (sseStableTimer) {
        clearTimeout(sseStableTimer);
        sseStableTimer = null;
    }
}

function destroyWaveSurfers() {
    waveformRenderToken++;
    audioPlayRequestToken++;
    if (waveformObserver) {
        waveformObserver.disconnect();
        waveformObserver = null;
    }
    waveformItems.forEach(item => {
        item.cancelWaveformLoad?.(false);
        item.resetAudioSource?.();
    });
    waveformItems = [];
    waveformQueue = [];
    waveformLoadsActive = 0;
    wavesurferInstances.forEach(ws => {
        try { ws.destroy(); } catch (e) { /* ignore */ }
    });
    wavesurferInstances = [];
    audioElements.forEach(audio => {
        try {
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
        } catch (e) { /* ignore */ }
    });
    audioElements = [];
    artifactObjectUrls.forEach(url => {
        try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
    });
    artifactObjectUrls.clear();
    clearVoiceAssetObjectUrls();
    voiceAssetCacheReady.clear();
    currentPlayingAudio = null;
}

function pumpWaveformQueue() {
    while (waveformLoadsActive < WAVEFORM_MAX_CONCURRENT && waveformQueue.length > 0) {
        const item = waveformQueue.shift();
        if (!item) continue;
        item._waveformQueued = false;
        if (!item.isConnected || item._waveformInitialized || item._waveformFailed) continue;

        const token = waveformRenderToken;
        let settled = false;
        const release = () => {
            if (settled) return;
            settled = true;
            if (token !== waveformRenderToken) return;
            waveformLoadsActive = Math.max(0, waveformLoadsActive - 1);
            pumpWaveformQueue();
        };

        waveformLoadsActive++;
        const instance = item.initializeWaveform?.(release);
        if (!instance) release();
    }
}

function queueWaveformInitialization(item, prioritize = false) {
    if (!item || item._waveformInitialized || item._waveformQueued || item._waveformFailed) return;
    item._waveformQueued = true;
    if (prioritize) waveformQueue.unshift(item);
    else waveformQueue.push(item);
    pumpWaveformQueue();
}

function activateResultWaveforms() {
    if (!$('page-4')?.classList.contains('active') || waveformItems.length === 0) return;
    if (waveformObserver) waveformObserver.disconnect();

    const scrollRoot = $('page-4')?.querySelector('.page-scroll') || null;
    if (typeof IntersectionObserver === 'function') {
        waveformObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                waveformObserver?.unobserve(entry.target);
                queueWaveformInitialization(entry.target);
            });
        }, { root: scrollRoot, rootMargin: '420px 0px', threshold: 0.01 });
        waveformItems.forEach(item => {
            if (item.isConnected) waveformObserver.observe(item);
        });
    }

    // 页面显示、容器获得真实宽度后优先预热首条；其余条目按可见区域串行解码。
    const firstVisible = waveformItems.find(item => item.isConnected && item.getBoundingClientRect().width > 1);
    if (firstVisible) queueWaveformInitialization(firstVisible, true);
}

function setGenerationVisualState(state) {
    const animation = $('gen-animation');
    const badge = $('processing-badge');
    const badgeLabel = $('processing-badge-label');
    const logDot = document.querySelector('.log-live-dot');
    const liveStatus = $('generation-live-status');
    const liveLabelText = $('generation-live-label-text');
    [animation, badge, logDot].forEach(el => {
        if (el) el.classList.remove('is-error', 'is-stopped', 'is-done', 'is-paused');
    });
    if (animation && state !== 'done') animation.classList.remove('done');

    const labels = {
        running: '任务进行中',
        paused: '已暂停',
        done: '处理完成',
        error: '需要处理',
        warning: '部分完成',
        stopped: '任务已停止',
    };
    if (badgeLabel) badgeLabel.textContent = labels[state] || labels.running;
    const liveLabels = {
        running: '批量任务进行中',
        paused: '任务已暂停，可恢复执行',
        done: '批量任务已完成',
        warning: '任务完成，部分内容需处理',
        error: '生成遇到问题，请检查记录',
        stopped: '任务已停止',
    };
    if (liveStatus) liveStatus.textContent = liveLabels[state] || liveLabels.running;
    const liveLabelTexts = {
        running: '当前阶段',
        paused: '任务已暂停',
        done: '任务完成',
        warning: '部分完成',
        error: '生成异常',
        stopped: '任务停止',
    };
    if (liveLabelText) liveLabelText.textContent = liveLabelTexts[state] || liveLabelTexts.running;

    if (state === 'paused') {
        animation?.classList.add('is-paused');
        badge?.classList.add('is-paused');
        logDot?.classList.add('is-paused');
    } else if (state === 'done') {
        animation?.classList.add('done');
        badge?.classList.add('is-done');
        logDot?.classList.add('is-done');
    } else if (state === 'error') {
        animation?.classList.add('is-error');
        badge?.classList.add('is-error');
        logDot?.classList.add('is-error');
    } else if (state === 'stopped' || state === 'warning') {
        animation?.classList.add('is-stopped');
        badge?.classList.add('is-stopped');
        logDot?.classList.add('is-stopped');
    }
    updateGenerationCancelUI();
}

function generationProgressTotal(progress, workspace = currentWorkspace) {
    return Math.max(
        0,
        Math.round(
            Number(progress?.total)
            || Number(workspace?.progress?.total)
            || summarizeParseResults(currentSession?.parse_results).total
            || Number(lastStats?.total)
            || 0,
        ),
    );
}

function generationProgressPercentForView(presentation, progress, total) {
    if (presentation.terminal) {
        return terminalProgressPercent(progress?.deliverable, total);
    }
    // The server's aggregate percent counts failures/cancellations as
    // processed work. That is useful for throughput, but it is misleading as
    // the generation progress bar: a failed 1/1 run must not look 99% done.
    // During an active/recovering run, keep the bar aligned with the completed
    // count shown below it. Terminal states use verified deliverables above.
    if (total > 0) {
        const completed = Math.max(0, Math.round(Number(progress?.completed) || 0));
        return Math.min(99, Math.round((Math.min(completed, total) / total) * 100));
    }
    return Math.min(99, Math.max(0, Math.round(Number(progress?.percent) || 0)));
}

function generationProgressIssueSummary(progress = {}) {
    const counts = [
        ['pending', '待处理'],
        ['failed', '失败'],
        ['cancelled', '已取消'],
        ['skipped', '已跳过'],
    ];
    return counts
        .map(([key, label]) => [Math.max(0, Math.round(Number(progress?.[key]) || 0)), label])
        .filter(([count]) => count > 0)
        .map(([count, label]) => `${count} 条${label}`)
        .join(' · ');
}

function generationProgressCopy(presentation, progress, total) {
    const countTotal = total;
    const countCompleted = Math.max(0, Math.round(Number(progress?.completed) || 0));
    const count = countTotal > 0 ? `${countCompleted} / ${countTotal}` : '等待计数';
    const issueSummary = generationProgressIssueSummary(progress);
    return `${presentation.progressStatus} · ${count}${issueSummary ? ` · ${issueSummary}` : ''}`;
}

function generationProgressAriaText(presentation, percent) {
    if (presentation?.terminal) return `${percent}% 可交付`;
    const status = {
        CREATED: '等待开始',
        PAUSE_REQUESTED: '正在暂停',
        PAUSED: '已暂停',
        RESUME_REQUESTED: '正在恢复',
        TERMINATING: '正在停止',
        BLOCKED: '需要处理',
        WAITING_RETRY: '等待重试',
        WAITING_USER: '等待处理',
    }[presentation?.key] || '处理中';
    return `${percent}% ${status}`;
}

function generationFileStatusText(presentation, progress, workspace = currentWorkspace) {
    const source = workspace?.source_filename || currentSession?.source_filename || '当前文档';
    const total = generationProgressTotal(progress, workspace);
    const completed = Math.max(0, Math.round(Number(progress?.completed) || 0));
    const count = total > 0 ? `${completed}/${total}` : `${completed}`;
    const status = {
        PAUSE_REQUESTED: `已收到「${source}」的暂停请求 · 当前进度 ${count}，等待安全暂停点。`,
        PAUSED: `「${source}」已暂停 · 当前进度 ${count}，点击“恢复任务”继续。`,
        RESUME_REQUESTED: `正在恢复「${source}」· 当前进度 ${count}，请稍候。`,
        TERMINATING: `正在停止「${source}」· 当前进度 ${count}，正在保存已完成内容。`,
        CANCELLED: `已取消「${source}」的生成任务 · 已完成 ${count}。`,
        FAILED: `「${source}」生成遇到问题 · 已完成 ${count}，可查看记录处理。`,
        BLOCKED: `「${source}」需要处理 · 已完成 ${count}，请查看任务记录。`,
        WAITING_RETRY: `「${source}」正在等待重试 · 已完成 ${count}。`,
        WAITING_USER: `「${source}」正在等待处理 · 已完成 ${count}。`,
    };
    return status[presentation.key] || '';
}

function restoreGenerationFileLabel(workspace = currentWorkspace) {
    if (!currentSession?.source_filename) return;
    const parseTotal = summarizeParseResults(currentSession.parse_results).total;
    const preview = Boolean(lastGenerationConfig?.preview);
    const previewLimit = Number(lastGenerationConfig?.preview_limit) || 3;
    const total = preview ? Math.min(parseTotal, previewLimit) : parseTotal;
    updateSessionLabels(currentSession.source_filename, currentSession.parse_results, {
        preview,
        total: total || Number(workspace?.progress?.total) || parseTotal,
    });
}

function syncGenerationControlStage(presentation) {
    if (!['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING'].includes(presentation?.key)) return;
    const currentStage = LOG_STAGE_ORDER?.[Math.max(0, logStageIndex)] || 'synthesize';
    const stage = currentStage === 'complete' ? 'synthesize' : currentStage;
    logStageStates.set(stage, 'warning');
    renderLogStageRail();
}

function generationRuntimeProjection(workspace = currentWorkspace) {
    const current = workspace && typeof workspace === 'object' ? workspace : {};
    if (typeof compositeRuntimeProjection === 'function') {
        return compositeRuntimeProjection(current, current.snapshot || null);
    }
    return current.snapshot?.runtime
        || current.runtime
        || current.snapshot?.latest_event?.payload
        || null;
}

function renderGenerationViewState(workspace = currentWorkspace, state = null, progress = null) {
    const current = workspace || currentWorkspace || {};
    const resolvedState = state || workspaceUserState(current, current?.snapshot || currentSession);
    const runtime = generationRuntimeProjection(current);
    const runtimeMessage = runtime?.message
        || current?.snapshot?.runtime?.message
        || current?.runtime?.message
        || '';
    const pendingPause = pendingWorkspaceCommand('PAUSE');
    const pendingResume = pendingWorkspaceCommand('RESUME');
    const presentation = generationStatePresentation(resolvedState, {
        runtimeMessage,
        pendingPause,
        pendingResume,
        pendingCancel: generationCancelRequested || Boolean(cancelWorkflowPromise),
    });
    const viewProgress = progress || workspaceProgress(current);
    const total = generationProgressTotal(viewProgress, current);
    // 合并生成运行期以作品阶段事件为最细进度信号；单条模式则使用
    // 当前条目的分段事件补上条目级进度。暂停/终态不使用，避免估算
    // 进度在冻结状态下继续爬升。
    const runtimeReadout = ['RUNNING', 'PREPARING', 'RECOVERING'].includes(presentation.key) && !presentation.terminal
        ? (compositeRuntimeReadout(runtime)
            || singleSegmentRuntimeReadout(runtime, viewProgress, total, current))
        : null;
    const percent = runtimeReadout
        ? runtimeReadout.percent
        : generationProgressPercentForView(presentation, viewProgress, total);
    const previousKey = document.body.dataset.generationState || '';
    const staticStatusKeys = new Set([
        'PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING',
        'BLOCKED', 'WAITING_RETRY', 'WAITING_USER', 'CANCELLED', 'FAILED',
    ]);

    document.body.dataset.generationState = presentation.key;
    setGenerationVisualState(presentation.visualState);
    if ($('gen-title')) $('gen-title').textContent = presentation.title;
    if ($('processing-badge-label')) $('processing-badge-label').textContent = presentation.badge;
    if ($('generation-live-label-text')) $('generation-live-label-text').textContent = presentation.liveLabel;
    if ($('generation-live-status')) $('generation-live-status').textContent = presentation.liveStatus;
    if ($('status-text')) $('status-text').textContent = presentation.liveStatus;
    if ($('generation-active-note')) $('generation-active-note').textContent = presentation.note;

    const pigCopy = {
        CREATED: ['READY', '等待开始生成'],
        PREPARING: ['PREPARING', '正在准备音频任务'],
        RUNNING: ['ON AIR', '她正在把文字念成声音'],
        RECOVERING: ['RECOVERING', '正在恢复音频任务'],
        PAUSE_REQUESTED: ['PAUSING', '正在安全暂停任务'],
        PAUSED: ['PAUSED', '任务已暂停，等待恢复'],
        RESUME_REQUESTED: ['RESUMING', '正在恢复音频任务'],
        TERMINATING: ['STOPPING', '正在停止音频任务'],
        SUCCEEDED: ['READY', '音频已经准备好'],
        PARTIAL_SUCCESS: ['REVIEW', '部分音频需要处理'],
        FAILED: ['CHECK', '生成遇到问题'],
        CANCELLED: ['STOPPED', '任务已取消'],
        BLOCKED: ['CHECK', '任务需要处理'],
        WAITING_RETRY: ['WAITING', '等待重试'],
        WAITING_USER: ['CHECK', '等待人工处理'],
        CLOSED: ['ARCHIVED', '任务已归档'],
    }[presentation.key] || ['VOICE', '正在处理文字'];
    const compositePigCopy = {
        preparing: ['SUBMITTING', '正在提交合并作品'],
        submitted: ['SYNTHESIZING', '讯飞正在合成音频'],
        downloading: ['DOWNLOADING', '正在等待音频就绪'],
    downloaded: ['CUTTING', '正在按停顿切割音频'],
    saved: ['CUTTING', '正在按停顿切割音频'],
    cut: ['ORGANIZING', '正在整理输出文件'],
    error: ['CHECK', '合并作品需要处理'],
};
    const singleSegmentPigCopy = {
        preparing: ['PREPARING', '正在准备当前条目的音频段'],
        submitted: ['SUBMITTING', '正在提交当前条目的音频段'],
        downloading: ['DOWNLOADING', '正在等待当前条目音频就绪'],
        downloaded: ['SAVING', '当前条目音频已下载'],
        saved: ['SAVING', '正在保存当前条目音频'],
        ready: ['READY', '当前条目音频已完成'],
        error: ['CHECK', '当前条目需要处理'],
    };
    const activeCompositePigCopy = runtimeReadout
        ? (compositePigCopy[String(runtime?.stage || '').toLowerCase()]
            || singleSegmentPigCopy[String(runtime?.stage || runtime?.status || '').toLowerCase()])
        : null;
    if (activeCompositePigCopy) {
        pigCopy[0] = activeCompositePigCopy[0];
        pigCopy[1] = activeCompositePigCopy[1];
    } else if (['PREPARING', 'RUNNING', 'RECOVERING'].includes(presentation.key) && runtimeMessage) {
        // Single-segment mode has no composite stage readout, but its live
        // provider message is still useful in the active status card.
        pigCopy[1] = runtimeMessage;
    }
    if ($('generation-v2-pig-status')) $('generation-v2-pig-status').textContent = pigCopy[0];
    if ($('generation-v2-pig-message')) $('generation-v2-pig-message').textContent = pigCopy[1];

    const activeDot = $('generation-active-dot');
    const activeDotLabel = $('generation-active-dot-label');
    const liveLabel = $('generation-live-label');
    const paused = ['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED'].includes(presentation.key);
    const stopped = ['TERMINATING', 'CANCELLED'].includes(presentation.key);
    activeDot?.classList.toggle('is-paused', paused);
    activeDot?.classList.toggle('is-stopped', stopped);
    liveLabel?.classList.toggle('is-paused', paused);
    liveLabel?.classList.toggle('is-stopped', stopped);
    if (activeDotLabel) activeDotLabel.textContent = presentation.key === 'RUNNING' ? '实时' : presentation.liveLabel;

    if (staticStatusKeys.has(presentation.key) || presentation.terminal) {
        const fileStatus = generationFileStatusText(presentation, viewProgress, current);
        if (fileStatus && $('generation-file-name')) $('generation-file-name').textContent = fileStatus;
    } else if (['PREPARING', 'RUNNING', 'RECOVERING'].includes(presentation.key)
        && staticStatusKeys.has(previousKey)) {
        restoreGenerationFileLabel(current);
    }

    if ($('progress-percent')) $('progress-percent').textContent = String(percent);
    if ($('progress-bar')) setProgressBarPercent(percent);
    const progressTrack = $('progress-bar')?.parentElement;
    progressTrack?.setAttribute('aria-valuenow', String(percent));
    progressTrack?.setAttribute('aria-valuetext', generationProgressAriaText(presentation, percent));
    setProgressIndeterminate(presentation.indeterminate && !runtimeReadout);
    if (runtimeReadout) {
        const stageDetail = $('generation-synthesis-stage-detail');
        if (stageDetail) stageDetail.textContent = runtimeReadout.stageLabel;
    }
    setProgressReadoutMode(
        presentation.terminal,
        presentation.key === 'PARTIAL_SUCCESS'
            || presentation.key === 'FAILED'
            || presentation.key === 'CANCELLED',
    );
    if (runtimeReadout || presentation.freezeProgress || !lastStats || ['PREPARING', 'RECOVERING'].includes(presentation.key)) {
        if ($('progress-stats')) {
            $('progress-stats').textContent = runtimeReadout
                ? runtimeReadout.stats
                : generationProgressCopy(presentation, viewProgress, total);
        }
    }
    syncGenerationControlStage(presentation);
    syncGenerationRecoveryState(current, resolvedState, viewProgress);
    updateGenerationControlUI(current);
    return presentation;
}


registerRendererModule("generation.runtimeView", {
    clearSSEReconnectTimer,
    destroyWaveSurfers,
    pumpWaveformQueue,
    queueWaveformInitialization,
    activateResultWaveforms,
    setGenerationVisualState,
    generationProgressTotal,
    generationProgressPercentForView,
    generationProgressIssueSummary,
    generationProgressCopy,
    generationProgressAriaText,
    generationFileStatusText,
    restoreGenerationFileLabel,
    syncGenerationControlStage,
    renderGenerationViewState,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
