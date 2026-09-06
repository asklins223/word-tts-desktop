/** Renderer module: generation.log */
(function attachRendererFeature_generation_log(root) {
    'use strict';

// ============================================================================
// 日志渲染
// ============================================================================

const LOG_STAGE_LABELS = {
    prepare: '准备任务',
    parse: '识别文档',
    synthesize: '生成音频',
    package: '整理交付',
    archive: '保存记录',
    complete: '任务完成',
    system: '系统状态',
};

const LOG_STAGE_ORDER = ['prepare', 'parse', 'synthesize', 'package', 'archive', 'complete'];

function formatLogDuration(durationMs) {
    const value = Number(durationMs);
    if (!Number.isFinite(value) || value < 0) return '';
    if (value < 1000) return `${Math.round(value)} 毫秒`;
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} 秒`;
    const minutes = Math.floor(value / 60000);
    const seconds = Math.round((value % 60000) / 1000);
    return `${minutes} 分 ${seconds} 秒`;
}

function normalizeLogEntry(rawEntry = {}) {
    const allowedLevels = ['success', 'error', 'warn', 'progress', 'info'];
    const allowedStatuses = ['running', 'success', 'warning', 'error', 'info'];
    const level = allowedLevels.includes(rawEntry.level) ? rawEntry.level : 'info';
    const fallbackStatus = {
        success: 'success',
        error: 'error',
        warn: 'warning',
        progress: 'running',
        info: 'info',
    }[level];
    const status = allowedStatuses.includes(rawEntry.status) ? rawEntry.status : fallbackStatus;
    const backendSeq = Number(rawEntry.seq);
    const hasBackendSeq = Number.isFinite(backendSeq) && backendSeq > 0;
    if (hasBackendSeq && logSeenSeq.has(backendSeq)) return null;
    if (hasBackendSeq) logSeenSeq.add(backendSeq);
    const localSeq = hasBackendSeq ? backendSeq : 1000000 + (++logLocalSeq);
    const stableKey = String(rawEntry.key || `event:${localSeq}`);
    return {
        ...rawEntry,
        seq: localSeq,
        key: stableKey,
        level,
        status,
        stage: String(rawEntry.stage || 'system'),
        kind: String(rawEntry.kind || 'notice'),
        title: String(rawEntry.title || rawEntry.msg || '任务记录'),
        detail: String(rawEntry.detail || (rawEntry.title ? rawEntry.msg || '' : '')),
        time: String(rawEntry.time || new Date().toLocaleTimeString('zh-CN', { hour12: false })),
        item: rawEntry.item && typeof rawEntry.item === 'object' ? rawEntry.item : null,
        work: rawEntry.work && typeof rawEntry.work === 'object' ? rawEntry.work : null,
        segments: rawEntry.segments && typeof rawEntry.segments === 'object' ? rawEntry.segments : null,
        progress: rawEntry.progress && typeof rawEntry.progress === 'object' ? rawEntry.progress : null,
    };
}

function logStatusLabel(entry) {
    if (entry.status === 'running') return '处理中';
    if (entry.status === 'success') return '已完成';
    if (entry.status === 'warning') return '需关注';
    if (entry.status === 'error') return '失败';
    return '已记录';
}

function createLogMeta(label, value, className = '') {
    if (value === undefined || value === null || value === '') return null;
    const meta = document.createElement('span');
    meta.className = `log-meta-item${className ? ` ${className}` : ''}`;
    const key = document.createElement('small');
    key.textContent = label;
    const content = document.createElement('strong');
    content.textContent = String(value);
    meta.append(key, content);
    return meta;
}

function createLogEntryElement(entry) {
    const article = document.createElement('article');
    article.className = `log-entry is-${entry.status}`;
    article.dataset.logKey = entry.key;
    article.dataset.logStatus = entry.status;
    article.dataset.logLevel = entry.level;
    article.dataset.logKind = entry.kind;
    article.setAttribute('aria-label', `${entry.title}，${logStatusLabel(entry)}`);

    const time = document.createElement('time');
    time.className = 'log-time';
    time.textContent = entry.time;
    if (entry.ts) time.dateTime = entry.ts;

    const node = document.createElement('span');
    node.className = 'log-node';
    node.setAttribute('aria-hidden', 'true');

    const content = document.createElement('div');
    content.className = 'log-content';
    const head = document.createElement('div');
    head.className = 'log-entry-head';
    const headingCopy = document.createElement('div');
    headingCopy.className = 'log-entry-heading';
    const stage = document.createElement('span');
    stage.className = 'log-stage-badge';
    stage.textContent = LOG_STAGE_LABELS[entry.stage] || '任务记录';
    const title = document.createElement('strong');
    title.className = 'log-entry-title';
    title.textContent = entry.title;
    headingCopy.append(stage, title);
    const status = document.createElement('span');
    status.className = 'log-status-badge';
    status.textContent = logStatusLabel(entry);
    head.append(headingCopy, status);
    content.appendChild(head);

    if (entry.detail) {
        const detail = document.createElement('p');
        detail.className = 'log-detail';
        detail.textContent = entry.detail;
        content.appendChild(detail);
    }

    const meta = document.createElement('div');
    meta.className = 'log-meta';
    const metaItems = [];
    if (entry.item) {
        metaItems.push(createLogMeta('题型', entry.item.doc_type));
        metaItems.push(createLogMeta('分类', entry.item.category));
        metaItems.push(createLogMeta('音色', entry.item.voice));
        metaItems.push(createLogMeta('文件', entry.item.filename, 'is-file'));
    }
    if (entry.progress) {
        metaItems.push(createLogMeta('进度', `${entry.progress.completed || 0}/${entry.progress.total || 0}`));
        if (Number(entry.progress.failed) > 0) {
            metaItems.push(createLogMeta('失败', entry.progress.failed, 'is-issue'));
        }
    }
    if (entry.work) {
        const workIndex = Number(entry.work.index);
        const workTotal = Number(entry.work.total);
        const workLabel = Number.isFinite(workIndex) && Number.isFinite(workTotal) && workTotal > 0
            ? `${workIndex}/${workTotal}`
            : entry.work.status || '';
        metaItems.push(createLogMeta('合并作品', workLabel));
        metaItems.push(createLogMeta('包含题目', entry.work.item_count));
        metaItems.push(createLogMeta('作品状态', entry.work.status));
    }
    if (entry.segments) {
        const sliced = entry.segments.sliced ?? entry.segments.completed ?? 0;
        const exported = entry.segments.exported ?? 0;
        metaItems.push(createLogMeta('切割题目', `${sliced}/${entry.segments.total || 0}`));
        if (entry.segments.exported !== undefined) {
            metaItems.push(createLogMeta('整理题目', `${exported}/${entry.segments.total || 0}`));
        }
    }
    const duration = formatLogDuration(entry.duration_ms);
    if (duration) metaItems.push(createLogMeta('耗时', duration));
    metaItems.filter(Boolean).forEach(item => meta.appendChild(item));
    if (meta.childElementCount > 0) content.appendChild(meta);

    if (entry.item?.text_preview) {
        const source = document.createElement('details');
        source.className = 'log-source-preview';
        const summary = document.createElement('summary');
        summary.textContent = '查看内容摘要';
        const sourceText = document.createElement('p');
        sourceText.textContent = entry.item.text_preview;
        source.append(summary, sourceText);
        content.appendChild(source);
    }

    article.append(time, node, content);
    return article;
}

function logEntryMatchesFilter(entry) {
    if (logFilter === 'running') return entry.status === 'running';
    if (logFilter === 'success') return entry.status === 'success';
    if (logFilter === 'issues') return entry.status === 'warning' || entry.status === 'error';
    return true;
}

function applyLogFilter() {
    logEntriesByKey.forEach(record => {
        record.element.hidden = !logEntryMatchesFilter(record.entry);
    });
    const visibleCount = [...logEntriesByKey.values()].filter(record => !record.element.hidden).length;
    const empty = $('progress-log').querySelector('.log-empty');
    if (visibleCount === 0 && logEntriesByKey.size > 0) {
        if (!empty) {
            const notice = document.createElement('div');
            notice.className = 'log-empty is-filtered';
            notice.textContent = '当前筛选条件下没有任务记录。';
            $('progress-log').appendChild(notice);
        }
    } else if (empty?.classList.contains('is-filtered')) {
        empty.remove();
    }
}

function updateLogStageRail(entry) {
    if (entry.kind !== 'stage' && entry.kind !== 'summary') return;
    if (generationWorkflowOwnsRuntimeView() && entry.status === 'running') return;
    let stageIndex = LOG_STAGE_ORDER.indexOf(entry.stage);
    if (entry.stage === 'complete') {
        // 终态只收束已经走到的处理阶段，并由独立的“完成”节点承载
        // 整体成功/部分完成/失败，避免把未执行阶段误画成绿色。
        const reachedStageIndex = logStageIndex;
        LOG_STAGE_ORDER.forEach((stage, index) => {
            if (stage === 'complete' || index > reachedStageIndex) return;
            const existing = logStageStates.get(stage);
            if (!existing || existing === 'running' || existing === 'info') {
                const isCurrentStage = index === reachedStageIndex;
                if (isCurrentStage && entry.status === 'error') logStageStates.set(stage, 'error');
                else if (isCurrentStage && entry.status === 'warning') logStageStates.set(stage, 'warning');
                else logStageStates.set(stage, 'success');
            }
        });
        logStageStates.set('complete', entry.status);
        logStageIndex = stageIndex;
    }
    if (stageIndex < 0) return;
    if (entry.stage !== 'complete') {
        if (stageIndex < logStageIndex && entry.status === 'running') return;
        logStageIndex = Math.max(logStageIndex, stageIndex);
        logStageStates.set(entry.stage, entry.status);
        LOG_STAGE_ORDER.forEach((stage, index) => {
            if (index < logStageIndex && !logStageStates.has(stage)) {
                logStageStates.set(stage, 'success');
            }
        });
    }
    renderLogStageRail();
}

function renderLogStageRail() {
    $$('#log-stage-rail [data-log-stage]').forEach((stageEl, index) => {
        const state = logStageStates.get(stageEl.dataset.logStage);
        stageEl.classList.remove('is-active', 'is-complete', 'is-error', 'is-warning');
        if (state === 'success') stageEl.classList.add('is-complete');
        else if (state === 'warning') stageEl.classList.add('is-warning');
        else if (state === 'error') stageEl.classList.add('is-error');
        else if (state === 'running' || (index === logStageIndex && !state)) stageEl.classList.add('is-active');
        const statusText = state === 'success'
            ? '已完成'
            : state === 'warning'
                ? '需关注'
                : state === 'error'
                    ? '失败'
                    : state === 'running'
                        ? '进行中'
                        : '未开始';
        const stageLabel = LOG_STAGE_LABELS[stageEl.dataset.logStage] || stageEl.textContent.trim();
        stageEl.setAttribute('aria-label', `${stageLabel}：${statusText}`);
        if (state === 'running') stageEl.setAttribute('aria-current', 'step');
        else stageEl.removeAttribute('aria-current');
    });
}

function trimLogTimeline() {
    while (logEntriesByKey.size > LOG_DOM_LIMIT) {
        let removableKey = null;
        for (const [key, record] of logEntriesByKey) {
            if (record.entry.kind === 'item' && record.entry.status === 'success') {
                removableKey = key;
                break;
            }
        }
        removableKey ||= logEntriesByKey.keys().next().value;
        const record = logEntriesByKey.get(removableKey);
        record?.element.remove();
        logEntriesByKey.delete(removableKey);
    }
}

function updateLogTimelineHeader(lastEntry = null) {
    logEntryCount = logEntriesByKey.size;
    const issueCount = [...logEntriesByKey.values()].filter(({ entry }) => entry.status === 'warning' || entry.status === 'error').length;
    $('log-count').textContent = logEntryCount >= LOG_DOM_LIMIT
        ? `最近 ${LOG_DOM_LIMIT} 个节点`
        : `${logEntryCount} 个节点`;
    $('log-issue-count').textContent = String(issueCount);
    if (lastStats?.total) {
        const total = integerProgressCount(lastStats.total);
        const processed = integerProgressCount(
            lastStats.processed ?? ((lastStats.completed || 0) + (lastStats.failed || 0)),
            total,
        );
        const pending = Math.max(0, total - processed);
        const eta = formatLogDuration(lastStats.eta_ms);
        $('log-summary').textContent = `已处理 ${processed} / ${total} · 剩余 ${pending}${issueCount ? ` · ${issueCount} 条异常记录` : ''}${eta ? ` · 预计还需 ${eta}` : ''}`;
    } else if (lastEntry) {
        $('log-summary').textContent = lastEntry.title;
    }
}

function updateLogNewRecordsButton() {
    const button = $('log-new-records-btn');
    button.hidden = logAutoFollow || logUnseenCount === 0;
    button.textContent = logUnseenCount > 0 ? `有 ${logUnseenCount} 条新动态 · 回到最新` : '回到最新';
}

function setLogDetailsExpanded(expanded) {
    const panel = $('log-panel');
    if (!panel) return;
    const layout = panel.closest('.generation-layout');
    const button = $('log-toggle-btn');
    panel.classList.toggle('is-collapsed', !expanded);
    layout?.classList.toggle('is-log-collapsed', !expanded);
    button?.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    if (button) button.textContent = expanded ? '收起详情' : '展开详情';
    if (expanded) setLogAutoFollow(true, { scrollToEnd: true });
}

function setLogAutoFollow(enabled, { scrollToEnd = false } = {}) {
    logAutoFollow = Boolean(enabled);
    const button = $('log-follow-btn');
    button.classList.toggle('is-active', logAutoFollow);
    button.setAttribute('aria-pressed', logAutoFollow ? 'true' : 'false');
    button.textContent = logAutoFollow ? '跟随最新' : '已暂停跟随';
    if (logAutoFollow) logUnseenCount = 0;
    updateLogNewRecordsButton();
    if (scrollToEnd) {
        requestAnimationFrame(() => {
            const body = $('progress-log');
            body.scrollTop = body.scrollHeight;
        });
    }
}

function setLogFilter(filter) {
    logFilter = ['all', 'running', 'success', 'issues'].includes(filter) ? filter : 'all';
    $$('[data-log-filter]').forEach(button => {
        const active = button.dataset.logFilter === logFilter;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    applyLogFilter();
}

function upsertLogEntry(rawEntry) {
    const entry = normalizeLogEntry(rawEntry);
    if (!entry) return { changed: false, isNew: false, entry: null };
    const existing = logEntriesByKey.get(entry.key);
    if (existing && Number(existing.entry.seq) >= Number(entry.seq)) {
        return { changed: false, isNew: false, entry: existing.entry };
    }
    const existingDetails = existing?.element?.querySelector('.log-source-preview');
    const preserveDetailsOpen = Boolean(existingDetails?.open);
    const restoreDetailsFocus = Boolean(
        existing?.element && existing.element.contains(document.activeElement)
    );
    const element = createLogEntryElement(entry);
    const nextDetails = element.querySelector('.log-source-preview');
    if (nextDetails && preserveDetailsOpen) nextDetails.open = true;
    existing?.element?.remove();
    $('progress-log').querySelector('.log-empty')?.remove();
    $('progress-log').appendChild(element);
    if (existing) logEntriesByKey.delete(entry.key);
    logEntriesByKey.set(entry.key, { entry, element });
    element.hidden = !logEntryMatchesFilter(entry);
    updateLogStageRail(entry);
    if (entry.status === 'warning' || entry.status === 'error') {
        $('log-live-announcer').textContent = `${entry.title}，${logStatusLabel(entry)}`;
    }
    if (nextDetails && restoreDetailsFocus) {
        requestAnimationFrame(() => nextDetails.querySelector('summary')?.focus());
    }
    return { changed: true, isNew: !existing, entry };
}

function finalizeLogUpdate(results) {
    const changed = results.filter(result => result.changed);
    if (changed.length === 0) return;
    trimLogTimeline();
    applyLogFilter();
    const lastEntry = changed[changed.length - 1].entry;
    updateLogTimelineHeader(lastEntry);
    if (logAutoFollow) {
        requestAnimationFrame(() => {
            const body = $('progress-log');
            body.scrollTop = body.scrollHeight;
        });
    } else {
        const visibleChanges = changed.filter(result => logEntryMatchesFilter(result.entry));
        if (visibleChanges.length > 0) {
            logUnseenCount += visibleChanges.length;
            updateLogNewRecordsButton();
        }
    }
}

function addLogEntry(entry) {
    finalizeLogUpdate([upsertLogEntry(entry)]);
}

function addLogEntries(entries) {
    const sorted = [...entries].sort((a, b) => (Number(a?.seq) || 0) - (Number(b?.seq) || 0));
    finalizeLogUpdate(sorted.map(entry => upsertLogEntry(entry)));
}

function resetLogTimeline(emptyText = '任务开始后，这里会按阶段展示详细处理记录。') {
    logEntriesByKey.clear();
    logSeenSeq.clear();
    logEntryCount = 0;
    logFilter = 'all';
    logAutoFollow = true;
    logUnseenCount = 0;
    logLocalSeq = 0;
    logStageIndex = -1;
    logStageStates.clear();
    $('progress-log').replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'log-empty';
    empty.textContent = emptyText;
    $('progress-log').appendChild(empty);
    $('log-count').textContent = '0 个节点';
    $('log-issue-count').textContent = '0';
    $('log-summary').textContent = '阶段记录和异常项实时更新';
    $$('#log-stage-rail [data-log-stage]').forEach(stage => {
        stage.classList.remove('is-active', 'is-complete', 'is-warning', 'is-error');
        const stageLabel = LOG_STAGE_LABELS[stage.dataset.logStage] || stage.textContent.trim();
        stage.setAttribute('aria-label', `${stageLabel}：未开始`);
        stage.removeAttribute('aria-current');
    });
    setLogFilter('all');
    setLogAutoFollow(true);
    // 任务时间线是生成页的默认详情视图；每次新任务重置后都保持展开，
    // 这样浏览器启动、提交和失败原因不会被折叠在进度卡下面。
    setLogDetailsExpanded(true);
}


registerRendererModule("generation.log", {
    LOG_STAGE_LABELS,
    LOG_STAGE_ORDER,
    formatLogDuration,
    normalizeLogEntry,
    logStatusLabel,
    createLogMeta,
    createLogEntryElement,
    logEntryMatchesFilter,
    applyLogFilter,
    updateLogStageRail,
    renderLogStageRail,
    trimLogTimeline,
    updateLogTimelineHeader,
    updateLogNewRecordsButton,
    setLogDetailsExpanded,
    setLogAutoFollow,
    setLogFilter,
    upsertLogEntry,
    finalizeLogUpdate,
    addLogEntry,
    addLogEntries,
    resetLogTimeline,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

