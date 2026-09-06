/** Renderer module: generation.progress */
(function attachRendererFeature_generation_progress(root) {
    'use strict';

// ============================================================================
// 进度 & 统计
// ============================================================================

function integerProgressCount(value, total = Number.POSITIVE_INFINITY) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    const count = Math.max(0, Math.floor(number + 0.5));
    return Number.isFinite(total) ? Math.min(count, Math.max(0, Math.floor(Number(total) || 0))) : count;
}

function visualProgressPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.min(Math.max(number, 0), 99);
}

function terminalProgressPercent(deliverable, total) {
    const safeTotal = Math.max(0, Math.round(Number(total) || 0));
    const safeDeliverable = Math.max(0, Math.round(Number(deliverable) || 0));
    if (safeTotal === 0) return safeDeliverable > 0 ? 100 : 0;
    return Math.min(100, Math.round((Math.min(safeDeliverable, safeTotal) / safeTotal) * 100));
}

function setProgressReadoutMode(terminal = false, hasIssues = false) {
    const title = $('progress-panel-title');
    const copyLabel = document.querySelector?.('#page-3 .generation-v2-copy-label');
    if (title) title.textContent = terminal ? '可交付进度' : '处理进度';
    if (copyLabel) copyLabel.textContent = terminal && hasIssues ? '结果状态' : '当前任务';
}

// 合并生成一次提交整篇文档，讯飞侧只有 提交中 → 已提交 → 已下载 三个
// 可靠节点，中间的合成长等待没有任何 provider 信号；逐条生成的事件则带
// item_id 和分段计数。这里把合并阶段映射成保守的进度值：已确认节点给
// 固定进度，合成等待按已等待时长缓慢爬升并封顶在 85%，终态仍由交付
// 计数决定。非合并模式（无 total_works）返回 null，走原有分段/条目进度。
const COMPOSITE_RUNTIME_STAGES = new Set([
    'preparing', 'submitted', 'downloading', 'downloaded', 'saved', 'cut', 'error',
]);

const COMPOSITE_STAGE_LABELS = {
    preparing: '提交作品',
    submitted: '讯飞合成中',
    downloading: '等待音频就绪',
    downloaded: '按停顿切割',
    saved: '按停顿切割',
    cut: '整理输出',
    error: '需要处理',
};

