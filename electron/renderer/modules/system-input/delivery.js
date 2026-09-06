/** Renderer module: systemInput.delivery */
(function attachRendererFeature_systemInput_delivery(root) {
    'use strict';

function systemInputDocumentEntryIsSupported(workspace = currentWorkspace) {
    const support = workspace?.system_input?.document_entry_support;
    if (!support || typeof support !== 'object' || support.supported !== true) return false;

    // The workspace may be restored from a cached projection. Reuse the
    // review model's current-contract check here so every system-input entry
    // (drawer, progress page, history navigation) rejects stale
    // ``supported=true`` facts in the same way as the document review view.
    const checker = typeof reviewDocumentEntrySupport === 'function'
        ? reviewDocumentEntrySupport
        : root.WORDTTS_RENDERER?.getModule?.('review.model')?.reviewDocumentEntrySupport;
    if (typeof checker !== 'function') return false;
    let items = Array.isArray(workspace?.items) ? workspace.items : [];
    const mapper = typeof workspaceItemsToParseResults === 'function'
        ? workspaceItemsToParseResults
        : root.WORDTTS_RENDERER?.getModule?.('review.model')?.workspaceItemsToParseResults;
    if (typeof mapper === 'function') {
        try {
            const groups = mapper(workspace);
            const mappedItems = Array.isArray(groups)
                ? groups.flatMap(group => Array.isArray(group?.items) ? group.items : [])
                : [];
            if (mappedItems.length) items = mappedItems;
        } catch (_error) {
            // Keep the raw workspace rows as a conservative fallback. The
            // review checker will reject them if page facts are unavailable.
        }
    }
    const result = checker(items, workspace.system_input);
    return result?.supported === true;
}

function openSystemInputConfigDrawer(workspace = currentWorkspace, { deliveryMode = '' } = {}) {
    const card = ensureSystemInputDeliveryPanel();
    const drawer = $('system-input-drawer');
    if (!drawer || !card) return false;
    const systemInput = workspace?.system_input;
    if (!systemInput || systemInput.available === false) {
        showToast('系统录入模块尚未准备好', 'warning');
        return false;
    }
    if (!systemInputDocumentEntryIsSupported(workspace)) {
        showToast(systemInput?.document_entry_support?.reason || '当前文档暂不支持系统录入', 'warning');
        return false;
    }
    if (systemInputDeliveryModeIsLocked(systemInput)) {
        const terminalSuccess = String(systemInput.input_status || '').trim().toLowerCase() === 'succeeded'
            || String(systemInput.input_run?.status || '').trim().toUpperCase() === 'SUCCEEDED';
        showToast(terminalSuccess ? '系统录入已完成，交付方式不可修改' : '当前录入运行存在未核验状态，配置已冻结', 'warning');
        return false;
    }
    if (!systemInputConfigurationEditable(systemInput)) {
        showToast('当前录入运行存在未核验或已确认的外部副作用，配置已冻结', 'warning');
        return false;
    }
    const requestedMode = ['audio_only', 'audio_and_input'].includes(deliveryMode)
        ? deliveryMode
        : '';
    const workflowKey = systemInputDrawerWorkflowKey(workspace);
    const stored = readSystemInputStoredDraft(workspace);
    const identityKey = systemInputDraftStorageKey(workspace);
    const memoryDraft = systemInputDrawerDraftWorkflowId === workflowKey && systemInputDrawerDraft?.identityKey === identityKey ? systemInputDrawerDraft : null;
    const draft = memoryDraft || stored?.draft || null;
    systemInputDeliveryModeDraft = requestedMode;
    systemInputDrawerWorkflowId = workflowKey;
    systemInputDrawerPreviousFocus = document.activeElement;
    renderSystemInputDrawerContext(workspace);
    if (draft && !draft.committed) {
        restoreSystemInputDrawerDraft(systemInput, draft);
        // A newly chosen delivery path must win over a draft captured from a
        // previous cancelled edit; otherwise reopening the target editor can
        // silently switch the form back to audio-only.
        if (requestedMode) setSystemInputField('system-input-delivery-mode', requestedMode);
    } else {
        systemInputPopulateForm(systemInput);
        if (requestedMode) setSystemInputField('system-input-delivery-mode', requestedMode);
    }
    renderSystemInputDisclosureSummaries(systemInput);
    resetSystemInputDisclosureState(systemInput);
    void loadSystemInputTemplates($('system-input-type')?.value || 'paper', { force: true });
    void loadSystemInputPlatformTemplates($('system-input-type')?.value || 'paper', { force: true });
    const form = $('system-input-form');
    if (form) form.scrollTop = 0;
    drawer.hidden = false;
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('system-input-drawer-open');
    openSystemInputTargetEditor(workspace, draft);
    return true;

}

function closeSystemInputConfigDrawer({ preserveDraft = true, committed = false } = {}) {
    if (systemInputConfigurationBusy && !committed) return;
    if (preserveDraft) targetEditorPersist();
    const drawer = $('system-input-drawer');
    const editorWorkspace = systemInputInteractionWorkspace();
    if (preserveDraft && !targetEditorActive()) captureSystemInputDrawerDraft(editorWorkspace);
    if (!preserveDraft) clearSystemInputDrawerDraft();
    closeSystemInputPicker();
    if (drawer) drawer.hidden = true;
    drawer?.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('system-input-drawer-open');
    if (drawer?.contains(document.activeElement)) document.activeElement.blur();
    systemInputDrawerWorkflowId = '';
    systemInputDeliveryModeDraft = '';
    renderSystemInputConfigEntry(editorWorkspace);
    renderSystemInputDeliveryChoice(editorWorkspace);
    if (isHistoryResultView() && editorWorkspace) renderSystemInputDelivery(editorWorkspace);
    targetEditorClosed({ saved: committed });
    const previousFocus = systemInputDrawerPreviousFocus;
    systemInputDrawerPreviousFocus = null;
    if (previousFocus && typeof previousFocus.focus === 'function' && document.contains(previousFocus)) {
        requestAnimationFrame(() => previousFocus.focus({ preventScroll: true }));
    }
}

function systemInputSafeReviewUrl(value) {
    const url = String(value || '').trim();
    return /^https?:\/\//i.test(url) ? url : '';
}

function createSystemInputDocumentCopyButton(documentName) {
    const name = String(documentName || '').trim();
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-text btn-sm system-input-copy-document-button';
    button.dataset.copySystemInputDocument = 'single';
    button.dataset.copyText = name;
    button.title = '复制审阅文档名';
    button.setAttribute('aria-label', `复制审阅文档名：${name}`);
    button.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><span data-copy-label>复制名称</span>';
    return button;
}

async function writeSystemInputClipboard(value) {
    const text = String(value || '').trim();
    if (!text) return false;
    try {
        const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : null;
        if (clipboard && typeof clipboard.writeText === 'function') {
            await clipboard.writeText(text);
            return true;
        }
    } catch (_error) {
        // Electron webContents can reject navigator.clipboard when the window
        // is not considered secure. Fall through to the synchronous DOM path.
    }
    if (!document.body || typeof document.execCommand !== 'function') return false;
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.setAttribute('aria-hidden', 'true');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '-9999px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    let copied = false;
    try {
        copied = document.execCommand('copy') === true;
    } catch (_error) {
        copied = false;
    } finally {
        textarea.remove();
    }
    return copied;
}

function markSystemInputDocumentCopied(button) {
    const label = button?.querySelector('[data-copy-label]');
    if (!label) return;
    const previous = label.textContent || '复制名称';
    label.textContent = '已复制';
    button.dataset.copyFeedback = 'true';
    window.setTimeout(() => {
        if (!button.isConnected) return;
        label.textContent = previous;
        delete button.dataset.copyFeedback;
    }, 1400);
}

async function copySystemInputDocumentName(value, trigger = null) {
    const text = String(value || '').trim();
    if (!text) {
        if (typeof showToast === 'function') showToast('当前没有可复制的审阅文档名', 'warning');
        return false;
    }
    const copied = await writeSystemInputClipboard(text);
    if (!copied) {
        if (typeof showToast === 'function') showToast('复制失败，请手动选择文档名复制', 'warning');
        return false;
    }
    markSystemInputDocumentCopied(trigger);
    const count = text.split(/\r?\n/).filter(Boolean).length;
    if (typeof showToast === 'function') {
        showToast(count > 1 ? `已复制 ${count} 个审阅文档名` : '审阅文档名已复制', 'success');
    }
    return true;
}

function copySystemInputDocumentNames(scope = document, trigger = null) {
    const root = scope && typeof scope.querySelectorAll === 'function' ? scope : document;
    const names = Array.from(root.querySelectorAll(
        '.system-input-copy-document-button[data-copy-system-input-document="single"]',
    ))
        .map(button => String(button.dataset.copyText || '').trim())
        .filter(Boolean);
    return copySystemInputDocumentName(names.join('\n'), trigger);
}

function systemInputReadoutItem(label, value) {
    const item = document.createElement('div');
    item.className = 'system-input-readout-item';
    const key = document.createElement('span');
    key.textContent = label;
    const content = document.createElement('strong');
    content.textContent = value || '—';
    item.append(key, content);
    return item;
}

function systemInputDiagnosticText(systemInput = {}) {
    const run = systemInput?.input_run;
    if (!run || typeof run !== 'object') return '';
    const runStatus = String(run.status || '').trim().toUpperCase();
    if (runStatus === 'SUCCEEDED') return '';
    const attempts = Array.isArray(run.attempts) ? run.attempts : [];
    const failedAttempt = [...attempts].reverse().find(attempt => (
        attempt
        && typeof attempt === 'object'
        && (attempt.error_code || attempt.error_message || attempt.evidence?.details)
    ));
    const source = failedAttempt || run;
    const code = String(source.error_code || run.error_code || '').trim();
    const message = String(source.error_message || run.error_message || '').trim();
    const details = source.evidence?.details && typeof source.evidence.details === 'object'
        ? source.evidence.details
        : {};
    const parts = [];
    // Surface a Chinese label for known service codes; unknown codes keep
    // their raw value so a new backend failure is still diagnosable.
    const codeLabel = typeof systemInputErrorCodeLabel === 'function'
        ? systemInputErrorCodeLabel(code)
        : '';
    if (codeLabel) parts.push(codeLabel);
    else if (code) parts.push(code);
    if (message) parts.push(message);
    // The page executor records the underlying exception in
    // details.error_type/details.error_message; without this branch the
    // real cause of INPUT_EXECUTOR_FAILED never reaches the user.
    const executorType = String(details.error_type || '').trim();
    const executorCause = String(details.error_message || '').trim();
    if (executorType) parts.push(`异常类型：${executorType.slice(0, 128)}`);
    if (executorCause && executorCause !== message) parts.push(`原因：${executorCause.slice(0, 300)}`);
    if (details.artifact_owner_workflow_id && details.workflow_id
        && String(details.artifact_owner_workflow_id) !== String(details.workflow_id)) {
        parts.push('页面图片仍归属父任务，请重新发起安全重试');
    } else if (details.phase === 'preflight' && details.step) {
        parts.push(`预检步骤：${String(details.step).slice(0, 128)}`);
        const cause = String(details.error_message || '').trim();
        if (cause && cause !== message && cause !== executorCause) parts.push(`原因：${cause.slice(0, 300)}`);
    }
    return parts.join(' · ').slice(0, 1200);
}

function systemInputDeliveryModeIsLocked(systemInput = {}) {
    const inputStatus = String(systemInput?.input_status || '').trim().toLowerCase();
    const runStatus = String(systemInput?.input_run?.status || '').trim().toUpperCase();
    // A terminal success summary is authoritative even when an older cached
    // projection omits the run object. The backend also rejects configuration
    // writes after this point, so the renderer must present the same state.
    if (inputStatus === 'succeeded' || runStatus === 'SUCCEEDED') return true;
    // If a run exists but the server has not explicitly marked its
    // configuration editable, fail closed around possible external effects.
    return Boolean(systemInput?.input_run) && !systemInputConfigurationEditable(systemInput);
}

function renderSystemInputDeliveryChoice(workspace = currentWorkspace) {
    const panel = ensureSystemInputDeliveryChoicePanel();
    if (!panel) return;
    const routeCard = typeof ensureSystemInputDeliveryPanel === 'function'
        ? ensureSystemInputDeliveryPanel()
        : null;
    const systemInput = workspace?.system_input;
    const enabled = Boolean(
        systemInput
        && systemInput.available !== false
        && systemInputDocumentEntryIsSupported(workspace),
    );
    panel.hidden = !enabled;
    if (!enabled) {
        systemInputDeliveryModeDraft = '';
        return;
    }
    const savedMode = systemInputSavedDeliveryMode(workspace);
    if (systemInputDeliveryModeDraft && systemInputDeliveryModeDraft === savedMode) {
        systemInputDeliveryModeDraft = '';
    }
    const deliveryModeLocked = systemInputDeliveryModeIsLocked(systemInput);
    if (deliveryModeLocked) {
        systemInputDeliveryModeDraft = '';
    }
    const mode = systemInputVisibleDeliveryMode(workspace);
    const modeDisabled = deliveryModeLocked || systemInputDeliveryModeBusy;
    const terminalSuccess = String(systemInput.input_status || '').trim().toLowerCase() === 'succeeded'
        || String(systemInput.input_run?.status || '').trim().toUpperCase() === 'SUCCEEDED';
    const deliveryComplete = systemInputRunIsComplete(systemInput);
    const status = $('system-input-delivery-choice-status');
    if (status) status.textContent = `${deliveryModeLocked ? '已锁定' : '当前'} · ${mode === 'audio_and_input' ? '生成并录入系统' : '只生成音频'}`;
    const choiceTitle = panel.querySelector('.system-input-intent-heading h2');
    const choiceSummary = panel.querySelector('.system-input-intent-heading p');
    if (choiceTitle) choiceTitle.textContent = deliveryModeLocked ? '交付方式已锁定' : '选择生成后的去向';
    if (choiceSummary) choiceSummary.textContent = deliveryModeLocked
        ? (deliveryComplete
            ? '系统录入已经完成，交付方式不可再切换。'
            : terminalSuccess
                ? '服务端已返回完成汇总，交付方式已锁定；如有未明确单元，请在最终结果页核验。'
                : '当前录入运行存在未核验状态，交付方式暂时锁定。')
        : '录入是可选步骤：可以只生成音频，也可以在音频验收后继续录入系统。';
    const deliveryOptions = panel.querySelector('#system-input-delivery-options');
    deliveryOptions?.setAttribute('aria-label', deliveryModeLocked ? '已完成的交付方式（不可修改）' : '本次任务交付方式');
    panel.classList.toggle('is-locked', deliveryModeLocked);
    document.querySelectorAll('input.system-input-delivery-choice').forEach(input => {
        const selected = input.value === mode;
        input.checked = selected;
        input.disabled = modeDisabled;
        const option = input.closest('.system-input-intent-option, .system-input-route-option');
        option?.classList.toggle('is-selected', selected);
        option?.classList.toggle('is-disabled', modeDisabled);
        option?.setAttribute('aria-disabled', modeDisabled ? 'true' : 'false');
        if (modeDisabled) {
            if (option) option.title = deliveryModeLocked
                ? (terminalSuccess ? '系统录入已完成，交付方式已锁定' : '当前录入运行存在未核验状态，交付方式暂时锁定')
                : '交付方式保存中，请稍候';
        } else {
            option?.removeAttribute('title');
        }
    });
    const routeOptions = routeCard?.querySelector('#system-input-route-options');
    routeOptions?.setAttribute('aria-label', deliveryModeLocked ? '已完成的交付方式（不可修改）' : '选择本次交付方式');
    routeCard?.querySelectorAll('[data-route-option-state]').forEach(stateNode => {
        stateNode.textContent = stateNode.dataset.routeOptionState === mode ? '当前' : '';
    });
    const note = $('system-input-delivery-note');
    if (note) note.textContent = deliveryModeLocked
        ? (deliveryComplete
            ? '系统录入已完成，交付方式已锁定；请点击“查看最终结果”查看回读结果。'
            : terminalSuccess
                ? '服务端已返回完成汇总，交付方式已锁定；请在最终结果页核验未明确的单元。'
                : '当前录入运行暂时锁定交付方式，请按页面提示处理未核验状态。')
        : mode === 'audio_and_input'
            ? '下一步：设置录入目标。保存后才会进入录入准备，音频不会重新生成。'
            : '音频完成后仍可在音频交付中心开启系统录入；当前不需要填写任何平台信息。';
}

async function persistSystemInputDeliveryMode(workspace, mode) {
    if (systemInputDeliveryModeIsLocked(workspace?.system_input)) {
        const terminalSuccess = String(workspace?.system_input?.input_status || '').trim().toLowerCase() === 'succeeded'
            || String(workspace?.system_input?.input_run?.status || '').trim().toUpperCase() === 'SUCCEEDED';
        showToast(terminalSuccess ? '系统录入已完成，交付方式不可修改' : '当前录入运行存在未核验状态，配置已冻结', 'warning');
        return false;
    }
    const historyMode = typeof isHistoryResultView === 'function' && isHistoryResultView();
    const workflowId = historyMode
        ? historyResultWorkflowId(activeResultContext)
        : String(currentSession?.session_id || '');
    if (!workflowApi?.saveSystemInputConfiguration || !workflowId) {
        showToast('当前任务暂时不能保存交付方式', 'error');
        return false;
    }
    let authoritative = authoritativeWorkspace(workflowId, workspace);
    if (!authoritative?.snapshot) {
        authoritative = await hydrateWorkflowWorkspace(workflowId, { silent: false });
    }
    if (systemInputDeliveryModeIsLocked(authoritative?.system_input)) {
        const terminalSuccess = String(authoritative?.system_input?.input_status || '').trim().toLowerCase() === 'succeeded'
            || String(authoritative?.system_input?.input_run?.status || '').trim().toUpperCase() === 'SUCCEEDED';
        showToast(terminalSuccess ? '系统录入已完成，交付方式不可修改' : '当前录入运行存在未核验状态，配置已冻结', 'warning');
        return false;
    }
    const expectedStateVersion = Number(authoritative?.snapshot?.state_version ?? currentSession.state_version ?? 0);
    if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
        showToast('任务版本缺失，暂时不能保存交付方式', 'error');
        return false;
    }
    const revisionValue = authoritative?.configuration?.configuration_revision
        ?? authoritative?.system_input?.entries?.[0]?.configuration_revision;
    const configurationRevision = Number(revisionValue);
    systemInputDeliveryModeBusy = true;
    renderSystemInputSurface(authoritative);
    try {
        const response = await workflowApi.saveSystemInputConfiguration(workflowId, {
            expected_state_version: expectedStateVersion,
            ...(Number.isInteger(configurationRevision) && configurationRevision > 0
                ? { configuration_revision: configurationRevision }
                : {}),
            configuration: { delivery_mode: mode },
        }, { idempotencyKey: `renderer-system-input-mode-${workflowId}-${expectedStateVersion}-${mode}` });
        if (!applySystemInputWorkspaceResponse(response)) {
            throw new Error('服务端未返回更新后的工作区');
        }
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `保存交付方式失败：${error.message || '请稍后重试'}`, 'error');
        await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    } finally {
        systemInputDeliveryModeBusy = false;
    }
}

