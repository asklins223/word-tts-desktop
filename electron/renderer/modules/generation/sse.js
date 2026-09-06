/** Renderer module: generation.sse */
(function attachRendererFeature_generation_sse(root) {
    'use strict';

// ============================================================================
// SSE 进度流
// ============================================================================

async function connectSSE(sessionId) {
    clearSSEReconnectTimer();
    const connectionToken = ++sseConnectionToken;
    if (workflowStream) {
        await workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    if (!workflowApi) return;
    const preparedStore = workflowStore?.prepare?.(sessionId, {
        workflow: currentSession?.session_id === sessionId
            ? { workflow_id: sessionId, ...currentSession }
            : null,
        lastEventId: currentSession?.session_id === sessionId
            ? (currentSession.last_event_id || currentSession.latest_event_id || null)
            : null,
        lastSeq: currentSession?.session_id === sessionId
            ? Number(currentSession.latest_seq || 0)
            : 0,
    });
    const recoveryNoticePending = sseRetryCount > 0;
    try {
        const persistedCursor = preparedStore?.lastEventId || workflowStore?.lastEventIdFor?.(sessionId) || null;
        const stream = await workflowApi.openWorkflowEvents(
            sessionId,
            persistedCursor || currentSession?.last_event_id || currentSession?.latest_event_id || null,
        );
        if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) {
            await stream.close().catch(() => {});
            return;
        }
        workflowStream = stream;
        stream.onFrame((frame) => {
            if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) return;
            const reduced = workflowStore ? workflowStore.consume(frame) : { accepted: true };
            if (!reduced.accepted) {
                if (reduced.reason === 'gap') {
                    resetWorkflowEventCursor(sessionId);
                    handleWorkflowStreamError(
                        Object.assign(
                            new Error('workflow event gap: expected ' + reduced.expectedSeq + ', got ' + reduced.actualSeq),
                            { code: 'EVENT_GAP' },
                        ),
                        sessionId,
                        connectionToken,
                    );
                }
                return;
            }
            const event = frame?.event;
            if (frame?.kind === 'snapshot') {
                const snapshot = frame.snapshot?.state || {};
                const snapshotForRender = {
                    ...snapshot,
                    latest_event_id: frame.snapshot?.snapshot_event_id || snapshot.latest_event_id,
                    latest_seq: frame.snapshot?.snapshot_seq ?? snapshot.latest_seq,
                };
                mergeWorkflowSnapshotIntoSession(snapshotForRender, currentSession);
                currentSession.last_event_id = frame.snapshot?.snapshot_event_id
                    || currentSession.last_event_id
                    || currentSession.latest_event_id
                    || null;
                // Snapshot frames are authoritative state, not just a cursor
                // update. Paint them immediately so pause/resume/stop/error
                // states are visible before the debounced workspace refresh.
                renderLiveWorkflowSnapshot(snapshotForRender, currentSession);
                if (snapshot.execution_state === 'TERMINAL') {
                    $('status-text').textContent = '任务已结束';
                }
                scheduleWorkspaceRefresh(sessionId);
                return;
            }
            if (!event) return;
            currentSession.last_event_id = event.event_id || currentSession.last_event_id || null;
            currentSession.latest_event_id = event.event_id || currentSession.latest_event_id || null;
            currentSession.latest_seq = Math.max(Number(currentSession.latest_seq || 0), Number(event.seq || 0));
            handleWorkflowEvent(event, sessionId);
            if (recoveryNoticePending) {
                addLogEntry({
                    level: 'success', stage: 'system', kind: 'notice', status: 'success',
                    key: 'connection:status', title: '生成服务连接已恢复', detail: '任务记录与进度已重新同步',
                });
            }
        });
        stream.onError((error) => handleWorkflowStreamError(error, sessionId, connectionToken));
        sseRetryCount = 0;
    } catch (error) {
        handleWorkflowStreamError(error, sessionId, connectionToken);
    }
}

const WORKFLOW_CONTROL_EVENT_TYPES = new Set([
    'WORKFLOW_PAUSE',
    'WORKFLOW_PAUSED',
    'WORKFLOW_RESUME',
    'WORKFLOW_CANCEL',
    'WORKFLOW_CANCELLED',
]);

function runtimeProgressNeedsIndeterminate(workspace = currentWorkspace) {
    // consume() notifies subscribers synchronously, but the richer
    // currentWorkspace hydration may still be one refresh behind. Prefer the
    // just-accepted Store projection so the legacy event handler cannot undo
    // a determinate composite readout rendered by the Store subscriber.
    const storeState = workflowStore?.getState?.();
    const workflowId = String(
        workspace?.snapshot?.workflow_id
        || workspace?.workflow_id
        || currentSession?.session_id
        || '',
    );
    const storeWorkflowId = String(
        storeState?.workflowId
        || storeState?.workflowProjection?.workflow_id
        || '',
    );
    const storeMatches = Boolean(storeWorkflowId && (!workflowId || storeWorkflowId === workflowId));
    const sourceSnapshot = storeMatches
        ? {
            ...(workspace?.snapshot || {}),
            ...(storeState?.workflowProjection || {}),
        }
        : (workspace?.snapshot || null);
    const runtime = typeof compositeRuntimeProjection === 'function'
        ? compositeRuntimeProjection(
            storeMatches ? (storeState?.workspace || {}) : (workspace || {}),
            sourceSnapshot,
        )
        : (sourceSnapshot?.runtime || workspace?.runtime || null);
    if (typeof compositeRuntimeReadout === 'function' && compositeRuntimeReadout(runtime)) return false;
    if (typeof singleSegmentRuntimeActive === 'function' && singleSegmentRuntimeActive(runtime)) return false;
    return true;
}

function applyWorkflowControlEvent(event, sessionId) {
    const eventType = String(event?.event_type || '');
    if (!WORKFLOW_CONTROL_EVENT_TYPES.has(eventType) || currentSession?.session_id !== sessionId) return false;
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    const patch = {
        WORKFLOW_PAUSE: { control_state: 'PAUSE_REQUESTED' },
        WORKFLOW_PAUSED: { control_state: 'PAUSED' },
        WORKFLOW_RESUME: { control_state: 'RUNNING' },
        WORKFLOW_CANCEL: { control_state: 'TERMINATING', execution_state: 'BLOCKED' },
        WORKFLOW_CANCELLED: {
            control_state: 'TERMINATED',
            execution_state: 'TERMINAL',
            result_status: payload.result_status || 'CANCELLED',
            last_error_code: 'WORKFLOW_CANCELLED',
            last_error_message: String(payload.reason || payload.message || '任务已取消').slice(0, 2000),
        },
    }[eventType];
    if (!patch) return false;
    Object.assign(currentSession, patch);
    const workflowId = String(currentWorkspace?.snapshot?.workflow_id || currentSession.session_id || '');
    if (currentWorkspace && workflowId === String(sessionId)) {
        currentWorkspace = {
            ...currentWorkspace,
            snapshot: {
                ...(currentWorkspace.snapshot || {}),
                ...patch,
                latest_event_id: event.event_id || currentWorkspace.snapshot?.latest_event_id,
                latest_seq: Number(event.seq) || currentWorkspace.snapshot?.latest_seq,
            },
        };
    }
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(
            currentWorkspace,
            currentWorkspace?.snapshot || currentSession,
            sessionId,
        );
    } else {
        renderWorkspaceShell(currentWorkspace, currentWorkspace?.snapshot || currentSession);
    }
    return true;
}

