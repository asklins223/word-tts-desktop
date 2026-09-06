/** Renderer module: app.navigation */
(function attachRendererFeature_app_navigation(root) {
    'use strict';

// ============================================================================
// 步骤导航
// ============================================================================

function updateResultScrollTopButton() {
    const button = $('result-scroll-top');
    const scrollPage = $('page-4')?.querySelector('.page-scroll');
    if (!button) return;
    button.hidden = !scrollPage || scrollPage.scrollTop < 240;
}

function scrollResultToTop() {
    const scrollPage = $('page-4')?.querySelector('.page-scroll');
    if (!scrollPage) return;
    scrollPage.scrollTo({ top: 0, behavior: 'smooth' });
    window.setTimeout(updateResultScrollTopButton, 280);
}

function rebuildAudioDeliveryPageForReturn({ historyResult = false } = {}) {
    if (typeof buildResultPage !== 'function') return false;

    // Entering a delivery subpage deliberately releases the native Audio
    // elements, object URLs, and WaveSurfer instances. The DOM nodes remain
    // mounted, but their event closures carry the old render token, so simply
    // switching back would leave buttons attached to an invalid playback
    // source and an empty waveform queue. Rebuild the result surface so every
    // item receives a fresh token, Audio element, artifact ticket, and queue
    // registration.
    const context = historyResult
        ? activeResultContext
        : activeResultContext
            ? { ...activeResultContext, workspace: currentWorkspace || activeResultContext.workspace }
            : null;
    const event = historyResult
        ? {
            workflow_id: context?.workflowId || context?.recordId || context?.workspace?.snapshot?.workflow_id || null,
            completed: context?.completed ?? 0,
            failed: context?.failed ?? 0,
            cancelled: context?.cancelled ?? 0,
            total: context?.total ?? 0,
            failed_items: context?.failedItems || [],
        }
        : latestCurrentResultEvent || {
            workflow_id: currentSession?.session_id || context?.workflowId || null,
            completed: context?.completed ?? 0,
            failed: context?.failed ?? 0,
            cancelled: context?.cancelled ?? 0,
            total: context?.total ?? 0,
            failed_items: context?.failedItems || [],
        };
    if (!event.workflow_id && !context && !currentSession?.session_id) return false;

    buildResultPage(event, context);
    return true;
}

function generationWorkspaceNavigationAllowed({
    activeWorkspaceName = activeWorkspace,
    hasSession = Boolean(currentSession?.session_id),
    generationActive = isGenerating || generationStartInFlight,
    generationAccepted = isAcceptedGenerationSnapshot(currentWorkspace?.snapshot || currentSession),
    generationResultState = generationResult,
    generationSnapshot = currentWorkspace?.snapshot || currentSession,
} = {}) {
    if (!hasSession) return false;

    const activeIndex = WORKSPACE_ORDER.indexOf(activeWorkspaceName);
    const generationIndex = WORKSPACE_ORDER.indexOf('generation');
    const hasRecoveryState = ['error', 'cancelled'].includes(String(generationResultState || ''));
    // A hard stop is deliberately a one-way UI transition. The old terminal
    // run remains available in history, but its generation console cannot be
    // reopened from the step rail; the next run must start from voice config.
    if (isHardStoppedWorkflowSnapshot(generationSnapshot) && !generationActive) return false;
    return activeIndex >= generationIndex || generationActive || generationAccepted || hasRecoveryState;
}

function isWorkspaceNavigationAllowed(workspaceName) {
    const workspace = String(workspaceName || '');
    if (workspace === 'delivery' && isHistoryResultView()) {
        return Boolean(activeResultContext?.workspace || activeResultContext?.files?.length);
    }
    if (workspace === 'import') return true;
    if (!currentSession?.session_id) return false;
    if (workspace === 'review' || workspace === 'voice') return true;

    if (workspace === 'generation') {
        return generationWorkspaceNavigationAllowed();
    }

    if (workspace === 'delivery') {
        const snapshot = currentWorkspace?.snapshot || currentSession;
        const terminalResult = String(snapshot?.result_status || '');
        return Boolean(
            latestCurrentResultEvent
            || activeResultContext?.files?.length
            || generationResult === 'done'
            || (
                isTerminalWorkflowSnapshot(snapshot)
                && ['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(terminalResult)
            )
        );
    }

    return false;
}

function workspaceNavigationLockReason(workspaceName) {
    if (!currentSession?.session_id && workspaceName !== 'import') return '请先导入文档';
    if (workspaceName === 'generation') {
        if (isHardStoppedWorkflowSnapshot(currentWorkspace?.snapshot || currentSession)) {
            return '当前生成已停止，请从声音配置重新生成';
        }
        return '开始生成后才能进入生成任务';
    }
    if (workspaceName === 'delivery') return '完成任务后才能进入音频交付中心';
    return '';
}

function goToStep(step) {
    const returningFromTaskSubpage = step === 4 && isSystemInputSubpageView();
    const returningHistoryResult = returningFromTaskSubpage && isHistoryResultView();
    currentView = 'workflow';
    currentStep = step;
    const workspaceForStep = { 1: 'import', 2: 'voice', 3: 'generation', 4: 'delivery' }[step] || 'import';
    setActiveWorkspaceView(workspaceForStep);
    if (step === 4 && returningFromTaskSubpage) {
        rebuildAudioDeliveryPageForReturn({ historyResult: returningHistoryResult });
    } else if (step === 4 && currentWorkspace) {
        if (typeof renderWorkspaceAfterHydrate === 'function') {
            renderWorkspaceAfterHydrate(currentWorkspace, currentSession, currentSession?.session_id);
        } else {
            renderWorkspaceShell(currentWorkspace, currentSession);
        }
    }

    const historyNav = $('history-nav-btn');
    historyNav?.classList.remove('active');
    historyNav?.removeAttribute('aria-current');
    const versionNav = $('version-nav-btn');
    versionNav?.classList.remove('active');
    versionNav?.removeAttribute('aria-current');
    const backToHistoryBtn = $('back-to-history-btn');
    if (backToHistoryBtn) backToHistoryBtn.hidden = true;

    // 切换页面
    $$('.step-page').forEach(p => p.classList.remove('active'));
    $(`page-${step}`)?.classList.add('active');

    updateStepper();

    // Rendering the toolbar/workspace shell can be triggered by a late
    // snapshot while this transition is in progress. Re-apply the step view
    // after that render so page 2 never ends up with two hidden workspaces.
    setActiveWorkspaceView(workspaceForStep);
    if (step === 2 && currentSession) renderVoiceWorkspace();

    // 滚动到顶部
    const scrollPage = $(`page-${step}`)?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (step === 4) updateResultScrollTopButton();

    const heading = $(`page-${step}`)?.querySelector('h1:not([hidden])');
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));

    if (step === 4) {
        // 结果页在构建波形时仍处于 display:none；等待两帧，确保 WaveSurfer 获得正确容器宽度。
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
    }
}

// 交付是一个主阶段，但它有一个可选分支。把分支收进同一条窄轨道，
// 既让“系统录入”和“最终结果”有明确入口，也不会把主流程拉长成七段。
function deliveryStageWorkspaceContext() {
    if (typeof taskSubpageWorkspace === 'function') {
        const subpageWorkspace = taskSubpageWorkspace();
        if (subpageWorkspace) return subpageWorkspace;
    }
    return currentWorkspace || activeResultContext?.workspace || null;
}

function deliveryStageCurrent() {
    if (currentView === 'system-input-progress') return 'input';
    if (currentView === 'final-delivery') return 'result';
    return 'audio';
}

function deliveryStageInputHasStarted(systemInput = {}) {
    const acceptanceStatus = String(systemInput?.audio_acceptance?.status || '').trim().toLowerCase();
    const inputStatus = String(systemInput?.input_status || '').trim().toLowerCase();
    const run = systemInput?.input_run;
    return acceptanceStatus === 'accepted'
        || Boolean(run && typeof run === 'object')
        || ['pending_execute', 'running', 'succeeded', 'partial_success', 'failed_retryable', 'failed', 'needs_reconcile', 'ambiguous'].includes(inputStatus);
}

function deliveryStageResultIsReady(systemInput = {}) {
    const mode = String(systemInput?.delivery_mode || '').trim().toLowerCase();
    if (mode !== 'audio_and_input') return true;
    if (typeof systemInputRunIsComplete === 'function' && systemInputRunIsComplete(systemInput)) return true;

    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    const inputStatus = String(systemInput?.input_status || '').trim().toLowerCase();
    const terminalRun = ['SUCCEEDED', 'FAILED', 'PARTIAL_SUCCESS', 'AMBIGUOUS', 'CANCELLED', 'STOPPED'].includes(runStatus);
    const terminalInput = ['succeeded', 'partial_success', 'failed_retryable', 'failed', 'needs_reconcile', 'ambiguous'].includes(inputStatus);
    return Boolean(systemInput?.input_run) && (terminalRun || terminalInput);
}

function deliveryStageNavigationAllowed(stage, workspace = deliveryStageWorkspaceContext()) {
    const key = String(stage || '').trim().toLowerCase();
    if (!['audio', 'input', 'result'].includes(key)) return false;
    // A task subpage is already inside the delivery workspace. Returning to
    // the audio surface must remain available even while a late workspace
    // refresh briefly leaves the broader delivery gate stale.
    const isTaskSubpage = typeof isSystemInputSubpageView === 'function'
        && isSystemInputSubpageView();
    const canReturnToAudio = key === 'audio' && isTaskSubpage && activeWorkspace === 'delivery';
    if (!canReturnToAudio && !isWorkspaceNavigationAllowed('delivery')) return false;
    if (key === 'audio') return true;
    const systemInput = workspace?.system_input;
    if (key === 'result' && (!systemInput || systemInput.available === false)) return true;
    if (!systemInput || systemInput.available === false) return false;
    if (typeof systemInputDocumentEntryIsSupported === 'function'
        && !systemInputDocumentEntryIsSupported(workspace)) return key === 'result';
    if (key === 'input') {
        // The rail is a navigation aid, not a way to bypass the user's
        // explicit audio acceptance. Before that point the only valid entry
        // is the combined action on the audio-delivery card.
        return String(systemInput.delivery_mode || '').trim().toLowerCase() === 'audio_and_input'
            && deliveryStageInputHasStarted(systemInput);
    }
    // Final results are not meaningful until the optional system-input route
    // has either completed or reached an explicit terminal/reconciliation
    // state. Audio-only tasks keep their existing result-page entry point.
    return deliveryStageResultIsReady(systemInput);
}

function deliveryStageInputStatus(workspace, current = false) {
    const systemInput = workspace?.system_input;
    if (!systemInput || systemInput.available === false) {
        return { state: 'unavailable', label: '暂不可用', detail: '当前文档暂不支持系统录入' };
    }
    const supported = typeof systemInputDocumentEntryIsSupported !== 'function'
        || systemInputDocumentEntryIsSupported(workspace);
    if (!supported) {
        return { state: 'unavailable', label: '暂不可用', detail: systemInput.document_entry_support?.reason || '当前文档暂不支持系统录入' };
    }

    const mode = String(systemInput.delivery_mode || '').trim().toLowerCase();
    if (mode !== 'audio_and_input') {
        return { state: 'optional', label: current ? '可选' : '按需开启', detail: '音频完成后仍可开启系统录入' };
    }

    const inputStatus = String(systemInput.input_status || '').trim().toLowerCase();
    const runStatus = String(systemInput.input_run?.status || '').trim().toUpperCase();
    if (typeof systemInputRunIsComplete === 'function' && systemInputRunIsComplete(systemInput)) {
        return { state: 'complete', label: current ? '当前' : '已完成', detail: '所有录入单元均已返回明确结果' };
    }
    if (['needs_reconcile', 'ambiguous', 'failed_retryable', 'failed'].includes(inputStatus)
        || runStatus === 'AMBIGUOUS') {
        return { state: 'attention', label: inputStatus === 'failed_retryable' ? '可重试' : '待核验', detail: '录入结果需要继续处理' };
    }
    if (['PENDING', 'RUNNING'].includes(runStatus)
        || ['pending_execute', 'running'].includes(inputStatus)) {
        return { state: 'active', label: current ? '当前' : '进行中', detail: '系统录入正在执行或等待执行' };
    }
    return { state: 'waiting', label: current ? '当前' : '待开始', detail: '完成音频验收和录入配置后开始' };
}

function deliveryStageStatus(stage, workspace, currentStage) {
    const current = currentStage === stage;
    if (stage === 'audio') {
        return { state: current ? 'active' : 'complete', label: current ? '当前' : '已完成', detail: '试听、核验与下载音频' };
    }
    if (stage === 'input') return deliveryStageInputStatus(workspace, current);
    const ready = deliveryStageResultIsReady(workspace?.system_input || {});
    return {
        // “可查看” means the final result has been assembled and is a
        // completed delivery milestone, even when the user is still on the
        // audio surface. Keep the current page blue, but do not show a ready
        // result as if it were still waiting for work.
        state: current ? 'active' : ready ? 'complete' : 'locked',
        label: current ? '当前' : ready ? '可查看' : '待录入',
        detail: ready ? '汇总音频与系统录入结果' : '完成系统录入后才能查看最终结果',
    };
}

function updateDeliveryStageRail(workspaceOverride = null) {
    const rail = $('delivery-stage-rail');
    if (!rail) return;
    const isSubpage = typeof isSystemInputSubpageView === 'function' && isSystemInputSubpageView();
    const canReturnToAudio = isSubpage && activeWorkspace === 'delivery';
    const visible = (currentView === 'workflow' || currentView === 'history-result' || isSubpage)
        && activeWorkspace === 'delivery'
        && (isWorkspaceNavigationAllowed('delivery') || canReturnToAudio);
    rail.hidden = !visible;
    rail.setAttribute('aria-hidden', visible ? 'false' : 'true');
    if (!visible) return;

    // A subpage can be opened from a freshly fetched history/projection
    // workspace before that projection becomes currentWorkspace. Accept the
    // rendered target explicitly so the rail never lags behind the page body.
    const workspace = workspaceOverride || deliveryStageWorkspaceContext();
    const currentStage = deliveryStageCurrent();
    const stages = ['audio', 'input', 'result'];
    const stageButtons = rail.querySelectorAll('[data-delivery-stage]');
    stageButtons.forEach(button => {
        const stage = String(button.dataset.deliveryStage || '');
        const status = deliveryStageStatus(stage, workspace, currentStage);
        const canNavigate = deliveryStageNavigationAllowed(stage, workspace);
        button.classList.remove('is-current', 'is-active', 'is-complete', 'is-optional', 'is-attention', 'is-waiting', 'is-unavailable', 'is-locked');
        const visualState = !canNavigate && stage !== currentStage && status.state !== 'unavailable'
            ? 'locked'
            : status.state;
        button.classList.add(`is-${visualState}`);
        button.classList.toggle('is-current', stage === currentStage);
        button.disabled = !canNavigate;
        button.setAttribute('aria-disabled', canNavigate ? 'false' : 'true');
        if (stage === currentStage) button.setAttribute('aria-current', 'step');
        else button.removeAttribute('aria-current');
        const stateNode = button.querySelector('.delivery-stage-state');
        if (stateNode) stateNode.textContent = status.label;
        button.setAttribute('aria-label', `${button.querySelector('.delivery-stage-copy strong')?.textContent?.replace(/\s*可选\s*$/, '') || stage}：${status.label}`);
        if (!canNavigate) {
            button.title = stage === 'audio'
                ? workspaceNavigationLockReason('delivery')
                : (status.detail || '当前不能打开这个交付阶段');
        } else if (stage !== currentStage) {
            button.title = `打开${button.querySelector('.delivery-stage-copy strong')?.textContent?.replace(/\s*可选\s*$/, '') || '交付阶段'}`;
        } else {
            button.removeAttribute('title');
        }
    });

    rail.querySelectorAll('[data-delivery-line]').forEach(line => {
        const lineName = String(line.dataset.deliveryLine || '');
        const lineIndex = { 'audio-input': 0, 'input-result': 1 }[lineName];
        line.classList.toggle('is-active', Number.isInteger(lineIndex) && lineIndex < stages.indexOf(currentStage));
    });

    const note = $('delivery-stage-rail-note');
    if (note) {
        const inputStatus = deliveryStageInputStatus(workspace, false);
        const mode = String(workspace?.system_input?.delivery_mode || '').trim().toLowerCase();
        note.textContent = inputStatus.state === 'unavailable'
            ? '当前文档暂不支持系统录入'
            : mode === 'audio_and_input'
                ? '已选择录入 · 音频核验后继续'
                : '录入为可选步骤 · 音频完成后仍可开启';
    }
}

function navigateDeliveryStage(stage) {
    const key = String(stage || '').trim().toLowerCase();
    const isTaskSubpage = typeof isSystemInputSubpageView === 'function'
        && isSystemInputSubpageView();
    if (key === 'audio' && isTaskSubpage && activeWorkspace === 'delivery') {
        returnFromTaskSubpage();
        return true;
    }
    const workspace = deliveryStageWorkspaceContext();
    if (!deliveryStageNavigationAllowed(key, workspace)) {
        const inputStatus = key === 'input' ? deliveryStageInputStatus(workspace, false) : null;
        showToast(inputStatus?.detail || workspaceNavigationLockReason('delivery') || '当前不能打开这个交付阶段', 'warning');
        return false;
    }
    if (key === 'audio') {
        returnFromTaskSubpage();
        return true;
    }
    if (key === 'input') {
        return showSystemInputProgressPage({ workspace, refresh: false });
    }
    return showFinalDeliveryPage({ workspace, refresh: false });
}

function updateStepper() {
    const activeIndex = Math.max(0, WORKSPACE_ORDER.indexOf(activeWorkspace));
    const isSubpage = isSystemInputSubpageView();
    const isHistorySubpage = isSubpage && isHistoryResultView();
    const isHistoryDeliverySurface = currentView === 'history-result' && activeWorkspace === 'delivery';
    const isWorkflowSurface = currentView === 'workflow' || isSubpage || isHistoryDeliverySurface;
    $$('.step-indicator').forEach(el => {
        const workspace = el.dataset.workspace || 'import';
        const index = WORKSPACE_ORDER.indexOf(workspace);
        el.classList.remove('active', 'completed');
        el.removeAttribute('aria-current');
        const isAccessible = isWorkspaceNavigationAllowed(workspace);
        el.disabled = !isAccessible;
        el.setAttribute('aria-disabled', isAccessible ? 'false' : 'true');
        if (!isAccessible) {
            const lockReason = workspaceNavigationLockReason(workspace);
            if (lockReason) el.title = lockReason;
        } else {
            el.removeAttribute('title');
        }
        if (isWorkflowSurface && index >= 0 && index < activeIndex && isAccessible) {
            el.classList.add('completed');
        } else if (workspace === activeWorkspace && isWorkflowSurface) {
            el.classList.add('active');
            el.setAttribute('aria-current', 'step');
        }
    });

    $$('.step-line').forEach(el => {
        const line = String(el.dataset.line || '');
        const lineIndex = { '1': 0, 'review-voice': 1, 'voice-generation': 2, 'generation-delivery': 3 }[line];
        el.classList.toggle('active', isWorkflowSurface && Number.isInteger(lineIndex) && lineIndex < activeIndex);
    });

    // On narrow windows the workflow rail is intentionally horizontally
    // scrollable. Keep the active step fully visible when a transition moves
    // from the first steps to delivery; otherwise only the edge of the step
    // indicator is visible while the toolbar already reports the new step.
    const stepper = $('stepper');
    const activeStep = stepper?.querySelector('.step-indicator.active');
    if (stepper && activeStep && stepper.scrollWidth > stepper.clientWidth) {
        const inset = 8;
        const stepperRect = stepper.getBoundingClientRect();
        const activeRect = activeStep.getBoundingClientRect();
        // getBoundingClientRect() reflects the current scroll position. Add
        // it back so the comparison below stays in the stepper's content
        // coordinate system when moving both forwards and backwards.
        const stepLeft = activeRect.left - stepperRect.left + stepper.scrollLeft;
        const stepRight = stepLeft + activeStep.offsetWidth;
        const visibleLeft = stepper.scrollLeft + inset;
        const visibleRight = stepper.scrollLeft + stepper.clientWidth - inset;
        if (stepLeft < visibleLeft) {
            stepper.scrollLeft = Math.max(0, stepLeft - inset);
        } else if (stepRight > visibleRight) {
            stepper.scrollLeft = Math.min(
                stepper.scrollWidth - stepper.clientWidth,
                stepRight - stepper.clientWidth + inset,
            );
        }
    }

    const toolbarStep = $('toolbar-step');
    const toolbarContextLabel = $('toolbar-context-label');
    const taskBadge = $('task-status-badge');
    const toolbarCounts = $('toolbar-counts');
    const toolbarDocument = $('toolbar-document');
    const isTaskContext = currentView === 'workflow' || isSubpage;
    const isCurrentTaskContext = currentView === 'workflow' || (isSubpage && !isHistorySubpage);
    if (toolbarContextLabel) toolbarContextLabel.textContent = isTaskContext
        ? (isHistorySubpage ? '历史任务' : '当前任务')
        : (currentView === 'version' ? '应用' : '任务中心');
    if (taskBadge) taskBadge.hidden = !isCurrentTaskContext;
    if (toolbarCounts) toolbarCounts.hidden = !isCurrentTaskContext;
    if (toolbarDocument) toolbarDocument.hidden = !isCurrentTaskContext || !toolbarDocument.textContent.trim();
    if (toolbarStep) {
        const subpageTitle = currentView === 'system-input-progress'
            ? '系统录入'
            : currentView === 'final-delivery'
                ? '最终结果'
                : '';
        toolbarStep.textContent = isTaskContext
            ? `${String(activeIndex + 1).padStart(2, '0')} / ${subpageTitle || WORKSPACE_TITLES[activeWorkspace] || STEP_TITLES[currentStep] || ''}`
            : (currentView === 'version' ? '版本中心' : '历史记录');
    }
    updateDeliveryStageRail();
    // A completed task opened from history owns its result-page workspace in
    // activeResultContext. Re-rendering the empty current workflow here would
    // hide the dynamically mounted system-input delivery card immediately
    // after buildResultPage has populated it.
    if (!isHistoryResultView() && currentView !== 'history-result') {
        if (typeof renderWorkspaceAfterHydrate === 'function') {
            renderWorkspaceAfterHydrate(currentWorkspace, currentSession, currentSession?.session_id);
        } else {
            renderWorkspaceShell(currentWorkspace, currentSession);
        }
    }
    if (isSubpage && isHistoryResultView()) renderSystemInputSubpage();
}

function setHistoryNavActive(active) {
    const historyNav = $('history-nav-btn');
    if (!historyNav) return;
    historyNav.classList.toggle('active', active);
    if (active) historyNav.setAttribute('aria-current', 'page');
    else historyNav.removeAttribute('aria-current');
}

function setVersionNavActive(active) {
    const versionNav = $('version-nav-btn');
    if (!versionNav) return;
    versionNav.classList.toggle('active', active);
    if (active) versionNav.setAttribute('aria-current', 'page');
    else versionNav.removeAttribute('aria-current');
}

function activateStandalonePage(pageId, view) {
    currentView = view;
    if (pageId === 'page-4' && view === 'history-result') setActiveWorkspaceView('delivery');
    $$('.step-page').forEach(page => page.classList.remove('active'));
    const page = $(pageId);
    page?.classList.add('active');
    setHistoryNavActive(view === 'history');
    setVersionNavActive(view === 'version');
    updateStepper();

    const scrollPage = page?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (pageId === 'page-4') updateResultScrollTopButton();
    const heading = page?.querySelector('h1');
    if (heading && !isForcedUpdateBlocking()) {
        requestAnimationFrame(() => heading.focus({ preventScroll: true }));
    }
}

function activateTaskSubpage(pageId, view) {
    currentView = view;
    currentStep = 4;
    setActiveWorkspaceView('delivery');
    const historyNav = $('history-nav-btn');
    historyNav?.classList.remove('active');
    historyNav?.removeAttribute('aria-current');
    const versionNav = $('version-nav-btn');
    versionNav?.classList.remove('active');
    versionNav?.removeAttribute('aria-current');
    $$('.step-page').forEach(page => page.classList.remove('active'));
    const page = $(pageId);
    page?.classList.add('active');
    updateStepper();
    const scrollPage = page?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    const heading = page?.querySelector('h1');
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));
}

