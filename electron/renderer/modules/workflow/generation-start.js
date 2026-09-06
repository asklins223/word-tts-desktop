/** Renderer module: workflow.generationStart */
(function attachRendererFeature_workflow_generationStart(root) {
    'use strict';

async function startProcessing(useDefaults, presetConfig, itemIds = null) {
    if (isRestarting) return;
    if (!currentSession) {
        showToast('当前文档会话已失效，请重新导入文档');
        goToStep(1);
        return;
    }
    if (isGenerating || generationStartInFlight) return;

    // A completed preview (or a completed failed run) is immutable.  When
    // the user changes its settings, this function may replace the session
    // with a durable rerun before patching the new configuration.
    let session = currentSession;
    const config = normalizeClientConfig(presetConfig || collectConfig(useDefaults));
    let requestedItemIds = Array.isArray(itemIds)
        ? [...new Set(itemIds.map((itemId) => String(itemId || '').trim()).filter(Boolean))]
        : null;
    if (requestedItemIds && requestedItemIds.length === 0) requestedItemIds = null;
    updateGenerationModeUI(config.generation_mode);
    const sourceTotal = summarizeParseResults(session.parse_results).total;
    let isPreviewScope = !requestedItemIds && Boolean(config.preview && sourceTotal > 3);
    let generationTotal = requestedItemIds
        ? requestedItemIds.length
        : (isPreviewScope ? Math.min(sourceTotal, 3) : sourceTotal);
    const attemptId = ++generationAttemptId;
    generationStartInFlight = true;
    generationStartAttemptId = attemptId;
    const controller = new AbortController();
    generateAbortController = controller;
    destroyWaveSurfers();
    clearSSEReconnectTimer();
    clearGenerationStartupTimer();
    isGenerating = true;
    activeWorkspace = 'generation';
    currentWorkspace = currentWorkspace?.workflow_id === session.session_id
        || currentWorkspace?.snapshot?.workflow_id === session.session_id
        ? currentWorkspace
        : null;
    if ($('history-nav-btn')) $('history-nav-btn').disabled = true;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    // A new run may briefly reuse the previous workspace while its first
    // authoritative snapshot is loading. Force the first render of the new
    // run through even when its initial scalar values match the old run.
    lastWorkspaceRenderKey = '';
    transientGenerationErrorMessage = '';
    updateSessionLabels(session.source_filename, session.parse_results, {
        preview: isPreviewScope,
        total: generationTotal,
    });
    hideGenerationRecovery();
    setGenerationVisualState('running');

    // 重置生成页面 UI
    setProgressBarPercent(0);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', '0% 处理中');
    setProgressReadoutMode(false);
    setProgressIndeterminate(true);
    $('progress-stats').textContent = `正在准备生成计划 · 0 / ${generationTotal || '—'}`;
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = generationTotal || '—';
    $('progress-failed').textContent = '0';
    if ($('progress-cancelled')) $('progress-cancelled').textContent = '0';
    if ($('progress-skipped')) $('progress-skipped').textContent = '0';
    if ($('progress-deliverable')) $('progress-deliverable').textContent = '0 / 0';
    $('generation-live-status').textContent = '正在准备生成计划…';
    $('gen-title').textContent = '正在生成音频';
    $('gen-animation').classList.remove('done');
    resetLogTimeline('生成任务即将开始，正在等待第一条处理记录…');
    generationStartupTimer = setTimeout(() => {
        generationStartupTimer = null;
        if (!isGenerating || currentSession?.session_id !== session.session_id || lastStats) return;
        $('progress-stats').textContent = `正在连接讯飞浏览器 · 0 / ${generationTotal || '—'}`;
        $('generation-live-status').textContent = '正在连接讯飞浏览器…';
        $('status-text').textContent = '生成中：正在连接讯飞浏览器…';
        addLogEntry({
            level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running',
            key: 'tts:startup', title: '正在启动音频引擎', detail: '首次启动或浏览器刚被中断后，正在重新建立讯飞会话，请保持应用开启。',
        });
    }, 700);
    $('type-stats').innerHTML = '';

    lastGenerationConfig = { ...config };
    void queueVoiceAssetCache([
        config.default_female_voice,
        config.default_male_voice,
        ...Object.values(config.role_voices || {}),
    ]);

    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        // 用户可能刚从中断/失败页面返回；先同步后端版本，避免把旧的
        // expected_state_version 重新提交而触发 STATE_CONFLICT。
        const snapshot = await refreshCurrentWorkflowSnapshot(session);
        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) return;
        // 另一个窗口、自动重试调度器或本页面较早的请求可能已经接受了
        // 这个工作流。此时不能再 PATCH 草稿；直接接管权威任务进度，
        // 否则用户会看到“配置已冻结”，而真正的浏览器任务仍在后台运行。
        if (isAcceptedGenerationSnapshot(snapshot)) {
            adoptAcceptedGeneration(session, snapshot, {
                reason: '检测到已有生成任务，已接管当前进度',
            });
            return;
        }
        if (snapshot?.execution_state === 'TERMINAL') {
            const expectedGroupStateVersion = Number(snapshot.group_state_version);
            if (!Number.isInteger(expectedGroupStateVersion)) {
                throw new Error('任务组版本缺失，无法安全创建新的生成任务');
            }
            const rerun = await workflowApi.rerun(session.session_id, {
                expected_group_state_version: expectedGroupStateVersion,
                source_workflow_id: session.session_id,
                reason: 'desktop-renderer-rerun',
            }, {
                idempotencyKey: `renderer-rerun-${session.session_id}-${expectedGroupStateVersion}`,
            });
            if (!rerun?.workflow_id) throw new Error('服务端未返回新的生成任务');
            const nextSession = {
                ...session,
                session_id: String(rerun.workflow_id),
                source_artifact_id: rerun.source_artifact_id || session.source_artifact_id || null,
                state_version: Number(rerun.state_version || 0),
                group_state_version: Number(rerun.group_state_version || expectedGroupStateVersion + 1),
                execution_state: rerun.execution_state || 'CREATED',
                control_state: rerun.control_state || 'RUNNING',
                result_status: rerun.result_status || 'IN_PROGRESS',
                cleanup_state: rerun.cleanup_state || 'NONE',
                latest_event_id: null,
                latest_seq: 0,
                last_event_id: null,
            };
            resetReviewNavigationState();
            currentSession = nextSession;
            session = nextSession;
            // A terminal run is immutable and its item IDs belong to the old
            // workflow.  Rerun the complete document; targeted retry is only
            // valid on the still-open original run.
            requestedItemIds = null;
            isPreviewScope = Boolean(config.preview && sourceTotal > 3);
            generationTotal = isPreviewScope ? Math.min(sourceTotal, 3) : sourceTotal;
            workflowStore?.resetCursor?.(session.session_id);
            updateSessionLabels(session.source_filename, session.parse_results, {
                preview: isPreviewScope,
                total: generationTotal,
            });
        }
        // 解析步骤会让 run 进入 ACTIVE，但在第一个执行 attempt 之前仍
        // 是可编辑的。把配置页当前选择写进 SQLite 后再接受 generate，
        // 确保后端不会继续使用创建草稿时的默认音色。已有 attempt 的
        // run 则由后端拒绝改变配置，避免修改已产生外部副作用的事实。
        const persistedConfiguration = buildWorkflowConfiguration(
            config,
            session.source_filename,
            currentConfig?.account_scope,
        );
        const workspaceBeforePatch = await workflowApi.getWorkspace(session.session_id);
        if (workspaceBeforePatch && currentSession?.session_id === session.session_id) {
            currentWorkspace = workspaceBeforePatch;
            workflowStore?.hydrate?.(workspaceBeforePatch, { snapshot: workspaceBeforePatch.snapshot || snapshot });
            if (typeof renderWorkspaceAfterHydrate === 'function') {
                renderWorkspaceAfterHydrate(currentWorkspace, workspaceBeforePatch.snapshot || snapshot, session.session_id);
            } else {
                renderWorkspaceShell(currentWorkspace, workspaceBeforePatch.snapshot || snapshot);
            }
        }
        const generationAction = workspaceAction('GENERATE', workspaceBeforePatch);
        if (generationAction && generationAction.enabled !== true) {
            throw new Error(generationAction.reason || '当前任务状态不允许生成');
        }
        const configurationRevisionBeforePatch = Number(
            workspaceBeforePatch?.configuration?.configuration_revision,
        );
        if (!Number.isInteger(configurationRevisionBeforePatch) || configurationRevisionBeforePatch < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        const patched = await workflowApi.patchWorkspace(session.session_id, {
            expected_state_version: Number(workspaceBeforePatch.snapshot?.state_version ?? session.state_version),
            configuration_revision: configurationRevisionBeforePatch,
            configuration: persistedConfiguration,
        }, {
            idempotencyKey: `renderer-config-${session.session_id}-${attemptId}-${configurationRevisionBeforePatch}`,
        });
        if (!patched) throw new Error('服务端未返回更新后的工作区');
        const workspaceAfterPatch = patched;
        if (workspaceAfterPatch && currentSession?.session_id === session.session_id) {
            currentWorkspace = workspaceAfterPatch;
            mergeWorkflowSnapshotIntoSession(workspaceAfterPatch.snapshot, session);
            session.parse_results = workspaceItemsToParseResults(workspaceAfterPatch);
            workflowStore?.hydrate?.(workspaceAfterPatch, { snapshot: workspaceAfterPatch.snapshot });
            if (typeof renderWorkspaceAfterHydrate === 'function') {
                renderWorkspaceAfterHydrate(currentWorkspace, workspaceAfterPatch.snapshot, session.session_id);
            } else {
                renderWorkspaceShell(currentWorkspace, workspaceAfterPatch.snapshot);
            }
            renderContentReview(session.parse_results);
        }
        const configurationRevision = Number(
            workspaceAfterPatch?.configuration?.configuration_revision,
        );
        if (!Number.isInteger(configurationRevision) || configurationRevision < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        const response = await submitGenerationCommand(
            session,
            config,
            controller,
            attemptId,
            requestedItemIds,
            configurationRevision,
        );

        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) return;
        mergeWorkflowSnapshotIntoSession(response?.current_snapshot, currentSession);
        const acceptedVersion = Number(response?.state_version);
        if (Number.isInteger(acceptedVersion) && acceptedVersion >= Number(currentSession.state_version || 0)) {
            currentSession.state_version = acceptedVersion;
        }
        generateAbortController = null;
        clearGenerationStartupTimer();
        connectSSE(session.session_id);
        setProgressIndeterminate(true);
        $('progress-stats').textContent = `已提交任务，正在连接讯飞浏览器 · 0 / ${generationTotal || '—'}`;
        $('generation-live-status').textContent = '正在连接讯飞浏览器并提交作品…';
        $('status-text').textContent = '生成中：正在连接讯飞浏览器…';

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== generationAttemptId) return;

        // PATCH 与后台调度之间存在竞态：PATCH 读取时仍可编辑，真正提交
        // 前自动化任务已经先被接受。CONFIG_FROZEN 在这里不是“重新报错”
        // 的终点，而是重新读取并接管那个已经在跑的任务。
        if (['CONFIG_FROZEN', 'GENERATION_ALREADY_RUNNING'].includes(err?.code) && workflowApi) {
            try {
                const authoritative = await workflowApi.getWorkflow(session.session_id);
                if (
                    currentSession?.session_id === session.session_id
                    && isAcceptedGenerationSnapshot(authoritative)
                ) {
                    adoptAcceptedGeneration(session, authoritative, {
                        reason: '任务已由后台接受，已接管当前进度',
                    });
                    showToast('任务已经在运行，已接管后台进度', 'warning');
                    return;
                }
            } catch (syncError) {
                console.warn('配置冻结后同步已接受任务失败:', syncError);
            }
        }
        clearGenerationStartupTimer();
        generateAbortController = null;
        generationResult = 'error';
        const serviceUnavailable = err instanceof TypeError || /failed to fetch/i.test(err.message || '');
        const failureMessage = serviceUnavailable
            ? '无法连接生成服务，请重试连接后再次生成。'
            : err?.code === 'CONFIG_FROZEN'
                    ? '本次任务已经开始过外部提交，不能在原任务中修改音色或参数；请点击“重新开始”新建任务后再生成。'
                    : `启动失败：${err?.message || '生成服务返回了未说明的错误'}`;
        transientGenerationErrorMessage = failureMessage;
        $('gen-title').textContent = '任务未能启动';
        $('generation-file-name').textContent = `未能启动「${session.source_filename || '当前文档'}」；设置与解析结果仍会保留。`;
        $('status-text').textContent = failureMessage;
        if (serviceUnavailable) {
            sourceImportServiceState = 'unavailable';
            setServiceState('error', '服务连接中断');
            setAppInteractive(false);
            refreshPendingServiceSourceFilePresentation();
            $('retry-service-btn').hidden = false;
        }
        setGenerationVisualState('error');
        setProgressIndeterminate(false);
        addLogEntry({
            level: 'error',
            stage: 'complete',
            kind: 'summary',
            status: 'error',
            key: 'task:summary',
            title: '生成任务未能启动',
            detail: failureMessage,
        });
        resetGenerateState();
        syncGenerationRecoveryState(
            currentWorkspace,
            workspaceUserState(currentWorkspace, currentSession),
        );
        syncTransientGenerationErrorShell(failureMessage);
        showToast(failureMessage, 'error');
    } finally {
        if (generationStartAttemptId === attemptId) {
            generationStartInFlight = false;
            generationStartAttemptId = 0;
            updateGenerationCancelUI();
        }
    }
}


registerRendererModule("workflow.generationStart", {
    startProcessing,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
