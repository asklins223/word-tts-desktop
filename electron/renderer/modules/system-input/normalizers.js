/** Renderer module: systemInput.normalizers */
(function attachRendererFeature_systemInput_normalizers(root) {
    'use strict';

function systemInputDisplayValue(value, fallback = '') {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        for (const key of ['name', 'label', 'text', 'value', 'id']) {
            const candidate = String(value[key] ?? '').trim();
            if (candidate) return candidate;
        }
        return fallback;
    }
    const text = String(value ?? '').trim();
    return text || fallback;
}

function systemInputChoice(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        const name = systemInputDisplayValue(value);
        const id = value.id ?? value.value;
        return name ? { ...(id !== undefined && id !== null ? { id } : {}), name } : null;
    }
    const name = systemInputDisplayValue(value);
    return name ? { name } : null;
}

function systemInputNormalizeLabel(value) {
    return String(value ?? '').replace(/\s+/g, '').toLocaleLowerCase('zh-CN');
}

function systemInputStatusLabel(status) {
    return {
        not_enabled: '未启用',
        pending_config: '配置待完成',
        pending_execute: '待录入',
        running: '录入中',
        succeeded: '已完成',
        failed_retryable: '可重试',
        needs_reconcile: '待核验',
        failed: '失败',
        partial_success: '部分完成',
        ambiguous: '待核验',
        pending: '待开始',
    }[String(status || '').trim().toLowerCase()] || '待配置';
}

function systemInputDeliveryModeLabel(mode) {
    return mode === 'audio_and_input' ? '生成音频并录入系统' : '只生成音频';
}

function systemInputErrorCodeLabel(code) {
    return {
        INPUT_EXECUTOR_FAILED: '页面录入执行失败',
        INPUT_EXECUTOR_INVALID_RESULT: '页面执行器反馈无效',
        INPUT_EXECUTOR_UNAVAILABLE: '页面录入执行器未连接',
        INPUT_RESULT_UNCONFIRMED: '录入结果未确认，需要核验',
        INPUT_RESULT_UNKNOWN: '录入结果未知，需要核验',
        INPUT_PARTIAL_EXTERNAL_RECORD: '平台已创建记录，可接续重试',
        INPUT_EXTERNAL_NOT_SUBMITTED: '平台未创建记录，可安全重试',
        INPUT_PLATFORM_PREFLIGHT_FAILED: '平台预检失败',
        INPUT_PLATFORM_SESSION_INVALID: '平台会话失效',
        INPUT_PLATFORM_TEMPLATE_UNAVAILABLE: '平台录入模板已失效',
        INPUT_EXISTING_PAPER_INCOMPATIBLE: '平台上已有试卷与配置不兼容',
        INPUT_RECONCILIATION_INVALID: '当前录入尝试没有可核验的外部操作',
        INPUT_RECONCILIATION_STATE_CONFLICT: '当前录入尝试不处于待核对状态',
        INPUT_RECONCILIATION_ID_CONFLICT: '核对的外部录入 ID 与服务端回执不一致',
        INPUT_RECONCILIATION_MAPPING_CONFLICT: '录入尝试已绑定到其他外部记录',
        INPUT_RECONCILIATION_REQUIRES_CONFIRMED_OPERATION: '请先确认外部操作，再修复本地录入状态',
        INPUT_VERIFY_UNSUPPORTED: '页面录入适配器不支持只读核验',
        INPUT_VERIFY_FAILED: '只读核验执行失败',
        INPUT_VERIFICATION_STATE_CONFLICT: '当前录入尝试不处于待核验状态',
        TEMPLATE_FORBIDDEN_FIELD: '模板包含不可保存的任务字段',
        AUDIO_GATE_FAILED: '音频技术产物闸门未通过',
        AUDIO_ACCEPTANCE_REQUIRED: '请先完成整批音频验收',
        SYSTEM_INPUT_ALREADY_RUNNING: '当前已有系统录入运行',
    }[String(code || '').trim().toUpperCase()] || '';
}

function systemInputTypeCapability(inputType, workspace = (typeof currentWorkspace !== 'undefined' ? currentWorkspace : null)) {
    const normalized = String(inputType || '').trim().toLocaleLowerCase('zh-CN');
    const rows = workspace?.system_input?.input_type_capabilities;
    const selected = Array.isArray(rows)
        ? rows.find(row => String(row?.input_type || '').trim().toLocaleLowerCase('zh-CN') === normalized)
        : null;
    if (selected && typeof selected === 'object' && !Array.isArray(selected)) {
        return { ...selected };
    }
    // Compatibility fallback for projections created before the capability
    // matrix was added.  Only paper is considered executable by default;
    // unknown future types fail closed in the renderer as well as on the API.
    if (normalized === 'paper') {
        return {
            input_type: 'paper',
            label: '试卷',
            external_supported: true,
            status: 'supported',
        };
    }
    return {
        input_type: normalized,
        label: normalized === 'textbook' ? '课文' : normalized === 'vocabulary' ? '词汇' : '未知类型',
        external_supported: false,
        status: 'reserved',
        reason: '当前录入类型的页面适配器尚未接入',
    };
}

