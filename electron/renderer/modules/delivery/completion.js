/** Renderer module: delivery.completion */
(function attachRendererFeature_delivery_completion(root) {
    'use strict';

// ============================================================================
// Step 4: 完成
// ============================================================================

const RESULT_NAVIGATION_DELAY_MS = 950;
let terminalCompletionPromise = null;

function deliveryNavigationIsEligible(attemptId = generationAttemptId, sessionId = currentSession?.session_id) {
    return currentView === 'workflow'
        && currentStep === 3
        && generationResult === 'done'
        && attemptId === generationAttemptId
        && currentSession?.session_id === sessionId
        && Boolean(latestCurrentResultEvent);
}

function navigateToDeliveryIfReady({
    attemptId = generationAttemptId,
    sessionId = currentSession?.session_id,
    delay = RESULT_NAVIGATION_DELAY_MS,
} = {}) {
    // A late authoritative workspace refresh can arrive after the original
    // done event. Reuse the same guard and timer so that refreshes cannot
    // leave a completed task stranded on the generation console.
    if (!deliveryNavigationIsEligible(attemptId, sessionId)) return false;
    if (resultNavigationTimer) return true;

    const navigate = () => {
        resultNavigationTimer = null;
        if (deliveryNavigationIsEligible(attemptId, sessionId)) goToStep(4);
    };
    if (Number(delay) <= 0) navigate();
    else resultNavigationTimer = setTimeout(navigate, Number(delay));
    return true;
}

function ensureTerminalDeliveryHandoff(workspace = currentWorkspace, snapshot = workspace?.snapshot || currentSession) {
    if (currentView !== 'workflow' || currentStep !== 3 || activeWorkspace !== 'generation') return false;
    if (generationResult === 'done' && latestCurrentResultEvent) {
        return navigateToDeliveryIfReady();
    }

    const resultStatus = String(snapshot?.result_status || '').trim().toUpperCase();
    if (!['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(resultStatus)) return false;
    if (!currentSession?.session_id || terminalCompletionPromise) return Boolean(terminalCompletionPromise);

    const finalize = root.WORDTTS_RENDERER?.getModule?.('generation.sse')?.finalizeSuccessfulWorkflowEvent;
    if (typeof finalize !== 'function') return false;

    const sessionId = currentSession.session_id;
    terminalCompletionPromise = Promise.resolve(finalize({
        type: 'done',
        workflow_id: sessionId,
        event_key: `workflow:terminal:${sessionId}`,
    }, sessionId))
        .catch(error => {
            console.warn('终态工作区交付跳转失败:', error);
            return false;
        })
        .finally(() => {
            terminalCompletionPromise = null;
        });
    return true;
}

function handleDone(event) {
    resetGenerateState();
    setProgressIndeterminate(false);
    generationResult = 'done';
    transientGenerationErrorMessage = '';
    hideGenerationRecovery();

    clearSSEReconnectTimer();
    sseConnectionToken++;
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }

    // 合并最后的统计数据（done 事件本身不携带 completed/failed）
    const doneData = {
        ...event,
        completed: lastStats ? lastStats.completed : (event.completed || 0),
        failed: lastStats ? lastStats.failed : (event.failed || 0),
        cancelled: Math.max(Number(lastStats?.cancelled) || 0, Number(event.cancelled) || 0),
        skipped: Math.max(Number(lastStats?.skipped) || 0, Number(event.skipped) || 0),
        total: lastStats ? lastStats.total : (event.total || 0),
        failed_items: lastStats?.failed_items || event.failed_items || [],
    };
    latestCurrentResultEvent = {
        ...doneData,
        workflow_id: doneData.workflow_id || currentSession?.session_id || null,
    };

    // 更新生成页面状态。终态进度代表“可交付结果”而不是“处理过的
    // 条目”；失败和取消也算 processed，但不能把交付进度伪装成 100%。
    const totalCount = Math.max(0, Math.round(Number(doneData.total) || 0));
    const completedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.completed) || 0)));
    const failedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.failed) || 0)));
    const cancelledCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.cancelled) || 0)));
    const skippedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.skipped) || 0)));
    const unresolved = failedCount + cancelledCount;
    const allFailed = totalCount > 0 && completedCount === 0 && unresolved + skippedCount >= totalCount;
    $('gen-title').textContent = allFailed
        ? '本次生成未完成'
        : (unresolved > 0 ? '音频已部分生成' : '生成完成');
    setGenerationVisualState(allFailed ? 'error' : (unresolved > 0 ? 'warning' : 'done'));
    const terminalPercent = terminalProgressPercent(completedCount, totalCount);
    setProgressReadoutMode(true, unresolved > 0 || skippedCount > 0);
    setProgressBarPercent(terminalPercent);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(terminalPercent));
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${terminalPercent}% 可交付`);
    $('progress-percent').textContent = String(terminalPercent);
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = String(completedCount);
    $('progress-remaining').textContent = String(Math.max(totalCount - completedCount - failedCount - cancelledCount - skippedCount, 0));
    $('progress-failed').textContent = String(failedCount);
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelledCount);
    if ($('progress-skipped')) $('progress-skipped').textContent = String(skippedCount);
    $('progress-stats').textContent = `${completedCount} / ${totalCount || completedCount}`
        + (failedCount > 0 ? `  ·  失败 ${failedCount}` : '')
        + (cancelledCount > 0 ? `  ·  已取消 ${cancelledCount}` : '')
        + (skippedCount > 0 ? `  ·  已跳过 ${skippedCount}` : '');

    if (unresolved > 0 || skippedCount > 0) {
        const firstFailure = Array.isArray(doneData.failed_items)
            ? doneData.failed_items.find(item => String(item?.error || item?.user_message || '').trim())
            : null;
        const firstReason = String(firstFailure?.error || firstFailure?.user_message || '')
            .trim()
            .replace(/\s+/g, ' ')
            .slice(0, 240);
        const recoveryMessage = allFailed
            ? (firstReason
                ? `没有生成可交付音频。首条失败原因：${firstReason}`
                : '没有生成可交付音频，请展开任务时间线查看失败原因后重试。')
            : `本次有 ${unresolved + skippedCount} 条内容未进入交付范围，可查看任务时间线后重试。`;
        // This is already a terminal result. The actionable retry control is
        // the result-page failed-item action; the generation recovery panel
        // must not expose a button that routes through the non-terminal retry
        // flow and then silently falls back to configuration.
        showGenerationRecovery(recoveryMessage, {
            title: allFailed ? '生成失败' : '部分完成',
            retryVisible: false,
        });
    } else {
        hideGenerationRecovery();
    }

    // 构建结果页面
    buildResultPage(doneData);
    void refreshHistoryRecords({ showLoading: false });

    // 短暂停留展示完成态，再进入音频交付中心。统一入口也会被权威
    // workspace 刷新复用，避免“完成事件先到、终态快照后到”时卡在生成页。
    navigateToDeliveryIfReady({
        attemptId: generationAttemptId,
        sessionId: currentSession?.session_id,
    });

    showToast(unresolved > 0 ? `任务结束，${doneData.failed || 0} 条失败、${doneData.cancelled || 0} 条已取消` : '处理完成');
}


registerRendererModule("delivery.completion", {
    handleDone,
    navigateToDeliveryIfReady,
    ensureTerminalDeliveryHandoff,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
