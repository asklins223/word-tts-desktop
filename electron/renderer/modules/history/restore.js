/** Renderer module: history.restore */
(function attachRendererFeature_history_restore(root) {
    'use strict';

function readWithTimeout(promise, timeoutMs = 6000) {
    const duration = Math.max(1, Number(timeoutMs) || 6000);
    let timer = null;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
            const error = new Error(`workspace read timed out after ${duration}ms`);
            error.code = 'WORKSPACE_READ_TIMEOUT';
            reject(error);
        }, duration);
    });
    return Promise.race([Promise.resolve(promise), timeout]).finally(() => {
        if (timer) clearTimeout(timer);
    });
}

async function hydrateActiveWorkflowCandidates(candidates = []) {
    const source = (Array.isArray(candidates) ? candidates : [])
        .slice(0, ACTIVE_WORKFLOW_HYDRATE_LIMIT);
    const hydrated = [];
    // Startup should stay responsive even when a stale task points at a slow
    // local backend. A small bounded window also prevents hundreds of active
    // candidates from turning one refresh into a request storm. The total
    // budget is part of the recovery contract, not just a UI timeout.
    const deadline = Date.now() + ACTIVE_WORKFLOW_HYDRATE_BUDGET_MS;
    for (let index = 0; index < source.length; index += ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY) {
        const batch = source.slice(index, index + ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY);
        const results = await Promise.all(batch.map(async candidate => {
            const workflowId = String(candidate?.workflow?.workflow_id || '');
            if (!workflowId || typeof workflowApi?.getWorkspace !== 'function') return candidate;
            const remaining = deadline - Date.now();
            if (remaining <= 0) {
                return {
                    ...candidate,
                    workspace: null,
                    workspace_sync_error: {
                        code: 'WORKSPACE_HYDRATE_TIMEOUT',
                        message: '活动任务恢复超出时间预算',
                    },
                };
            }
            try {
                const workspace = await readWithTimeout(
                    workflowApi.getWorkspace(workflowId),
                    Math.min(ACTIVE_WORKFLOW_HYDRATE_TIMEOUT_MS, remaining),
                );
                return { ...candidate, workspace, workspace_sync_error: null };
            } catch (error) {
                console.warn('活动任务工作区读取失败:', workflowId, error);
                return {
                    ...candidate,
                    workspace: null,
                    workspace_sync_error: {
                        code: error?.code || 'WORKSPACE_SYNC_ERROR',
                        message: error?.message || '工作区暂时无法同步',
                    },
                };
            }
        }));
        hydrated.push(...results);
        if (Date.now() >= deadline && index + batch.length < source.length) {
            hydrated.push(...source.slice(index + batch.length).map(candidate => ({
                ...candidate,
                workspace: null,
                workspace_sync_error: {
                    code: 'WORKSPACE_HYDRATE_TIMEOUT',
                    message: '活动任务恢复超出时间预算',
                },
            })));
            break;
        }
    }
    return hydrated;
}

function sessionFromWorkspace(workspace, candidate = {}, record = {}) {
    const snapshot = workspace?.snapshot || candidate?.workflow || {};
    const workflowId = String(snapshot.workflow_id || candidate?.workflow?.workflow_id || record.workflow_id || record.id || '');
    if (!workflowId) return null;
    const parseResults = workspaceItemsToParseResults(workspace);
    return {
        session_id: workflowId,
        workflow_id: workflowId,
        source_filename: workspace?.source_filename || record.source_filename || '未命名文档.docx',
        source_artifact_id: snapshot.source_artifact_id || null,
        state_version: Number(snapshot.state_version || 0),
        group_state_version: Number(snapshot.group_state_version || 0),
        execution_state: snapshot.execution_state || 'CREATED',
        control_state: snapshot.control_state || 'RUNNING',
        result_status: snapshot.result_status || 'IN_PROGRESS',
        cleanup_state: snapshot.cleanup_state || 'NONE',
        status: snapshot.status || 'ACTIVE',
        latest_event_id: snapshot.latest_event_id || null,
        latest_seq: Number(snapshot.latest_seq || 0),
        last_error_code: snapshot.last_error_code || null,
        last_error_message: snapshot.last_error_message || null,
        last_event_id: snapshot.latest_event_id || null,
        parse_results: parseResults,
        active_candidate: candidate,
    };
}

