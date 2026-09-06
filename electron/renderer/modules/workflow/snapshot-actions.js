/** Renderer module: workflow.snapshotActions */
(function attachRendererFeature_workflow_snapshotActions(root) {
    'use strict';

function setAppInteractive(enabled) {
    const effectiveEnabled = enabled && !isRestarting;
    const uploadZone = $('upload-zone');
    if (uploadZone) {
        uploadZone.classList.toggle('is-disabled', !effectiveEnabled);
        uploadZone.setAttribute('aria-disabled', effectiveEnabled ? 'false' : 'true');
        uploadZone.tabIndex = effectiveEnabled ? 0 : -1;
    }
    ['start-generate-btn', 'skip-config-btn'].forEach(id => {
        const button = $(id);
        if (button) button.disabled = !effectiveEnabled;
    });
    renderProviderStatus();
    updateConfigActionState(currentWorkspace);
}

/**
 * 将服务端工作流快照合并到当前会话。
 *
 * 生成期间事件流可能先送达一个较早的快照；状态版本只能向前推进，
 * 不能让旧快照把已经拿到的版本回退，否则用户返回配置后重试会把
 * stale expected_state_version 提交给后端。
 */
function workflowSnapshotIsOlder(candidate, reference) {
    if (!candidate || !reference) return false;
    const candidateWorkflowId = String(candidate.workflow_id || '');
    const referenceWorkflowId = String(reference.workflow_id || reference.session_id || '');
    if (candidateWorkflowId && referenceWorkflowId && candidateWorkflowId !== referenceWorkflowId) return false;

    const candidateSeq = Number(candidate.latest_seq);
    const referenceSeq = Number(reference.latest_seq);
    const hasCandidateSeq = Number.isInteger(candidateSeq) && candidateSeq >= 0;
    const hasReferenceSeq = Number.isInteger(referenceSeq) && referenceSeq >= 0;
    if (
        hasCandidateSeq
        && hasReferenceSeq
        && candidateSeq < referenceSeq
    ) return true;

    const candidateVersion = Number(candidate.state_version);
    const referenceVersion = Number(reference.state_version);
    const hasCandidateVersion = Number.isInteger(candidateVersion) && candidateVersion >= 0;
    const hasReferenceVersion = Number.isInteger(referenceVersion) && referenceVersion >= 0;
    if (hasCandidateVersion && hasReferenceVersion && candidateVersion < referenceVersion) return true;

    // A response without either freshness marker cannot safely replace a live
    // snapshot that already has one.  Older API responses used to omit these
    // fields; treating them as current made a scheduled refresh erase the
    // runtime projection and send the progress bar back to 0.
    return (!hasCandidateSeq && !hasCandidateVersion)
        && (hasReferenceSeq || hasReferenceVersion);
}

function workflowSnapshotBelongsToSession(snapshot, session = currentSession) {
    if (!snapshot || !session) return true;
    const snapshotWorkflowId = String(snapshot.workflow_id || '');
    const sessionWorkflowId = String(session.session_id || session.workflow_id || '');
    return !snapshotWorkflowId || !sessionWorkflowId || snapshotWorkflowId === sessionWorkflowId;
}

function workflowSnapshotIsStaleForSession(candidate, session = currentSession) {
    if (!candidate || !session) return false;
    if (!workflowSnapshotBelongsToSession(candidate, session)) return true;
    if (workflowSnapshotIsOlder(candidate, session)) return true;
    const workspaceSnapshot = currentWorkspace?.snapshot;
    return Boolean(
        workspaceSnapshot
        && String(workspaceSnapshot.workflow_id || '') === String(session.session_id || '')
        && workflowSnapshotIsOlder(candidate, workspaceSnapshot)
    );
}

function latestWorkflowSnapshotForSession(session = currentSession, fallback = null) {
    const workspaceSnapshot = currentWorkspace?.snapshot;
    if (
        workspaceSnapshot
        && (!session?.session_id || String(workspaceSnapshot.workflow_id || '') === String(session.session_id))
        && !workflowSnapshotIsOlder(workspaceSnapshot, session)
    ) return workspaceSnapshot;
    if (session) {
        return {
            ...session,
            workflow_id: session.workflow_id || session.session_id || fallback?.workflow_id,
        };
    }
    return fallback;
}

function mergeWorkflowSnapshotIntoSession(snapshot, session = currentSession) {
    if (!snapshot || !session) return session;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return session;
    if (workflowSnapshotIsOlder(snapshot, session)) return session;
    const snapshotVersion = Number(snapshot.state_version);
    if (Number.isInteger(snapshotVersion) && snapshotVersion >= 0) {
        session.state_version = snapshotVersion;
    }
    ['execution_state', 'control_state', 'result_status', 'cleanup_state', 'source_artifact_id', 'latest_event_id', 'latest_seq'].forEach((key) => {
        if (snapshot[key] !== undefined && snapshot[key] !== null) session[key] = snapshot[key];
    });
    ['last_error_code', 'last_error_message'].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(snapshot, key)) {
            session[key] = snapshot[key] === null || snapshot[key] === undefined
                ? null
                : String(snapshot[key]);
        }
    });
    const snapshotGroupVersion = Number(snapshot.group_state_version);
    const sessionGroupVersion = Number(session.group_state_version);
    if (Number.isInteger(snapshotGroupVersion) && snapshotGroupVersion >= 0
        && (!Number.isInteger(sessionGroupVersion) || snapshotGroupVersion >= sessionGroupVersion)) {
        session.group_state_version = snapshotGroupVersion;
    }
    return session;
}

