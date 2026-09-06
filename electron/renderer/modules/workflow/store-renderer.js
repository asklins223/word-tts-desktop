/** Renderer module: workflow.storeRenderer */
(function attachRendererFeature_workflow_storeRenderer(root) {
    'use strict';

function compositeRuntimeProjection(workspace = {}, snapshot = null) {
    const snapshotSource = snapshot && typeof snapshot === 'object'
        ? snapshot
        : (workspace?.snapshot && typeof workspace.snapshot === 'object' ? workspace.snapshot : null);
    const snapshotRuntime = snapshotSource?.runtime;
    const runtime = {};
    const copyRuntimeFields = (source) => {
        if (!source || typeof source !== 'object' || Array.isArray(source)) return;
        ['stage', 'status', 'message', 'item_id'].forEach((field) => {
            if (source[field] === undefined || source[field] === null) return;
            runtime[field] = String(source[field]).slice(0, 500);
        });
        ['completed_segments', 'total_segments', 'submitted_works', 'downloaded_works', 'total_works', 'item_count']
            .forEach((field) => {
                const value = Number(source[field]);
                if (Number.isInteger(value) && value >= 0) runtime[field] = value;
            });
        const elapsed = Number(source.elapsed_seconds ?? source.elapsedSeconds);
        if (Number.isFinite(elapsed) && elapsed >= 0) runtime.elapsed_seconds = elapsed;
    };

    // The server snapshot exposes the latest event rather than a separate
    // runtime object. Project that payload as well so a full workspace refresh
    // cannot erase the determinate progress that the live Store just painted.
    copyRuntimeFields(snapshotRuntime);
    copyRuntimeFields(snapshotSource?.latest_event?.payload);
    const scalarRuntime = workspace?.runtime;

    // The store keeps the ordered event projection in camelCase, while the
    // server snapshot uses the SSE payload's snake_case names.  Merge both so
    // a live store notification remains useful even before the next full
    // workspace hydration arrives.
    [
        ['stage', 'stage'],
        ['status', 'status'],
        ['message', 'message'],
        ['itemId', 'item_id'],
    ].forEach(([sourceKey, targetKey]) => {
        const value = scalarRuntime?.[sourceKey];
        const snapshotValue = runtime[targetKey];
        if ((snapshotValue === undefined || snapshotValue === null || !String(snapshotValue).trim())
            && value !== undefined && value !== null && String(value).trim()) {
            runtime[targetKey] = String(value);
        }
    });
    const elapsedSeconds = Number(scalarRuntime?.elapsedSeconds);
    if (!Number.isFinite(Number(runtime.elapsed_seconds))
        && Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
        runtime.elapsed_seconds = elapsedSeconds;
    }

    // Single-segment progress is kept as a separate scalar projection in the
    // Store. Carry it into the shared runtime object so a rich workspace
    // refresh cannot make the active readout lose its current segment count.
    const segments = workspace?.segments;
    ['completed', 'total'].forEach((field) => {
        const sourceValue = Number(segments?.[field]);
        const targetField = field === 'completed' ? 'completed_segments' : 'total_segments';
        if ((runtime[targetField] === undefined || runtime[targetField] === null)
            && Number.isFinite(sourceValue) && sourceValue >= 0) {
            runtime[targetField] = Math.round(sourceValue);
        }
    });

    // Composite events are also projected into workspace.works.  This is the
    // reliable fallback when the snapshot was created before the latest event
    // or when a renderer is running against an older workspace response.
    const works = workspace?.works;
    const totalWorks = Number(works?.total);
    const snapshotTotalWorks = Number(runtime.total_works);
    if (Number.isFinite(totalWorks) && totalWorks > 0) {
        // A hydrated server snapshot is newer than the store's last scalar
        // event projection when it contains the same fact. Only use works as
        // a fallback for fields the snapshot does not carry yet; otherwise a
        // refresh can visibly move a downloaded-work count backwards.
        if (!Number.isFinite(snapshotTotalWorks) || snapshotTotalWorks <= 0) {
            runtime.total_works = Math.round(totalWorks);
        }
        [
            ['submitted', 'submitted_works'],
            ['downloaded', 'downloaded_works'],
            ['items', 'item_count'],
        ].forEach(([sourceKey, targetKey]) => {
            const value = Number(works?.[sourceKey]);
            const snapshotValue = Number(runtime[targetKey]);
            if (Number.isFinite(value) && value >= 0
                && (!Number.isFinite(snapshotValue) || snapshotValue < 0)) {
                runtime[targetKey] = Math.round(value);
            }
        });
    }
    return runtime;
}

function mergeStoreRuntimeIntoWorkspace(workspace, scalarWorkspace) {
    if (!workspace || !scalarWorkspace) return workspace;
    const scalarRuntime = scalarWorkspace.runtime && typeof scalarWorkspace.runtime === 'object'
        ? scalarWorkspace.runtime
        : {};
    const runtime = {};
    ['status', 'stage', 'message', 'itemId', 'elapsedSeconds'].forEach((field) => {
        if (scalarRuntime[field] !== undefined && scalarRuntime[field] !== null) {
            runtime[field] = scalarRuntime[field];
        }
    });
    const scalarWorks = scalarWorkspace.works && typeof scalarWorkspace.works === 'object'
        ? scalarWorkspace.works
        : {};
    const totalWorks = Number(scalarWorks.total);
    const hasWorks = Number.isFinite(totalWorks) && totalWorks > 0;
    if (!Object.keys(runtime).length && !hasWorks) return workspace;

    const merged = { ...workspace };
    if (Object.keys(runtime).length) {
        merged.runtime = { ...(workspace.runtime || {}), ...runtime };
    }
    if (hasWorks) {
        merged.works = {
            ...(workspace.works || {}),
            total: Math.round(totalWorks),
            submitted: Math.max(0, Math.round(Number(scalarWorks.submitted) || 0)),
            downloaded: Math.max(0, Math.round(Number(scalarWorks.downloaded) || 0)),
            items: Math.max(0, Math.round(Number(scalarWorks.items) || 0)),
        };
    }
    return merged;
}

function renderWorkflowWorkspace(storeState) {
    const workspace = storeState?.workspace;
    if (!workspace || !isGenerating || generationResult) return;
    const activeWorkflowId = String(currentSession?.session_id || '');
    const storeWorkflowId = String(storeState?.workflowId || '');
    if (activeWorkflowId && storeWorkflowId && activeWorkflowId !== storeWorkflowId) return;
    const shellWorkspace = mergeStoreRuntimeIntoWorkspace(
        storeState?.workspaceData || currentWorkspace || {},
        workspace,
    );
    const shellSnapshot = storeState?.workflowProjection || shellWorkspace.snapshot || currentSession;
    const authoritativeShellWorkspace = shellSnapshot
        ? { ...shellWorkspace, snapshot: shellSnapshot }
        : shellWorkspace;
    const shellState = workspaceUserState(authoritativeShellWorkspace, shellSnapshot);
    // A final segment/runtime event can arrive before the server has verified
    // artifacts and moved the workflow to its terminal delivery state. Keep
    // the active view below 100%; only the terminal delivery projection may
    // claim completion.
    if (shellState?.terminal || isTerminalWorkflowSnapshot(shellSnapshot)) return;
    const controlState = String(
        shellSnapshot?.control_state
        || workspace.controlState
        || shellWorkspace?.snapshot?.control_state
        || '',
    ).toUpperCase();
    if (generationWorkflowOwnsRuntimeView(shellWorkspace, shellSnapshot)
        || GENERATION_RUNTIME_FROZEN_CONTROL_STATES.has(controlState)) {
        renderGenerationViewState(authoritativeShellWorkspace, shellState, workspaceProgress(authoritativeShellWorkspace));
        return;
    }
    const phase = String(workspace.phase || '');
    const message = String(shellSnapshot?.runtime?.message || workspace.runtime?.message || '');
    const segments = workspace.segments || {};
    const runtime = compositeRuntimeProjection(workspace, shellSnapshot);
    // 合并生成运行期的作品阶段读数；逐条模式没有 total_works，返回 null。
    const runtimeReadout = phase !== 'attention'
        ? compositeRuntimeReadout(runtime)
        : null;
    const itemProgress = workspaceProgress(authoritativeShellWorkspace);
    const itemTotal = generationProgressTotal(itemProgress, authoritativeShellWorkspace);
    const itemCompleted = Math.max(0, Math.min(
        itemTotal || Number.MAX_SAFE_INTEGER,
        Math.round(Number(itemProgress?.completed) || 0),
    ));
    const itemPercent = itemTotal > 0
        ? Math.min(99, Math.round((itemCompleted / itemTotal) * 100))
        : 0;
    const itemIssues = generationProgressIssueSummary(itemProgress);
    const compositeRuntimeActive = Number(runtime.total_works) > 0;
    const singleRuntimeReadout = phase !== 'attention' && !compositeRuntimeActive
        ? singleSegmentRuntimeReadout(runtime, itemProgress, itemTotal, shellWorkspace)
        : null;
    const activeRuntimeReadout = runtimeReadout || singleRuntimeReadout;
    const hasSegments = Number(segments.total) > 0;
    const completed = hasSegments ? Math.min(Math.max(0, Number(segments.completed) || 0), Number(segments.total)) : 0;
    const renderKey = [
        phase,
        message,
        compositeRuntimeActive
            ? [
                runtime.stage,
                runtime.status,
                runtime.total_works,
                runtime.submitted_works,
                runtime.downloaded_works,
                runtime.item_count,
                runtime.elapsed_seconds,
            ].join(':')
            : 'no-composite',
        hasSegments ? `${completed}/${segments.total}` : 'none',
        activeRuntimeReadout ? `${activeRuntimeReadout.percent}|${activeRuntimeReadout.stats}` : 'none',
        `${itemCompleted}/${itemTotal}`,
        itemIssues,
        String(workspace.executionState ?? ''),
        controlState,
        String(workspace.resultStatus ?? ''),
    ].join('|');
    if (renderKey === lastWorkspaceRenderKey) return;
    lastWorkspaceRenderKey = renderKey;

    // 合并模式也会携带 total_segments（它代表待切割的题目数），但在
    // 提交/合成/下载阶段 completed_segments 必然还是 0。作品阶段读数
    // 必须优先，否则每次 SSE 通知都会把上方刚画好的进度覆盖回 0%。
    if (activeRuntimeReadout) {
        setProgressBarPercent(activeRuntimeReadout.percent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(activeRuntimeReadout.percent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${activeRuntimeReadout.percent}% 处理中`);
        $('progress-percent').textContent = String(activeRuntimeReadout.percent);
        setProgressIndeterminate(false);
        $('progress-stats').textContent = activeRuntimeReadout.stats;
        const stageDetail = $('generation-synthesis-stage-detail');
        if (stageDetail) stageDetail.textContent = activeRuntimeReadout.stageLabel;
    } else if (compositeRuntimeActive) {
        // total_works identifies composite generation even when a new/error
        // provider stage is not yet known by compositeRuntimeReadout. Never
        // fall through to the segment counter here: downloaded works can make
        // completed_segments equal total_segments before cutting/verification
        // has finished, which would falsely paint the task as 100% complete.
        setProgressBarPercent(itemPercent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(itemPercent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${itemPercent}% 处理中`);
        $('progress-percent').textContent = String(itemPercent);
        setProgressIndeterminate(true);
        const count = `${itemCompleted} / ${itemTotal || '—'}`;
        const stage = String(runtime.stage || '').trim();
        const fallbackMessage = message || (stage ? `讯飞浏览器：${stage}` : '正在处理合并作品');
        $('progress-stats').textContent = `${fallbackMessage} · ${count}${itemIssues ? ` · ${itemIssues}` : ''}`;
    } else if (hasSegments) {
        const percent = Math.min(99, Math.round((completed / Number(segments.total)) * 100));
        setProgressBarPercent(percent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(percent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${percent}% 处理中`);
        $('progress-percent').textContent = String(percent);
        setProgressIndeterminate(false);
        $('progress-stats').textContent = `${message || '讯飞浏览器处理中'} · ${completed} / ${segments.total}`;
    } else if (phase && phase !== 'attention') {
        // 分段计数还没产生（浏览器启动/准备阶段）：保持不确定进度，只
        // 同步阶段文案。这里必须同时重置百分比；否则一次失败后恢复
        // 时会把旧快照的 99% 留在进度条上，造成“脚本已运行但界面不动”
        // 的假象。
        setProgressBarPercent(itemPercent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(itemPercent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${itemPercent}% 处理中`);
        $('progress-percent').textContent = String(itemPercent);
        setProgressIndeterminate(true);
        const count = `${itemCompleted} / ${itemTotal || '—'}`;
        $('progress-stats').textContent = `${message || '正在准备生成任务'} · ${count}${itemIssues ? ` · ${itemIssues}` : ''}`;
    }
    if (message && $('generation-live-status')?.textContent !== message) {
        $('generation-live-status').textContent = message;
        $('status-text').textContent = message;
    }
}

function renderWorkflowStoreState(storeState) {
    const storedWorkspace = storeState?.workspaceData;
    const workflowId = String(storeState?.workflowId || '');
    const activeId = String(currentSession?.session_id || '');
    if (storedWorkspace && workflowId && workflowId === activeId) {
        // Keep the rich server workspace as the source of item/delivery facts,
        // while letting the ordered event projection update the shell's status
        // immediately between scheduled full workspace hydrations.
        const projectedSnapshot = storeState.workflowProjection || storedWorkspace.snapshot;
        currentWorkspace = {
            ...(currentWorkspace || {}),
            ...storedWorkspace,
            snapshot: projectedSnapshot ? { ...projectedSnapshot } : null,
        };
        currentWorkspace = mergeStoreRuntimeIntoWorkspace(currentWorkspace, storeState.workspace);
        renderWorkspaceShell(currentWorkspace, currentWorkspace.snapshot || currentSession);
        // The shell render intentionally paints aggregate item progress. Force
        // the Store overlay below it even when no new event arrived during
        // this notification.
        lastWorkspaceRenderKey = '';
    }
    renderWorkflowWorkspace(storeState);
}

if (workflowStore && typeof workflowStore.subscribe === 'function') {
    workflowStore.subscribe(renderWorkflowStoreState);
}

// ============================================================================
// 启动
// ============================================================================


registerRendererModule("workflow.storeRenderer", {
    compositeRuntimeProjection,
    mergeStoreRuntimeIntoWorkspace,
    renderWorkflowWorkspace,
    renderWorkflowStoreState,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