async function adoptWorkflowWorkspace(workspace, { candidate = {}, record = {}, reason = '已恢复任务工作区' } = {}) {
    if (!workspace?.snapshot?.workflow_id) return false;
    const previousWorkflowId = String(currentSession?.session_id || '');
    const session = sessionFromWorkspace(workspace, candidate, record);
    if (!session) return false;
    if (workflowStream && previousWorkflowId && previousWorkflowId !== session.session_id) {
        await workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    if (previousWorkflowId !== session.session_id) {
        itemContentCache.clear();
        resetReviewNavigationState();
    }
    currentSession = session;
    currentWorkspace = workspace;
    generatedFiles = [];
    activeResultContext = null;
    latestCurrentResultEvent = null;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    resetLogTimeline('正在恢复任务记录…');
    lastGenerationConfig = workspace.configuration?.effective
        ? normalizeClientConfig(workspace.configuration.effective)
        : lastGenerationConfig;
    workflowStore?.prepare?.(session.session_id, {
        workflow: { workflow_id: session.session_id, ...workspace.snapshot },
        lastEventId: session.last_event_id,
        lastSeq: session.latest_seq,
    });
    workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
    if (workspace.configuration?.effective) {
        applyConfigToForm(workspace.configuration.effective, { includeRoles: true });
    }
    updateSessionLabels(session.source_filename, session.parse_results);
    renderContentReview(session.parse_results);
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(workspace, workspace.snapshot, session.session_id);
    } else {
        renderWorkspaceShell(workspace, workspace.snapshot);
    }
    renderActiveCandidateHint([]);

    const state = workspaceUserState(workspace, workspace.snapshot);
    generationResult = null;
    transientGenerationErrorMessage = '';
    const recovery = generationRecoveryPresentation(workspace, state);
    if (recovery) {
        addLogEntry({
            level: 'error',
            stage: 'complete',
            kind: 'summary',
            status: 'error',
            key: 'task:recovery',
            title: recovery.title,
            detail: recovery.message,
        });
    }
    if (isAcceptedGenerationSnapshot(workspace.snapshot)) {
        // Opening an active task from history must land on the same generation
        // console as an in-session task. Otherwise the workspace is hydrated
        // successfully but the user remains on the history page and cannot
        // see the authoritative pause/resume/stop state.
        goToStep(3);
        adoptAcceptedGeneration(session, workspace.snapshot, { reason });
    } else {
        isGenerating = false;
        generationStartInFlight = false;
        clearGenerationStartupTimer();
        if (isHardStoppedWorkflowSnapshot(workspace.snapshot)) {
            // A cancelled run is an immutable history fact, not a page the
            // user can resume. Reopen it directly in the editable voice step;
            // clicking Generate there will create the next run explicitly.
            hideGenerationRecovery();
            goToStep(2);
            setActiveWorkspaceView('voice');
            renderVoiceWorkspace();
            $('status-text').textContent = '当前生成已停止，请确认声音配置后重新生成。';
        } else if (state.view === 'issues' || ['WAITING_RETRY', 'WAITING_USER'].includes(state.key)) {
            goToStep(3);
        } else if (state.key === 'CREATED' && session.parse_results.length > 0) {
            showContentReview();
        } else {
            goToStep(3);
        }
        if (typeof renderWorkspaceAfterHydrate === 'function') {
            renderWorkspaceAfterHydrate(workspace, workspace.snapshot, session.session_id);
        } else {
            renderWorkspaceShell(workspace, workspace.snapshot);
        }
        updateGenerationCancelUI();
    }
    return true;
}