function renderLiveWorkflowSnapshot(snapshot, session = currentSession) {
    if (!snapshot || !session || currentSession?.session_id !== session.session_id) return;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return;
    const stale = workflowSnapshotIsStaleForSession(snapshot, session);
    mergeWorkflowSnapshotIntoSession(snapshot, session);
    const effectiveSnapshot = stale
        ? latestWorkflowSnapshotForSession(session, snapshot)
        : snapshot;
    const workspaceId = String(currentWorkspace?.snapshot?.workflow_id || '');
    if (workspaceId === String(session.session_id)) {
        currentWorkspace = { ...currentWorkspace, snapshot: { ...effectiveSnapshot } };
    }
    // Cancellation returns a terminal local snapshot. Keep the badge and
    // action buttons in sync before the next SSE refresh, while preserving a
    // newer provider projection already accepted by the live Store.
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(currentWorkspace, effectiveSnapshot, session.session_id);
    } else {
        renderWorkspaceShell(currentWorkspace, effectiveSnapshot);
    }
}

/**
 * 在会话恢复/重新生成前读取一次权威工作流快照。
 * 这个请求不替代后端的乐观锁，只负责避免渲染器继续使用中断前缓存的
 * state_version；真正的命令仍然必须带 expected_state_version 到后端校验。
 */
async function refreshCurrentWorkflowSnapshot(session = currentSession) {
    if (!workflowApi || !session?.session_id) return null;
    const snapshot = await workflowApi.getWorkflow(session.session_id);
    if (currentSession?.session_id !== session.session_id) return null;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return null;
    const stale = workflowSnapshotIsStaleForSession(snapshot, session);
    mergeWorkflowSnapshotIntoSession(snapshot, session);
    const effectiveSnapshot = stale
        ? latestWorkflowSnapshotForSession(session, snapshot)
        : snapshot;
    if (!stale && currentWorkspace && currentWorkspace.snapshot) {
        // A server workspace snapshot is authoritative.  Replacing this
        // nested object matters when the server clears a blocker/runtime
        // field; merging would resurrect the stale value from the previous
        // snapshot.
        currentWorkspace = { ...currentWorkspace, snapshot: { ...snapshot } };
    }
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(currentWorkspace, effectiveSnapshot, session.session_id);
    } else {
        renderWorkspaceShell(currentWorkspace, effectiveSnapshot);
    }
    return effectiveSnapshot;
}

const ACCEPTED_GENERATION_EXECUTION_STATES = new Set([
    'RUNNING',
    'RECOVERING',
]);
const TERMINAL_WORKFLOW_RESULT_STATES = new Set([
    'SUCCEEDED',
    'PARTIAL_SUCCESS',
    'FAILED',
    'CANCELLED',
]);

function isTerminalWorkflowSnapshot(snapshot) {
    return Boolean(
        snapshot
        && String(snapshot.execution_state || '') === 'TERMINAL'
        && String(snapshot.control_state || '') === 'TERMINATED'
        && TERMINAL_WORKFLOW_RESULT_STATES.has(String(snapshot.result_status || ''))
    );
}

function isHardStoppedWorkflowSnapshot(snapshot) {
    if (!isTerminalWorkflowSnapshot(snapshot)) return false;
    const latestEventType = String(
        snapshot?.latest_event?.event_type
        || snapshot?.latest_event_type
        || '',
    ).toUpperCase();
    return String(snapshot?.result_status || '') === 'CANCELLED'
        || String(snapshot?.last_error_code || '') === 'WORKFLOW_CANCELLED'
        || latestEventType === 'WORKFLOW_CANCELLED';
}

function isAcceptedGenerationSnapshot(snapshot) {
    if (!snapshot || isTerminalWorkflowSnapshot(snapshot)) return false;
    const executionState = String(snapshot.execution_state || '');
    if (ACCEPTED_GENERATION_EXECUTION_STATES.has(executionState)) return true;
    if (String(snapshot.control_state || '') === 'TERMINATING') return true;
    if (executionState !== 'PREPARING') return false;

    // Parsing also uses PREPARING, so do not cancel a normal editable parse
    // draft. A prepared TTS run has a frozen configuration, and a recovered
    // active-list candidate carries the durable generation-accepted marker.
    const workflowId = String(snapshot.workflow_id || '');
    const workspace = currentWorkspace
        && String(currentWorkspace?.snapshot?.workflow_id || '') === workflowId
        ? currentWorkspace
        : null;
    const frozenFields = workspace?.configuration?.frozen_fields;
    const candidateAccepted = currentSession?.session_id === workflowId
        && currentSession?.active_candidate?.generation_accepted === true;
    return (Array.isArray(frozenFields) && frozenFields.length > 0) || candidateAccepted;
}

function isCancellationSettledSnapshot(snapshot) {
    return isTerminalWorkflowSnapshot(snapshot);
}

/**
 * The ZIP Artifact is created on demand.  A terminal result with at least one
 * verified audio file must therefore keep the ZIP action visible even before
 * the first export has been materialized.
 */
