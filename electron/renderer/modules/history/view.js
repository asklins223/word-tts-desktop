/** Renderer module: history.view */
(function attachRendererFeature_history_view(root) {
    'use strict';

function renderHistoryRecords(records) {
    const list = $('history-list');
    const empty = $('history-empty');
    if (!list || !empty) return;
    const visibleRecords = (Array.isArray(records) ? records : [])
        .filter(historyRecordMatchesFilter)
        .slice()
        .sort((left, right) => historyRecordTimestamp(right) - historyRecordTimestamp(left));
    list.replaceChildren();
    empty.hidden = visibleRecords.length > 0;
    const emptyTitle = empty.querySelector('h2');
    const emptyMessage = empty.querySelector('p');
    const hasRecords = Array.isArray(records) && records.length > 0;
    if (emptyTitle) emptyTitle.textContent = hasRecords ? '没有符合条件的任务' : '还没有生成记录';
    if (emptyMessage) emptyMessage.textContent = hasRecords
        ? '试试更换关键词或状态筛选，所有任务仍会保留在本机历史记录中。'
        : '完成一次音频生成后，结果会自动保存在这里。';
    if (visibleRecords.length === 0) return;

    visibleRecords.forEach(record => {
        const activeCandidate = record.active_candidate || null;
        const availableCount = nonNegativeCount(record.available_files);
        // Zero is an authoritative value.  Only an absent/invalid completed
        // field may fall back to the legacy available_files projection.
        const completed = nonNegativeCount(record.completed, availableCount);
        const failed = nonNegativeCount(record.failed);
        const cancelled = nonNegativeCount(record.cancelled);
        const skipped = nonNegativeCount(record.skipped);
        const total = Math.max(completed + failed + cancelled + skipped, nonNegativeCount(record.total));
        const pending = Math.max(
            0,
            record.pending !== null && record.pending !== undefined && Number.isFinite(Number(record.pending))
                ? nonNegativeCount(record.pending)
                : total - completed - failed - cancelled - skipped,
        );
        const presentation = historyStatusPresentation(record);
        const isInputTask = historyRecordDeliveryMode(record) === 'audio_and_input';
        const inputPresentation = isInputTask ? historyInputStatusPresentation(record) : null;
        const inputUnits = historyInputUnitsProgress(record);
        const terminal = isTerminalWorkflowSnapshot(record);
        const item = document.createElement('article');
        item.className = `history-item${activeCandidate ? ' is-active-task' : ''}`;

        const icon = document.createElement('span');
        icon.className = 'history-item-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = historyFormatLabel(record);

        const main = document.createElement('div');
        main.className = 'history-item-main';
        const titleRow = document.createElement('div');
        titleRow.className = 'history-item-title-row';
        const title = document.createElement('h2');
        title.className = 'history-item-title';
        title.textContent = record.source_filename || '未命名文档.docx';
        title.title = title.textContent;
        titleRow.appendChild(title);

        const badges = document.createElement('div');
        badges.className = 'history-item-badges';
        const status = document.createElement('span');
        status.className = `history-status-badge ${presentation.className}`.trim();
        status.textContent = presentation.label;
        const statusFacts = [record.execution_state, record.result_status, record.control_state];
        if (isInputTask) {
            statusFacts.push(`INPUT:${historyRecordInputStatus(record)}`);
            const inputBadge = document.createElement('span');
            inputBadge.className = `history-status-badge history-input-badge ${inputPresentation.className}`.trim();
            const inputDot = document.createElement('span');
            inputDot.className = 'history-input-dot';
            inputDot.setAttribute('aria-hidden', 'true');
            inputBadge.append(inputDot, document.createTextNode(inputPresentation.label));
            inputBadge.title = `录入状态：${inputPresentation.label}`;
            badges.appendChild(inputBadge);
        }
        status.title = statusFacts.filter(Boolean).join(' · ');
        badges.prepend(status);
        titleRow.appendChild(badges);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const scope = document.createElement('span');
        scope.textContent = record.preview ? '试听任务' : '完整任务';
        const mode = document.createElement('span');
        mode.textContent = historyGenerationModeLabel(record);
        const completedAt = document.createElement('span');
        completedAt.textContent = `${terminal ? '完成' : '更新'} ${historyDateLabel(terminal ? record.completed_at : record.updated_at)}`;
        if (isInputTask) {
            const deliveryTag = document.createElement('span');
            deliveryTag.className = 'history-mode-tag';
            deliveryTag.textContent = historyDeliveryTagLabel(record);
            deliveryTag.title = systemInputDeliveryModeLabel(record.delivery_mode);
            meta.appendChild(deliveryTag);
        }
        meta.append(completedAt, scope, mode);
        if (activeCandidate) {
            const activeLabel = document.createElement('span');
            activeLabel.className = 'history-active-label';
            activeLabel.textContent = historyActiveStatusLabel(activeCandidate);
            meta.appendChild(activeLabel);
        }

        const stats = document.createElement('div');
        stats.className = 'history-item-stats';
        const audioStat = document.createElement('span');
        audioStat.className = 'history-stat';
        const audioStrong = document.createElement('strong');
        audioStrong.textContent = `${availableCount}/${total}`;
        audioStat.append(audioStrong, document.createTextNode(' 个可交付'));
        stats.append(audioStat);
        if (inputUnits) {
            const inputStat = document.createElement('span');
            inputStat.className = 'history-stat is-input';
            if (inputUnits.succeeded < inputUnits.total
                && ['needs_reconcile', 'failed_retryable', 'failed'].includes(historyRecordInputStatus(record))) {
                inputStat.classList.add('is-input-attention');
            }
            const inputStrong = document.createElement('strong');
            inputStrong.textContent = `${inputUnits.succeeded}/${inputUnits.total}`;
            inputStat.append(inputStrong, document.createTextNode(' 单元已录入'));
            stats.appendChild(inputStat);
        }
        const formatStat = document.createElement('span');
        formatStat.className = 'history-stat';
        const formatStrong = document.createElement('strong');
        formatStrong.textContent = historyFormatLabel(record);
        formatStat.append(formatStrong, document.createTextNode(' 格式'));
        stats.appendChild(formatStat);
        if (failed > 0) {
            const failedStat = document.createElement('span');
            failedStat.className = 'history-stat';
            const failedStrong = document.createElement('strong');
            failedStrong.textContent = String(failed);
            failedStat.append(failedStrong, document.createTextNode(' 条失败'));
            stats.appendChild(failedStat);
        }
        if (cancelled > 0) {
            const cancelledStat = document.createElement('span');
            cancelledStat.className = 'history-stat is-cancelled';
            const cancelledStrong = document.createElement('strong');
            cancelledStrong.textContent = String(cancelled);
            cancelledStat.append(cancelledStrong, document.createTextNode(' 条已取消'));
            stats.appendChild(cancelledStat);
        }
        if (skipped > 0) {
            const skippedStat = document.createElement('span');
            skippedStat.className = 'history-stat is-skipped';
            const skippedStrong = document.createElement('strong');
            skippedStrong.textContent = String(skipped);
            skippedStat.append(skippedStrong, document.createTextNode(' 条已跳过'));
            stats.appendChild(skippedStat);
        }
        if (pending > 0) {
            const pendingStat = document.createElement('span');
            pendingStat.className = 'history-stat';
            const pendingStrong = document.createElement('strong');
            pendingStrong.textContent = String(pending);
            pendingStat.append(pendingStrong, document.createTextNode(' 条待处理'));
            stats.appendChild(pendingStat);
        }

        main.append(titleRow, meta, stats);

        const actions = document.createElement('div');
        actions.className = 'history-item-actions';
        const viewBtn = createHistoryAction(
            historyActiveActionLabel(record),
            'btn-primary',
            () => viewHistoryRecord(record.workflow_id || record.id),
        );
        if (record.zip_available && record.zip_artifact_id) {
            const zipBtn = createHistoryAction('下载 ZIP', 'btn-ghost', async () => {
                zipBtn.disabled = true;
                try {
                    await downloadZip({
                        mode: 'history',
                        recordId: record.id,
                        workflowId: record.workflow_id || record.id,
                        sourceFilename: record.source_filename,
                        zipArtifactId: record.zip_artifact_id || null,
                    });
                } finally {
                    zipBtn.disabled = false;
                }
            });
            actions.appendChild(zipBtn);
        }
        const deleteBtn = createHistoryAction(
            terminal ? '归档' : '删除',
            'btn-ghost history-delete-btn',
            () => deleteHistoryRecord(record, deleteBtn),
        );
        deleteBtn.disabled = terminal ? false : record.can_delete === false;
        if (terminal) {
            deleteBtn.title = '隐藏已完成任务，保留其审计事实和文件';
        } else if (record.can_delete === false) {
            deleteBtn.title = record.delete_reason || '当前任务暂时不能删除';
        } else {
            deleteBtn.title = '删除未完成任务及其相关本地数据';
        }
        actions.prepend(viewBtn);
        actions.appendChild(deleteBtn);
        item.append(icon, main, actions);
        list.appendChild(item);
    });
}

function renderHistoryMessage(message, className = 'history-loading') {
    const list = $('history-list');
    const empty = $('history-empty');
    if (!list || !empty) return;
    empty.hidden = true;
    list.replaceChildren();
    const notice = document.createElement('div');
    notice.className = className;
    notice.textContent = message;
    list.appendChild(notice);
}

function renderActiveCandidateHint(candidates = activeWorkflowCandidates) {
    const hint = $('active-task-hint');
    const text = $('active-task-hint-text');
    if (!hint || !text) return;
    const visible = !currentSession && Array.isArray(candidates) && candidates.length > 0;
    hint.hidden = !visible;
    if (visible) {
        text.textContent = activeCandidateHintText(candidates, activeWorkflowListTruncated);
        hint.title = '打开历史记录查看未结束任务';
        hint.onclick = () => showHistoryPage();
        hint.tabIndex = 0;
        hint.onkeydown = event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                showHistoryPage();
            }
        };
    } else {
        hint.onclick = null;
        hint.onkeydown = null;
        hint.removeAttribute('tabindex');
    }
}


registerRendererModule("history.view", {
    renderHistoryRecords,
    renderHistoryMessage,
    renderActiveCandidateHint,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