function handleSystemInputDeliveryModeChange(event) {
    const input = event.target.closest('input.system-input-delivery-choice');
    if (!input || input.disabled || systemInputDeliveryModeBusy) return;
    const workspace = systemInputInteractionWorkspace();
    const systemInput = workspace?.system_input;
    const requestedMode = input.value === 'audio_and_input' ? 'audio_and_input' : 'audio_only';
    const savedMode = systemInputSavedDeliveryMode(workspace);
    if (!systemInput
        || systemInput.available === false
        || systemInputDeliveryModeIsLocked(systemInput)
        || !systemInputConfigurationEditable(systemInput)) {
        renderSystemInputDeliveryChoice(workspace);
        return;
    }
    if (requestedMode === savedMode) {
        systemInputDeliveryModeDraft = '';
        renderSystemInputSurface(workspace);
        return;
    }
    systemInputDeliveryModeDraft = requestedMode;
    renderSystemInputSurface(workspace);
    if (requestedMode === 'audio_only') {
        void persistSystemInputDeliveryMode(workspace, requestedMode).then(() => {
            systemInputDeliveryModeDraft = '';
            renderSystemInputSurface(systemInputInteractionWorkspace());
        });
        return;
    }
    if (!openSystemInputConfigDrawer(workspace, { deliveryMode: requestedMode })) {
        systemInputDeliveryModeDraft = '';
        renderSystemInputSurface(workspace);
    }
}