function resultZipState(context, resultCount) {
    const delivery = context?.workspace?.delivery || context?.delivery || null;
    const hasAuthoritativeDelivery = Boolean(
        delivery && typeof delivery === 'object'
        && ('zip_available' in delivery || 'zip_artifact_id' in delivery),
    );
    const authoritativeArtifactId = hasAuthoritativeDelivery
        ? (delivery.zip_available === true ? delivery.zip_artifact_id : null)
        : (context?.zipArtifactId || null);
    const hasArtifact = Boolean(
        authoritativeArtifactId
        && (hasAuthoritativeDelivery ? delivery.zip_available === true : context?.zipAvailable === true),
    );
    const count = Number(resultCount);
    const hasDeliverableAudio = Number.isFinite(count) && count > 0;
    const scopeRequired = Boolean(context && ('delivery' in context || 'workspace' in context));
    const hasScope = Boolean(
        delivery
        && Array.isArray(delivery.included_item_ids)
        && Array.isArray(delivery.excluded_item_ids)
        && delivery.exclusion_reasons
        && typeof delivery.exclusion_reasons === 'object',
    );
    const terminal = String(context?.executionState || '') === 'TERMINAL'
        || TERMINAL_WORKFLOW_RESULT_STATES.has(String(context?.resultStatus || ''));
    return {
        // Legacy callers that only know terminal + file count keep the old
        // projection; the real result context always carries a workspace or
        // delivery object and therefore must pass the range contract.
        visible: hasArtifact || (hasDeliverableAudio && terminal && (!scopeRequired || hasScope)),
        ready: hasArtifact,
    };
}

function setGenerationControlLabel(button, text) {
    if (!button) return;
    const label = button.querySelector('.generation-control-label');
    if (label) label.textContent = text;
    else button.textContent = text;
}

function updateGenerationCancelUI() {
    const button = $('cancel-generation-btn');
    if (!button) return;
    const cancelAction = workspaceAction('CANCEL', currentWorkspace);
    const projectionAllowsCancel = cancelAction?.enabled === true;
    const sessionActive = Boolean(
        currentSession?.session_id
        && !isTerminalWorkflowSnapshot(currentSession)
        && (
            projectionAllowsCancel
            || isGenerating
            || generationStartInFlight
            || cancelWorkflowPromise
        )
    );
    button.hidden = !sessionActive;
    button.disabled = Boolean(cancelWorkflowPromise) || isRestarting
        || (!projectionAllowsCancel && !isGenerating && !generationStartInFlight);
    const cancelLabel = cancelWorkflowPromise ? '正在停止…' : '停止生成';
    setGenerationControlLabel(button, cancelLabel);
    button.setAttribute('aria-label', cancelWorkflowPromise ? '正在停止生成' : '停止生成并结束本次任务');
    if (cancelWorkflowPromise) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    if (cancelAction?.reason && !cancelAction.enabled) button.title = cancelAction.reason;
    else button.title = cancelWorkflowPromise ? '正在等待任务停止' : '停止生成并结束本次任务';
    updateGenerationControlUI(currentWorkspace);
}

function updateConfigActionState(workspace = currentWorkspace) {
    const button = $('start-generate-btn');
    if (!button) return;
    const action = workspaceAction('GENERATE', workspace);
    const hasAuthoritativeAction = Boolean(workspace && Array.isArray(workspace.available_actions));
    const serviceReady = $('service-state')?.classList.contains('is-ready') !== false;
    // A user stop intentionally leaves the old run terminal and immutable,
    // but the voice form must remain the explicit entry point for creating a
    // fresh rerun. startProcessing() performs that rerun fence before saving
    // the current voice configuration, so the button is allowed here even
    // though the old workspace's GENERATE action is disabled.
    const hardStoppedAwaitingRerun = isHardStoppedWorkflowSnapshot(workspace?.snapshot || workspace)
        && activeWorkspace === 'voice'
        && !isGenerating
        && !generationStartInFlight;
    const blockedByWorkspace = hasAuthoritativeAction
        && action?.enabled !== true
        && !hardStoppedAwaitingRerun;
    const disabled = !currentSession?.session_id
        || !workspace
        || isRestarting
        || isParsing
        || isGenerating
        || generationStartInFlight
        || !serviceReady
        || blockedByWorkspace;
    button.disabled = disabled;
    button.setAttribute('aria-disabled', disabled ? 'true' : 'false');
    if (hardStoppedAwaitingRerun) {
        button.title = '当前生成已停止；确认声音配置后可重新生成';
    } else if (blockedByWorkspace) {
        button.title = action?.reason || '当前任务状态不允许生成';
    } else if (!serviceReady) {
        button.title = '等待生成服务连接';
    } else {
        button.removeAttribute('title');
    }
}