function handleWorkflowEvent(event, sessionId) {
    // A retryable failure closes the stream and settles the generation UI.
    // Ignore already-buffered runtime updates that arrive after that error;
    // otherwise a late "still processing" event can overwrite the actionable
    // retry state and make the page look permanently stuck.
    if (!isGenerating || generationResult !== null) return;
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    // The stream is a freshness signal; the server workspace remains the
    // authoritative source for item partitions, blockers and actions.
    scheduleWorkspaceRefresh(sessionId);
    const eventType = String(event.event_type || '');
    const isControlEvent = applyWorkflowControlEvent(event, sessionId);
    if (generationWorkflowOwnsRuntimeView() && !isControlEvent) {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        return;
    }
    const attemptKey = String(payload.attempt_id || event.attempt_id || payload.submission_id || event.seq || 'current');
    const eventMessage = String(payload.message || payload.error || payload.error_code || '生成任务未能完成');
    if (eventType === 'WORKFLOW_PAUSE') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'warning', seq: event.seq,
            key: `workflow:pause:${attemptKey}`, title: '正在暂停任务', detail: '已收到暂停请求，等待当前处理点结束。',
        });
    } else if (eventType === 'WORKFLOW_PAUSED') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'warning', seq: event.seq,
            key: `workflow:paused:${attemptKey}`, title: '任务已暂停', detail: '任务停在安全点，不会继续提交新的内容。',
        });
    } else if (eventType === 'WORKFLOW_RESUME') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq,
            key: `workflow:resume:${attemptKey}`, title: '正在恢复任务', detail: '恢复请求已提交，正在同步任务进度。',
        });
    } else if (eventType === 'TTS_PLAN_PREPARED') {
        $('status-text').textContent = '已准备生成计划，正在连接讯飞浏览器…';
        $('generation-live-status').textContent = '已准备生成计划，正在连接讯飞浏览器…';
        addLogEntry({ level: 'info', stage: 'prepare', kind: 'stage', status: 'running', seq: event.seq, key: `tts:plan:${attemptKey}`, title: '已准备生成计划', detail: `共 ${payload.item_count || 0} 条内容` });
    } else if (event.event_type === 'TTS_RUNTIME_STATUS') {
        const elapsed = Number(payload.elapsed_seconds);
        const elapsedText = Number.isFinite(elapsed) && elapsed >= 1 ? `（已等待 ${Math.round(elapsed)} 秒）` : '';
        const waiting = payload.status === 'waiting';
        const message = String(payload.message || (waiting ? '讯飞浏览器正在处理，任务仍在运行' : '正在启动讯飞浏览器会话'));
        $('status-text').textContent = message;
        $('generation-live-status').textContent = message;
        // The store has already rendered the accepted event. A heartbeat is
        // deliberately missing stage fields, so do not let this legacy
        // status handler turn a valid composite percentage back into an
        // indeterminate sweep after the store just made it determinate.
        setProgressIndeterminate(runtimeProgressNeedsIndeterminate());
        addLogEntry({
            level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq,
            key: `tts:runtime:${attemptKey}`,
            title: waiting ? '讯飞浏览器仍在处理' : '正在启动讯飞浏览器',
            detail: `${message}${elapsedText}；如果浏览器窗口被关闭，任务会进入可恢复状态。`,
        });
    } else if (event.event_type === 'TTS_RUNTIME_PROGRESS') {
        const completed = Number(payload.completed_segments);
        const total = Number(payload.total_segments);
        const progress = Number.isFinite(completed) && Number.isFinite(total)
            ? { completed, total }
            : null;
        const itemLabel = payload.item_id ? `条目 ${payload.item_id}` : '当前作品';
        const message = String(payload.message || `讯飞浏览器正在处理${itemLabel}`);
        $('status-text').textContent = message;
        $('generation-live-status').textContent = message;
        addLogEntry({
            level: payload.error ? 'warn' : 'progress', stage: 'synthesize', kind: 'stage',
            status: payload.error ? 'warning' : 'running', seq: event.seq,
            key: `tts:runtime:${attemptKey}`,
            title: payload.error ? '讯飞条目处理需要检查' : '讯飞条目处理进度',
            detail: message,
            progress,
        });
    } else if (event.event_type === 'TTS_SUBMISSION_IN_FLIGHT') {
        setProgressIndeterminate(runtimeProgressNeedsIndeterminate());
        $('status-text').textContent = '正在连接讯飞浏览器并提交作品…';
        $('generation-live-status').textContent = '正在连接讯飞浏览器并提交作品…';
        addLogEntry({ level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq, key: `tts:in-flight:${attemptKey}`, title: '正在提交讯飞作品', detail: '已进入外部提交阶段；浏览器启动或恢复期间请保持应用开启。' });
    } else if (event.event_type === 'PROVIDER_RECEIPT_OBSERVED') {
        setProgressIndeterminate(runtimeProgressNeedsIndeterminate());
        $('status-text').textContent = '作品已提交，正在下载并核验音频…';
        $('generation-live-status').textContent = '作品已提交，正在下载并核验音频…';
        addLogEntry({ level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq, key: `tts:receipt:${attemptKey}`, title: '已找到讯飞作品', detail: payload.receipt_id ? `正在下载作品并核验音频（Receipt ${payload.receipt_id}）` : '正在下载作品并核验音频' });
    } else if (event.event_type === 'TTS_SUBMISSION_AMBIGUOUS') {
        addLogEntry({ level: 'error', stage: 'synthesize', kind: 'summary', status: 'error', seq: event.seq, key: `tts:ambiguous:${attemptKey}`, title: '提交未完成，可重新生成', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
    } else if (event.event_type === 'TTS_SUBMISSION_REJECTED') {
        addLogEntry({ level: 'error', stage: 'synthesize', kind: 'summary', status: 'error', seq: event.seq, key: `tts:rejected:${attemptKey}`, title: '讯飞提交未被接受', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
    } else if (event.event_type === 'TTS_OUTPUT_VERIFIED') {
        // The event is evidence of the worker's write, not itself the final
        // UI state.  Read the authoritative snapshot and verified artifact
        // projection before moving to the result page.
        setProgressIndeterminate(true);
        $('status-text').textContent = '音频已写入，正在确认最终任务状态…';
        void finalizeSuccessfulWorkflowEvent({
            type: 'done',
            artifact_ids: payload.artifact_ids || [],
            event_seq: event.seq,
            event_key: `tts:verified:${attemptKey}`,
        }, sessionId).catch((error) => {
            console.warn('核验生成终态失败，保留任务页等待重连:', error);
            $('status-text').textContent = '正在确认最终任务状态，请稍候…';
        });
    } else if (event.event_type === 'GENERATION_TASK_FAILED') {
        void workflowApi?.getWorkflow(sessionId).then((snapshot) => {
            if (currentSession?.session_id !== sessionId) return;
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            const settled = isTerminalWorkflowSnapshot(snapshot)
                || ['WAITING_RETRY', 'WAITING_USER', 'BLOCKED'].includes(snapshot?.execution_state);
            if (!settled) {
                $('status-text').textContent = '生成服务正在恢复任务状态…';
                return;
            }
            // getWorkflow carries the durable error fields but is not a full
            // workspace refresh. Publish it to the live workspace immediately
            // so a scheduled refresh cannot briefly paint the old RUNNING UI.
            renderLiveWorkflowSnapshot(snapshot, currentSession);
            addLogEntry({ level: 'error', stage: 'complete', kind: 'summary', status: 'error', seq: event.seq, key: `task:failed:${attemptKey}`, title: '生成任务未能完成', detail: eventMessage });
            handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
        }).catch((error) => {
            console.warn('生成失败后同步工作流状态失败，保留任务页:', error);
            $('status-text').textContent = '生成失败，正在等待任务状态同步…';
        });
    } else if (event.event_type === 'WORKFLOW_CANCEL') {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        $('status-text').textContent = '正在停止生成任务…';
    } else if (event.event_type === 'WORKFLOW_CANCELLED') {
        void finalizeCancelledWorkflowEvent({ event_seq: event.seq }, sessionId).catch((error) => {
            console.warn('取消后同步工作流状态失败:', error);
            $('status-text').textContent = '取消结果刷新失败，请重新打开任务查看。';
        });
    }
}

async function refreshGeneratedArtifacts(sessionId) {
    if (!workflowApi) return [];
    let workspace = null;
    let items;
    let artifacts;
    // The workspace endpoint is the only response that carries item state,
    // verified Artifact facts, and delivery scope from one server snapshot.
    // Reading three endpoints in parallel can combine different revisions and
    // briefly render an item with another attempt's filename or status.
    if (typeof workflowApi.getWorkspace === 'function') {
        workspace = await workflowApi.getWorkspace(sessionId);
    }
    if (workspace && Array.isArray(workspace.items) && Array.isArray(workspace.artifacts)) {
        items = workspace.items;
        artifacts = workspace.artifacts;
    } else {
        // Keep older renderer/API combinations usable, but never prefer this
        // split projection when the authoritative workspace is available.
        [items, artifacts] = await Promise.all([
            workflowApi.listItems(sessionId),
            workflowApi.listArtifacts(sessionId),
        ]);
    }
    // The request can outlive a restart/new task.  Never let a late response
    // from the old workflow overwrite the result list of the current task.
    if (currentSession?.session_id !== sessionId) return [];
    generatedFiles = resultFilesFromArtifacts(items, artifacts, workspace);
    if (workspace) {
        currentWorkspace = workspace;
        workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
        currentSession.delivery = workspace.delivery ? {
            zip_available: Boolean(workspace.delivery.zip_available),
            zip_artifact_id: workspace.delivery.zip_artifact_id || null,
        } : null;
        if (Array.isArray(workspace.items)) {
            currentSession.parse_results = workspaceItemsToParseResults(workspace);
        }
        currentSession.progress = workspace.progress ? {
            total: nonNegativeCount(workspace.progress.total),
            completed: nonNegativeCount(workspace.progress.completed),
            failed: nonNegativeCount(workspace.progress.failed),
            cancelled: nonNegativeCount(workspace.progress.cancelled),
            skipped: nonNegativeCount(workspace.progress.skipped),
            pending: nonNegativeCount(workspace.progress.pending),
            deliverable: nonNegativeCount(workspace.progress.deliverable, generatedFiles.length),
            deliverable_percent: nonNegativeCount(workspace.progress.deliverable_percent),
        } : null;
        mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
        if (typeof renderWorkspaceAfterHydrate === 'function') {
            renderWorkspaceAfterHydrate(currentWorkspace, currentSession, sessionId);
        } else {
            renderWorkspaceShell(currentWorkspace, currentSession);
        }
    }
    return generatedFiles;
}

let successfulWorkflowFinalizationInFlight = false;

async function finalizeSuccessfulWorkflowEvent(event, sessionId) {
    if (successfulWorkflowFinalizationInFlight) return false;
    successfulWorkflowFinalizationInFlight = true;
    try {
        return await finalizeSuccessfulWorkflowEventInternal(event, sessionId);
    } finally {
        successfulWorkflowFinalizationInFlight = false;
    }
}

async function finalizeSuccessfulWorkflowEventInternal(event, sessionId) {
    if (!workflowApi || !sessionId || currentSession?.session_id !== sessionId) return false;
    await refreshGeneratedArtifacts(sessionId);
    if (currentSession?.session_id !== sessionId) return false;
    const workspace = currentWorkspace?.snapshot?.workflow_id === sessionId ? currentWorkspace : null;
    const snapshot = workspace?.snapshot || null;
    if (!isTerminalWorkflowSnapshot(snapshot)
        || !['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(String(snapshot.result_status || ''))) {
        $('status-text').textContent = '音频已写入，任务状态仍在确认中…';
        return false;
    }
    const progress = workspaceProgress(workspace);
    const deliveryBlockers = (Array.isArray(workspace?.blockers) ? workspace.blockers : []).filter(blocker => (
        ['BLOCKING', 'ERROR'].includes(String(blocker?.severity || '').toUpperCase())
        && ['ARTIFACT_MISSING_OR_UNVERIFIED', 'ARTIFACT_FORMAT_UNSUPPORTED', 'ARTIFACT_METADATA_CONFLICT'].includes(String(blocker?.code || '').toUpperCase())
    ));
    if (
        progress.pending > 0
        || progress.deliverable > generatedFiles.length
        || deliveryBlockers.length > 0
    ) {
        const blocker = deliveryBlockers[0];
        $('status-text').textContent = blocker
            ? `任务已结束，但${blocker.title || '交付产物'}仍未通过核验。`
            : '音频已写入，仍有交付产物正在确认中…';
        renderWorkspaceShell(currentWorkspace, currentSession);
        return false;
    }
    addLogEntry({
        level: 'success',
        stage: 'complete',
        kind: 'summary',
        status: 'success',
        seq: event.event_seq,
        key: event.event_key || `workflow:verified:${sessionId}`,
        title: '音频已完成核验',
        detail: '生成文件已写入本地任务空间。',
    });
    handleDone({
        type: 'done',
        workflow_id: sessionId,
        completed: progress.completed,
        failed: progress.failed,
        cancelled: progress.cancelled,
        skipped: progress.skipped,
        total: progress.total,
        file_list: generatedFiles,
    });
    return true;
}

async function finalizeCancelledWorkflowEvent(event, sessionId) {
    if (!workflowApi || !sessionId || currentSession?.session_id !== sessionId) return false;
    await refreshGeneratedArtifacts(sessionId);
    if (currentSession?.session_id !== sessionId) return false;
    const workspace = currentWorkspace?.snapshot?.workflow_id === sessionId ? currentWorkspace : null;
    const snapshot = workspace?.snapshot || null;
    if (!isTerminalWorkflowSnapshot(snapshot)) {
        $('status-text').textContent = '取消结果刷新失败，请重新打开任务查看。';
        return false;
    }
    if (isHardStoppedWorkflowSnapshot(snapshot)) {
        resetGenerationAfterHardStop(currentSession, snapshot);
        return true;
    }
    const progress = workspaceProgress(workspace);
    if (snapshot.result_status === 'SUCCEEDED' || snapshot.result_status === 'PARTIAL_SUCCESS') {
        return finalizeSuccessfulWorkflowEvent({
            type: 'done',
            event_seq: event.event_seq,
            event_key: `workflow:cancelled:${sessionId}`,
        }, sessionId);
    }
    if (snapshot.result_status !== 'CANCELLED') {
        $('status-text').textContent = '任务已结束，正在同步最终结果…';
        return false;
    }
    handleSSEEvent({
        type: 'cancelled',
        completed: progress.completed,
        cancelled: progress.cancelled,
        total: progress.total,
    });
    return true;
}

function resetWorkflowEventCursor(sessionId) {
    workflowStore?.resetCursor?.(sessionId);
    if (currentSession?.session_id !== sessionId) return;
    // The next connection must request the server snapshot. Keeping the old
    // cursor in currentSession would make Store.prepare resurrect it as its
    // initial cursor even after localStorage has been cleared.
    currentSession.last_event_id = null;
    currentSession.latest_event_id = null;
    currentSession.latest_seq = 0;
}

function handleWorkflowStreamError(error, sessionId, connectionToken) {
    if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId || !isGenerating) return;
    const requiresSnapshotResync = error?.code === 'CURSOR_EXPIRED'
        || error?.code === 'EVENT_GAP'
        || Number(error?.status) === 410;
    if (requiresSnapshotResync) resetWorkflowEventCursor(sessionId);
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    sseRetryCount += 1;
    if (sseRetryCount >= SSE_MAX_RETRIES) {
        handleSSEEvent({ type: 'error', msg: '与生成服务的连接已中断；已写入的任务记录仍然保留' });
        return;
    }
    const delay = Math.min(1000 * (2 ** (sseRetryCount - 1)), 10000);
    sseReconnectTimer = setTimeout(async () => {
        sseReconnectTimer = null;
        if (connectionToken !== sseConnectionToken || !isGenerating) return;
        if (requiresSnapshotResync) await hydrateWorkflowWorkspace(sessionId, { silent: true });
        if (connectionToken === sseConnectionToken && isGenerating) void connectSSE(sessionId);
    }, delay);
}

function handleSSEEvent(event) {
    switch (event.type) {
        case 'log_init':
            addLogEntries(Array.isArray(event.entries) ? event.entries : []);
            break;

        case 'log':
            if (generationWorkflowOwnsRuntimeView()
                && ['running', 'progress', 'info'].includes(String(event.entry?.status || '').toLowerCase())) break;
            addLogEntry(event.entry);
            break;

        case 'stats':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            lastStats = event;
            updateProgress(event);
            updateStats(event);
            break;

        case 'status':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            $('status-text').textContent = event.text;
            if ($('generation-live-status') && event.text) {
                $('generation-live-status').textContent = event.text;
            }
            break;

        case 'download':
            lastDownloadEvent = event;
            updateFileList(event);
            break;

        case 'done':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            // A transport-level done frame is only a freshness signal. The
            // workspace must confirm terminal execution/control/result facts
            // and verified item artifacts before the UI enters delivery.
            void finalizeSuccessfulWorkflowEvent(event, currentSession?.session_id).catch(error => {
                console.warn('完成事件后的工作区核验失败:', error);
                $('status-text').textContent = '正在确认最终任务状态，请稍候…';
            });
            break;

        case 'cancelled':
            if (hardStopNavigationRequested
                && resetGenerationAfterHardStop(currentSession, currentWorkspace?.snapshot || currentSession)) {
                break;
            }
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'warn',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'warning',
                    key: 'task:summary',
                    title: '任务已取消',
                    detail: `已完成 ${event.completed || 0} / ${event.total || 0} 条，已取消 ${event.cancelled || 0} 条`,
                    duration_ms: event.duration_ms,
                });
            }
            generationResult = 'cancelled';
            transientGenerationErrorMessage = '';
            resetGenerateState();
            setProgressIndeterminate(false);
            $('gen-title').textContent = '任务已取消';
            $('generation-file-name').textContent = `已取消「${currentSession?.source_filename || '当前文档'}」的生成任务。`;
            $('status-text').textContent = '生成任务已取消';
            const cancelledTotal = Math.max(
                0,
                Math.round(Number(event.total) || summarizeParseResults(currentSession?.parse_results).total || 0),
            );
            const cancelledCompleted = Math.max(0, Math.round(Number(event.completed) || 0));
            const cancelledCount = Math.max(0, Math.round(Number(event.cancelled) || 0));
            const cancelledPercent = terminalProgressPercent(cancelledCompleted, cancelledTotal);
            setProgressReadoutMode(true, true);
            setProgressBarPercent(cancelledPercent);
            $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(cancelledPercent));
            $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${cancelledPercent}% 可交付`);
            if ($('progress-percent')) $('progress-percent').textContent = String(cancelledPercent);
            if ($('progress-completed')) $('progress-completed').textContent = String(cancelledCompleted);
            if ($('progress-remaining')) $('progress-remaining').textContent = String(Math.max(cancelledTotal - cancelledCompleted - cancelledCount, 0));
            if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelledCount);
            if ($('progress-stats')) $('progress-stats').textContent = `${cancelledCompleted} / ${cancelledTotal || cancelledCompleted} · 已取消 ${cancelledCount}`;
            setGenerationVisualState('stopped');
            showToast('任务已取消');
            break;

        case 'error': {
            const errorMessage = String(event?.msg || '生成服务返回了未说明的错误').trim()
                || '生成服务返回了未说明的错误';
            transientGenerationErrorMessage = `生成出错：${errorMessage}`;
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'error',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'error',
                    key: 'task:summary',
                    title: '生成任务未能完成',
                    detail: errorMessage,
                    duration_ms: event.duration_ms,
                });
            }
            showToast(`错误: ${errorMessage}`);
            generationResult = 'error';
            resetGenerateState();
            setProgressIndeterminate(false);
            if (workflowStream) {
                workflowStream.close().catch(() => {});
                workflowStream = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            $('gen-title').textContent = '生成出错';
            $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」生成遇到问题；可查看任务详情后重试。`;
            $('status-text').textContent = `错误: ${errorMessage}`;
            setGenerationVisualState('error');
            syncGenerationRecoveryState(
                currentWorkspace,
                workspaceUserState(currentWorkspace, currentSession),
            );
            syncTransientGenerationErrorShell(transientGenerationErrorMessage);
            break;
        }

        case 'end':
            resetGenerateState();
            setProgressIndeterminate(false);
            if (workflowStream) {
                workflowStream.close().catch(() => {});
                workflowStream = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            // 如果未收到 done 或 error 事件，说明生成异常终止
            if (generationResult === null) {
                generationResult = 'error';
                transientGenerationErrorMessage = '生成任务意外停止。你可以重试，或返回配置页检查设置。';
                addLogEntry({
                    level: 'warn',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'warning',
                    key: 'task:summary',
                    title: '生成任务意外停止',
                    detail: '未收到明确的完成、失败或取消状态，可重试任务并检查生成服务',
                });
                $('gen-title').textContent = '生成已停止';
                $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」生成意外停止；可重试或返回配置检查设置。`;
                $('status-text').textContent = '生成已停止，请检查日志或重新开始';
                setGenerationVisualState('stopped');
                syncGenerationRecoveryState(
                    currentWorkspace,
                    workspaceUserState(currentWorkspace, currentSession),
                );
                syncTransientGenerationErrorShell(transientGenerationErrorMessage);
            }
            break;

        case 'heartbeat':
            // 心跳证明连接已稳定跨过一个服务端等待周期。
            sseRetryCount = 0;
            break;
    }
}


registerRendererModule("generation.sse", {
    connectSSE,
    WORKFLOW_CONTROL_EVENT_TYPES,
    runtimeProgressNeedsIndeterminate,
    applyWorkflowControlEvent,
    handleWorkflowEvent,
    refreshGeneratedArtifacts,
    finalizeSuccessfulWorkflowEvent,
    finalizeCancelledWorkflowEvent,
    resetWorkflowEventCursor,
    handleWorkflowStreamError,
    handleSSEEvent,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