function renderSystemInputDrawerContext(workspace = currentWorkspace) {
    const systemInput = workspace?.system_input;
    renderSystemInputBoundaryReview(systemInput, workspace);
    const source = $('system-input-drawer-source');
    const unitCount = $('system-input-drawer-unit-count');
    const note = $('system-input-drawer-context-note');
    if (source) source.textContent = String(currentSession?.source_filename || workspace?.source_filename || '当前文档');
    if (unitCount) {
        const count = Array.isArray(systemInput?.units) ? systemInput.units.length : 0;
        unitCount.textContent = count ? `${count} 条${systemInput?.input_type === 'textbook' ? '课文' : '试卷'}` : '待解析';
    }
    if (note) note.textContent = '这里只设置平台目标，不会重新生成音频';
}

function renderSystemInputConfigEntry(workspace = currentWorkspace) {
    const entry = ensureSystemInputConfigEntry();
    if (!entry) return;
    const systemInput = workspace?.system_input;
    const enabled = Boolean(
        systemInput
        && systemInput.available !== false
        && systemInputDocumentEntryIsSupported(workspace),
    );
    const mode = systemInputVisibleDeliveryMode(workspace);
    const configuredForInput = mode === 'audio_and_input';
    entry.hidden = !enabled || !configuredForInput;
    if (!enabled || !configuredForInput) return;
    const type = systemInput.input_type === 'textbook' ? '课文' : systemInput.input_type === 'vocabulary' ? '词汇' : '试卷';
    const unitCount = Array.isArray(systemInput.units) ? systemInput.units.length : 0;
    const summary = $('system-input-config-entry-summary');
    const entries = Array.isArray(systemInput.entries) ? systemInput.entries : [];
    const entriesByUnit = new Map(entries
        .map(item => [String(item?.unit_id || '').trim(), item])
        .filter(([unitId]) => unitId));
    const isConfigured = unitCount > 0
        && entriesByUnit.size === unitCount
        && systemInput.units.every(unit => {
            const item = entriesByUnit.get(String(unit?.unit_id || '').trim());
            const status = String(item?.input_status || '').trim().toLowerCase();
            return Boolean(item) && !['', 'pending_config', 'not_enabled'].includes(status);
        });
    if (summary) summary.textContent = isConfigured
        ? `${type} · ${unitCount || '待解析'} 个录入单元。配置已保存，生成后会按单元继续录入。`
        : `${type} · ${unitCount || '待解析'} 个录入单元。还需补充平台字段，才能进入录入准备。`;
    const state = $('system-input-config-entry-state');
    if (state) {
        state.textContent = isConfigured ? '已设置' : '待设置';
        state.classList.toggle('is-configured', isConfigured);
    }
    const button = $('system-input-open-config-btn');
    if (button) {
        button.disabled = !systemInputConfigurationEditable(systemInput);
        button.textContent = isConfigured ? '编辑录入目标' : '设置录入目标';
        button.title = isConfigured ? '编辑已保存的录入目标' : '打开录入目标设置';
    }
}