function updateGenerationControlUI(workspace = currentWorkspace) {
    const current = workspace || {};
    const state = workspaceUserState(current, currentSession);
    const presentation = generationStatePresentation(state, {
        pendingPause: pendingWorkspaceCommand('PAUSE'),
        pendingResume: pendingWorkspaceCommand('RESUME'),
        pendingCancel: generationCancelRequested || Boolean(cancelWorkflowPromise),
    });
    const pending = workflowStore?.getState?.().pendingCommands || {};
    const sessionId = currentSession?.session_id || '';
    const setActionButton = (id, type, visibleWhen = true) => {
        const button = $(id);
        if (!button) return;
        const action = workspaceAction(type, current);
        const commandKey = `${sessionId}:${type}`;
        const visible = Boolean(sessionId && visibleWhen && action && action.enabled === true && !presentation.terminal);
        button.hidden = !visible;
        button.disabled = !visible || Boolean(pending[commandKey]) || isRestarting;
        button.setAttribute('aria-busy', pending[commandKey] ? 'true' : 'false');
        const label = type === 'PAUSE'
            ? (pending[commandKey] ? '正在暂停…' : '暂停生成')
            : (pending[commandKey] ? '正在恢复…' : '恢复生成');
        setGenerationControlLabel(button, label);
        button.setAttribute('aria-label', pending[commandKey]
            ? (type === 'PAUSE' ? '正在暂停生成' : '正在恢复生成')
            : (type === 'PAUSE' ? '暂停生成并保留当前进度' : '恢复生成并继续当前任务'));
        if (visible) button.title = pending[commandKey]
            ? (type === 'PAUSE' ? '正在等待任务进入暂停状态' : '正在等待任务恢复')
            : (action.reason || (type === 'PAUSE' ? '暂停生成并保留当前进度' : '恢复生成并继续当前任务'));
    };
    setActionButton('pause-generation-btn', 'PAUSE', presentation.key === 'PREPARING' || presentation.key === 'RUNNING' || presentation.key === 'RECOVERING');
    setActionButton('resume-generation-btn', 'RESUME', presentation.key === 'PAUSED' || presentation.key === 'PAUSE_REQUESTED');
    const controlBar = $('generation-task-controls');
    if (controlBar) {
        const cancelButton = $('cancel-generation-btn');
        const cancelAction = workspaceAction('CANCEL', current);
        const hasVisibleControl = ['pause-generation-btn', 'resume-generation-btn']
            .some(id => $(id) && !$(id).hidden)
            || Boolean(
                sessionId
                && cancelButton
                && !cancelButton.hidden
                && (cancelAction?.enabled === true || isGenerating || generationStartInFlight || cancelWorkflowPromise),
            );
        controlBar.hidden = !hasVisibleControl;
    }
    updateConfigActionState(current);
}

/**
 * 接管已经被接受的工作流，而不是把它误当成仍可编辑的草稿。
 * 这是启动竞态和自动重试抢先启动两条路径共用的收敛点。
 */
function adoptAcceptedGeneration(session, snapshot, { reason = '任务已在后台运行' } = {}) {
    if (
        !session?.session_id
        || currentSession?.session_id !== session.session_id
        || !isAcceptedGenerationSnapshot(snapshot)
    ) return false;

    mergeWorkflowSnapshotIntoSession(snapshot, session);
    clearGenerationStartupTimer();
    generateAbortController = null;
    generationResult = null;
    transientGenerationErrorMessage = '';
    isGenerating = true;
    lastWorkspaceRenderKey = '';
    const presentation = renderGenerationViewState(
        currentWorkspace,
        workspaceUserState(currentWorkspace, snapshot),
        workspaceProgress(currentWorkspace),
    );
    // History takeover can have a persisted provider segment snapshot even
    // when no new SSE frame arrives after the page opens.  Overlay the Store's
    // runtime projection now, otherwise the item aggregate painted above can
    // hide that saved live progress until the next event.
    const storeState = workflowStore?.getState?.();
    const storeWorkflowId = String(
        storeState?.workflowId
        || storeState?.workflowProjection?.workflow_id
        || '',
    );
    if (
        storeState
        && storeWorkflowId === String(session.session_id)
        && typeof renderWorkflowWorkspace === 'function'
    ) {
        renderWorkflowWorkspace(storeState);
    }

    // Keep the adoption reason for an actively running task, but let the
    // authoritative presentation own paused/stopping/error states. This is
    // important when history opens an already-paused workflow: it must stay
    // visibly paused and must not restart an indeterminate progress animation.
    if (reason && ['PREPARING', 'RUNNING', 'RECOVERING'].includes(presentation?.key)) {
        $('generation-live-status').textContent = reason;
        $('status-text').textContent = reason;
        if (!lastStats) {
            const total = summarizeParseResults(session.parse_results).total;
            $('progress-stats').textContent = `${reason} · 0 / ${total || '—'}`;
        }
    }
    updateGenerationCancelUI();
    void connectSSE(session.session_id);
    return true;
}

function resumedGenerationSnapshot(workspace, response) {
    const snapshot = workspace?.snapshot
        || response?.current_snapshot
        || response?.workflow
        || response?.snapshot
        || response;
    return snapshot && typeof snapshot === 'object' ? snapshot : null;
}

function shouldAdoptResumedGeneration(
    type,
    workspace,
    response,
    { generationActive = isGenerating, startInFlight = generationStartInFlight } = {},
) {
    if (String(type || '').toUpperCase() !== 'RESUME' || generationActive || startInFlight) return false;
    const snapshot = resumedGenerationSnapshot(workspace, response);
    if (String(snapshot?.control_state || '').toUpperCase() !== 'RUNNING') return false;
    return isAcceptedGenerationSnapshot(snapshot);
}

function adoptResumedGenerationIfNeeded(type, workspace, response) {
    if (!shouldAdoptResumedGeneration(type, workspace, response)) return false;
    const snapshot = resumedGenerationSnapshot(workspace, response);
    if (!snapshot || !currentSession?.session_id) return false;
    const workflowId = String(snapshot.workflow_id || '');
    if (workflowId && workflowId === String(currentSession.session_id)) {
        if (workspace?.snapshot) {
            currentWorkspace = workspace;
        } else if (currentWorkspace) {
            currentWorkspace = {
                ...currentWorkspace,
                snapshot: { ...(currentWorkspace.snapshot || {}), ...snapshot },
            };
        }
    }
    return adoptAcceptedGeneration(currentSession, snapshot, {
        reason: '任务已恢复，正在接管生成进度',
    });
}