function compositeRuntimeReadout(runtime = null) {
    const stage = String(runtime?.stage || '').toLowerCase();
    const totalWorks = Math.max(0, Math.round(Number(runtime?.total_works) || 0));
    if (totalWorks <= 0 || !COMPOSITE_RUNTIME_STAGES.has(stage)) return null;
    const elapsedSeconds = Math.max(0, Number(runtime?.elapsed_seconds ?? runtime?.elapsedSeconds) || 0);
    const submittedWorks = Math.min(totalWorks, Math.max(0, Math.round(Number(runtime?.submitted_works) || 0)));
    const downloadedWorks = Math.min(totalWorks, Math.max(0, Math.round(Number(runtime?.downloaded_works) || 0)));
    const waitingPercent = Math.min(
        85,
        Math.max(12, Math.round(12 + 73 * (elapsedSeconds / (elapsedSeconds + 240)))),
    );
    // Each downloaded work accounts for the 72–90% portion of the composite
    // phase.  This keeps a multi-work run from looking nearly complete after
    // only its first work is ready, while still preserving the one-work 90%
    // checkpoint used by the existing flow.
    const downloadedPercent = downloadedWorks > 0
        ? Math.min(90, 72 + Math.round(18 * (downloadedWorks / totalWorks)))
        : 0;
    let percent;
    if (stage === 'preparing') {
        // Composite works are submitted sequentially.  Preparing the next
        // work must not reset the bar below the previous work's submitted or
        // downloaded checkpoint.
        percent = submittedWorks > 0 || downloadedWorks > 0
            ? Math.max(waitingPercent, downloadedPercent)
            : 5;
    } else if (stage === 'downloaded' || stage === 'saved') {
        percent = Math.max(waitingPercent, downloadedPercent);
    } else if (stage === 'cut') {
        percent = 92;
    } else if (stage === 'error') {
        // Keep an error visible at the latest trustworthy checkpoint without
        // pretending an early failed work is almost complete.
        percent = Math.max(waitingPercent, downloadedPercent);
    } else {
        // 合成/等待下载阶段：进度随已等待时长对数式爬升（约 4 分钟到
        // 一半），只作为“任务仍在推进”的可视信号，不代表真实完成度。
        // 后续作品重新进入 submitted/downloading 时，也不能覆盖已经
        // 下载完成作品对应的检查点，否则进度条会倒退。
        percent = Math.max(waitingPercent, downloadedPercent);
    }
    const worksCopy = totalWorks > 1 ? `作品 ${downloadedWorks || submittedWorks}/${totalWorks}` : '';
    const eta = elapsedSeconds >= 1 ? formatLogDuration(elapsedSeconds * 1000) : '';
    let stats;
    if (stage === 'preparing') stats = `正在提交合并作品${worksCopy ? ` · ${worksCopy}` : ''}`;
    else if (stage === 'downloading') stats = `讯飞正在合成，等待下载页就绪${eta ? ` · 已等待 ${eta}` : ''}${worksCopy ? ` · ${worksCopy}` : ''}`;
    else if (stage === 'downloaded' || stage === 'saved') stats = `合并音频已下载 · 正在按停顿切割${worksCopy ? ` · ${worksCopy}` : ''}`;
    else if (stage === 'cut') stats = `切割完成 · 正在整理输出文件`;
    else if (stage === 'error') stats = `合并作品需要处理${worksCopy ? ` · ${worksCopy}` : ''}`;
    else {
        stats = `合并作品已提交 · 讯飞合成中${eta ? ` · 已等待 ${eta}` : ''}${worksCopy ? ` · ${worksCopy}` : ''}`;
    }
    return { percent, stats, stageLabel: COMPOSITE_STAGE_LABELS[stage] };
}

const SINGLE_SEGMENT_RUNTIME_STAGE_LABELS = {
    preparing: '准备当前条目',
    submitted: '提交中',
    downloading: '等待下载',
    downloaded: '下载完成',
    saved: '已保存',
    ready: '已完成',
    error: '需要处理',
};

function singleSegmentRuntimeActive(runtime = null) {
    const totalSegments = Math.max(0, Math.round(Number(runtime?.total_segments) || 0));
    const itemId = String(runtime?.item_id || runtime?.itemId || '').trim();
    // Composite runtime also carries total_segments for the later cut step,
    // but it has no item_id. Keep this guard so an unknown composite stage
    // cannot accidentally enter the single-segment readout.
    return totalSegments > 0 && Boolean(itemId) && Number(runtime?.total_works || 0) <= 0;
}