function clearSystemInputRefreshTimer() {
    if (systemInputRefreshTimer) {
        clearTimeout(systemInputRefreshTimer);
        systemInputRefreshTimer = null;
    }
}

function scheduleSystemInputRefresh(workflowId = currentSession?.session_id, delay = 1500) {
    if (!workflowId || !workflowApi?.getWorkspace || systemInputRefreshTimer) return;
    systemInputRefreshTimer = setTimeout(async () => {
        systemInputRefreshTimer = null;
        const historyContext = isHistoryResultView()
            && historyResultWorkflowId() === String(workflowId)
            ? activeResultContext
            : null;
        if (systemInputRefreshInFlight || (!historyContext && currentSession?.session_id !== workflowId)) return;
        systemInputRefreshInFlight = true;
        try {
            const workspace = historyContext
                ? await refreshHistoryResultWorkspace(historyContext, { silent: true })
                : await hydrateWorkflowWorkspace(workflowId, { silent: true });
            // A transient request failure does not get to strand the UI in
            // “录入进行中”; keep trying until the durable run reaches a
            // terminal state or the user switches tasks.
            if (!workspace && (historyContext || currentSession?.session_id === workflowId)) {
                scheduleSystemInputRefresh(workflowId, 3000);
            }
        } finally {
            systemInputRefreshInFlight = false;
        }
    }, Math.max(250, Number(delay) || 1500));
}

