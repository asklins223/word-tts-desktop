/** Renderer module: workflow.workspace */
(function attachRendererFeature_workflow_workspace(root) {
    'use strict';

function renderProviderStatus() {
    const target = $('provider-status');
    const text = $('provider-status-text');
    if (!target || !text) return;
    const unavailable = currentConfig?.tts_engine === 'xunfei' && currentConfig.xunfei_available === false;
    const provider = currentWorkspace?.provider || authoritativeWorkspace()?.provider || null;
    const providerStatus = String(provider?.status || '').toUpperCase();
    const providerUnavailable = ['UNAVAILABLE', 'DISABLED'].includes(providerStatus);
    const providerCanStartGeneration = provider?.can_start_generation === true;
    const ready = $('service-state')?.classList.contains('is-ready')
        && !unavailable
        && !providerUnavailable
        && (provider ? provider.can_generate !== false && provider.ready !== false : true);
    const actionButton = $('provider-action-btn');
    target.classList.toggle('is-ready', ready);
    target.classList.toggle('is-error', unavailable || providerUnavailable);
    target.classList.toggle('is-warning', !ready && !unavailable && !providerUnavailable);
    const providerMessage = String(provider?.reason || '').trim();
    text.textContent = unavailable
        ? '依赖未就绪，请先安装浏览器运行环境'
        : providerMessage || (ready ? '已连接，可提交生成任务' : '等待生成服务连接');
    if (actionButton) {
        // For the legacy browser provider, Generate is the user-visible
        // login/reconnect entry point. Do not expose a no-op "reconnect"
        // button beside it when the provider explicitly allows that path.
        actionButton.hidden = (ready || providerCanStartGeneration) && !unavailable;
        actionButton.disabled = isRestarting || isParsing || sourceImportInFlight;
        actionButton.textContent = providerStatus === 'EXPIRED' || providerStatus === 'LOGIN_REQUIRED'
            ? '重新连接'
            : '重新检测';
    }
}

function renderWorkspaceShell(workspace = currentWorkspace, snapshot = currentSession) {
    const current = normalizedWorkspace(workspace, snapshot);
    if (!current && !currentSession) {
        const badge = $('task-status-badge');
        if (badge) {
            badge.className = 'task-status-badge is-neutral';
            badge.textContent = '待导入';
            badge.title = '';
        }
        if ($('toolbar-counts')) $('toolbar-counts').textContent = '';
        const progressBar = $('progress-bar');
        if (progressBar) {
            setProgressBarPercent(0);
            progressBar.parentElement?.setAttribute('aria-valuenow', '0');
            progressBar.parentElement?.setAttribute('aria-valuetext', '0% 处理中');
        }
        if ($('progress-percent')) $('progress-percent').textContent = '0';
        setProgressReadoutMode(false);
        if ($('progress-stats')) $('progress-stats').textContent = '准备中...';
        if ($('generation-live-status')) $('generation-live-status').textContent = '等待导入文档';
        setProgressIndeterminate(false);
        delete document.body.dataset.workflowState;
        delete document.body.dataset.generationState;
        updateGenerationControlUI({ available_actions: [] });
        renderProviderStatus();
        renderSystemInputSurface(null);
        root.WORDTTS_RENDERER?.getModule?.('app.navigation')?.updateDeliveryStageRail?.();
        return;
    }
    if (!current) {
        // A new session is assigned before its first authoritative workspace
        // snapshot arrives. Clear the previous task's banner/progress during
        // that gap so stale actions cannot appear to belong to the new task.
        const badge = $('task-status-badge');
        if (badge) {
            badge.className = 'task-status-badge is-neutral';
            badge.textContent = '同步中';
            badge.title = '正在读取任务状态';
        }
        if ($('toolbar-counts')) $('toolbar-counts').textContent = '';
        const progressBar = $('progress-bar');
        if (progressBar) {
            setProgressBarPercent(0);
            progressBar.parentElement?.setAttribute('aria-valuenow', '0');
            progressBar.parentElement?.setAttribute('aria-valuetext', '0% 处理中');
        }
        if ($('progress-percent')) $('progress-percent').textContent = '0';
        setProgressReadoutMode(false);
        if ($('progress-stats')) $('progress-stats').textContent = '正在同步任务状态…';
        if ($('generation-live-status')) $('generation-live-status').textContent = '正在同步任务状态…';
        setProgressIndeterminate(false);
        document.body.dataset.workflowState = 'SYNCING';
        document.body.dataset.generationState = 'SYNCING';
        updateGenerationControlUI({ available_actions: [] });
        renderProviderStatus();
        renderSystemInputSurface(null);
        root.WORDTTS_RENDERER?.getModule?.('app.navigation')?.updateDeliveryStageRail?.();
        return;
    }
    const state = workspaceUserState(current, current.snapshot || snapshot);
    const progress = workspaceProgress(current);
    renderWorkspaceProgress(current, state);
    renderGenerationViewState(current, state, progress);
    const badge = $('task-status-badge');
    if (badge) {
        badge.className = `task-status-badge is-${state.tone || 'info'}`;
        badge.textContent = state.label;
        badge.title = state.description || '';
    }
    const counts = $('toolbar-counts');
    if (counts) {
        const issueSummary = generationProgressIssueSummary(progress);
        counts.textContent = progress.total > 0
            ? `${progress.completed}/${progress.total} 已完成 · ${progress.deliverable} 可交付${issueSummary ? ` · ${issueSummary}` : ''}`
            : '';
    }
    document.body.dataset.workflowState = state.key || '';
    renderProviderStatus();
    updateConfigActionState(current);
    updateGenerationControlUI(current);
    if (generationResult === 'error' && transientGenerationErrorMessage && activeWorkspace === 'generation') {
        syncTransientGenerationErrorShell(transientGenerationErrorMessage);
    }
    renderSystemInputSurface(current);
    root.WORDTTS_RENDERER?.getModule?.('app.navigation')?.updateDeliveryStageRail?.();

    // The done frame and the durable terminal snapshot are separate signals.
    // If the snapshot is the first thing that confirms completion, let the
    // completion module build the verified result before handing off.
    if (state?.terminal && ['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(String(state.key || '').toUpperCase())) {
        root.WORDTTS_RENDERER?.getModule?.('delivery.completion')?.ensureTerminalDeliveryHandoff?.(current, current.snapshot || snapshot);
    }
}

// A full workspace refresh carries rich item/delivery facts, while the Store
// carries the latest bounded SSE runtime projection.  Render both layers in
// this order: otherwise the Store subscriber can paint live provider progress
// and the final shell render can immediately overwrite it with the aggregate
// item progress (usually still 0 until the artifact is verified).
function renderWorkspaceAfterHydrate(workspace, snapshot = null, workflowId = currentSession?.session_id) {
    renderWorkspaceShell(workspace, snapshot);
    // The shell render is authoritative for static item facts, but it can
    // repaint an active task's bar from aggregate progress. Force the Store
    // overlay to run even when this refresh did not introduce a new event.
    lastWorkspaceRenderKey = '';
    const storeState = workflowStore?.getState?.();
    const targetId = String(
        workflowId
        || snapshot?.workflow_id
        || workspace?.snapshot?.workflow_id
        || currentSession?.session_id
        || '',
    );
    const storeId = String(
        storeState?.workflowId
        || storeState?.workflowProjection?.workflow_id
        || '',
    );
    if (
        storeState
        && targetId
        && storeId === targetId
        && typeof renderWorkflowStoreState === 'function'
    ) {
        renderWorkflowStoreState(storeState);
    }
}

async function hydrateWorkflowWorkspace(workflowId = currentSession?.session_id, { snapshot = null, silent = true, adoptConfiguration = false } = {}) {
    if (!workflowApi || !workflowId || typeof workflowApi.getWorkspace !== 'function') return null;
    if (workspaceRefreshInFlight?.workflowId === String(workflowId)) return workspaceRefreshInFlight.promise;
    const promise = (async () => {
        try {
            const workspace = await workflowApi.getWorkspace(workflowId);
            if (!workspace || currentSession?.session_id !== workflowId) return null;
            const incomingSnapshot = workspace.snapshot || snapshot;
            if (!workflowSnapshotBelongsToSession(incomingSnapshot, currentSession)) return null;
            const stale = workflowSnapshotIsStaleForSession(incomingSnapshot, currentSession);
            const storeSnapshot = workflowStore?.getState?.().workflowProjection;
            const liveStoreSnapshot = stale
                && storeSnapshot
                && workflowSnapshotBelongsToSession(storeSnapshot, currentSession)
                && !workflowSnapshotIsOlder(storeSnapshot, currentSession)
                ? storeSnapshot
                : null;
            const effectiveSnapshot = stale
                ? (liveStoreSnapshot || latestWorkflowSnapshotForSession(currentSession, incomingSnapshot))
                : incomingSnapshot;
            const effectiveWorkspace = stale
                ? (
                    currentWorkspace
                    && String(currentWorkspace?.snapshot?.workflow_id || '') === String(workflowId)
                        ? { ...currentWorkspace, snapshot: { ...(currentWorkspace.snapshot || {}), ...effectiveSnapshot } }
                        : { ...workspace, snapshot: { ...(workspace.snapshot || {}), ...effectiveSnapshot } }
                )
                : workspace;
            currentWorkspace = effectiveWorkspace;
            mergeWorkflowSnapshotIntoSession(effectiveSnapshot, currentSession);
            if (Array.isArray(workspace.items)) {
                currentSession.parse_results = workspaceItemsToParseResults(workspace);
            }
            if (adoptConfiguration && effectiveWorkspace.configuration?.effective) {
                applyConfigToForm(effectiveWorkspace.configuration.effective, { includeRoles: true });
            }
            workflowStore?.hydrate?.(effectiveWorkspace, { snapshot: effectiveSnapshot });
            renderWorkspaceAfterHydrate(effectiveWorkspace, effectiveSnapshot, workflowId);
            renderContentReview(currentSession?.parse_results);
            // A workspace refresh can finish after the user has returned from
            // generation. Re-assert the nested step view here so an older
            // async response cannot leave both page-2 workspaces hidden.
            if (currentView === 'workflow' && currentStep === 2) {
                const stepView = activeWorkspace === 'review' ? 'review' : 'voice';
                setActiveWorkspaceView(stepView);
                if (stepView === 'voice') renderVoiceWorkspace();
            }
            return effectiveWorkspace;
        } catch (error) {
            if (!silent) showToast(workflowAdapter.issueMessage?.(error)?.message || '任务工作区暂时无法同步', 'error');
            return null;
        } finally {
            if (workspaceRefreshInFlight?.workflowId === String(workflowId)) workspaceRefreshInFlight = null;
        }
    })();
    workspaceRefreshInFlight = { workflowId: String(workflowId), promise };
    return promise;
}

function scheduleWorkspaceRefresh(workflowId = currentSession?.session_id) {
    if (!workflowId || !workflowApi) return;
    clearTimeout(workspaceRefreshTimer);
    workspaceRefreshTimer = setTimeout(() => {
        workspaceRefreshTimer = null;
        void hydrateWorkflowWorkspace(workflowId);
    }, 220);
}

async function performWorkspaceAction(action) {
    if (!action || action.enabled !== true || !currentSession?.session_id || !workflowApi) return false;
    const type = String(action.type || '');
    if (type === 'ACCEPT_AUDIO') return performSystemInputAudioAcceptance(action);
    if (type === 'START_INPUT') return performSystemInputStart(action);
    if (type === 'OPEN_VIEW') {
        const view = workspaceUserState(currentWorkspace, currentSession).view;
        if (view === 'delivery') goToStep(4);
        else if (view === 'issues') goToStep(3);
        else if (view === 'voice') goToStep(2);
        return true;
    }
    if (type === 'GENERATE') {
        goToStep(3);
        void startProcessing(false);
        return true;
    }
    if (type === 'DOWNLOAD_ZIP') {
        goToStep(4);
        return true;
    }
    const commandMap = {
        PAUSE: 'pause',
        RESUME: 'resume',
        CANCEL: 'cancel',
        RETRY: 'retry',
        ARCHIVE: 'archive',
        EXPORT_ZIP: 'export-zip',
        RERUN: 'rerun',
    };
    const command = commandMap[type];
    if (!command) return false;
    const key = `${currentSession.session_id}:${type}`;
    if (workflowStore?.getState?.().pendingCommands?.[key]) return false;
    if (workflowCommandCoordinator) {
        try {
            const outcome = await workflowCommandCoordinator.run(action, {
                reason: 'desktop-workspace-action',
            });
            if (!outcome.ok) {
                if (outcome.reason === 'action-disabled-after-refresh') {
                    showToast('任务状态已变化，已刷新最新操作。', 'warning');
                } else if (outcome.reason === 'workspace-refresh-failed') {
                    showToast('任务状态刷新失败，请稍后重试。', 'warning');
                }
                return false;
            }
            if (type === 'RERUN') {
                const rerunSnapshot = outcome.response?.workflow || outcome.response;
                const nextWorkflowId = String(rerunSnapshot?.workflow_id || '');
                if (!nextWorkflowId || typeof workflowApi.getWorkspace !== 'function') {
                    throw new Error('服务端未返回新的工作流');
                }
                const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
                await adoptWorkflowWorkspace(nextWorkspace, {
                    record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename },
                    reason: '已创建新的生成任务，请确认配置后开始生成',
                });
                showToast('已创建新的生成任务，请确认配置后开始生成');
                return true;
            }
            if (type === 'RESUME' && !isGenerating) {
                adoptResumedGenerationIfNeeded(type, outcome.workspace, outcome.response);
            }
            showToast(type === 'PAUSE'
                ? '已发送暂停请求'
                : type === 'RESUME'
                    ? '已发送恢复请求'
                    : type === 'EXPORT_ZIP'
                        ? '正在整理 ZIP 交付文件'
                        : '操作已提交');
            return true;
        } catch (error) {
            showToast(workflowAdapter.issueMessage?.(error)?.message || `操作失败：${error.message || '请稍后重试'}`, 'error');
            renderWorkspaceAfterHydrate(currentWorkspace, currentSession, currentSession?.session_id);
            return false;
        }
    }
    workflowStore?.markCommandPending?.(key, true);
    try {
        const snapshot = await refreshCurrentWorkflowSnapshot(currentSession);
        if (command === 'rerun' && typeof workflowApi.rerun === 'function') {
            const expectedGroupStateVersion = Number(
                action.expected_group_state_version ?? snapshot?.group_state_version ?? currentSession.group_state_version,
            );
            if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
                throw new Error('任务组版本缺失，无法安全重新运行');
            }
            const response = await workflowApi.rerun(currentSession.session_id, {
                expected_group_state_version: expectedGroupStateVersion,
                source_workflow_id: currentSession.session_id,
                reason: 'desktop-workspace-action',
            }, {
                idempotencyKey: `renderer-rerun-${currentSession.session_id}-${expectedGroupStateVersion}`,
            });
            const nextWorkflowId = String(response?.workflow_id || response?.workflow?.workflow_id || '');
            if (!nextWorkflowId) throw new Error('服务端未返回新的工作流');
            const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
            await adoptWorkflowWorkspace(nextWorkspace, {
                record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename },
                reason: '已创建新的生成任务，请确认配置后开始生成',
            });
            showToast('已创建新的生成任务，请确认配置后开始生成');
            return true;
        }
        const body = {
            expected_state_version: Number(snapshot?.state_version ?? currentSession.state_version ?? 0),
        };
        if (command === 'retry') {
            if (action.target) body.target = action.target;
            const targetVersion = Number(action.expected_target_state_version);
            if (Number.isInteger(targetVersion) && targetVersion >= 0) {
                body.expected_target_state_version = targetVersion;
            }
            if (action.expected_attempt_id) body.expected_attempt_id = String(action.expected_attempt_id);
            body.reason = 'desktop-workspace-action';
        } else if (command !== 'export-zip') {
            body.reason = 'desktop-workspace-action';
        }
        const response = await workflowApi.sendCommand(currentSession.session_id, command, body, {
            idempotencyKey: `renderer-${command}-${currentSession.session_id}-${Number(snapshot?.state_version || currentSession.state_version || 0)}`,
        });
        mergeWorkflowSnapshotIntoSession(response?.current_snapshot || response, currentSession);
        const refreshedWorkspace = await hydrateWorkflowWorkspace(currentSession.session_id, { silent: false });
        if (type === 'RESUME' && !isGenerating) {
            adoptResumedGenerationIfNeeded(type, refreshedWorkspace, response);
        }
        showToast(type === 'PAUSE'
            ? '已发送暂停请求'
            : type === 'RESUME'
                ? '已发送恢复请求'
                : '操作已提交');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `操作失败：${error.message || '请稍后重试'}`, 'error');
        return false;
    } finally {
        workflowStore?.markCommandPending?.(key, false);
        renderWorkspaceAfterHydrate(currentWorkspace, currentSession, currentSession?.session_id);
    }
}