function workflowProgressCounts(snapshot, session = currentSession) {
    const progress = snapshot?.progress || session?.progress || {};
    const total = nonNegativeCount(progress.total, summarizeParseResults(session?.parse_results).total);
    const completed = nonNegativeCount(progress.completed, generatedFiles.length);
    const failed = nonNegativeCount(progress.failed);
    const cancelled = nonNegativeCount(progress.cancelled);
    return {
        total: Math.max(0, total),
        completed: Math.max(0, Math.min(completed, total || completed)),
        failed: Math.max(0, failed),
        cancelled: Math.max(0, cancelled),
    };
}

/**
 * 发出一次幂等取消命令。后端会立即返回本地终态；浏览器 worker 即使
 * 之后才退出，也不能再发布结果。
 */
async function cancelCurrentWorkflow(session = currentSession, {
    reason = 'desktop-user-cancel',
} = {}) {
    if (!workflowApi || !session?.session_id) return null;
    if (cancelWorkflowPromise) return cancelWorkflowPromise;

    const sessionId = session.session_id;
    const idempotencyKey = `renderer-cancel-${sessionId}-${generationAttemptId}`;
    generationCancelRequested = true;
    clearGenerationStartupTimer();
    if (generateAbortController) {
        generateAbortController.abort();
        generateAbortController = null;
        // 使尚未返回的 generate/patch 请求不能在取消命令之后继续接管页面。
        generationAttemptId++;
    }
    renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
    updateGenerationCancelUI();

    const operation = (async () => {
        let snapshot = await workflowApi.getWorkflow(sessionId);
        renderLiveWorkflowSnapshot(snapshot, session);
        if (isTerminalWorkflowSnapshot(snapshot)) return snapshot;

        // Cancellation is a local terminalization command, not a projected
        // workspace action. Always send it with the freshest version so a
        // stale/partial workspace cannot hide the only decisive stop path.
        let commandAttempts = 0;
        while (!isTerminalWorkflowSnapshot(snapshot) && commandAttempts < 2) {
            try {
                const response = await workflowApi.sendCommand(
                    sessionId,
                    'cancel',
                    {
                        expected_state_version: Number(snapshot.state_version || session.state_version || 0),
                        reason,
                    },
                    { idempotencyKey },
                );
                snapshot = response?.current_snapshot || response || snapshot;
                renderLiveWorkflowSnapshot(snapshot, session);
                break;
            } catch (error) {
                if (error?.code !== 'STATE_CONFLICT' || commandAttempts >= 1) throw error;
                snapshot = await workflowApi.getWorkflow(sessionId);
                renderLiveWorkflowSnapshot(snapshot, session);
            }
            commandAttempts++;
        }
        return snapshot;
    })();
    cancelWorkflowPromise = operation;
    try {
        return await operation;
    } finally {
        if (cancelWorkflowPromise === operation) cancelWorkflowPromise = null;
        generationCancelRequested = false;
        updateGenerationCancelUI();
    }
}

function resetGenerationAfterHardStop(session = currentSession, snapshot = currentWorkspace?.snapshot) {
    if (!session?.session_id || currentSession?.session_id !== session.session_id) return false;
    if (!isHardStoppedWorkflowSnapshot(snapshot)) return false;

    const alreadyAtVoiceConfig = activeWorkspace === 'voice'
        && currentStep === 2
        && !isGenerating
        && generationResult === null;
    if (!alreadyAtVoiceConfig) {
        // Invalidate every renderer-side generation callback and tear down the
        // browser-facing stream. The server terminal snapshot remains in
        // history, but no late runtime event may put this task back on the
        // generation page.
        generationAttemptId++;
        generationStartAttemptId = 0;
        generationStartInFlight = false;
        generateAbortController?.abort();
        generateAbortController = null;
        clearGenerationStartupTimer();
        clearSSEReconnectTimer();
        sseConnectionToken++;
        if (resultNavigationTimer) {
            clearTimeout(resultNavigationTimer);
            resultNavigationTimer = null;
        }
        if (workspaceRefreshTimer) {
            clearTimeout(workspaceRefreshTimer);
            workspaceRefreshTimer = null;
        }
        if (workflowStream) {
            workflowStream.close().catch(() => {});
            workflowStream = null;
        }
        destroyWaveSurfers();

        generatedFiles = [];
        activeResultContext = null;
        latestCurrentResultEvent = null;
        lastStats = null;
        lastDownloadEvent = null;
        sseRetryCount = 0;
        generationResult = null;
        transientGenerationErrorMessage = '';
        generationCancelRequested = false;
        isGenerating = false;
        resetLogTimeline('当前生成已停止；请确认声音配置后重新生成。');
        hideGenerationRecovery();

        mergeWorkflowSnapshotIntoSession(snapshot, session);
        if (currentWorkspace?.snapshot?.workflow_id === session.session_id) {
            currentWorkspace = { ...currentWorkspace, snapshot: { ...snapshot } };
        }
    }

    // A cancelled workflow is immutable. Keep its document and voice choices
    // in memory so the next click on “开始生成” can create a fresh rerun,
    // while making the voice step the only reachable generation entry point.
    goToStep(2);
    setActiveWorkspaceView('voice');
    renderVoiceWorkspace();
    updateGenerationCancelUI();
    updateConfigActionState(currentWorkspace);
    $('status-text').textContent = '当前生成已停止，请确认声音配置后重新生成。';
    if (!alreadyAtVoiceConfig) showToast('已停止生成，请确认声音配置后重新生成。', 'info');
    return true;
}

