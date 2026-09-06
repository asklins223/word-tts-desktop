/** Renderer module: systemInput.picker */
(function attachRendererFeature_systemInput_picker(root) {
    'use strict';

function systemInputPickerUsesTransientSearch(picker) {
    return Boolean(
        picker
        && (!picker.allowCustomValue || picker.selectionOnly || picker.input?.readOnly)
    );
}

function setSystemInputPickerCommittedValue(picker, value = '', label = '') {
    if (!picker) return;
    picker.committedValue = String(value ?? '');
    picker.committedLabel = String(label ?? '').trim();
}

function reconcileSystemInputPickerCommittedValue(picker) {
    if (!picker || (picker.open && picker.searchActive)) return;
    const inputValue = systemInputNormalizeLabel(picker.input.value);
    if (!inputValue) {
        picker.selectedValue = '';
        setSystemInputPickerCommittedValue(picker);
        return;
    }
    const matching = picker.options.find(option => (
        systemInputNormalizeLabel(option.label) === inputValue
        || String(option.value) === String(picker.selectedValue)
    ));
    if (matching) {
        picker.selectedValue = matching.value;
        setSystemInputPickerCommittedValue(picker, matching.value, matching.label);
        return;
    }
    // A saved legacy value may be temporarily absent while a catalogue is
    // loading. Keep its label/identity together instead of mistaking a new
    // search query for a committed value.
    if (picker.committedLabel && systemInputNormalizeLabel(picker.committedLabel) === inputValue) {
        picker.selectedValue = picker.committedValue || '';
        return;
    }
    picker.selectedValue = '';
    if (systemInputPickerUsesTransientSearch(picker)) {
        setSystemInputPickerCommittedValue(picker, '', picker.input.value);
    }
}

function closeSystemInputPicker(options = {}) {
    const picker = systemInputPickerOpen;
    if (!picker) return;
    const shouldRestoreFocus = Boolean(options.restoreFocus && document.activeElement !== picker.input);
    if (systemInputPickerUsesTransientSearch(picker) && picker.searchActive && !picker.selectedValue) {
        // Fixed catalogue fields use the visible input as a search box. A
        // search is not a new configuration value until an option is chosen.
        picker.input.value = picker.committedLabel || '';
        picker.selectedValue = picker.committedValue || '';
    }
    picker.open = false;
    // Search text is only meaningful for the current open interaction. Keep
    // it out of the committed value so the next open always starts from the
    // complete option list.
    picker.query = '';
    picker.searchActive = false;
    picker.root?.classList.remove('is-open');
    picker.menu.hidden = true;
    // Older Chromium/jsdom builds may expose neither the popover selector nor
    // hidePopover. Closing the picker must remain safe in those environments.
    if (typeof picker.menu.hidePopover === 'function') {
        try {
            if (typeof picker.menu.matches !== 'function' || picker.menu.matches(':popover-open')) picker.menu.hidePopover();
        } catch (_) {
            // The native menu is already hidden above; an unsupported selector
            // should not interrupt drawer close or focus restoration.
        }
    }
    picker.input.setAttribute('aria-expanded', 'false');
    picker.input.removeAttribute('aria-activedescendant');
    systemInputPickerOpen = null;
    if (shouldRestoreFocus) {
        // The input's focus handler opens the picker for keyboard users. A
        // programmatic focus restore after choosing/Escape is only focus
        // restoration, not a new open request; suppress that one synchronous
        // focus event or the menu can reopen with stale interaction state.
        picker.suppressFocusOpen = true;
        try {
            picker.input.focus({ preventScroll: true });
        } finally {
            picker.suppressFocusOpen = false;
        }
    }
}

function closeSystemInputPickersExcept(picker) {
    if (systemInputPickerOpen && systemInputPickerOpen !== picker) closeSystemInputPicker();
}

function renderSystemInputPicker(fieldId) {
    const picker = systemInputPickerRegistry.get(fieldId);
    if (!picker) return;
    // Query is transient interaction state. The visible value is the
    // committed choice, so reopening a picker must never reuse that label as a
    // filter and collapse the menu to one option. Only text entered while the
    // menu is open is treated as a search query.
    const query = picker.open && picker.searchActive
        ? systemInputNormalizeLabel(picker.query || '')
        : '';
    const candidates = picker.options.filter(option => {
        if (option.disabled) return true;
        if (!query) return true;
        return [option.label, option.detail].some(value => systemInputNormalizeLabel(value).includes(query));
    });
    const customValue = String(picker.input.value || '').trim();
    const showCustomOption = Boolean(
        picker.allowCustomValue
        && !picker.input.readOnly
        && customValue
        && !picker.options.some(option => (
            systemInputNormalizeLabel(option.label) === systemInputNormalizeLabel(customValue)
        )),
    );
    picker.input.removeAttribute('aria-activedescendant');
    picker.menu.replaceChildren();
    if (showCustomOption) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'system-input-picker-option is-custom';
        button.setAttribute('role', 'option');
        button.dataset.custom = 'true';
        button.id = `${fieldId}-option-custom`;
        button.tabIndex = -1;
        button.setAttribute('aria-selected', 'false');
        const copy = document.createElement('span');
        copy.className = 'system-input-picker-option-copy';
        const label = document.createElement('strong');
        label.textContent = `使用「${customValue}」`;
        const detail = document.createElement('small');
        detail.textContent = '直接用于当前录入目标，不会自动保存为常用模板';
        copy.append(label, detail);
        const mark = document.createElement('span');
        mark.className = 'system-input-picker-option-mark is-action';
        mark.textContent = '直接使用';
        button.append(copy, mark);
        button.addEventListener('pointerdown', event => event.preventDefault());
        button.addEventListener('click', () => commitSystemInputPickerCustomValue(picker));
        picker.menu.appendChild(button);
    }
    if (!candidates.length && !showCustomOption) {
        const empty = document.createElement('div');
        empty.className = 'system-input-picker-empty';
        empty.textContent = picker.emptyLabel || '没有匹配项';
        picker.menu.appendChild(empty);
    }
    candidates.forEach((option, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'system-input-picker-option';
        button.setAttribute('role', 'option');
        button.dataset.value = option.value;
        button.dataset.index = String(index);
        button.id = `${fieldId}-option-${index}`;
        button.tabIndex = -1;
        button.disabled = option.disabled;
        button.setAttribute('aria-selected', option.value === picker.selectedValue ? 'true' : 'false');
        const copy = document.createElement('span');
        copy.className = 'system-input-picker-option-copy';
        const label = document.createElement('strong');
        label.textContent = option.label;
        copy.appendChild(label);
        if (option.detail) {
            const detail = document.createElement('small');
            detail.textContent = option.detail;
            copy.appendChild(detail);
        }
        const mark = document.createElement('span');
        mark.className = 'system-input-picker-option-mark';
        mark.textContent = '✓';
        mark.setAttribute('aria-hidden', 'true');
        button.append(copy, mark);
        button.addEventListener('pointerdown', event => event.preventDefault());
        button.addEventListener('click', () => chooseSystemInputPickerOption(picker, option));
        picker.menu.appendChild(button);
    });
    picker.menu.hidden = !picker.open;
    if (picker.open && typeof picker.menu.showPopover === 'function' && $('system-input-drawer')?.classList.contains('is-workspace')) {
        picker.menu.setAttribute('popover', 'manual');
        const rect = picker.input.getBoundingClientRect();
        const availableBelow = window.innerHeight - rect.bottom - 12;
        const above = availableBelow < 180 && rect.top > availableBelow;
        const available = Math.max(80, Math.min(280, above ? rect.top - 12 : availableBelow));
        Object.assign(picker.menu.style, {
            position: 'fixed', margin: '0', left: `${Math.max(8, rect.left)}px`,
            top: above ? 'auto' : `${rect.bottom + 5}px`, bottom: above ? `${window.innerHeight - rect.top + 5}px` : 'auto',
            width: `${Math.min(rect.width, window.innerWidth - rect.left - 8)}px`, maxHeight: `${available}px`,
        });
        if (!picker.menu.matches(':popover-open')) picker.menu.showPopover();
    }
    picker.root.classList.toggle('is-open', picker.open);
    picker.input.setAttribute('aria-expanded', picker.open ? 'true' : 'false');
}

function openSystemInputPicker(picker, { resetQuery = true } = {}) {
    if (!picker || picker.input.disabled) return;
    closeSystemInputPickersExcept(picker);
    if (resetQuery) {
        picker.query = '';
        picker.searchActive = false;
    }
    picker.open = true;
    systemInputPickerOpen = picker;
    renderSystemInputPicker(picker.fieldId);
}

function chooseSystemInputPickerOption(picker, option) {
    if (!picker || !option || option.disabled) return;
    picker.selectedValue = option.value;
    setSystemInputPickerCommittedValue(picker, option.value, option.label);
    picker.input.value = option.label;
    picker.query = '';
    picker.searchActive = false;
    picker.onChoose?.(option);
    // Choosing an option updates the picker input programmatically, so the
    // form's normal change handler would otherwise never sync the current
    // per-unit draft. Emit the same event as a manual field change so status
    // summaries reflect the selection immediately.
    picker.input.dispatchEvent(new Event('change', { bubbles: true }));
    renderSystemInputDisclosureSummaries(systemInputInteractionWorkspace()?.system_input);
    clearSystemInputValidationError(picker.fieldId);
    closeSystemInputPicker({ restoreFocus: true });
    renderSystemInputPicker(picker.fieldId);
}

function commitSystemInputPickerCustomValue(picker) {
    if (!picker || !picker.allowCustomValue || picker.input.readOnly) return false;
    const value = String(picker.input.value || '').trim();
    if (!value) return false;
    picker.selectedValue = '';
    setSystemInputPickerCommittedValue(picker, '', value);
    picker.input.value = value;
    picker.query = '';
    picker.searchActive = false;
    picker.onCommit?.(value);
    // Custom commits can clear dependent fields (for example, a textbook
    // parent choice clears every child). Emit the same form event as a
    // catalog option so the current unit draft and disclosure summaries are
    // updated before the user switches units or saves immediately.
    picker.input.dispatchEvent(new Event('change', { bubbles: true }));
    clearSystemInputValidationError(picker.fieldId);
    closeSystemInputPicker({ restoreFocus: true });
    renderSystemInputPicker(picker.fieldId);
    return true;
}

function moveSystemInputPickerHighlight(picker, delta) {
    const options = [...picker.menu.querySelectorAll('.system-input-picker-option:not(:disabled)')];
    if (!options.length) return;
    const active = options.findIndex(option => option.classList.contains('is-active'));
    const next = active < 0
        ? (delta > 0 ? 0 : options.length - 1)
        : Math.max(0, Math.min(options.length - 1, active + delta));
    options.forEach((option, index) => option.classList.toggle('is-active', index === next));
    picker.input.setAttribute('aria-activedescendant', options[next].id);
    options[next]?.scrollIntoView({ block: 'nearest' });
}

function chooseHighlightedSystemInputPickerOption(picker) {
    const active = picker.menu.querySelector('.system-input-picker-option.is-active:not(:disabled)')
        || picker.menu.querySelector('.system-input-picker-option:not(:disabled)');
    if (!active) return false;
    if (active.dataset.custom === 'true') return commitSystemInputPickerCustomValue(picker);
    const option = picker.options.find(item => item.value === active.dataset.value);
    if (!option) return false;
    chooseSystemInputPickerOption(picker, option);
    return true;
}

function syncSystemInputPicker(fieldId, { selectedValue = null } = {}) {
    const picker = systemInputPickerRegistry.get(fieldId);
    if (!picker) return;
    if (selectedValue !== null) {
        picker.selectedValue = String(selectedValue || '');
        const matching = picker.options.find(option => String(option.value) === picker.selectedValue);
        setSystemInputPickerCommittedValue(
            picker,
            picker.selectedValue,
            matching?.label || picker.input.value,
        );
    } else {
        reconcileSystemInputPickerCommittedValue(picker);
    }
    if (selectedValue !== null || !picker.open) {
        picker.query = '';
        picker.searchActive = false;
    }
    renderSystemInputPicker(fieldId);
}

function setSystemInputPickerEnabled(fieldId, enabled, {
    enabledPlaceholder = '',
    disabledPlaceholder = '',
} = {}) {
    const picker = systemInputPickerRegistry.get(fieldId);
    if (!picker) return;
    const isEnabled = Boolean(enabled);
    picker.input.disabled = !isEnabled;
    picker.input.setAttribute('aria-disabled', isEnabled ? 'false' : 'true');
    picker.root.classList.toggle('is-disabled', !isEnabled);
    const placeholder = isEnabled ? enabledPlaceholder : disabledPlaceholder;
    if (placeholder) picker.input.placeholder = placeholder;
    if (!isEnabled && picker.open) closeSystemInputPicker();
    renderSystemInputPicker(fieldId);
}

function setSystemInputPickerOptions(fieldId, options) {
    const picker = systemInputPickerRegistry.get(fieldId);
    if (!picker) return;
    picker.options = systemInputPickerNormalizeOptions(options);
    if (!picker.open) picker.query = '';
    reconcileSystemInputPickerCommittedValue(picker);
    renderSystemInputPicker(fieldId);
}

function bindSystemInputPicker(fieldId, {
    onChoose,
    onInput,
    onCommit,
    allowCustomValue = false,
    selectionOnly = false,
    emptyLabel = '没有匹配项',
} = {}) {
    const input = $(fieldId);
    const root = systemInputPickerRoot(fieldId);
    const menu = root?.querySelector('.system-input-picker-menu');
    if (!input || !root || !menu) return null;
    const existing = systemInputPickerRegistry.get(fieldId);
    if (existing) return existing;
    const picker = {
        fieldId,
        input,
        root,
        menu,
        options: [],
        selectedValue: '',
        committedValue: '',
        committedLabel: '',
        query: '',
        searchActive: false,
        open: false,
        suppressFocusOpen: false,
        onChoose,
        onInput,
        onCommit,
        allowCustomValue: Boolean(allowCustomValue),
        selectionOnly: Boolean(selectionOnly),
        emptyLabel,
    };
    systemInputPickerRegistry.set(fieldId, picker);
    input.removeAttribute('list');
    input.autocomplete = 'off';
    input.setAttribute('aria-controls', menu.id);
    input.setAttribute('aria-disabled', input.disabled ? 'true' : 'false');
    if (picker.selectionOnly) {
        // Selection-only fields still use the shared picker and keyboard
        // navigation, but never accept arbitrary text. This keeps a platform
        // catalogue field visually consistent without presenting an editable
        // text box as if it were a free-form setting.
        input.readOnly = true;
        input.setAttribute('aria-readonly', 'true');
        input.setAttribute('aria-autocomplete', 'none');
        input.dataset.systemInputSelectionOnly = 'true';
        root.classList.add('is-selection-only');
    }
    root.classList.toggle('is-disabled', input.disabled);
    input.addEventListener('focus', () => {
        if (picker.suppressFocusOpen) return;
        openSystemInputPicker(picker);
    });
    input.addEventListener('click', () => openSystemInputPicker(picker));
    input.addEventListener('input', () => {
        if (picker.selectionOnly) {
            // Programmatic restores may still dispatch input. Repaint the
            // selected label, but never treat it as a search query or custom
            // value commit.
            picker.query = '';
            picker.searchActive = false;
            renderSystemInputPicker(fieldId);
            return;
        }
        picker.selectedValue = '';
        picker.query = input.value;
        picker.searchActive = true;
        picker.onInput?.(input.value);
        openSystemInputPicker(picker, { resetQuery: false });
        renderSystemInputPicker(fieldId);
    });
    input.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (!picker.open) openSystemInputPicker(picker);
            moveSystemInputPickerHighlight(picker, event.key === 'ArrowDown' ? 1 : -1);
            return;
        }
        if (event.key === 'Enter') {
            if (picker.open && chooseHighlightedSystemInputPickerOption(picker)) event.preventDefault();
            return;
        }
        if (event.key === 'Escape' && picker.open) {
            event.preventDefault();
            event.stopPropagation();
            closeSystemInputPicker({ restoreFocus: true });
            return;
        }
        if (event.key === 'Tab') closeSystemInputPicker();
    });
    if (!systemInputPickerDocumentBound) {
        document.addEventListener('pointerdown', event => {
            if (!systemInputPickerOpen) return;
            if (systemInputPickerOpen.root.contains(event.target)) return;
            closeSystemInputPicker();
        });
        window.addEventListener('resize', () => closeSystemInputPicker());
        document.addEventListener('scroll', event => {
            if (systemInputPickerOpen && !systemInputPickerOpen.menu.contains(event.target)) closeSystemInputPicker();
        }, true);
        systemInputPickerDocumentBound = true;
    }
    renderSystemInputPicker(fieldId);
    return picker;
}