function syncSystemInputRefresh(workspace = currentWorkspace) {
    const systemInput = workspace?.system_input;
    const runStatus = String(systemInput?.input_run?.status || '').toUpperCase();
    const runActive = ['PENDING', 'RUNNING'].includes(runStatus)
        || String(systemInput?.input_status || '').toLowerCase() === 'running';
    if (!runActive) {
        clearSystemInputRefreshTimer();
        return;
    }
    scheduleSystemInputRefresh(workspace?.snapshot?.workflow_id || currentSession?.session_id);
}

function systemInputBoundaryReviewData(systemInput) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const grouping = units
        .map(unit => unit?.evidence?.unit_grouping)
        .find(value => value && typeof value === 'object') || {};
    const rawCandidates = Array.isArray(grouping.candidate_boundaries)
        ? grouping.candidate_boundaries
        : [];
    const candidates = rawCandidates.map(candidate => {
        if (!candidate || typeof candidate !== 'object') return null;
        const itemIds = Array.isArray(candidate.item_ids)
            ? candidate.item_ids.map(value => String(value || '').trim()).filter(Boolean)
            : [];
        return {
            item_ids: itemIds,
            item_count: Number(candidate.item_count) || itemIds.length,
            first_sequence: Number.isInteger(Number(candidate.first_sequence)) ? Number(candidate.first_sequence) : null,
            last_sequence: Number.isInteger(Number(candidate.last_sequence)) ? Number(candidate.last_sequence) : null,
        };
    }).filter(candidate => candidate && candidate.item_ids.length > 0);
    return { grouping, candidates };
}

function renderSystemInputBoundaryReview(systemInput, workspace = currentWorkspace) {
    const section = $('system-input-boundary-review');
    if (!section) return;
    const groupingData = systemInputBoundaryReviewData(systemInput);
    const grouping = groupingData.grouping;
    const candidates = groupingData.candidates;
    const status = String(systemInput?.unit_count_status || '');
    const stale = grouping?.strategy === 'user_override_stale';
    const needsReview = status === 'multiple_candidate' || stale;
    section.hidden = !needsReview;
    if (!needsReview) {
        if (typeof targetEditorSyncBoundaryReview === 'function') targetEditorSyncBoundaryReview();
        return;
    }

    const statusElement = $('system-input-boundary-status');
    if (statusElement) {
        statusElement.textContent = stale ? '需重新确认' : '待确认';
        statusElement.classList.toggle('is-confirmed', false);
    }
    const note = $('system-input-boundary-note');
    if (note) {
        note.textContent = stale
            ? '原确认边界与当前解析结果不再完全匹配。请根据当前候选范围重新确认；确认前不会发起系统录入。'
            : '检测到多个候选范围。标记只作为证据，请先确认边界；确认后才能保存平台配置并开始系统录入。';
    }
    const list = $('system-input-boundary-candidates');
    if (list) {
        list.replaceChildren();
        candidates.forEach((candidate, index) => {
            const row = document.createElement('div');
            row.className = 'system-input-boundary-candidate';
            const indexElement = document.createElement('span');
            indexElement.className = 'system-input-boundary-candidate-index';
            indexElement.textContent = String(index + 1);
            const copy = document.createElement('span');
            copy.className = 'system-input-boundary-candidate-copy';
            const title = document.createElement('strong');
            title.textContent = `候选录入单元 ${index + 1}`;
            const detail = document.createElement('small');
            detail.textContent = `${candidate.item_count || candidate.item_ids.length} 条内容`;
            copy.append(title, detail);
            const range = document.createElement('span');
            range.className = 'system-input-boundary-candidate-range';
            const first = candidate.first_sequence;
            const last = candidate.last_sequence;
            range.textContent = first !== null && last !== null
                ? `第${first + 1}–${last + 1}条`
                : '范围待核验';
            row.append(indexElement, copy, range);
            list.appendChild(row);
        });
    }
    const workflowId = String(workspace?.snapshot?.workflow_id || currentSession?.session_id || '');
    const frozen = Boolean(systemInput?.input_run) || !workflowId;
    const splitButton = $('system-input-boundary-split-btn');
    if (splitButton) {
        splitButton.disabled = frozen
            || systemInputBoundaryBusy
            || candidates.length < 2
            || candidates.some(candidate => candidate.item_ids.length === 0);
        splitButton.setAttribute('aria-busy', systemInputBoundaryBusy ? 'true' : 'false');
        splitButton.title = splitButton.disabled
            ? (frozen ? '当前运行已经创建，边界已冻结' : '当前没有足够的连续候选范围')
            : '按当前解析给出的候选范围确认多个录入单元';
    }
    const mergeButton = $('system-input-boundary-merge-btn');
    if (mergeButton) {
        mergeButton.disabled = frozen || systemInputBoundaryBusy;
        mergeButton.setAttribute('aria-busy', systemInputBoundaryBusy ? 'true' : 'false');
        mergeButton.title = mergeButton.disabled
            ? '当前运行已经创建，边界已冻结'
            : '将当前文档全部内容确认成一个录入单元';
    }
    if (typeof targetEditorSyncBoundaryReview === 'function') targetEditorSyncBoundaryReview();
}

