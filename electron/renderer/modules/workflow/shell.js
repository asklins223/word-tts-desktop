/** Renderer module: workflow.shell */
(function attachRendererFeature_workflow_shell(root) {
    'use strict';

function workspaceProgress(workspace = null) {
    const sourceWorkspace = workspace || authoritativeWorkspace();
    if (typeof workflowReducer.normalizeProgress === 'function') {
        return workflowReducer.normalizeProgress(sourceWorkspace?.progress, sourceWorkspace?.items, sourceWorkspace?.artifacts);
    }
    return sourceWorkspace?.progress || { total: 0, completed: 0, failed: 0, cancelled: 0, skipped: 0, pending: 0, deliverable: 0, percent: 0, deliverable_percent: 0 };
}

function renderWorkspaceProgress(workspace = currentWorkspace, state = null) {
    const progress = workspaceProgress(workspace);
    const total = Math.max(0, Number(progress.total) || 0);
    const completed = Math.max(0, Number(progress.completed) || 0);
    const deliverable = Math.max(0, Number(progress.deliverable) || 0);
    const pending = Math.max(0, Number(progress.pending) || 0);
    if ($('progress-completed')) $('progress-completed').textContent = String(completed);
    if ($('progress-remaining')) $('progress-remaining').textContent = total > 0 ? String(pending) : '—';
    if ($('progress-failed')) $('progress-failed').textContent = String(Math.max(0, Number(progress.failed) || 0));
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(Math.max(0, Number(progress.cancelled) || 0));
    if ($('progress-skipped')) $('progress-skipped').textContent = String(Math.max(0, Number(progress.skipped) || 0));
    if ($('progress-deliverable')) $('progress-deliverable').textContent = total > 0
        ? `${Math.max(0, Number(progress.deliverable) || 0)} / ${total}`
        : '0 / 0';
    // Live provider segment progress is more granular during an active run;
    // only let the item projection own the bar before/after that run.
    const terminal = Boolean(state?.terminal) || isTerminalWorkflowSnapshot(workspace?.snapshot || workspace);
    if (total > 0 && (!isGenerating || terminal)) {
        const percent = terminal
            ? terminalProgressPercent(deliverable, total)
            : Math.min(99, Math.max(0, Number(progress.percent) || 0));
        setProgressBarPercent(percent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(percent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', terminal ? `${percent}% 可交付` : `${percent}% 处理中`);
        $('progress-percent').textContent = String(percent);
        setProgressReadoutMode(terminal, terminal && (progress.failed > 0 || progress.cancelled > 0 || progress.skipped > 0));
    }
}

function setWorkspaceTheme(theme, { persist = true } = {}) {
    const next = theme === 'dark' ? 'dark' : 'light';
    themePreference = next;
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    const toggle = $('theme-toggle');
    if (toggle) {
        const dark = next === 'dark';
        toggle.setAttribute('aria-label', dark ? '切换浅色模式' : '切换深色模式');
        toggle.title = dark ? '切换浅色模式' : '切换深色模式';
        toggle.setAttribute('aria-pressed', dark ? 'true' : 'false');
    }
    if (persist) {
        try { rendererStorage?.setItem('wordtts_theme_preference', next); } catch (_) { /* ignore */ }
    }
}

function initializeTheme() {
    let stored = '';
    try { stored = rendererStorage?.getItem('wordtts_theme_preference') || ''; } catch (_) { /* ignore */ }
    const systemPrefersDark = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const initial = stored === 'dark' || stored === 'light'
        ? stored
        : (systemPrefersDark ? 'dark' : 'light');
    // Do not persist the system-derived default.  Only an explicit user
    // toggle becomes a local UI preference, so later OS theme changes remain
    // visible until the user chooses a mode.
    setWorkspaceTheme(initial, { persist: stored === 'dark' || stored === 'light' });
}

function setServiceState(state, label) {
    const service = $('service-state');
    if (!service) return;
    service.classList.remove('is-ready', 'is-warning', 'is-error');
    if (state) service.classList.add(`is-${state}`);
    const labelEl = service.querySelector('.service-label');
    if (labelEl) labelEl.textContent = label;
    renderProviderStatus();
}

function summarizeParseResults(parseResults) {
    if (!Array.isArray(parseResults)) return { total: 0, types: [] };
    const types = [...new Set(parseResults.flatMap(item => {
        if (Array.isArray(item?.content_types) && item.content_types.length) {
            return item.content_types;
        }
        return [item?.content_type || item?.doc_type];
    }).filter(Boolean))];
    const total = parseResults.reduce((sum, item) => {
        const count = Number(item?.item_count ?? item?.items?.length ?? 0);
        return sum + (Number.isFinite(count) ? count : 0);
    }, 0);
    return { total, types };
}

function updateSessionLabels(filename = '', parseResults = currentSession?.parse_results, generationScope = null) {
    const displayName = filename || '已导入的文档';
    const { total, types } = summarizeParseResults(parseResults);
    const generationTotal = generationScope?.total ?? total;
    const generationDescriptor = generationScope?.preview
        ? `试听模式 · 前 ${generationTotal} 条内容`
        : (generationTotal > 0 ? `共 ${generationTotal} 条内容` : '');
    const sourceName = $('source-file-name');
    const sourceMeta = $('source-file-meta');
    const generationName = $('generation-file-name');
    const summaryDocument = $('summary-document');
    const generateButtonLabel = $('generate-button-label');
    const toolbarDocument = $('toolbar-document');
    if (sourceName) sourceName.textContent = displayName;
    if (sourceMeta) sourceMeta.textContent = filename
        ? (total > 0 ? `已识别 ${total} 条 · ${types.length} 种题型` : '文档解析完成')
        : '当前文档';
    if (summaryDocument) summaryDocument.textContent = total > 0
        ? `${total} 条 · ${types.length} 种题型`
        : '等待解析';
    if (generateButtonLabel) {
        const previewEnabled = Boolean($('preview')?.checked && total > 3);
        generateButtonLabel.textContent = previewEnabled
            ? `先试听 ${Math.min(total, 3)} 条`
            : (total > 0 ? `开始生成 ${total} 条音频` : '开始生成音频');
    }
    if (toolbarDocument) {
        toolbarDocument.textContent = filename || '';
        toolbarDocument.title = filename || '';
        toolbarDocument.hidden = !filename;
    }
    if (generationName) {
        generationName.textContent = filename
            ? `正在处理「${displayName}」${generationDescriptor ? ` · ${generationDescriptor}` : ''}，请保持应用开启。`
            : `${PRODUCT_NAME} 正在准备当前文档，请保持应用开启。`;
    }
}

function setActiveWorkspaceView(workspaceName) {
    const next = WORKSPACE_ORDER.includes(workspaceName) ? workspaceName : 'import';
    activeWorkspace = next;
    const review = $('content-review-view');
    const voice = $('voice-config-view');
    const reviewVisible = next === 'review';
    const voiceVisible = next === 'voice';
    if (review) {
        review.hidden = !reviewVisible;
        review.setAttribute('aria-hidden', reviewVisible ? 'false' : 'true');
    }
    if (voice) {
        voice.hidden = !voiceVisible;
        voice.setAttribute('aria-hidden', voiceVisible ? 'false' : 'true');
    }
    document.body.dataset.activeWorkspace = next;
    return next;
}


registerRendererModule("workflow.shell", {
    workspaceProgress,
    renderWorkspaceProgress,
    setWorkspaceTheme,
    initializeTheme,
    setServiceState,
    summarizeParseResults,
    updateSessionLabels,
    setActiveWorkspaceView,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

