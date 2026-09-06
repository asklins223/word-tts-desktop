/** Renderer module: systemInput.surface */
(function attachRendererFeature_systemInput_surface(root) {
    'use strict';

function ensureSystemInputDeliveryChoicePanel() {
    const existing = $('system-input-delivery-choice-panel');
    if (existing) return existing;
    const voiceView = $('voice-config-view');
    const heading = voiceView?.querySelector('.voice-heading');
    if (!heading) return null;

    const panel = document.createElement('section');
    panel.className = 'system-input-intent-card';
    panel.id = 'system-input-delivery-choice-panel';
    panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'system-input-delivery-choice-title');

    const headingRow = document.createElement('div');
    headingRow.className = 'system-input-intent-heading';
    const copy = document.createElement('div');
    const title = document.createElement('h2');
    title.id = 'system-input-delivery-choice-title';
    title.textContent = '选择生成后的去向';
    const summary = document.createElement('p');
    summary.textContent = '录入是可选步骤：可以只生成音频，也可以在音频验收后继续录入系统。';
    copy.append(title, summary);
    const status = document.createElement('span');
    status.className = 'system-input-intent-status';
    status.id = 'system-input-delivery-choice-status';
    headingRow.append(copy, status);

    const options = document.createElement('div');
    options.className = 'system-input-intent-options';
    options.id = 'system-input-delivery-options';
    options.setAttribute('role', 'radiogroup');
    options.setAttribute('aria-label', '本次任务交付方式');
    [
        {
            value: 'audio_only',
            title: '只生成音频',
            description: '生成、试听、下载音频；完成音频交付即可结束。',
        },
        {
            value: 'audio_and_input',
            title: '生成音频并录入系统',
            description: '音频验收后，按录入单元提交到平台；不会重新生成音频。',
        },
    ].forEach(optionData => {
        const option = document.createElement('label');
        option.className = 'system-input-intent-option';
        option.dataset.deliveryMode = optionData.value;
        const input = document.createElement('input');
        input.className = 'system-input-delivery-choice';
        input.type = 'radio';
        input.name = 'system-input-delivery-choice';
        input.value = optionData.value;
        const radio = document.createElement('span');
        radio.className = 'system-input-intent-radio';
        radio.setAttribute('aria-hidden', 'true');
        const optionCopy = document.createElement('span');
        optionCopy.className = 'system-input-intent-copy';
        const optionTitle = document.createElement('strong');
        optionTitle.textContent = optionData.title;
        const optionDescription = document.createElement('small');
        optionDescription.textContent = optionData.description;
        optionCopy.append(optionTitle, optionDescription);
        option.append(input, radio, optionCopy);
        options.appendChild(option);
    });

    const note = document.createElement('p');
    note.className = 'system-input-intent-note';
    note.id = 'system-input-delivery-note';
    panel.append(headingRow, options, note);
    panel.addEventListener('change', handleSystemInputDeliveryModeChange);
    heading.insertAdjacentElement('afterend', panel);
    return panel;
}

function ensureSystemInputRouteOptions(card) {
    if (!card) return null;
    const existing = card.querySelector('#system-input-route-options');
    if (existing) return existing;

    const options = document.createElement('div');
    options.className = 'system-input-route-options';
    options.id = 'system-input-route-options';
    options.setAttribute('role', 'radiogroup');
    options.setAttribute('aria-label', '选择本次交付方式');
    [
        {
            value: 'audio_only',
            index: '01',
            title: '只生成音频',
            description: '试听、下载音频，完成音频交付即可结束。',
        },
        {
            value: 'audio_and_input',
            index: '02',
            title: '生成音频并录入系统',
            description: '音频验收后，按录入单元继续提交；不重新生成音频。',
        },
    ].forEach(optionData => {
        const option = document.createElement('label');
        option.className = 'system-input-route-option';
        option.dataset.deliveryMode = optionData.value;
        const input = document.createElement('input');
        input.className = 'system-input-delivery-choice';
        input.type = 'radio';
        input.name = 'system-input-route-choice';
        input.value = optionData.value;
        const marker = document.createElement('span');
        marker.className = 'system-input-route-option-marker';
        marker.textContent = optionData.index;
        marker.setAttribute('aria-hidden', 'true');
        const copy = document.createElement('span');
        copy.className = 'system-input-route-option-copy';
        const title = document.createElement('strong');
        title.textContent = optionData.title;
        const description = document.createElement('small');
        description.textContent = optionData.description;
        copy.append(title, description);
        const state = document.createElement('span');
        state.className = 'system-input-route-option-state';
        state.dataset.routeOptionState = optionData.value;
        option.append(input, marker, copy, state);
        options.appendChild(option);
    });

    const guidance = card.querySelector('.system-input-route-guidance');
    if (guidance) guidance.after(options);
    else card.insertBefore(options, card.firstChild);
    return options;
}

function ensureSystemInputConfigEntry() {
    const existing = $('system-input-config-entry');
    if (existing) return existing;
    const panel = ensureSystemInputDeliveryChoicePanel();
    if (!panel) return null;
    const section = document.createElement('section');
    section.className = 'system-input-target-entry';
    section.id = 'system-input-config-entry';
    section.hidden = true;
    section.setAttribute('aria-labelledby', 'system-input-config-entry-title');
    const icon = document.createElement('span');
    icon.className = 'system-input-target-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18"/></svg>';
    const copy = document.createElement('div');
    copy.className = 'system-input-target-copy';
    const title = document.createElement('h2');
    title.id = 'system-input-config-entry-title';
    title.textContent = '录入目标';
    const summary = document.createElement('p');
    summary.id = 'system-input-config-entry-summary';
    summary.textContent = '生成前设置平台字段和题型模板。';
    copy.append(title, summary);
    const controls = document.createElement('div');
    controls.className = 'system-input-target-controls';
    const status = document.createElement('span');
    status.className = 'system-input-target-state';
    status.id = 'system-input-config-entry-state';
    status.textContent = '待设置';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-primary btn-sm';
    button.id = 'system-input-open-config-btn';
    button.textContent = '设置录入目标';
    button.title = '打开录入目标设置';
    button.addEventListener('click', () => {
        openSystemInputConfigDrawer(currentWorkspace, {
            deliveryMode: systemInputVisibleDeliveryMode(currentWorkspace),
        });
    });
    controls.append(status, button);
    section.append(icon, copy, controls);
    panel.insertAdjacentElement('afterend', section);
    return section;
}

function syncSystemInputPickerInput(fieldId, value, selectedValue = null) {
    const picker = systemInputPickerRegistry.get(fieldId);
    if (!picker) return;
    picker.input.value = value == null ? '' : String(value);
    syncSystemInputPicker(fieldId, { selectedValue });
}

function addSystemInputDistrictChoice(value) {
    const choice = systemInputDistrictChoice(value) || systemInputChoice(value);
    if (!choice) return false;
    const key = systemInputNormalizeLabel(choice.id ?? choice.name);
    if (!key || systemInputDistrictSelections.some(item => systemInputNormalizeLabel(item.id ?? item.name) === key)) return false;
    systemInputDistrictSelections.push(choice);
    renderSystemInputDistrictChips();
    return true;
}

function bindSystemInputPickers() {
    bindSystemInputPicker('system-input-province', {
        onChoose: () => handleSystemInputProvinceChange(),
        emptyLabel: '没有匹配的省份',
    });
    bindSystemInputPicker('system-input-city', {
        onChoose: () => handleSystemInputCityChange(),
        emptyLabel: '没有匹配的城市',
    });
    bindSystemInputPicker('system-input-districts', {
        onChoose: option => {
            addSystemInputDistrictChoice(option.raw || { id: option.value, name: option.label });
            const input = $('system-input-districts');
            if (input) input.value = '';
            syncSystemInputPickerInput('system-input-districts', '', '');
        },
        emptyLabel: '没有匹配的区县，请调整搜索词',
    });
    bindSystemInputPicker('system-input-stage', {
        onChoose: () => handleSystemInputStageChange(),
        emptyLabel: '没有匹配的学段',
    });
    bindSystemInputPicker('system-input-grade', {
        onChoose: () => handleSystemInputGradeChange(),
        emptyLabel: '没有匹配的年级',
    });
    bindSystemInputPicker('system-input-app-template', { emptyLabel: '还没有保存的应用配置模板' });
    bindSystemInputPicker('system-input-paper-type-search', {
        onChoose: option => setSystemInputField('system-input-paper-type', option.label),
        emptyLabel: '没有匹配的考试类型',
    });
    bindSystemInputPicker('system-input-platform-template-search', {
        onChoose: option => {
            setSystemInputField('system-input-platform-template', option.value);
            handleSystemInputPlatformTemplateChange(option);
            $('system-input-platform-template')?.dispatchEvent(new Event('change', { bubbles: true }));
        },
        selectionOnly: true,
        emptyLabel: '暂无平台题型模板，请先加载模板列表',
    });
    [
        'system-input-textbook-version',
        'system-input-textbook-stage',
        'system-input-textbook-grade',
        'system-input-textbook-volume',
        'system-input-textbook-unit',
        'system-input-textbook-lesson',
    ].forEach(fieldId => {
        bindSystemInputPicker(fieldId, {
            onChoose: () => handleSystemInputTextbookFieldChange(fieldId),
            onCommit: () => handleSystemInputTextbookFieldChange(fieldId),
            allowCustomValue: true,
            emptyLabel: '目录中没有匹配项，可先同步教材列表',
        });
    });
    renderSystemInputTextbookOptions();
}

function bindSystemInputDrawerDismissControls(drawer, { bindClose = true } = {}) {
    if (!drawer) return;
    if (bindClose) {
        const close = drawer.querySelector('#system-input-drawer-close');
        if (close && close.dataset.systemInputDismissBound !== 'true') {
            close.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                closeSystemInputConfigDrawer();
            });
            close.dataset.systemInputDismissBound = 'true';
        }
    }
    const cancel = drawer.querySelector('#system-input-drawer-cancel');
    if (cancel && cancel.dataset.systemInputDismissBound !== 'true') {
        cancel.addEventListener('click', event => {
            event.preventDefault();
            closeSystemInputConfigDrawer();
        });
        cancel.dataset.systemInputDismissBound = 'true';
    }
    if (drawer.dataset.systemInputDismissControlsBound === 'true') return;
    drawer.addEventListener('click', event => {
        if (event.target === event.currentTarget) closeSystemInputConfigDrawer();
    });
    drawer.addEventListener('keydown', event => {
        if (event.key === 'Escape' && systemInputPickerOpen) {
            closeSystemInputPicker({ restoreFocus: true });
            event.preventDefault();
            return;
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            closeSystemInputConfigDrawer();
            return;
        }
        if (event.currentTarget.classList.contains('is-workspace')) return;
        if (event.key !== 'Tab') return;
        const focusable = [...event.currentTarget.querySelectorAll(
            'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        )].filter(element => !element.hidden && element.getClientRects().length > 0);
        if (!focusable.length) {
            event.preventDefault();
            return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!event.currentTarget.contains(document.activeElement)) {
            event.preventDefault();
            first.focus();
        } else if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    drawer.dataset.systemInputDismissControlsBound = 'true';
}

function ensureSystemInputDeliveryPanel() {
    const existingCard = $('system-input-card');
    if (existingCard) {
        // The delivery card can survive a route refresh independently of the
        // detached modal. Re-assert the modal mount before returning the card
        // so the next open can never produce only a backdrop.
        if (typeof mountSystemInputTargetEditor === 'function') mountSystemInputTargetEditor();
        bindSystemInputDrawerDismissControls($('system-input-drawer'));
        ensureSystemInputRouteOptions(existingCard);
        if (!existingCard.dataset.deliveryChoiceBound && typeof handleSystemInputDeliveryModeChange === 'function') {
            existingCard.addEventListener('change', handleSystemInputDeliveryModeChange);
            existingCard.dataset.deliveryChoiceBound = 'true';
        }
        // Keep the optional route decision close to the verified audio
        // delivery, even when the renderer reuses an already mounted panel.
        const existingFrame = document.querySelector('.delivery-frame');
        const existingAudioList = existingFrame?.querySelector('.audio-list-section');
        if (existingFrame && existingAudioList?.parentElement === existingFrame && existingCard.parentElement === existingFrame) {
            existingFrame.insertBefore(existingCard, existingAudioList);
        }
        return existingCard;
    }
    const template = $('system-input-delivery-template');
    const frame = document.querySelector('.delivery-frame');
    if (!template || !frame) return null;
    const fragment = template.content.cloneNode(true);
    const cardNode = fragment.querySelector('#system-input-card');
    const drawerNode = fragment.querySelector('#system-input-drawer');
    if (!cardNode || !drawerNode) return null;
    const audioList = frame.querySelector('.audio-list-section');
    const zipCard = $('zip-card');
    if (audioList?.parentElement === frame) {
        // The route choice is part of the delivery decision, not an end cap
        // after a potentially very long audio list.
        frame.insertBefore(cardNode, audioList);
    } else if (zipCard?.parentElement === frame) {
        zipCard.after(cardNode);
    } else {
        frame.appendChild(cardNode);
    }
    // The drawer must not live under page-4: opening it from the voice
    // configuration page would otherwise leave it inside a hidden ancestor.
    // Mount it at the document root so the fixed overlay is reachable from
    // both configuration and delivery workspaces.
    document.body.appendChild(drawerNode);
    const card = $('system-input-card');
    const drawer = $('system-input-drawer');
    if (!card || !drawer) return null;
    // The platform template is downstream of the scope selectors. Move the
    // authored field into that disclosure after grade so keyboard and visual
    // order both follow province -> city -> stage -> grade -> template.
    const platformTemplateField = $('system-input-platform-template-field');
    const gradeField = $('system-input-grade')?.closest('.system-input-form-field');
    if (platformTemplateField && gradeField) gradeField.after(platformTemplateField);
    mountSystemInputTargetEditor();
    bindSystemInputDrawerDismissControls(drawer, { bindClose: false });
    ensureSystemInputRouteOptions(card);
    if (!card.dataset.deliveryChoiceBound && typeof handleSystemInputDeliveryModeChange === 'function') {
        card.addEventListener('change', handleSystemInputDeliveryModeChange);
        card.dataset.deliveryChoiceBound = 'true';
    }
    bindSystemInputPickers();
    bindSystemInputChoiceGroups(drawer);
    bindSystemInputDisclosures(drawer);
    $('system-input-textbook-sync-btn')?.addEventListener('click', () => {
        void startSystemInputTextbookCatalogSync();
    });
    $('system-input-platform-template-sync-btn')?.addEventListener('click', () => {
        void startSystemInputPlatformTemplateCatalogSync();
    });
    renderSystemInputTextbookCatalogStatus();
    renderSystemInputPlatformTemplateCatalogStatus();
    systemInputPanelReady = true;
    $('system-input-config-btn')?.addEventListener('click', () => {
        // Configuration is a delivery-center action. Keep the user on the
        // audio delivery page while the shared editor opens; the progress
        // page is reserved for an accepted or running system-input task.
        const workspace = systemInputInteractionWorkspace();
        openSystemInputConfigDrawer(workspace, { deliveryMode: 'audio_and_input' });
    });
    $('system-input-start-btn')?.addEventListener('click', () => {
        const workspace = systemInputInteractionWorkspace();
        const systemInput = workspace?.system_input;
        const inputStatus = String(systemInput?.input_status || '').trim().toLowerCase();
        if (systemInputRunIsComplete(systemInput) || inputStatus === 'succeeded') {
            showFinalDeliveryPage({ workspace, refresh: false });
            return;
        }
        if (['PENDING', 'RUNNING'].includes(String(systemInput?.input_run?.status || '').toUpperCase())) {
            showSystemInputProgressPage({ workspace, refresh: false });
            return;
        }
        if (typeof systemInputNeedsReconciliation === 'function'
            && systemInputNeedsReconciliation(systemInput, inputStatus)) {
            showSystemInputProgressPage({ workspace, refresh: false });
            return;
        }
        const inputRunMissing = !systemInput.input_run;
        const acceptancePending = systemInput.audio_acceptance?.status !== 'accepted';
        if (inputRunMissing && systemInput.delivery_mode === 'audio_and_input' && acceptancePending) {
            // The delivery card is the single entry point for the common
            // route: accept the verified batch, refresh the authoritative
            // projection, then start input when every server-side gate is
            // ready. The input status may already be `pending_execute` after
            // the target was configured, so this gate must not rely on the
            // earlier `not_enabled` status.
            void runSystemInputDeliveryAction('ACCEPT_AUDIO_AND_START');
            return;
        }
        if (inputStatus === 'not_enabled' && inputRunMissing) {
            openSystemInputConfigDrawer(workspace, { deliveryMode: 'audio_and_input' });
            return;
        }
        const startAction = workspaceAction('START_INPUT', workspace);
        if (startAction?.enabled !== true) {
            showToast(startAction?.reason || '当前不能开始系统录入', 'warning');
            return;
        }
        void runSystemInputDeliveryAction('START_INPUT');
    });
    $('system-input-drawer-close')?.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        closeSystemInputConfigDrawer();
    });
    const drawerClose = $('system-input-drawer-close');
    if (drawerClose) drawerClose.dataset.systemInputDismissBound = 'true';
    $('system-input-boundary-split-btn')?.addEventListener('click', () => {
        void confirmSystemInputBoundaryChoice('multiple');
    });
    $('system-input-boundary-merge-btn')?.addEventListener('click', () => {
        void confirmSystemInputBoundaryChoice('single');
    });
    $('system-input-form')?.addEventListener('submit', event => { void submitSystemInputConfiguration(event); });
    $('system-input-form')?.addEventListener('input', event => {
        const fieldId = event.target?.id;
        if (fieldId) clearSystemInputValidationError(fieldId);
        syncSystemInputUnitDraftFromForm(event);
    });
    $('system-input-form')?.addEventListener('change', event => {
        const fieldId = event.target?.id;
        if (fieldId) clearSystemInputValidationError(fieldId);
        syncSystemInputUnitDraftFromForm(event);
    });
    $('system-input-unit-select')?.addEventListener('change', handleSystemInputUnitChange);
    $('system-input-apply-all-units-btn')?.addEventListener('click', () => { void applySystemInputCurrentUnitToAll(); });
    $('system-input-type')?.addEventListener('change', () => {
        systemInputPlatformTemplatePendingSelection = null;
        // App templates are type-scoped. Clear the visible selection before
        // loading the new type's list so a stale paper/textbook name cannot
        // look applicable while the request is in flight.
        setSystemInputAppTemplateSelection(null);
        updateSystemInputFormVisibility();
        // The target list is the only user-facing editor. Rebuild it with the
        // new type immediately so its columns, status checks, and shared
        // tools never remain on the previous type while the canonical form
        // is being updated.
        refreshSystemInputTargetEditor?.();
        void loadSystemInputTextbookCatalog();
        void loadSystemInputTemplates($('system-input-type')?.value || 'paper', { force: true });
        void loadSystemInputPlatformTemplates($('system-input-type')?.value || 'paper', { force: true });
    });
    $('system-input-paper-category')?.addEventListener('change', handleSystemInputPaperCategoryChange);
    $('system-input-apply-app-template-btn')?.addEventListener('click', () => { void applySystemInputAppTemplate(); });
    $('system-input-save-template-btn')?.addEventListener('click', () => { void saveSystemInputAppTemplate(); });
    $('system-input-platform-template')?.addEventListener('change', () => handleSystemInputPlatformTemplateChange());
    $('system-input-districts')?.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ',' && event.key !== '，') return;
        event.preventDefault();
        addSystemInputDistrictFromInput();
    });
    $('system-input-districts')?.addEventListener('blur', addSystemInputDistrictFromInput);
    void loadSystemInputRegions();
    void loadSystemInputTextbookCatalog();
    void loadSystemInputTemplates();
    void loadSystemInputPlatformTemplates();
    return card;
}


registerRendererModule("systemInput.surface", {
    ensureSystemInputDeliveryChoicePanel,
    ensureSystemInputConfigEntry,
    syncSystemInputPickerInput,
    addSystemInputDistrictChoice,
    bindSystemInputPickers,
    ensureSystemInputDeliveryPanel,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