async function confirmSystemInputBoundaryChoice(mode) {
    if (systemInputBoundaryBusy || !['single', 'multiple'].includes(mode)) return false;
    const historyContext = isHistoryResultView() ? activeResultContext : null;
    const workflowId = historyResultWorkflowId(historyContext) || String(currentSession?.session_id || '');
    if (!workflowApi?.confirmSystemInputBoundaries || !workflowId) {
        showToast('当前版本暂时不能保存录入单元边界', 'error');
        return false;
    }
    let workspace = historyContext?.workspace || authoritativeWorkspace(workflowId, currentWorkspace);
    if (!workspace?.snapshot) {
        workspace = historyContext
            ? await workflowApi.getWorkspace(workflowId)
            : await hydrateWorkflowWorkspace(workflowId, { silent: false });
    }
    const systemInput = workspace?.system_input;
    if (!workspace?.snapshot || !systemInput || systemInput.input_run) {
        showToast('当前录入单元边界已经冻结，请刷新任务后重试', 'warning');
        return false;
    }
    const expectedStateVersion = Number(workspace.snapshot.state_version);
    if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
        showToast('任务版本缺失，暂时不能确认录入单元', 'error');
        return false;
    }
    const groupingData = systemInputBoundaryReviewData(systemInput);
    const boundaries = mode === 'multiple'
        ? groupingData.candidates.map(candidate => ({ item_ids: candidate.item_ids.slice() }))
        : [];
    if (mode === 'multiple' && (boundaries.length < 2 || boundaries.some(boundary => !boundary.item_ids.length))) {
        showToast('当前没有足够的连续候选范围可拆分', 'warning');
        return false;
    }
    const drawer = $('system-input-drawer');
    const drawerWasOpen = Boolean(drawer && !drawer.hidden);
    const draft = drawerWasOpen ? captureSystemInputDrawerDraft(workspace) : null;
    systemInputBoundaryBusy = true;
    renderSystemInputSurface(workspace);
    try {
        const response = await workflowApi.confirmSystemInputBoundaries(workflowId, {
            expected_state_version: expectedStateVersion,
            mode,
            boundaries,
        }, {
            idempotencyKey: `renderer-system-input-boundaries-${workflowId}-${expectedStateVersion}-${mode}`,
        });
        const updated = historyContext
            ? updateHistoryResultWorkspace(response?.workspace, historyContext)
            : applySystemInputWorkspaceResponse(response);
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        if (drawerWasOpen && updated.system_input) {
            renderSystemInputDrawerContext(updated);
            if (draft) restoreSystemInputDrawerDraft(updated.system_input, draft);
            else systemInputPopulateForm(updated.system_input);
        }
        showToast(mode === 'multiple' ? '已确认多个录入单元' : '已合并为一个录入单元', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `确认录入单元失败：${error.message || '请稍后重试'}`, 'error');
        if (historyContext) await refreshHistoryResultWorkspace(historyContext, { silent: true });
        else await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    } finally {
        systemInputBoundaryBusy = false;
        renderSystemInputSurface(historyContext?.workspace || currentWorkspace);
    }
}