function applyCancellationOutcome(session, snapshot) {
    if (!snapshot || currentSession?.session_id !== session?.session_id) return;
    renderLiveWorkflowSnapshot(snapshot, session);
    if (hardStopNavigationRequested && isHardStoppedWorkflowSnapshot(snapshot)) {
        resetGenerationAfterHardStop(session, snapshot);
        return;
    }
    if (isTerminalWorkflowSnapshot(snapshot)) {
        // A cancellation response is only a snapshot hint. Re-read the full
        // workspace so item partitions and verified artifacts cannot lag the
        // terminal control/result facts shown on the result page.
        void refreshGeneratedArtifacts(session.session_id).then(() => {
            if (currentSession?.session_id !== session.session_id) return;
            const authoritative = currentWorkspace?.snapshot;
            if (!isTerminalWorkflowSnapshot(authoritative)) {
                $('status-text').textContent = '本地取消状态待刷新…';
                return;
            }
            const counts = workspaceProgress(currentWorkspace);
            if (authoritative.result_status === 'CANCELLED') {
                handleSSEEvent({ type: 'cancelled', ...counts });
            } else if (['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(String(authoritative.result_status || ''))) {
                handleDone({
                    type: 'done',
                    ...counts,
                    file_list: generatedFiles,
                });
            }
        }).catch(() => {
            $('status-text').textContent = '任务已停止，结果刷新失败，请重新打开任务查看。';
        });
        return;
    }
    // The cancel route is expected to return a terminal local snapshot. Keep
    // a conservative fallback for a concurrent state error without implying
    // that another provider request is required.
    isGenerating = true;
    updateGenerationCancelUI();
    $('status-text').textContent = '停止请求未返回终态，请刷新任务。';
    $('generation-live-status').textContent = '正在更新本地任务状态…';
}

async function createEditableWorkflowFromTerminal(session, snapshot) {
    if (!workflowApi || typeof workflowApi.rerun !== 'function') {
        throw new Error('当前运行时不支持创建新的配置任务');
    }
    const expectedGroupStateVersion = Number(snapshot?.group_state_version);
    if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
        throw new Error('任务组版本缺失，无法返回可编辑配置');
    }
    const rerun = await workflowApi.rerun(session.session_id, {
        expected_group_state_version: expectedGroupStateVersion,
        source_workflow_id: session.session_id,
        reason: 'desktop-return-to-configuration',
    }, {
        idempotencyKey: `renderer-return-config-rerun-${session.session_id}-${expectedGroupStateVersion}`,
    });
    const nextWorkflowId = String(rerun?.workflow_id || '');
    if (!nextWorkflowId) throw new Error('服务端未返回新的配置任务');
    const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
    if (!nextWorkspace?.snapshot?.workflow_id) throw new Error('新的配置任务工作区不可用');
    await adoptWorkflowWorkspace(nextWorkspace, {
        record: {
            workflow_id: nextWorkflowId,
            source_filename: nextWorkspace.source_filename || session.source_filename,
        },
        reason: '已创建新的配置任务，请确认后重新生成',
    });
    return nextWorkspace;
}

async function returnToConfigSafely({ buttonId = 'return-config-btn' } = {}) {
    const button = $(buttonId);
    if (button?.dataset.busy === 'true') return false;
    if (button) {
        button.dataset.busy = 'true';
        button.disabled = true;
    }
    let session = currentSession;
    try {
        if (!session || !workflowApi) {
            goToStep(2);
            return true;
        }
        let snapshot = await refreshCurrentWorkflowSnapshot(session);
        const mustStop = generationStartInFlight
            || isGenerating
            || isAcceptedGenerationSnapshot(snapshot);
        if (mustStop && !isTerminalWorkflowSnapshot(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('本地停止尚未完成，请刷新任务后重试返回配置', 'warning');
                return false;
            }
        }
        snapshot = await holdAutomaticRetry(session) || snapshot;
        // hold 与调度器之间仍可能发生一次竞态：如果调度器已经把任务
        // 推进到 RUNNING，不能继续打开配置页，而要立即走同一取消路径。
        if (isAcceptedGenerationSnapshot(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration-race',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('返回配置时任务状态发生变化，请刷新后重试', 'warning');
                return false;
            }
        }
        // A terminal run is immutable because it already owns attempts and
        // artifacts. Returning to configuration means starting a fresh run in
        // the same workflow group, not editing history in place.
        if (isTerminalWorkflowSnapshot(snapshot)) {
            const nextWorkspace = await createEditableWorkflowFromTerminal(session, snapshot);
            session = currentSession;
            snapshot = nextWorkspace.snapshot;
            showToast('已创建新的配置任务，请确认后重新生成');
        }
        if (currentSession?.session_id !== session.session_id) return false;
        hideGenerationRecovery();
        goToStep(2);
        // Returning from a terminal generation state must always land on the
        // editable voice workspace. The explicit re-render also covers a
        // workspace refresh that resolved between the cancellation response
        // and the navigation above.
        setActiveWorkspaceView('voice');
        renderVoiceWorkspace();
        return true;
    } catch (error) {
        console.error('返回配置前停止任务失败:', error);
        showToast(`无法停止当前任务：${error.message || '请稍后重试'}`, 'error');
        return false;
    } finally {
        if (button) {
            button.dataset.busy = 'false';
            button.disabled = isRestarting;
        }
        updateGenerationCancelUI();
    }
}

/**
 * Return control of a safe, pre-submission retry to the configuration editor.
 * The scheduler remains enabled for unattended recovery, but it must not race
 * a user who is changing the voice after closing the browser deliberately.
 */