function systemInputSavedDeliveryMode(workspace = currentWorkspace) {
    return workspace?.system_input?.delivery_mode === 'audio_and_input'
        ? 'audio_and_input'
        : 'audio_only';
}

function systemInputConfigurationEditable(systemInput) {
    if (!systemInput || systemInput.available === false) return false;
    if (typeof systemInput.configuration_editable === 'boolean') {
        return systemInput.configuration_editable;
    }
    // Older projections did not expose this fact. Be conservative whenever a
    // run exists, because its external side-effect state is then unknown.
    return !systemInput.input_run;
}

function systemInputSuggestedConfiguration(systemInput = currentWorkspace?.system_input) {
    const suggested = systemInput?.suggested_configuration;
    return suggested && typeof suggested === 'object' && !Array.isArray(suggested)
        ? suggested
        : null;
}

function systemInputTypeSelectionLocked(systemInput = currentWorkspace?.system_input) {
    const suggested = systemInputSuggestedConfiguration(systemInput);
    if (!suggested) return false;
    const status = String(suggested.input_type_status || '').trim();
    const inputType = String(suggested.input_type || '').trim();
    // 词汇尚未接入外部录入，保留手动入口以便展示说明。
    if (!inputType || inputType === 'vocabulary') return false;
    // 仅在识别结果干净（建议/用户已保存）时锁定；冲突与未知必须
    // 交给用户裁决，不能把对不上的结果悄悄当成已确认。
    return status === 'suggested' || status === 'user_override';
}

function systemInputPaperCategoryLocked(systemInput = currentWorkspace?.system_input) {
    const suggested = systemInputSuggestedConfiguration(systemInput);
    if (!suggested) return false;
    const status = String(suggested.paper_category_status || '').trim();
    const category = String(suggested.paper_category || '').trim();
    if (!category) return false;
    return status === 'suggested' || status === 'confirmed' || status === 'user_override';
}

function historyResultWorkflowId(context = activeResultContext) {
    if (context?.mode !== 'history') return '';
    return String(context.workflowId || context.recordId || context.workspace?.snapshot?.workflow_id || '').trim();
}

function isSystemInputSubpageView(view = currentView) {
    return SYSTEM_INPUT_SUBPAGE_VIEWS.has(String(view || ''));
}

function isHistoryResultView(context = activeResultContext) {
    return (currentView === 'history-result' || isSystemInputSubpageView())
        && Boolean(historyResultWorkflowId(context));
}

function systemInputInteractionWorkspace(fallback = currentWorkspace) {
    return isHistoryResultView() ? (activeResultContext?.workspace || fallback) : fallback;
}

function systemInputVisibleDeliveryMode(workspace = currentWorkspace) {
    return systemInputDeliveryModeDraft || systemInputSavedDeliveryMode(workspace);
}

function systemInputPickerRoot(fieldId) {
    return document.querySelector(`[data-system-input-picker-for="${CSS.escape(fieldId)}"]`);
}

function systemInputPickerOption(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        const raw = value.raw && typeof value.raw === 'object' ? value.raw : value;
        const label = String(value.label ?? value.name ?? systemInputDisplayValue(value)).trim();
        const detail = String(value.detail ?? (Array.isArray(value.path) ? value.path.join(' / ') : '')).trim();
        const optionValue = value.value ?? value.id ?? value.key ?? value.name ?? label;
        if (!label) return null;
        return {
            value: String(optionValue ?? ''),
            label,
            detail: detail && detail !== label ? detail : '',
            raw,
            disabled: Boolean(value.disabled),
        };
    }
    const label = String(value ?? '').trim();
    return label ? { value: label, label, detail: '', raw: value, disabled: false } : null;
}

function systemInputPickerNormalizeOptions(options) {
    const seen = new Set();
    return [...(options || [])].map(systemInputPickerOption).filter(option => {
        if (!option || seen.has(option.value)) return false;
        seen.add(option.value);
        return true;
    });
}


registerRendererModule("systemInput.normalizers", {
    systemInputDisplayValue,
    systemInputChoice,
    systemInputNormalizeLabel,
    systemInputStatusLabel,
    systemInputErrorCodeLabel,
    systemInputDeliveryModeLabel,
    systemInputTypeCapability,
    systemInputSavedDeliveryMode,
    systemInputSuggestedConfiguration,
    systemInputTypeSelectionLocked,
    systemInputPaperCategoryLocked,
    systemInputConfigurationEditable,
    historyResultWorkflowId,
    isSystemInputSubpageView,
    isHistoryResultView,
    systemInputInteractionWorkspace,
    systemInputVisibleDeliveryMode,
    systemInputPickerRoot,
    systemInputPickerOption,
    systemInputPickerNormalizeOptions,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