function taskSubpageWorkspace(workspace = null) {
    return finalDeliveryContextWorkspace(workspace)
        || systemInputInteractionWorkspace(workspace)
        || currentWorkspace;
}

function refreshTaskSubpageWorkspace(workspace, { refresh = true } = {}) {
    if (!refresh) return;
    const workflowId = String(workspace?.snapshot?.workflow_id || currentSession?.session_id || '').trim();
    if (!workflowId) return;
    if (isHistoryResultView()) {
        void refreshHistoryResultWorkspace(activeResultContext, { silent: true });
    } else {
        void hydrateWorkflowWorkspace(workflowId, { silent: true });
    }
}

function showSystemInputProgressPage({ workspace = null, refresh = true } = {}) {
    const target = taskSubpageWorkspace(workspace);
    if (!target?.system_input || target.system_input.available === false) {
        showToast('当前任务没有可继续录入的系统目标', 'warning');
        return false;
    }
    if (!systemInputDocumentEntryIsSupported(target)) {
        showToast(target.system_input.document_entry_support?.reason || '当前文档暂不支持系统录入', 'warning');
        return false;
    }
    destroyWaveSurfers();
    activateTaskSubpage('page-system-input', 'system-input-progress');
    renderSystemInputProgressPage(target);
    updateDeliveryStageRail(target);
    refreshTaskSubpageWorkspace(target, { refresh });
    return true;
}