async function refreshHistoryRecords({ showLoading = true } = {}) {
    const requestToken = ++historyRequestToken;
    if (showLoading && currentView === 'history') renderHistoryMessage('正在读取本机历史记录…');
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const [data, activePage] = await Promise.all([
            workflowApi.listWorkflows(100),
            typeof workflowApi.listActiveWorkflowPage === 'function'
                ? workflowApi.listActiveWorkflowPage(ACTIVE_WORKFLOW_HYDRATE_LIMIT).catch(() => ({ workflows: [], truncated: false }))
                : typeof workflowApi.listActiveWorkflows === 'function'
                    ? workflowApi.listActiveWorkflows(ACTIVE_WORKFLOW_HYDRATE_LIMIT).then(workflows => ({ workflows, truncated: false })).catch(() => ({ workflows: [], truncated: false }))
                    : Promise.resolve({ workflows: [], truncated: false }),
        ]);
        if (requestToken !== historyRequestToken) return historyRecords;
        const activeCandidates = Array.isArray(activePage?.workflows) ? activePage.workflows : [];
        const hydratedCandidates = await hydrateActiveWorkflowCandidates(activeCandidates);
        if (requestToken !== historyRequestToken) return historyRecords;
        const activeByWorkflowId = new Map(
            hydratedCandidates
                .map(candidate => [String(candidate?.workflow?.workflow_id || ''), candidate])
                .filter(([workflowId]) => workflowId),
        );
        activeWorkflowListTruncated = activePage?.truncated === true;
        activeWorkflowCandidates = hydratedCandidates;
        workflowStore?.setActiveCandidates?.(activeWorkflowCandidates);
        renderActiveCandidateHint(activeWorkflowCandidates);
        historyRecords = (Array.isArray(data) ? data : [])
            .slice(0, 20)
            .map(record => ({
                ...record,
                active_candidate: activeByWorkflowId.get(String(record.workflow_id || record.id || '')) || null,
            }));
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        return historyRecords;
    } catch (error) {
        if (requestToken !== historyRequestToken) return historyRecords;
        console.error('读取历史记录失败:', error);
        if (currentView === 'history') renderHistoryMessage('历史记录暂时无法读取，请确认生成服务已连接后重试。', 'history-error');
        return historyRecords;
    }
}

async function viewHistoryRecord(historyId) {
    if (!historyId || isRestarting) return;
    const requestToken = ++historyRequestToken;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const record = historyRecords.find(item => item.id === historyId || item.workflow_id === historyId) || {};
        const [workflow, items, artifacts, workspace] = await Promise.all([
            workflowApi.getWorkflow(historyId),
            workflowApi.listItems(historyId),
            workflowApi.listArtifacts(historyId),
            typeof workflowApi.getWorkspace === 'function'
                ? workflowApi.getWorkspace(historyId).catch(error => {
                    console.warn('读取历史工作区失败，保留基础历史详情:', historyId, error);
                    return null;
                })
                : Promise.resolve(null),
        ]);
        if (requestToken !== historyRequestToken) return;
        const authoritativeSnapshot = workspace?.snapshot || workflow;
        const files = resultFilesFromArtifacts(items, artifacts, workspace);
        if (!isTerminalWorkflowSnapshot(authoritativeSnapshot)) {
            const candidate = record.active_candidate || {};
            if (workspace) {
                historyReturnStep = 1;
                await adoptWorkflowWorkspace(workspace, {
                    candidate,
                    record,
                    reason: '已恢复任务工作区',
                });
                showToast('已打开未结束任务；暂停任务不会自动恢复', 'info');
                return;
            }
            const progress = workspace?.progress || {};
            const completed = nonNegativeCount(progress.completed, files.length);
            const total = nonNegativeCount(progress.total, nonNegativeCount(record.total, completed));
            await showAlertDialog({
                kicker: '活动任务',
                title: record.source_filename || '未命名文档',
                message: `当前进度：${completed} / ${total}；任务尚未结束。`,
                detail: candidate.workspace_sync_error?.message || '任务工作区暂时无法同步，请稍后重试。',
                tone: 'warning',
                confirmLabel: '知道了',
            });
            return;
        }
        const progress = workspace?.progress || {};
        const delivery = workspace?.delivery || {};
        const { completed, total, failed, cancelled } = historyProgressCounts(progress, record, files.length);
        const failedItems = (Array.isArray(record.failed_items) ? record.failed_items : [])
            .filter(item => !['CANCELLED', 'SKIPPED'].includes(String(item?.status || '')));
        const context = {
            mode: 'history',
            recordId: historyId,
            workflowId: historyId,
            sourceFilename: record.source_filename || '未命名文档.docx',
            files,
            artifacts: Array.isArray(workspace?.artifacts) ? workspace.artifacts : artifacts,
            completed,
            failed,
            cancelled,
            total: total || nonNegativeCount(authoritativeSnapshot.item_count, files.length),
            format: files[0]?.format
                || workspace?.configuration?.effective?.format
                || record.format
                || null,
            generationMode: record.generation_mode || GENERATION_MODE_SINGLE,
            preview: Boolean(record.preview),
            zipAvailable: Boolean(delivery.zip_available),
            zipArtifactId: delivery.zip_artifact_id || null,
            failedItems,
            delivery,
            workspace,
            stateVersion: Number(authoritativeSnapshot.state_version || record.state_version || 0),
            executionState: authoritativeSnapshot.execution_state || record.execution_state || null,
            resultStatus: authoritativeSnapshot.result_status || record.result_status || null,
        };
        buildResultPage({
            workflow_id: historyId,
            completed: context.completed,
            failed: context.failed,
            cancelled: context.cancelled,
            total: context.total,
            failed_items: context.failedItems,
        }, context);
        activateStandalonePage('page-4', 'history-result');
        const backToHistoryBtn = $('back-to-history-btn');
        if (backToHistoryBtn) backToHistoryBtn.hidden = false;
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
    } catch (error) {
        if (requestToken !== historyRequestToken) return;
        console.error('读取历史详情失败:', error);
        showToast('这条历史记录暂时无法打开，可能文件已被移除');
        await refreshHistoryRecords({ showLoading: false });
    }
}