function singleSegmentRuntimeReadout(runtime = null, progress = {}, total = 0, workspace = null) {
    if (!singleSegmentRuntimeActive(runtime)) return null;
    const safeTotal = Math.max(0, Math.round(Number(total) || 0));
    if (safeTotal <= 0) return null;

    const stage = String(runtime?.stage || runtime?.status || '').toLowerCase();
    const totalSegments = Math.max(1, Math.round(Number(runtime?.total_segments) || 0));
    const stageCount = Math.min(
        totalSegments,
        Math.max(0, Math.round(Number(runtime?.completed_segments) || 0)),
    );
    const segmentRatio = stageCount / totalSegments;
    const elapsedSeconds = Math.max(0, Number(runtime?.elapsed_seconds ?? runtime?.elapsedSeconds) || 0);
    const elapsedBoost = Math.min(6, Math.floor(elapsedSeconds / 30));
    const stagePercent = {
        preparing: 5,
        submitted: Math.min(48, 12 + Math.round(segmentRatio * 28) + elapsedBoost),
        downloading: Math.min(82, 42 + Math.round(segmentRatio * 30) + elapsedBoost),
        downloaded: Math.min(89, 78 + Math.round(segmentRatio * 10)),
        saved: Math.min(98, 90 + Math.round(segmentRatio * 8)),
        ready: 98,
        error: Math.min(75, 12 + Math.round(segmentRatio * 20)),
    }[stage] ?? Math.min(10, 1 + elapsedBoost);
    const completedItems = Math.min(
        safeTotal,
        Math.max(0, Math.round(Number(progress?.completed) || 0)),
    );
    const itemId = String(runtime?.item_id || runtime?.itemId || '').trim();
    const currentItem = Array.isArray(workspace?.items)
        ? workspace.items.find(item => String(item?.item_id || '') === itemId)
        : null;
    const currentItemSettled = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'SKIPPED'].includes(
        String(currentItem?.status || '').toUpperCase(),
    );
    const activeFraction = currentItemSettled ? 0 : stagePercent / 100;
    let percent = Math.round(((completedItems + activeFraction) / safeTotal) * 100);
    // A provider event proves that work has started. Keep a visible sliver
    // even when the first stage has not crossed a completion checkpoint yet.
    if (percent <= 0 && completedItems < safeTotal) percent = 1;
    percent = Math.min(99, Math.max(0, percent));

    const message = String(runtime?.message || '讯飞浏览器正在处理当前条目').trim();
    const elapsed = elapsedSeconds >= 1 ? ` · 已等待 ${formatLogDuration(elapsedSeconds * 1000)}` : '';
    const stageCountLabel = stage === 'submitted'
        ? '已提交'
        : ['downloading', 'downloaded'].includes(stage)
            ? '已下载'
            : stage === 'ready'
                ? '已完成'
                : stage === 'error'
                    ? '需处理'
                    : '已保存';
    return {
        percent,
        stats: `${message} · ${stageCountLabel} ${stageCount}/${totalSegments} 段 · 已完成 ${completedItems}/${safeTotal}${elapsed}`,
        stageLabel: SINGLE_SEGMENT_RUNTIME_STAGE_LABELS[stage] || '实时处理',
        itemId,
        completedSegments: stageCount,
        totalSegments,
    };
}