function showFinalDeliveryPage({ workspace = null, refresh = false } = {}) {
    const target = taskSubpageWorkspace(workspace);
    if (!target && !activeResultContext && !currentSession) {
        showToast('当前没有可展示的交付结果', 'warning');
        return false;
    }
    destroyWaveSurfers();
    activateTaskSubpage('page-final-delivery', 'final-delivery');
    renderFinalDeliveryPage(target);
    updateDeliveryStageRail(target);
    refreshTaskSubpageWorkspace(target, { refresh });
    return true;
}

function returnFromTaskSubpage() {
    if (isHistoryResultView()) {
        activateStandalonePage('page-4', 'history-result');
        rebuildAudioDeliveryPageForReturn({ historyResult: true });
        const backToHistoryBtn = $('back-to-history-btn');
        if (backToHistoryBtn) backToHistoryBtn.hidden = false;
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
        return;
    }
    goToStep(4);
}

function showHistoryPage({ refresh = true } = {}) {
    if (isRestarting) return;
    if (currentView === 'workflow' || isSystemInputSubpageView()) {
        historyReturnStep = generationResult === 'done' && latestCurrentResultEvent ? 4 : currentStep;
    }
    destroyWaveSurfers();
    activateStandalonePage('page-history', 'history');
    const backToHistoryBtn = $('back-to-history-btn');
    if (backToHistoryBtn) backToHistoryBtn.hidden = true;
    const historyBackBtn = $('history-back-btn');
    if (historyBackBtn) historyBackBtn.textContent = currentSession ? '返回当前任务' : '返回导入文档';
    if (refresh) void refreshHistoryRecords();
    else renderHistoryRecords(historyRecords);
}