function renderSystemInputDelivery(workspace = currentWorkspace) {
    const card = ensureSystemInputDeliveryPanel();
    if (!card) return;
    const systemInput = workspace?.system_input;
    const visible = Boolean(
        workspace
        && systemInput
        && systemInput.available !== false
        && systemInputDocumentEntryIsSupported(workspace),
    );
    card.hidden = !visible;
    const drawer = $('system-input-drawer');
    if (!visible) {
        // Target dialogs live under <body> so they can escape the workspace
        // clipping context. Close that detached layer together with the
        // drawer when a workspace refresh removes the delivery surface.
        const editorWasActive = typeof targetEditorActive === 'function' && targetEditorActive();
        if (editorWasActive && typeof targetEditorPersist === 'function') targetEditorPersist();
        if (typeof closeSystemInputPicker === 'function') closeSystemInputPicker();
        if (typeof targetEditorClosed === 'function') targetEditorClosed({ saved: false });
        if (drawer) {
            drawer.hidden = true;
            drawer.setAttribute('aria-hidden', 'true');
        }
        clearSystemInputRefreshTimer();
        return;
    }
    syncSystemInputRefresh(workspace);
    const status = String(systemInput.input_status || 'not_enabled').trim().toLowerCase();
    const statusEl = $('system-input-status');
    if (statusEl) {
        statusEl.textContent = systemInputStatusLabel(status);
        statusEl.className = `system-input-status is-${status}`;
    }
    const mode = systemInputDeliveryModeLabel(systemInput.delivery_mode);
    const type = systemInput.input_type === 'textbook' ? '课文' : systemInput.input_type === 'vocabulary' ? '词汇' : '试卷';
    const units = Array.isArray(systemInput.units) ? systemInput.units : [];
    const deliveryModeLocked = systemInputDeliveryModeIsLocked(systemInput);
    const terminalSuccess = status === 'succeeded'
        || String(systemInput.input_run?.status || '').trim().toUpperCase() === 'SUCCEEDED';
    const deliveryComplete = systemInputRunIsComplete(systemInput);
    const summary = $('system-input-summary');
    if (summary) summary.textContent = `${mode} · ${type} · ${units.length} 个录入单元。系统录入只通过页面控件执行，状态和录入 ID 从只读反馈回读。`;
    const routeTitle = $('system-input-route-title');
    const routeNote = $('system-input-route-note');
    if (routeTitle) {
        routeTitle.textContent = deliveryComplete
            ? '系统录入已完成'
            : terminalSuccess
                ? '系统录入汇总已完成'
                : deliveryModeLocked
                    ? '系统录入暂时锁定'
                    : systemInput.delivery_mode === 'audio_and_input'
                        ? '系统录入已加入交付路线'
                        : '当前只交付音频';
    }
    if (routeNote) {
        routeNote.textContent = deliveryComplete
            ? '系统录入已完成，交付方式已锁定；请点击“查看最终结果”查看回读结果。'
            : terminalSuccess
                ? '服务端已返回完成汇总，交付方式已锁定；请在最终结果页核验未明确的单元。'
                : deliveryModeLocked
                    ? '当前录入运行暂时锁定交付方式，请按页面提示处理未核验状态。'
                    : systemInput.delivery_mode === 'audio_and_input'
                        ? '音频验收后会直接开始系统录入；不需要重新生成音频。'
                        : '下载音频即可完成本次交付；如需录入，可随时设置目标并继续。';
    }
    const readout = $('system-input-readout');
    if (readout) {
        readout.replaceChildren();
        const gateStatus = systemInput.audio_gate?.technical_status === 'passed' ? '技术闸门通过' : '等待技术核验';
        const acceptanceStatus = systemInput.audio_acceptance?.status === 'accepted' ? '用户已验收' : '待整批验收';
        readout.append(
            systemInputReadoutItem('交付方式', mode),
            systemInputReadoutItem('录入类型', type),
            systemInputReadoutItem('音频闸门', gateStatus),
            systemInputReadoutItem('音频验收', acceptanceStatus),
        );
        const diagnostic = systemInputDiagnosticText(systemInput);
        if (diagnostic) readout.append(systemInputReadoutItem('最近状态', diagnostic));
    }
    const inputEnabled = String(systemInput.delivery_mode || '').trim().toLowerCase() === 'audio_and_input';
    const actions = card.querySelector('.system-input-actions');
    actions?.classList.toggle('is-single-action', !inputEnabled);
    const configButton = $('system-input-config-btn');
    if (configButton) {
        configButton.hidden = !inputEnabled;
        configButton.disabled = deliveryModeLocked || !systemInputConfigurationEditable(systemInput);
        configButton.textContent = inputEnabled ? '编辑录入目标' : '设置并开启录入';
        configButton.title = deliveryModeLocked
            ? (terminalSuccess ? '系统录入已完成，交付方式和录入目标均已锁定' : '当前录入运行存在未核验状态，交付方式和录入目标暂时锁定')
            : inputEnabled
            ? '编辑已保存的录入目标'
            : '设置录入目标并开启系统录入；不会重新生成音频';
    }
    const acceptAction = workspaceAction('ACCEPT_AUDIO', workspace);
    const startButton = $('system-input-start-btn');
    const startAction = workspaceAction('START_INPUT', workspace);
    if (startButton) {
        const accepted = systemInput.audio_acceptance?.status === 'accepted';
        const runStatus = String(systemInput.input_run?.status || '').trim().toUpperCase();
        const completed = systemInputRunIsComplete(systemInput);
        const summarySucceeded = status === 'succeeded'
            || runStatus === 'SUCCEEDED';
        const runActive = ['pending', 'running'].includes(status)
            || ['PENDING', 'RUNNING'].includes(runStatus);
        const needsReconcile = typeof systemInputNeedsReconciliation === 'function'
            && systemInputNeedsReconciliation(systemInput, status);
        const acceptancePending = inputEnabled
            && !systemInput.input_run
            && !accepted
            && !completed;
        const canConfigureFromCard = !inputEnabled
            && status === 'not_enabled'
            && !systemInput.input_run
            && !completed;
        const canContinueToConfiguration = inputEnabled
            && !systemInput.input_run
            && accepted
            && !completed;
        const acceptanceOnlyBlocker = String(startAction?.reason || '').includes('整批音频验收');
        const canAcceptAndStart = acceptancePending
            && acceptAction?.enabled === true
            && (startAction?.enabled === true || acceptanceOnlyBlocker);

        startButton.disabled = completed
            ? false
            : runActive
                ? true
                : needsReconcile
                    ? false
                    : canConfigureFromCard
                        ? !systemInputConfigurationEditable(systemInput)
                        : acceptancePending
                            ? !canAcceptAndStart
                            : canContinueToConfiguration
                                ? false
                                : startAction?.enabled !== true;
        startButton.textContent = completed
            ? '查看最终结果'
            : runActive
            ? '录入进行中'
            : needsReconcile
                ? '处理待核验结果'
                : canConfigureFromCard
                    ? '设置并开启录入'
                    : acceptancePending
                        ? '确认音频并开始录入'
                        : canContinueToConfiguration
                            ? '继续录入系统'
                            : summarySucceeded
                                ? '查看录入结果'
                : status === 'failed_retryable'
                    ? '安全重试录入'
                    : '继续录入系统';
        startButton.title = completed
            ? '查看音频与系统录入的最终结果'
            : runActive
                ? '系统录入正在执行，页面会自动刷新服务端状态'
                : needsReconcile
            ? '打开录入进度，核对平台记录后再安全重试'
                : canConfigureFromCard
                    ? '打开录入目标设置并开启系统录入；不会重新生成音频'
                    : acceptancePending
                        ? (canAcceptAndStart
                            ? '确认当前整批已验证音频后直接开始系统录入'
                            : (startAction?.reason && !acceptanceOnlyBlocker
                                ? startAction.reason
                                : (acceptAction?.reason || '当前不能确认整批音频并开始录入')))
                        : canContinueToConfiguration
                            ? '打开录入目标设置；不会重新生成音频'
                            : summarySucceeded
                                ? '查看服务端汇总和逐单元结果；缺失明细会标记为待核验'
                                : startAction?.enabled === true
                                    ? '按录入单元开始页面录入'
                                    : (startAction?.reason || '当前不能开始系统录入');
        // Disabled buttons only carry their reason in a hover title. The hint
        // line keeps the blocker visible without requiring a pointer.
        const actionsHint = $('system-input-actions-hint');
        if (actionsHint) {
            const actionBlocked = startButton.disabled === true
                && !completed
                && !runActive
                && !needsReconcile
                && !canContinueToConfiguration;
            const blockerReason = actionBlocked
                ? canConfigureFromCard
                    ? '当前不能打开录入目标设置'
                    : acceptancePending
                        ? (startAction?.reason && !acceptanceOnlyBlocker
                            ? startAction.reason
                            : (acceptAction?.reason || '请先完成整批音频验收'))
                        : (startAction?.reason || '当前不能开始系统录入')
                : '';
            actionsHint.hidden = !blockerReason;
            if (blockerReason) actionsHint.textContent = `暂不能继续录入：${blockerReason}`;
        }
    }
    const unitList = $('system-input-units');
    if (unitList) {
        unitList.replaceChildren();
        const entriesByUnit = new Map((Array.isArray(systemInput.entries) ? systemInput.entries : [])
            .map(entry => [String(entry.unit_id || ''), entry]));
        units.forEach((unit, index) => {
            const row = document.createElement('article');
            row.className = 'system-input-unit';
            const main = document.createElement('div');
            main.className = 'system-input-unit-main';
            const title = document.createElement('strong');
            title.className = 'system-input-unit-title';
            title.textContent = systemInputDisplayValue(unit.label, `第${index + 1}个录入单元`);
            const config = unit.configuration || {};
            const meta = document.createElement('span');
            meta.className = 'system-input-unit-meta';
            // 课文单元展示课文名称；试卷展示试卷名称/模板。
            const unitConfigTitle = String(unit.input_type || '') === 'textbook'
                ? systemInputDisplayValue(config.textbookNameZh, '配置待完成')
                : systemInputDisplayValue(config.paperName || config.platformTemplateName || config.platformTemplateId, '配置待完成');
            meta.textContent = [
                `${Array.isArray(systemInput.content_segments) ? systemInput.content_segments.filter(segment => String(segment.unit_id) === String(unit.unit_id)).length : 0} 条内容`,
                unitConfigTitle,
            ].join(' · ');
            main.append(title, meta);
            const entry = entriesByUnit.get(String(unit.unit_id || ''));
            const state = document.createElement('div');
            state.className = 'system-input-unit-status';
            const entryStatus = systemInputEntryStatus(systemInput, entry);
            const stateParts = [systemInputStatusLabel(entryStatus)];
            if (entry?.entry_id) stateParts.push(`录入 ID：${entry.entry_id}`);
            if (entry?.external_record_id) stateParts.push(`外部 ID：${entry.external_record_id}`);
            state.textContent = stateParts.join(' · ');
            const reviewDocumentName = systemInputReviewDocumentName(entry);
            if (reviewDocumentName) {
                const reviewNode = document.createElement('span');
                reviewNode.className = 'system-input-unit-review';
                const reviewLabel = document.createElement('span');
                reviewLabel.textContent = '审阅文档名';
                const nameNode = document.createElement('strong');
                nameNode.textContent = reviewDocumentName;
                nameNode.title = reviewDocumentName;
                reviewNode.append(reviewLabel, nameNode, createSystemInputDocumentCopyButton(reviewDocumentName));
                state.appendChild(reviewNode);
            }
            row.append(main, state);
            unitList.appendChild(row);
        });
    }
}

