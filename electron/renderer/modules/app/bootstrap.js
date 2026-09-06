/** Renderer module: app.bootstrap */
(function attachRendererFeature_app_bootstrap(root) {
    'use strict';

// ============================================================================
// 初始化
// ============================================================================

function applyPerformanceMode() {
    const cores = Number(navigator.hardwareConcurrency || 0);
    const memory = Number(navigator.deviceMemory || 0);
    const lowPerformance = (cores > 0 && cores <= 4) || (memory > 0 && memory <= 4);
    document.documentElement.classList.toggle('low-performance', lowPerformance);
}

function resetActivePageScroll() {
    const activePage = document.querySelector('.step-page.active');
    const scrollRoot = activePage?.querySelector('.page-scroll, .page-center');
    if (!scrollRoot) return;
    scrollRoot.scrollTop = 0;
    scrollRoot.scrollLeft = 0;
}

function pinReviewActionsToWindow() {
    const actions = document.querySelector('.review-actions');
    const reviewPage = $('page-2');
    if (!actions || !reviewPage || actions.parentElement === reviewPage) return;
    // Keep the dock outside `.page-scroll`; the CSS fixed positioning then
    // remains tied to the application window instead of the document flow.
    reviewPage.appendChild(actions);
}

async function init() {
    initializeTheme();
    applyPerformanceMode();
    if (platform === 'darwin') {
        document.body.classList.add('platform-darwin');
    } else if (platform === 'win32') {
        document.body.classList.add('platform-win32');
    }

    bindNativeAppNotices();
    void bindNativeAppUpdates();
    pinReviewActionsToWindow();
    bindEvents();

    // 初始化预设 UI
    refreshPresetUI();
    window.WordTTSUI?.enhanceSelects(document);
    // 输出格式是产品固定约束：先清理任何旧版/异常页面残留的格式选项，
    // 再恢复当前配置，避免 MP3 选项缺失时自定义下拉框把第一项显示成 WAV。
    enforceOutputCompatibility();
    const savedConfig = loadCurrentConfig();
    if (savedConfig) {
        applyConfigToForm(savedConfig, { includeRoles: false });
    } else {
        rememberCurrentConfig();
    }

    const connected = await connectService(isElectron);
    updateStepper();
    updateConfigSummary();
    if (connected) {
        await refreshHistoryRecords({ showLoading: false });
        // Startup is intentionally passive: persisted workspaces remain
        // discoverable in history, but opening the app never changes the
        // current page or adopts a task without an explicit user action.
        renderActiveCandidateHint(activeWorkflowCandidates);
    }
    resetActivePageScroll();
}

function bindEvents() {
    // Task subpages are mounted after the initial workflow shell is built.
    // Delegate their back affordance so the action stays live even when a
    // late workspace render replaces the heading contents.
    document.addEventListener('click', event => {
        const target = event.target?.closest?.('.task-subpage-back') || null;
        if (!target) return;
        event.preventDefault();
        returnFromTaskSubpage();
    });
    document.addEventListener('click', event => {
        const target = event.target instanceof Element
            ? event.target.closest('[data-copy-system-input-document]')
            : null;
        if (!target || target.disabled) return;
        event.preventDefault();
        if (target.dataset.copySystemInputDocument === 'all') {
            const scope = target.closest('.final-delivery-entries-panel') || document;
            void copySystemInputDocumentNames(scope, target);
            return;
        }
        void copySystemInputDocumentName(target.dataset.copyText || '', target);
    });

    $$('[data-workspace]').forEach((entry) => {
        const activate = () => {
            const workspace = entry.dataset.workspace || '';
            if (!isWorkspaceNavigationAllowed(workspace)) {
                const lockReason = workspaceNavigationLockReason(workspace);
                if (lockReason) showToast(lockReason);
                return;
            }
            if (workspace === 'import') goToStep(1);
            else if (workspace === 'review') showContentReview();
            else if (workspace === 'voice') goToStep(2);
            else if (workspace === 'generation') goToStep(3);
            else if (workspace === 'delivery') goToStep(4);
        };
        entry.addEventListener('click', activate);
        entry.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                activate();
            }
        });
    });
    $$('[data-delivery-stage]').forEach((entry) => {
        const activate = () => {
            navigateDeliveryStage(entry.dataset.deliveryStage || '');
        };
        entry.addEventListener('click', activate);
        entry.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                activate();
            }
        });
    });
    $('theme-toggle')?.addEventListener('click', () => setWorkspaceTheme(themePreference === 'dark' ? 'light' : 'dark'));

    // 重新开始按钮（工具栏）
    $('restart-btn').addEventListener('click', requestRestart);
    $('retry-service-btn').addEventListener('click', async () => {
        const connected = await connectService(true);
        if (connected) await refreshHistoryRecords({ showLoading: currentView === 'history' });
    });
    $('provider-action-btn')?.addEventListener('click', async () => {
        const button = $('provider-action-btn');
        if (button) {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        }
        try {
            const connected = await connectService(true);
            if (connected && currentSession?.session_id) {
                await hydrateWorkflowWorkspace(currentSession.session_id, { silent: false });
            }
        } finally {
            if (button) {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            }
            renderProviderStatus();
        }
    });
    $('history-nav-btn').addEventListener('click', () => showHistoryPage());
    $('version-nav-btn')?.addEventListener('click', () => showVersionPage());
    $('import-history-link')?.addEventListener('click', () => showHistoryPage());
    $('back-to-history-btn').addEventListener('click', () => showHistoryPage());
    $('history-back-btn').addEventListener('click', returnToWorkflow);
    $('history-start-btn').addEventListener('click', returnToWorkflow);
    $('final-delivery-back-bottom-btn')?.addEventListener('click', returnFromTaskSubpage);
    $('final-delivery-new-file-btn')?.addEventListener('click', requestRestart);
    $('final-view-audio-btn')?.addEventListener('click', returnFromTaskSubpage);
    $('final-download-audio-btn')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        if (!button || button.disabled || button.getAttribute('aria-busy') === 'true') return;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            await downloadZip(activeResultContext);
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            renderFinalDeliveryPage(taskSubpageWorkspace());
        }
    });
    $('system-input-page-accept-btn')?.addEventListener('click', () => {
        void runSystemInputDeliveryAction('ACCEPT_AUDIO');
    });
    $('system-input-page-start-btn')?.addEventListener('click', () => {
        const workspace = systemInputInteractionWorkspace();
        const systemInput = workspace?.system_input;
        const inputStatus = String(systemInput?.input_status || '').trim().toLowerCase();
        if (systemInputRunIsComplete(systemInput) || inputStatus === 'succeeded') {
            showFinalDeliveryPage({ workspace, refresh: false });
            return;
        }
        if (inputStatus === 'not_enabled' && !systemInput.input_run) {
            if (systemInput.audio_acceptance?.status === 'accepted') {
                // The progress page only confirms, starts, and tracks. Return
                // to the delivery center before opening the shared editor so
                // configuration never becomes a hidden progress-page action.
                returnFromTaskSubpage();
                openSystemInputConfigDrawer(systemInputInteractionWorkspace(), { deliveryMode: 'audio_and_input' });
            } else {
                showToast('请先确认整批音频，再开始系统录入', 'warning');
            }
            return;
        }
        const startAction = workspaceAction('START_INPUT', workspace);
        if (startAction?.enabled !== true) {
            showToast(startAction?.reason || '当前不能开始系统录入', 'warning');
            return;
        }
        void runSystemInputDeliveryAction('START_INPUT');
    });
    $('system-input-page-pause-btn')?.addEventListener('click', () => {
        void performSystemInputRunControl('pause');
    });
    $('system-input-page-resume-btn')?.addEventListener('click', () => {
        void performSystemInputRunControl('resume');
    });
    $('system-input-page-stop-btn')?.addEventListener('click', () => {
        void performSystemInputRunControl('stop');
    });
    $('system-input-reconcile-not-submitted-btn')?.addEventListener('click', () => {
        void resolveSystemInputAmbiguity('NOT_SUBMITTED');
    });
    $('system-input-reconcile-verify-btn')?.addEventListener('click', () => {
        void runSystemInputVerification();
    });
    $('version-check-btn')?.addEventListener('click', event => { void runUpdateAction('check', event.currentTarget); });
    $('version-download-btn')?.addEventListener('click', event => { void runUpdateAction('download', event.currentTarget); });
    $('version-install-btn')?.addEventListener('click', event => { void runUpdateAction('install', event.currentTarget); });
    $('version-open-release-btn')?.addEventListener('click', event => { void openUpdateReleasePage(event.currentTarget); });
    $('update-required-download')?.addEventListener('click', event => { void runUpdateAction('download', event.currentTarget); });
    $('update-required-install')?.addEventListener('click', event => { void runUpdateAction('install', event.currentTarget); });
    $('update-required-retry')?.addEventListener('click', event => { void runUpdateAction('check', event.currentTarget); });
    $('update-required-open-release')?.addEventListener('click', event => { void openUpdateReleasePage(event.currentTarget); });
    $('history-search-input')?.addEventListener('input', event => {
        historyFilters.query = String(event.target.value || '').trim().toLocaleLowerCase('zh-CN');
        renderHistoryRecords(historyRecords);
    });
    $('history-status-filter')?.addEventListener('change', event => {
        historyFilters.status = String(event.target.value || 'all');
        renderHistoryRecords(historyRecords);
    });
    $('history-sort-order')?.addEventListener('change', event => {
        historyFilters.sort = String(event.target.value || 'updated');
        renderHistoryRecords(historyRecords);
    });

    $$('[data-log-filter]').forEach(button => {
        button.addEventListener('click', () => setLogFilter(button.dataset.logFilter || 'all'));
    });
    $('log-follow-btn').addEventListener('click', () => {
        setLogAutoFollow(!logAutoFollow, { scrollToEnd: !logAutoFollow });
    });
    $('log-new-records-btn').addEventListener('click', () => {
        setLogAutoFollow(true, { scrollToEnd: true });
    });
    $('log-toggle-btn').addEventListener('click', () => {
        setLogDetailsExpanded($('log-panel').classList.contains('is-collapsed'));
    });
    $('progress-log').addEventListener('scroll', () => {
        if (!logAutoFollow) return;
        const body = $('progress-log');
        if (body.scrollHeight - body.scrollTop - body.clientHeight > 64) {
            setLogAutoFollow(false);
        }
    }, { passive: true });
    const resultScrollPage = $('page-4')?.querySelector('.page-scroll');
    resultScrollPage?.addEventListener('scroll', updateResultScrollTopButton, { passive: true });
    $('result-scroll-top')?.addEventListener('click', scrollResultToTop);

    // Step 1: 上传
    const uploadZone = $('upload-zone');
    uploadZone.addEventListener('click', selectFile);
    uploadZone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            selectFile();
        }
    });
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (isRestarting || isForcedUpdateBlocking() || uploadZone.getAttribute('aria-disabled') === 'true') return;
        uploadZone.classList.add('dragover');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('dragover');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        setGlobalFileDropActive(false);
        uploadZone.classList.remove('dragover');
        if (isRestarting || isForcedUpdateBlocking()) return;
        const file = e.dataTransfer.files[0];
        void handleIncomingSourceFile(file);
    });

    // 全局文件拖拽：无论用户当前在哪个步骤，都先拦截系统默认打开行为，
    // 显示统一导入层，并把文件交给同一条“新任务”确认/导入链路。
    window.addEventListener('dragenter', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        setGlobalFileDropActive(true);
    });
    window.addEventListener('dragover', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        setGlobalFileDropActive(true);
    });
    window.addEventListener('dragleave', event => {
        if (!isFileDragEvent(event)) return;
        if (!event.relatedTarget) setGlobalFileDropActive(false);
    });
    window.addEventListener('drop', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        setGlobalFileDropActive(false);
        const file = event.dataTransfer?.files?.[0];
        void handleIncomingSourceFile(file);
    });

    $('cancel-import-btn')?.addEventListener('click', () => { void cancelSourceImport(); });

    // 隐藏的 file input（浏览器模式）
    $('hidden-file-input').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) handleFileSelected(file);
        // 重置 value 以便重复选择同一文件时仍能触发 change 事件
        e.target.value = '';
    });

    // Step 2: 预设管理
    $('save-preset-btn').addEventListener('click', handleSavePreset);
    $('apply-preset-btn').addEventListener('click', handleApplyPreset);
    $('delete-preset-btn').addEventListener('click', handleDeletePreset);

    // Step 2: 配置
    bindVoiceWorkspaceEvents();
    $('format').addEventListener('change', (e) => {
        enforceOutputCompatibility();
        rememberCurrentConfig();
    });
    $('quality').addEventListener('change', (e) => {
        updateConfigSummary();
        rememberCurrentConfig();
    });
    $('preview').addEventListener('change', () => {
        updateConfigSummary();
        rememberCurrentConfig();
    });
    $$('input[name="generation-mode"]').forEach(input => {
        input.addEventListener('change', () => {
            updateGenerationModeUI(selectedGenerationMode());
            updateConfigSummary();
            rememberCurrentConfig();
        });
    });
    $('change-file-btn').addEventListener('click', requestRestart);
    $('back-to-upload-btn').addEventListener('click', requestRestart);
    $('retry-generation-btn').addEventListener('click', () => {
        void retryGenerationFromRecovery();
    });
    $('return-config-btn').addEventListener('click', () => {
        void returnToConfigSafely();
    });
    $('cancel-generation-btn')?.addEventListener('click', async () => {
        if (!currentSession || cancelWorkflowPromise) return;
        const session = currentSession;
        const button = $('cancel-generation-btn');
        if (button) button.disabled = true;
        hardStopNavigationRequested = true;
        try {
            const snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-user-cancel',
            });
            if (isHardStoppedWorkflowSnapshot(snapshot)) {
                resetGenerationAfterHardStop(session, snapshot);
            } else if (isTerminalWorkflowSnapshot(snapshot)) {
                // The provider won a completion race before the stop fence;
                // retain the normal terminal result instead of pretending it
                // was cancelled.
                applyCancellationOutcome(session, snapshot);
            } else {
                showToast('停止请求未立即返回终态，请刷新任务', 'warning');
            }
        } catch (error) {
            console.error('停止生成失败:', error);
            showToast(`停止生成失败：${error.message || '请稍后重试'}`, 'error');
            $('status-text').textContent = '停止请求未完成，请再次点击“停止生成”';
        } finally {
            hardStopNavigationRequested = false;
            updateGenerationCancelUI();
        }
    });
    $('pause-generation-btn')?.addEventListener('click', () => {
        void runFreshWorkspaceAction('PAUSE');
    });
    $('resume-generation-btn')?.addEventListener('click', () => {
        void runFreshWorkspaceAction('RESUME');
    });
    $('review-next-btn')?.addEventListener('click', () => {
        if (!currentSession) return;
        goToStep(2);
        showToast('内容已确认，请进入配置中心');
    });
    $('review-reprocess-btn')?.addEventListener('click', () => { void requestRestart(); });
    $('skip-config-btn').addEventListener('click', () => {
        applyConfigToForm(collectConfig(true), { includeRoles: true });
        showToast('已恢复推荐设置');
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

    $('audio-search-input').addEventListener('input', scheduleAudioFilter);
    $('audio-type-filter').addEventListener('change', scheduleAudioFilter);

    // Step 4: 下载
    $('download-zip-btn').addEventListener('click', async () => {
        const button = $('download-zip-btn');
        if (button.disabled) return;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            await downloadZip();
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    });
    $('cancel-artifact-transfer-btn')?.addEventListener('click', () => { void cancelArtifactTransfer(); });
    $('generate-full-btn').addEventListener('click', () => {
        if (!lastGenerationConfig) return;
        destroyWaveSurfers();
        applyConfigToForm({ ...lastGenerationConfig, preview: false }, { includeRoles: true });
        goToStep(2);
        showToast('已保留试听设置，确认后可生成完整文档');
    });
    $('result-return-config-btn').addEventListener('click', async () => {
        destroyWaveSurfers();
        if (lastGenerationConfig) applyConfigToForm(lastGenerationConfig, { includeRoles: true });
        const moved = await returnToConfigSafely({ buttonId: 'result-return-config-btn' });
        if (moved) showToast('已返回配置；修改参数后会重新生成全部内容');
    });
    $('rerun-task-btn')?.addEventListener('click', () => {
        const action = workflowAdapter.action?.(activeResultContext?.workspace || currentWorkspace, 'RERUN');
        if (!action) return;
        if (activeResultContext?.mode === 'history') void rerunResultContext(activeResultContext);
        else void performWorkspaceAction(action);
    });
    $('retry-failed-btn').addEventListener('click', () => { void retryFailedItems(); });
    $('new-file-btn').addEventListener('click', requestRestart);
}


registerRendererModule("app.bootstrap", {
    applyPerformanceMode,
    resetActivePageScroll,
    pinReviewActionsToWindow,
    init,
    bindEvents,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