function showVersionPage({ fromUpdate = false } = {}) {
    if (isRestarting) return;
    destroyWaveSurfers();
    activateStandalonePage('page-version', 'version');
    renderVersionCenter();
    if (!fromUpdate && updateState.status === 'idle' && isElectron) {
        void runUpdateAction('check', $('version-check-btn'));
    }
}

function returnToWorkflow() {
    const returnStep = currentSession ? historyReturnStep : 1;
    if (returnStep === 4 && latestCurrentResultEvent && currentSession) {
        buildResultPage(latestCurrentResultEvent);
    }
    goToStep(returnStep);
}


registerRendererModule("app.navigation", {
    updateResultScrollTopButton,
    scrollResultToTop,
    generationWorkspaceNavigationAllowed,
    isWorkspaceNavigationAllowed,
    workspaceNavigationLockReason,
    goToStep,
    updateStepper,
    deliveryStageInputHasStarted,
    deliveryStageResultIsReady,
    deliveryStageInputStatus,
    deliveryStageStatus,
    updateDeliveryStageRail,
    deliveryStageNavigationAllowed,
    navigateDeliveryStage,
    setHistoryNavActive,
    setVersionNavActive,
    activateStandalonePage,
    activateTaskSubpage,
    taskSubpageWorkspace,
    refreshTaskSubpageWorkspace,
    showSystemInputProgressPage,
    showFinalDeliveryPage,
    returnFromTaskSubpage,
    showHistoryPage,
    showVersionPage,
    returnToWorkflow,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
