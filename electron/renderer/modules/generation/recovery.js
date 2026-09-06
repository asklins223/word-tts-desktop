/** Renderer module: generation.recovery */
(function attachRendererFeature_generation_recovery(root) {
    'use strict';

const GENERATION_RECOVERY_EXECUTION_STATES = new Set([
    'BLOCKED',
    'WAITING_RETRY',
    'WAITING_USER',
    'FAILED',
]);

function generationRecoveryMessage(workspace, state, progress, { transientMessage = transientGenerationErrorMessage } = {}) {
    const current = workspace || {};
    const snapshot = current.snapshot || {};
    const blocker = typeof workflowAdapter.blockerSummary === 'function'
        ? workflowAdapter.blockerSummary(current)
        : null;
    const failedItem = (Array.isArray(current.items) ? current.items : [])
        .find(item => String(item?.status || '').toUpperCase() === 'FAILED');
    const explicitMessage = [
        snapshot.last_error_message,
        current.last_error_message,
        currentSession?.last_error_message,
        blocker?.message,
        failedItem?.user_message,
    ].map(value => String(value || '').trim()).find(Boolean);
    if (explicitMessage) {
        if (blocker?.title && explicitMessage === String(blocker.message || '').trim()) {
            return `${blocker.title}：${explicitMessage}`;
        }
        return explicitMessage;
    }

    const transient = String(transientMessage || '').trim();
    if (transient) return transient;

    const failed = Math.max(0, Math.round(Number(progress?.failed) || 0));
    const key = String(state?.key || '').toUpperCase();
    if (key === 'WAITING_RETRY') {
        return failed > 0
            ? `生成过程已中断，有 ${failed} 条内容未完成；任务已停在安全重试点，可点击“重试生成”继续。`
            : '生成过程已中断，任务已停在安全重试点，可点击“重试生成”继续。';
    }
    if (key === 'WAITING_USER') {
        return failed > 0
            ? `有 ${failed} 条内容未完成，任务正在等待处理；请先查看任务记录再决定下一步。`
            : '任务正在等待人工处理；请先查看任务记录再决定下一步。';
    }
    if (key === 'BLOCKED') return '任务被阻塞，当前自动化流程已经停止；请查看任务记录中的处理原因。';
    if (key === 'FAILED') return '生成任务未能完成；任务记录已保留，请返回声音配置后重新生成。';
    if (failed > 0) return `当前有 ${failed} 条内容未完成，任务记录已保留；请等待状态收敛后再重试。`;
    return '生成任务未能继续，请重试或返回声音配置调整设置。';
}

function generationRecoveryPresentation(
    workspace = currentWorkspace,
    state = null,
    {
        generationResultState = generationResult,
        transientMessage = transientGenerationErrorMessage,
    } = {},
) {
    const current = workspace || {};
    const resolvedState = state || workspaceUserState(current, current.snapshot || currentSession);
    const key = String(resolvedState?.key || '').toUpperCase();
    const progress = workspaceProgress(current);
    const snapshot = current.snapshot || {};
    const control = String(snapshot.control_state || '').toUpperCase();
    const terminal = Boolean(resolvedState?.terminal || isTerminalWorkflowSnapshot(snapshot));
    const blocker = typeof workflowAdapter.blockerSummary === 'function'
        ? workflowAdapter.blockerSummary(current)
        : null;
    const hasFailure = Math.max(0, Number(progress?.failed) || 0) > 0;
    const hasPersistedError = Boolean(
        String(snapshot.last_error_message || current.last_error_message || currentSession?.last_error_message || '').trim(),
    );
    const hasIssueState = GENERATION_RECOVERY_EXECUTION_STATES.has(key);
    const isTransientError = String(generationResultState || '').toLowerCase() === 'error';
    const hasTransientError = Boolean(String(transientMessage || '').trim());
    const hardStopped = isHardStoppedWorkflowSnapshot(snapshot);

    // A pause/stop control state owns the page until its command settles. Do
    // not let an old failed-item count reopen an error panel over that state.
    if (hardStopped || ['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING'].includes(control)) {
        return null;
    }
    if (!isTransientError && !hasTransientError && !hasIssueState && !hasPersistedError && !blocker && !hasFailure) return null;

    const retryAction = workspaceAction('RETRY', current);
    // A transient renderer error is only retryable while the workflow is
    // still in the startup/accepted execution window. Once the server has
    // projected WAITING_RETRY/WAITING_USER without an enabled RETRY action,
    // showing a button would create a misleading no-op recovery panel.
    const transientRetryAllowed = isTransientError
        && ['CREATED', 'PREPARING', 'RUNNING', 'RECOVERING'].includes(key);
    // Terminal runs are immutable; their retry action belongs to the result
    // page (or a fresh rerun), not this live-generation recovery panel.
    const retryVisible = !terminal
        && (retryAction?.enabled === true || transientRetryAllowed);
    const title = key === 'WAITING_RETRY'
        ? '任务已中断，可重试'
        : key === 'WAITING_USER'
            ? '任务需要处理'
            : key === 'BLOCKED'
                ? '任务被阻塞'
                : key === 'FAILED'
                    ? '生成失败'
                    : '生成异常';
    return {
        key: key || 'ERROR',
        title,
        message: generationRecoveryMessage(current, resolvedState, progress, { transientMessage }),
        retryVisible,
        retryLabel: key === 'WAITING_RETRY' ? '重试生成' : '重试生成',
        returnVisible: true,
    };
}

function generationRecoveryIsSuppressed({
    generationActive = isGenerating,
    retryInFlight = generationRecoveryRetryInFlight,
} = {}) {
    return Boolean(generationActive || retryInFlight);
}

function syncGenerationRecoveryState(workspace = currentWorkspace, state = null, progress = null) {
    // A recovery card describes an idle, actionable failure. Once a retry has
    // started, an old persisted error must not remain above the new progress
    // view while the retry handshake is still settling.
    if (generationRecoveryIsSuppressed()) {
        hideGenerationRecovery();
        return null;
    }
    const presentation = generationRecoveryPresentation(workspace, state, {
        generationResultState: generationResult,
        transientMessage: transientGenerationErrorMessage,
    });
    if (!presentation) {
        hideGenerationRecovery();
        return null;
    }
    // An accepted RECOVERING/RUNNING task is already owned by the background
    // worker.  The recovery card may still be useful as an explanation of the
    // previous failure, but its retry button would be a no-op while the live
    // task is being adopted.  Only expose the action once the server has
    // settled into a retryable state and the renderer is idle.
    const retryVisible = presentation.retryVisible
        && !isGenerating
        && !generationStartInFlight;
    showGenerationRecovery(presentation.message, {
        title: presentation.title,
        retryVisible,
        retryLabel: presentation.retryLabel,
        returnVisible: presentation.returnVisible,
    });
    return presentation;
}

function showGenerationRecovery(message, {
    title = '生成异常',
    retryVisible = true,
    retryLabel = '重试生成',
    returnVisible = true,
    returnLabel = '返回声音配置',
} = {}) {
    const panel = $('generation-recovery');
    const titleEl = $('generation-recovery-title');
    const messageEl = $('generation-error-message');
    if (titleEl) titleEl.textContent = title;
    if (messageEl) {
        messageEl.textContent = message || '生成任务未能继续，请重试或返回声音配置。';
    }
    if (panel) panel.hidden = false;
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        retryButton.hidden = !retryVisible;
        retryButton.disabled = !retryVisible;
        retryButton.textContent = retryLabel;
    }
    const returnButton = $('return-config-btn');
    if (returnButton) {
        returnButton.hidden = !returnVisible;
        returnButton.disabled = !returnVisible;
        returnButton.textContent = returnLabel;
    }
}

function hideGenerationRecovery() {
    const panel = $('generation-recovery');
    if (panel) panel.hidden = true;
    const titleEl = $('generation-recovery-title');
    if (titleEl) titleEl.textContent = '生成异常';
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        retryButton.hidden = false;
        retryButton.disabled = false;
        retryButton.textContent = '重试生成';
    }
    const returnButton = $('return-config-btn');
    if (returnButton) {
        returnButton.hidden = false;
        returnButton.disabled = false;
        returnButton.textContent = '返回声音配置';
    }
}