function updateProgress(event) {
    if (generationWorkflowOwnsRuntimeView()) {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        return;
    }
    setProgressIndeterminate(false);
    const total = integerProgressCount(event.total);
    const completed = integerProgressCount(event.completed, total);
    const failed = integerProgressCount(event.failed, total);
    const cancelled = integerProgressCount(event.cancelled, total);
    const processed = integerProgressCount(
        event.processed ?? (completed + failed + cancelled),
        total,
    );
    const pct = total > 0 ? Math.round((processed / total) * 100) : 0;
    const phase = String(event.phase || '');
    const isBatchSubmit = phase === 'batch-submit';
    const isBatchDownload = phase === 'batch-download';
    const isBatchExport = phase === 'batch-export';
    const isCompositeSubmit = phase === 'composite-submit';
    const isCompositeDownload = phase === 'composite-download';
    const isCompositeCut = phase === 'composite-cut';
    const isCompositeExport = phase === 'composite-export';
    const isCompositeError = phase === 'composite-error';
    const isPackage = phase === 'package';
    const isArchive = phase === 'archive';
    const isBatchPhase = isBatchSubmit || isBatchDownload || isBatchExport;
    const isPostProcessPhase = isPackage || isArchive;
    const mode = normalizeGenerationMode(event.generation_mode || lastGenerationConfig?.generation_mode);
    const work = event.work && typeof event.work === 'object' ? event.work : null;
    const segments = event.segments && typeof event.segments === 'object' ? event.segments : null;
    updateGenerationModeUI(mode);
    // stats 只是阶段快照，不代表任务终态；即使 completed 已经等于 total，
    // 后面仍可能在打包 ZIP、保存历史记录。只有 done 事件才允许进度条到 100%，
    // 其余状态统一保留尾部空间，避免用户看到 100% 后继续等待。
    const visualPct = visualProgressPercent(pct);
    const eta = formatLogDuration(event.eta_ms);
    setProgressBarPercent(visualPct);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(visualPct));
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${visualPct}% 处理中`);
    setProgressReadoutMode(false);
    const workTotal = integerProgressCount(work?.total ?? 0);
    const workCompleted = integerProgressCount(work?.completed ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const workSubmitted = integerProgressCount(work?.submitted ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const workDownloaded = integerProgressCount(work?.downloaded ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const segmentTotal = integerProgressCount(segments?.total ?? 0);
    const segmentSliced = integerProgressCount(
        segments?.sliced ?? segments?.completed ?? 0,
        segmentTotal || Number.POSITIVE_INFINITY,
    );
    const segmentExported = integerProgressCount(
        segments?.exported ?? (isCompositeExport ? completed : 0),
        segmentTotal || Number.POSITIVE_INFINITY,
    );
    const compositeWorkCopy = workTotal > 0
        ? `作品 ${workCompleted}/${workTotal} · 已提交 ${workSubmitted} · 已下载 ${workDownloaded}`
        : '';
    const compositeSegmentCopy = segmentTotal > 0
        ? `题目切割 ${segmentSliced}/${segmentTotal}`
        : '';
    const compositeExportCopy = segmentTotal > 0
        ? `题目整理 ${segmentExported}/${segmentTotal}`
        : '';
    let completedLabel = '已完成';
    if (isCompositeSubmit) completedLabel = '已提交作品';
    else if (isCompositeDownload) completedLabel = '已下载作品';
    else if (isCompositeCut) completedLabel = '已切割题目';
    else if (isCompositeExport) completedLabel = '已整理题目';
    else if (isBatchSubmit) completedLabel = '已提交';
    else if (isBatchDownload) completedLabel = '已下载';
    else if (isBatchExport) completedLabel = '已整理';
    else if (isPackage) completedLabel = '正在整理';
    else if (isArchive) completedLabel = '正在归档';
    $('progress-completed-label').textContent = completedLabel;
    let phaseCopy = `${completed} / ${total}`;
    if (isCompositeSubmit) phaseCopy = `合并作品提交中 · ${compositeWorkCopy}`;
    else if (isCompositeDownload) phaseCopy = `合并音频下载中 · ${compositeWorkCopy}`;
    else if (isCompositeCut) phaseCopy = `按停顿安全切割中 · ${compositeSegmentCopy || compositeWorkCopy}`;
    else if (isCompositeExport) phaseCopy = `独立音频整理中 · ${compositeExportCopy || compositeWorkCopy}`;
    else if (isCompositeError) phaseCopy = `合并作品出现异常 · ${compositeWorkCopy}`;
    else if (isBatchSubmit) phaseCopy = `已提交 ${processed} / ${total} · 等待下载`;
    else if (isBatchDownload) phaseCopy = `已下载 ${processed} / ${total} · 等待整理`;
    else if (isBatchExport) phaseCopy = `已整理 ${processed} / ${total} · 正在输出`;
    else if (isPackage) phaseCopy = `正在打包交付文件 · 已生成 ${completed} / ${total}`;
    else if (isArchive) phaseCopy = `正在保存历史记录 · 已生成 ${completed} / ${total}`;
    $('progress-stats').textContent = phaseCopy
        + (failed > 0 ? `  ·  失败 ${failed}` : '')
        + (cancelled > 0 ? `  ·  已取消 ${cancelled}` : '')
        + (eta ? `  ·  预计 ${eta}` : '');
    $('progress-percent').textContent = String(Math.round(visualPct));
    const displayedCompleted = isCompositeCut
        ? String(segmentTotal > 0 ? segmentSliced : processed)
        : isCompositeExport
            ? String(segmentTotal > 0 ? segmentExported : processed)
            : isBatchPhase || isPostProcessPhase
                ? String(processed)
                : String(completed);
    $('progress-completed').textContent = displayedCompleted;
    $('progress-remaining').textContent = String(Math.max(total - processed, 0));
    $('progress-failed').textContent = String(failed);
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelled);
    updateLogTimelineHeader();
}

function updateStats(event) {
    const container = $('type-stats');
    container.innerHTML = '';

    if (event.by_type) {
        for (const [type, counts] of Object.entries(event.by_type)) {
            const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[type]) || '#a8a29e';
            const pill = document.createElement('span');
            pill.className = 'type-stat-pill';

            const dot = document.createElement('span');
            dot.className = 'type-stat-dot';
            dot.style.background = color;

            const label = document.createElement('span');
            label.textContent = type;

            const count = document.createElement('span');
            count.className = 'type-stat-count';
            count.textContent = `${counts.done}/${counts.total}`;

            pill.appendChild(dot);
            pill.appendChild(label);
            pill.appendChild(count);
            container.appendChild(pill);
        }
    }

    // 底部状态栏
    const statsBar = $('stats-bar');
    statsBar.innerHTML = '';

    if (event.by_type) {
        for (const [type, counts] of Object.entries(event.by_type)) {
            const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[type]) || '#a8a29e';
            const pill = document.createElement('span');
            pill.className = 'stat-pill';

            const dot = document.createElement('span');
            dot.className = 'stat-dot';
            dot.style.background = color;

            const label = document.createElement('span');
            label.textContent = type + ' ';

            const count = document.createElement('span');
            count.className = 'stat-count';
            count.textContent = `${counts.done}/${counts.total}`;

            label.appendChild(count);
            pill.appendChild(dot);
            pill.appendChild(label);
            statsBar.appendChild(pill);
        }
    }

    const totalPill = document.createElement('span');
    totalPill.className = 'stat-pill';
    const totalLabel = document.createElement('span');
    totalLabel.textContent = '成功 ';
    const totalCount = document.createElement('span');
    totalCount.className = 'stat-count';
    totalCount.textContent = `${event.completed}/${event.total}`;
    totalLabel.appendChild(totalCount);
    totalPill.appendChild(totalLabel);
    statsBar.appendChild(totalPill);

    if (event.failed > 0) {
        const failPill = document.createElement('span');
        failPill.className = 'stat-pill error-pill';
        const failLabel = document.createElement('span');
        failLabel.textContent = '失败 ';
        const failCount = document.createElement('span');
        failCount.className = 'stat-count';
        failCount.textContent = String(event.failed);
        failLabel.appendChild(failCount);
        failPill.appendChild(failLabel);
        statsBar.appendChild(failPill);
    }
    if (event.cancelled > 0) {
        const cancelledPill = document.createElement('span');
        cancelledPill.className = 'stat-pill cancelled-pill';
        const cancelledLabel = document.createElement('span');
        cancelledLabel.textContent = '已取消 ';
        const cancelledCount = document.createElement('span');
        cancelledCount.className = 'stat-count';
        cancelledCount.textContent = String(event.cancelled);
        cancelledLabel.appendChild(cancelledCount);
        cancelledPill.appendChild(cancelledLabel);
        statsBar.appendChild(cancelledPill);
    }
}


registerRendererModule("generation.progress", {
    integerProgressCount,
    visualProgressPercent,
    terminalProgressPercent,
    setProgressReadoutMode,
    compositeRuntimeReadout,
    singleSegmentRuntimeActive,
    singleSegmentRuntimeReadout,
    updateProgress,
    updateStats,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