function renderSystemInputChoiceGroup(fieldId) {
    const input = $(fieldId);
    const group = document.querySelector(`[data-system-input-choice-group="${CSS.escape(fieldId)}"]`);
    if (!input || !group) return;
    const value = String(input.value || '');
    const buttons = [...group.querySelectorAll('[data-value]')];
    const selectedButton = buttons.find(button => String(button.dataset.value) === value && !button.disabled) || buttons.find(button => !button.disabled);
    buttons.forEach(button => {
        const selected = String(button.dataset.value || '') === value;
        button.classList.toggle('is-selected', selected);
        button.setAttribute('aria-checked', selected ? 'true' : 'false');
        button.tabIndex = button === selectedButton ? 0 : -1;
    });
}

function setSystemInputChoice(fieldId, value) {
    setSystemInputField(fieldId, value);
    renderSystemInputChoiceGroup(fieldId);
    $(fieldId)?.dispatchEvent(new Event('change', { bubbles: true }));
}

function bindSystemInputChoiceGroups(root) {
    root.querySelectorAll('[data-system-input-choice-group]').forEach(group => {
        if (group.dataset.bound === 'true') return;
        group.dataset.bound = 'true';
        const fieldId = group.dataset.systemInputChoiceGroup;
        group.addEventListener('click', event => {
            const button = event.target.closest('[data-value]');
            if (!button || button.disabled) return;
            setSystemInputChoice(fieldId, button.dataset.value || '');
        });
        group.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
            const buttons = [...group.querySelectorAll('[data-value]:not(:disabled)')];
            if (!buttons.length) return;
            event.preventDefault();
            const index = buttons.indexOf(event.target.closest('[data-value]'));
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
                : (Math.max(0, index) + (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) + buttons.length) % buttons.length;
            setSystemInputChoice(fieldId, buttons[next].dataset.value);
            buttons[next].focus();
        });
        renderSystemInputChoiceGroup(fieldId);
    });
}


registerRendererModule("systemInput.picker", {
    closeSystemInputPicker,
    closeSystemInputPickersExcept,
    renderSystemInputPicker,
    openSystemInputPicker,
    chooseSystemInputPickerOption,
    commitSystemInputPickerCustomValue,
    moveSystemInputPickerHighlight,
    chooseHighlightedSystemInputPickerOption,
    syncSystemInputPicker,
    setSystemInputPickerEnabled,
    setSystemInputPickerOptions,
    bindSystemInputPicker,
    renderSystemInputChoiceGroup,
    setSystemInputChoice,
    bindSystemInputChoiceGroups,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