function syncTransientGenerationErrorShell(message) {
    if (!message || currentView !== 'workflow' || activeWorkspace !== 'generation') return;
    const badge = $('task-status-badge');
    if (badge) {
        badge.className = 'task-status-badge is-danger';
        badge.textContent = '生成异常';
        badge.title = message;
    }
    document.body.dataset.workflowState = 'FAILED';
    document.body.dataset.generationState = 'FAILED';
}

function clearGenerationStartupTimer() {
    if (generationStartupTimer) {
        clearTimeout(generationStartupTimer);
        generationStartupTimer = null;
    }
}

function setProgressIndeterminate(enabled) {
    const bar = $('progress-bar');
    const track = bar?.parentElement;
    bar?.classList.toggle('is-indeterminate', Boolean(enabled));
    if (track) track.setAttribute('aria-busy', enabled ? 'true' : 'false');
}


registerRendererModule("generation.recovery", {
    GENERATION_RECOVERY_EXECUTION_STATES,
    generationRecoveryMessage,
    generationRecoveryPresentation,
    generationRecoveryIsSuppressed,
    syncGenerationRecoveryState,
    showGenerationRecovery,
    hideGenerationRecovery,
    syncTransientGenerationErrorShell,
    clearGenerationStartupTimer,
    setProgressIndeterminate,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