async function deleteHistoryRecord(record, button) {
    const workflowId = record?.workflow_id || record?.id;
    if (!workflowId || isRestarting) return;
    const terminal = isTerminalWorkflowSnapshot(record);
    const action = terminal ? 'archive' : 'delete';
    const filename = record.source_filename || '未命名文档';
    const fileCount = Math.max(0, Number(record.available_files) || 0);
    const confirmed = await showConfirmDialog({
        kicker: '历史记录',
        title: terminal ? '归档这条生成记录？' : '删除这条未完成任务？',
        message: terminal
            ? `将从历史列表隐藏「${filename}」及其 ${fileCount} 个音频文件。`
            : `将永久删除「${filename}」及其 ${fileCount} 个本地音频文件。`,
        detail: terminal
            ? '审计事件和 Artifact 会保留，不会物理删除文件。'
            : '本地工作流、Artifact、源文件暂存数据和本地文件都会清理；若任务已经提交到外部服务，本操作不会撤销外部提交。删除后无法恢复。',
        tone: 'danger',
        confirmLabel: terminal ? '归档记录' : '删除任务',
    });
    if (!confirmed) return;
    historyRequestToken++;
    if (button) button.disabled = true;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const request = {
            expected_state_version: Number(record.state_version || 0),
            reason: terminal ? 'desktop-history-archive' : 'desktop-history-delete',
        };
        if (action === 'archive') await workflowApi.archiveWorkflow(workflowId, request);
        else await workflowApi.deleteWorkflow(workflowId, request);
        const archivedCurrentResult = latestCurrentResultEvent?.workflow_id === workflowId
            || currentSession?.session_id === workflowId;
        historyRecords = historyRecords.filter(item => item.id !== workflowId && item.workflow_id !== workflowId);
        if (archivedCurrentResult) {
            currentSession = null;
            resetReviewNavigationState();
            generatedFiles = [];
            activeResultContext = null;
            latestCurrentResultEvent = null;
            historyReturnStep = 1;
            const uploadZone = $('upload-zone');
            uploadZone?.classList.remove('has-file', 'is-processing', 'dragover');
            uploadZone?.setAttribute('aria-busy', 'false');
            const uploadTitle = uploadZone?.querySelector('.upload-text-large');
            const uploadHint = uploadZone?.querySelector('.upload-hint');
            if (uploadTitle) uploadTitle.textContent = '拖拽文档到这里，或点击选择';
            if (uploadHint) uploadHint.textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
            updateSessionLabels();
            if ($('stats-bar')) $('stats-bar').replaceChildren();
            if ($('status-text')) $('status-text').textContent = '就绪';
            if ($('history-back-btn')) $('history-back-btn').textContent = '返回导入文档';
        }
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        showToast(
            action === 'archive'
                ? (archivedCurrentResult ? '当前任务已归档，Artifact 仍保留' : '历史记录已归档')
                : '未完成任务及其相关本地数据已删除',
        );
    } catch (error) {
        console.error(action === 'archive' ? '归档历史记录失败:' : '删除未完成任务失败:', error);
        showToast(`${action === 'archive' ? '归档' : '删除'}失败：${error.message || '请稍后重试'}`);
        if (button) button.disabled = false;
    }
}


registerRendererModule("history.restore", {
    readWithTimeout,
    hydrateActiveWorkflowCandidates,
    sessionFromWorkspace,
    adoptWorkflowWorkspace,
    refreshHistoryRecords,
    viewHistoryRecord,
    deleteHistoryRecord,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