async function runFreshWorkspaceAction(actionType) {
    if (!workflowApi || !currentSession?.session_id) return false;
    const workspace = await hydrateWorkflowWorkspace(currentSession.session_id, {
        silent: false,
    });
    // A failed refresh must not fall back to the cached action: sending that
    // action is exactly how an old expected_state_version produced the
    // expected/current conflict shown by the UI.
    if (!workspace) return false;
    const action = workspaceAction(actionType, workspace);
    if (!action || action.enabled !== true) {
        showToast('任务状态已刷新，当前不可执行该操作。', 'warning');
        return false;
    }
    return performWorkspaceAction(action);
}

async function rerunResultContext(context = activeResultContext) {
    const workspace = context?.workspace || null;
    const workflowId = String(context?.workflowId || context?.recordId || '');
    const action = workflowAdapter.action?.(workspace, 'RERUN');
    if (!workflowId || action?.enabled !== true || typeof workflowApi?.rerun !== 'function') {
        showToast(action?.reason || '当前任务暂时不能重新运行', 'warning');
        return false;
    }
    const expectedGroupStateVersion = Number(
        action.expected_group_state_version ?? workspace?.snapshot?.group_state_version,
    );
    if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
        showToast('任务组版本缺失，无法安全重新运行', 'error');
        return false;
    }
    try {
        const rerun = await workflowApi.rerun(workflowId, {
            expected_group_state_version: expectedGroupStateVersion,
            source_workflow_id: workflowId,
            reason: 'desktop-result-rerun',
        }, { idempotencyKey: `renderer-rerun-${workflowId}-${expectedGroupStateVersion}` });
        const nextWorkflowId = String(rerun?.workflow_id || '');
        if (!nextWorkflowId) throw new Error('服务端未返回新的工作流');
        const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
        await adoptWorkflowWorkspace(nextWorkspace, {
            record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename || context.sourceFilename },
            reason: '已创建新的生成任务，请确认配置后开始生成',
        });
        showToast('已创建新的生成任务，请确认配置后开始生成');
        return true;
    } catch (error) {
        console.error('重新运行任务失败:', error);
        showToast(workflowAdapter.issueMessage?.(error, '重新运行未完成')?.message || '重新运行未完成', 'error');
        return false;
    }
}


registerRendererModule("workflow.workspace", {
    renderProviderStatus,
    renderWorkspaceShell,
    renderWorkspaceAfterHydrate,
    hydrateWorkflowWorkspace,
    scheduleWorkspaceRefresh,
    performWorkspaceAction,
    runFreshWorkspaceAction,
    rerunResultContext,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