async function holdAutomaticRetry(session = currentSession) {
    if (!workflowApi || !session?.session_id) return null;
    let snapshot = await refreshCurrentWorkflowSnapshot(session);
    for (let attempt = 0; attempt < 2; attempt++) {
        if (!snapshot || !['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state))) {
            return snapshot;
        }
        // Use the version from the same authoritative GET that produced the
        // state predicate. session.state_version may already have been
        // overwritten by an SSE update from the scheduler.
        const expectedStateVersion = Number(snapshot.state_version);
        if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) return snapshot;
        try {
            const response = await workflowApi.holdRetry(session.session_id, {
                expected_state_version: expectedStateVersion,
                reason: 'desktop-return-to-configuration',
            });
            const current = response?.current_snapshot;
            if (current) mergeWorkflowSnapshotIntoSession(current, session);
            return current || snapshot;
        } catch (error) {
            if (error?.code === 'STATE_CONFLICT' && attempt === 0) {
                try {
                    snapshot = await refreshCurrentWorkflowSnapshot(session);
                    continue;
                } catch (_) {
                    // Keep the last authoritative snapshot for the caller's
                    // best-effort navigation decision.
                }
            }
            console.warn('暂停后台自动重试失败:', error);
            return snapshot;
        }
    }
    return snapshot;
}

function verifiedItemIdsFromArtifacts(artifacts) {
    return new Set((Array.isArray(artifacts) ? artifacts : [])
        .filter(artifact => (
            artifact?.item_id
            && artifact.lifecycle_state === 'READY'
            && artifact.verified === true
            && artifact.artifact_type === 'tts-segment'
        ))
        .map(artifact => String(artifact.item_id)));
}

/**
 * Retry only durable local failures from the still-open run. TTS legacy
 * AMBIGUOUS items are normalized to the same local retry path on use.
 */