function systemInputUnitRows(workspace = currentWorkspace) {
    const systemInput = workspace?.system_input || {};
    const units = Array.isArray(systemInput.units) ? systemInput.units : [];
    const entries = Array.isArray(systemInput.entries) ? systemInput.entries : [];
    const entriesByUnit = new Map();
    entries.forEach(entry => {
        const unitId = String(entry?.unit_id || '').trim();
        if (unitId) entriesByUnit.set(unitId, entry);
    });
    const rows = units.map((unit, index) => {
        const unitId = String(unit?.unit_id || '').trim();
        return {
            unit,
            entry: unitId ? (entriesByUnit.get(unitId) || null) : (entries[index] || null),
            index,
        };
    });
    const knownEntryIds = new Set(rows.map(row => row.entry).filter(Boolean));
    entries.forEach(entry => {
        if (!knownEntryIds.has(entry)) rows.push({ unit: null, entry, index: rows.length });
    });
    return rows;
}

function systemInputUnitRowLabel(row, fallbackIndex = 0) {
    return systemInputDisplayValue(
        row?.unit?.label || row?.entry?.unit_label,
        `第${Number(fallbackIndex) + 1}个录入单元`,
    );
}

function systemInputUnitRowContentCount(row, systemInput = {}) {
    const unitId = String(row?.unit?.unit_id || row?.entry?.unit_id || '').trim();
    if (!unitId) return 0;
    return (Array.isArray(systemInput.content_segments) ? systemInput.content_segments : [])
        .filter(segment => String(segment?.unit_id || '') === unitId).length;
}

function systemInputReviewDocumentName(entry) {
    // 平台侧可搜索的“审阅文档名”：只展示提交到平台的试卷/课文标题，
    // 缺失时回退到兼容旧数据的 document_name，再回退到单元标签。
    if (!entry || typeof entry !== 'object') return '';
    const configuration = entry.configuration || {};
    const platformTitle = String(
        configuration.paperName
            || configuration.paper_name
            || configuration.textbookNameZh
            || configuration.textbook_name_zh
            || '',
    ).trim();
    const documentName = String(entry.document_name || '').trim();
    const unitLabel = String(entry.unit_label || '').trim();
    return platformTitle || documentName || unitLabel;
}

function systemInputReviewPresentation(entry, entryStatus = '') {
    const name = systemInputReviewDocumentName(entry);
    if (name) {
        return { name, detail: '审阅文档名（提交到平台后可据此搜索）' };
    }
    const reviewStatus = String(entry?.review_url_status || '').trim().toLowerCase();
    const reviewSource = String(entry?.review_url_source || '').trim().toLowerCase();
    if (reviewStatus === 'unavailable' || reviewSource === 'platform_not_supported') {
        return {
            name: '',
            label: '平台未提供链接',
            detail: '当前录入平台不支持获取审阅链接，请使用录入文档名搜索。',
        };
    }
    const status = String(entryStatus || entry?.input_status || '').trim().toLowerCase();
    return {
        name: '',
        label: status === 'succeeded' ? '文档名待同步' : '文档名待配置',
        detail: '保存录入目标后，会显示可直接复制到平台搜索的文档名。',
    };
}


registerRendererModule("systemInput.delivery", {
    systemInputDocumentEntryIsSupported,
    openSystemInputConfigDrawer,
    closeSystemInputConfigDrawer,
    systemInputSafeReviewUrl,
    createSystemInputDocumentCopyButton,
    copySystemInputDocumentName,
    copySystemInputDocumentNames,
    systemInputReadoutItem,
    systemInputDiagnosticText,
    systemInputDeliveryModeIsLocked,
    renderSystemInputDeliveryChoice,
    persistSystemInputDeliveryMode,
    handleSystemInputDeliveryModeChange,
    renderSystemInputDrawerContext,
    renderSystemInputConfigEntry,
    clearSystemInputRefreshTimer,
    scheduleSystemInputRefresh,
    syncSystemInputRefresh,
    systemInputBoundaryReviewData,
    renderSystemInputBoundaryReview,
    confirmSystemInputBoundaryChoice,
    renderSystemInputDelivery,
    systemInputUnitRows,
    systemInputUnitRowLabel,
    systemInputUnitRowContentCount,
    systemInputReviewDocumentName,
    systemInputReviewPresentation,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