async function retryFailedItems() {
    const button = $('retry-failed-btn');
    if (!workflowApi || !currentSession || !lastGenerationConfig || isGenerating || isRestarting) return;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    const session = currentSession;
    try {
        let snapshot = await holdAutomaticRetry(session);
        if (!snapshot) throw new Error('当前任务状态不可用');
        if (snapshot.execution_state === 'TERMINAL') {
            showToast('当前任务已封存，请返回配置后重新开始生成');
            return;
        }
        if (!['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state))) {
            showToast('旧任务仍在处理，请等待它停止后再重试');
            return;
        }

        const [items, artifacts] = await Promise.all([
            workflowApi.listItems(session.session_id),
            workflowApi.listArtifacts(session.session_id),
        ]);
        const verifiedItemIds = verifiedItemIdsFromArtifacts(artifacts);
        const failedItems = (Array.isArray(items) ? items : [])
            .filter(item => (
                item?.status === 'FAILED'
                && item.item_id
                && !verifiedItemIds.has(String(item.item_id))
            ))
            .sort((left, right) => (
                Number(left.sequence || 0) - Number(right.sequence || 0)
                || String(left.item_id).localeCompare(String(right.item_id))
            ));
        const ambiguousCount = (Array.isArray(items) ? items : [])
            .filter(item => item?.status === 'AMBIGUOUS').length;
        if (failedItems.length === 0) {
            showToast(ambiguousCount > 0
                ? '有未完成条目，请重新生成'
                : '当前没有可安全重试的失败项');
            return;
        }
        const stepId = String(snapshot.current_step_id || '');
        if (!stepId) throw new Error('当前任务缺少可重试的生成步骤');

        const retryableIds = [];
        for (const item of failedItems) {
            const response = await workflowApi.retry(session.session_id, {
                expected_state_version: Number(snapshot.state_version),
                expected_target_state_version: Number(item.state_version),
                target: {
                    target_type: 'ITEM',
                    step_id: stepId,
                    item_id: String(item.item_id),
                },
                reason: 'desktop-retry-failed-items',
            });
            const nextSnapshot = response?.current_snapshot;
            if (nextSnapshot) {
                mergeWorkflowSnapshotIntoSession(nextSnapshot, session);
                snapshot = nextSnapshot;
            }
            retryableIds.push(String(item.item_id));
        }
        if (retryableIds.length === 0) return;

        destroyWaveSurfers();
        goToStep(3);
        showToast(`正在仅重试 ${retryableIds.length} 个失败项`);
        await startProcessing(false, { ...lastGenerationConfig }, retryableIds);
    } catch (error) {
        console.error('失败项重试失败:', error);
        showToast(`失败项重试失败：${error.message || '请检查任务记录'}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

async function retryGenerationFromRecovery() {
    if (isGenerating || generationStartInFlight || isRestarting || !currentSession) return;
    const recoveryButton = $('retry-generation-btn');
    if (recoveryButton?.dataset.busy === 'true') return;
    if (recoveryButton) {
        recoveryButton.dataset.busy = 'true';
        recoveryButton.disabled = true;
        recoveryButton.setAttribute('aria-busy', 'true');
    }
    generationRecoveryRetryInFlight = true;
    hideGenerationRecovery();
    const workspace = authoritativeWorkspace() || currentWorkspace;
    const state = workspaceUserState(workspace, workspace?.snapshot || currentSession);
    const retryAction = workspaceAction('RETRY', workspace);

    try {
        // WAITING_RETRY/WAITING_USER expose item-scoped safe retry actions. Use
        // the existing failed-item flow so every target carries its own state
        // version and a mixed result never causes a blind whole-workflow submit.
        if (!state.terminal && retryAction?.enabled === true) {
            await retryFailedItems();
            return;
        }

        // A renderer-side startup error can happen before the server accepts
        // the generation command, so there is no durable RETRY action yet.
        // Reuse the saved voice configuration and perform the normal guarded
        // start.
        if (String(generationResult || '').toLowerCase() === 'error'
            && ['CREATED', 'PREPARING', 'RUNNING', 'RECOVERING'].includes(state.key)) {
            startProcessing(false, lastGenerationConfig || undefined);
            return;
        }

        if (state.terminal && !isHardStoppedWorkflowSnapshot(workspace?.snapshot || currentSession)) {
            await returnToConfigSafely();
            return;
        }

        showToast('当前任务暂时没有可安全重试的内容，请查看任务记录或返回声音配置。', 'warning');
    } finally {
        generationRecoveryRetryInFlight = false;
        if (recoveryButton) {
            recoveryButton.dataset.busy = 'false';
            recoveryButton.removeAttribute('aria-busy');
            recoveryButton.disabled = Boolean(isGenerating || generationStartInFlight);
        }
        // retryFailedItems catches its own API errors. If it never reached a
        // new generation, restore the actionable recovery state after the
        // immediate hide instead of leaving the user without a next step.
        if (!isGenerating && !generationStartInFlight
            && currentView === 'workflow' && activeWorkspace === 'generation') {
            syncGenerationRecoveryState(
                currentWorkspace,
                workspaceUserState(currentWorkspace, currentSession),
            );
        }
    }
}

async function submitGenerationCommand(
    session,
    config,
    controller,
    attemptId,
    itemIds = null,
    configurationRevision = null,
) {
    let expectedConfigurationRevision = Number(configurationRevision);
    // A STATE_CONFLICT retry is still one logical generate command. Keep its
    // idempotency key stable so a response that arrived after the conflict
    // cannot result in a second server-side command.
    const commandIdempotencyKey = `renderer-generate-${session.session_id}-${attemptId}`;
    const refreshConfigurationRevision = async () => {
        const workspace = await workflowApi.getWorkspace(session.session_id);
        const revision = Number(workspace?.configuration?.configuration_revision);
        if (!Number.isInteger(revision) || revision < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        expectedConfigurationRevision = revision;
    };
    if (!Number.isInteger(expectedConfigurationRevision) || expectedConfigurationRevision < 1) {
        await refreshConfigurationRevision();
    }
    const submit = () => workflowApi.generateWorkflow(session.session_id, {
        expected_state_version: session.state_version,
        configuration_revision: expectedConfigurationRevision,
        reason: 'desktop-renderer',
        ...(Array.isArray(itemIds) && itemIds.length > 0 ? { item_ids: itemIds } : {}),
    }, { idempotencyKey: commandIdempotencyKey });

    try {
        return await submit();
    } catch (error) {
        // GET 与 POST 之间仍可能有后台事件/其它窗口推进版本；只对明确的
        // 乐观锁冲突重新读取一次并重试，绝不对未知网络错误盲目重发。
        if (error?.code !== 'STATE_CONFLICT') throw error;
        await refreshCurrentWorkflowSnapshot(session);
        await refreshConfigurationRevision();
        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) {
            const aborted = new Error('generation attempt was superseded');
            aborted.name = 'AbortError';
            throw aborted;
        }
        return submit();
    }
}

function setRestartingUI(restarting) {
    isRestarting = restarting;
    [
        'restart-btn',
        'change-file-btn',
        'back-to-upload-btn',
        'new-file-btn',
        'retry-failed-btn',
        'result-return-config-btn',
        'generate-full-btn',
        'download-zip-btn',
        'retry-service-btn',
        'retry-generation-btn',
        'return-config-btn',
        'pause-generation-btn',
        'resume-generation-btn',
        'cancel-generation-btn',
        'cancel-import-btn',
        'history-nav-btn',
        'history-start-btn',
        'history-back-btn',
        'back-to-history-btn',
        'version-nav-btn',
        'version-check-btn',
        'version-download-btn',
        'version-install-btn',
        'version-open-release-btn',
    ].forEach(id => {
        const button = $(id);
        if (button) button.disabled = restarting;
    });
    syncRestartButtonState();
    if (restarting) {
        setAppInteractive(false);
        $('status-text').textContent = '正在结束当前任务...';
    }
}


registerRendererModule("workflow.snapshotActions", {
    setAppInteractive,
    workflowSnapshotIsOlder,
    workflowSnapshotBelongsToSession,
    workflowSnapshotIsStaleForSession,
    latestWorkflowSnapshotForSession,
    mergeWorkflowSnapshotIntoSession,
    renderLiveWorkflowSnapshot,
    refreshCurrentWorkflowSnapshot,
    ACCEPTED_GENERATION_EXECUTION_STATES,
    TERMINAL_WORKFLOW_RESULT_STATES,
    isTerminalWorkflowSnapshot,
    isHardStoppedWorkflowSnapshot,
    isAcceptedGenerationSnapshot,
    isCancellationSettledSnapshot,
    resultZipState,
    setGenerationControlLabel,
    updateGenerationCancelUI,
    updateConfigActionState,
    updateGenerationControlUI,
    adoptAcceptedGeneration,
    resumedGenerationSnapshot,
    shouldAdoptResumedGeneration,
    adoptResumedGenerationIfNeeded,
    workflowProgressCounts,
    cancelCurrentWorkflow,
    resetGenerationAfterHardStop,
    applyCancellationOutcome,
    createEditableWorkflowFromTerminal,
    returnToConfigSafely,
    holdAutomaticRetry,
    verifiedItemIdsFromArtifacts,
    retryFailedItems,
    retryGenerationFromRecovery,
    submitGenerationCommand,
    setRestartingUI,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
