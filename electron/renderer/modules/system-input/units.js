/** Renderer module: systemInput.units */
(function attachRendererFeature_systemInput_units(root) {
    'use strict';

const systemInputAnswerTimeDefaultStates = new Map();

function systemInputAnswerTimeValue(configuration) {
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return undefined;
    const camelCaseValue = configuration.answerTimeMinutes;
    if (camelCaseValue !== undefined && camelCaseValue !== null && String(camelCaseValue).trim() !== '') {
        return camelCaseValue;
    }
    const snakeCaseValue = configuration.answer_time_minutes;
    return snakeCaseValue !== undefined && snakeCaseValue !== null && String(snakeCaseValue).trim() !== ''
        ? snakeCaseValue
        : undefined;
}

function systemInputDefaultAnswerTimeForCategory(category) {
    return String(category || '').trim() === '听说考试'
        ? SYSTEM_INPUT_PAPER_DEFAULT_ANSWER_TIME
        : SYSTEM_INPUT_DEFAULT_ANSWER_TIME;
}

function systemInputAnswerTimeDefaultStateKey(unitId = '') {
    return String(unitId || '');
}

function setSystemInputAnswerTimeField(value, {
    category = '题型专项',
    autoDefault = false,
    unitId = systemInputSelectedUnitId,
} = {}) {
    setSystemInputField('system-input-answer-time', value);
    const field = $('system-input-answer-time');
    const key = systemInputAnswerTimeDefaultStateKey(unitId);
    if (autoDefault) {
        systemInputAnswerTimeDefaultStates.set(key, {
            category: String(category || '').trim(),
            value: String(value ?? ''),
        });
        if (field) field.dataset.systemInputAnswerTimeAutoDefault = 'true';
        return;
    }
    systemInputAnswerTimeDefaultStates.delete(key);
    if (field) delete field.dataset.systemInputAnswerTimeAutoDefault;
}

function systemInputAnswerTimeIsAutoDefault(value, unitId = systemInputSelectedUnitId) {
    const state = systemInputAnswerTimeDefaultStates.get(systemInputAnswerTimeDefaultStateKey(unitId));
    return Boolean(state && String(state.value) === String(value ?? ''));
}

function systemInputMarkAnswerTimeAsManual() {
    const field = $('system-input-answer-time');
    if (!field) return;
    systemInputAnswerTimeDefaultStates.delete(
        systemInputAnswerTimeDefaultStateKey(systemInputSelectedUnitId),
    );
    delete field.dataset.systemInputAnswerTimeAutoDefault;
}

function systemInputSeedAnswerTimeDefaultStates(systemInput, units) {
    systemInputAnswerTimeDefaultStates.clear();
    (Array.isArray(units) ? units : []).forEach((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        const configuration = systemInputUnitConfiguration(unit, systemInput);
        if (systemInputAnswerTimeValue(configuration) !== undefined) return;
        const category = configuration.paperCategory
            || configuration.paper_category
            || systemInput?.paper_category
            || '题型专项';
        systemInputAnswerTimeDefaultStates.set(
            systemInputAnswerTimeDefaultStateKey(unitId),
            {
                category: String(category).trim(),
                value: String(systemInputDefaultAnswerTimeForCategory(category)),
            },
        );
    });
}

function systemInputConfigForForm(systemInput) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const selectedId = String(systemInputSelectedUnitId || units[0]?.unit_id || '');
    const selected = units.find(unit => String(unit?.unit_id || '') === selectedId) || units[0] || null;
    const draft = systemInputUnitDrafts.get(selectedId);
    if (draft && typeof draft === 'object') return draft;
    return selected?.configuration && typeof selected.configuration === 'object'
        ? selected.configuration
        : (systemInput || {});
}

function systemInputCopy(value) {
    try {
        return JSON.parse(JSON.stringify(value ?? {}));
    } catch (_) {
        return value && typeof value === 'object' ? { ...value } : {};
    }
}

function systemInputUnitConfiguration(unit, fallback = {}) {
    const allowedKeys = new Set([
        'unit_id', 'input_type', 'delivery_mode', 'app_template_id', 'unit_count_override',
        'paper_category', 'paperCategory', 'paper_name', 'paperName', 'paper_type', 'paperType',
        'province_id', 'provinceId', 'city_id', 'cityId', 'district_id', 'districtIds',
        'stage_id', 'stageId', 'grade_id', 'gradeId', 'year', 'answer_time_minutes',
        'answerTimeMinutes', 'platform_template_id', 'platformTemplateId',
        'platform_template_version', 'platformTemplateVersion', 'platform_template_name',
        'platformTemplateName',
        // 课文（textbook）页面字段：与平台“新增课文”表单一一对应。
        'textbook_name_zh', 'textbookNameZh', 'textbook_name_en', 'textbookNameEn',
        'textbook_form', 'textbookForm', 'textbook_version', 'textbookVersion',
        'textbook_stage', 'textbookStage', 'textbook_grade', 'textbookGrade',
        'textbook_volume', 'textbookVolume', 'textbook_unit', 'textbookUnit',
        'textbook_lesson', 'textbookLesson',
    ]);
    const fallbackKeys = new Set(allowedKeys);
    const fallbackUnits = Array.isArray(fallback?.units) ? fallback.units : [];
    // A legacy workflow may still have one global paper name. It is valid as
    // the explicit name for a single target, but it must not become the name
    // of every newly separated unit. Multi-unit names are unit-local facts.
    if (fallbackUnits.length > 1) {
        fallbackKeys.delete('paper_name');
        fallbackKeys.delete('paperName');
    }
    const pick = (value, keys = allowedKeys) => Object.fromEntries(
        Object.entries(value && typeof value === 'object' && !Array.isArray(value) ? value : {})
            .filter(([key]) => keys.has(key)),
    );
    const configuration = {
        ...pick(fallback, fallbackKeys),
        ...pick(unit?.configuration),
    };
    if (unit?.unit_id && !configuration.unit_id) configuration.unit_id = unit.unit_id;
    return configuration;
}

function systemInputNormalizeUnitConfiguration(configuration, unit, index, total, units, inputType = 'paper') {
    const next = systemInputUnitConfiguration(null, configuration);
    if (inputType === 'textbook') {
        // 与后端 _canonical_unit 对齐：试卷页面专属键不得残留在课文
        // 单元配置里；空的课文字段同样剔除，避免覆盖建议值。
        ['paperName', 'paper_name', 'paperCategory', 'paper_category', 'paperType', 'paper_type',
            'provinceId', 'province_id', 'cityId', 'city_id', 'districtIds', 'district_ids',
            'districtId', 'district_id', 'stageId', 'stage_id', 'gradeId', 'grade_id',
            'year', 'answerTimeMinutes', 'answer_time_minutes',
            'platformTemplateId', 'platform_template_id', 'platformTemplateVersion',
            'platform_template_version', 'platformTemplateName', 'platform_template_name',
        ].forEach(key => delete next[key]);
        const textbookFields = [
            'textbookNameZh', 'textbookNameEn', 'textbookForm', 'textbookVersion',
            'textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit',
            'textbookLesson',
        ];
        textbookFields.forEach(key => {
            const alias = key.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
            const raw = next[key] ?? next[alias];
            const value = typeof systemInputDisplayValue === 'function'
                ? systemInputDisplayValue(raw).trim()
                : String(raw ?? '').trim();
            delete next[alias];
            if (value) next[key] = value;
            else delete next[key];
        });
        return next;
    }
    if (inputType === 'vocabulary') {
        // 词汇适配器尚未接入，但类型切换仍然会经过同一套保存和草稿
        // 归一化链路。与后端的 future-type 分支保持一致，不能把之前
        // 编辑试卷或课文留下的页面字段带进词汇单元。
        ['paperName', 'paper_name', 'paperCategory', 'paper_category', 'paperType', 'paper_type',
            'provinceId', 'province_id', 'cityId', 'city_id', 'districtIds', 'district_ids',
            'districtId', 'district_id', 'stageId', 'stage_id', 'gradeId', 'grade_id',
            'year', 'answerTimeMinutes', 'answer_time_minutes',
            'platformTemplateId', 'platform_template_id', 'platformTemplateVersion',
            'platform_template_version', 'platformTemplateName', 'platform_template_name',
            'textbookNameZh', 'textbook_name_zh', 'textbookNameEn', 'textbook_name_en',
            'textbookForm', 'textbook_form', 'textbookVersion', 'textbook_version',
            'textbookStage', 'textbook_stage', 'textbookGrade', 'textbook_grade',
            'textbookVolume', 'textbook_volume', 'textbookUnit', 'textbook_unit',
            'textbookLesson', 'textbook_lesson',
        ].forEach(key => delete next[key]);
        return next;
    }
    if (inputType !== 'paper') return next;
    // 课文字段是课文专属 schema；试卷单元不得携带，避免跨类型残留。
    Object.keys(next).forEach(key => {
        if (/^textbook([A-Z]|_)/.test(key)) delete next[key];
    });
    // Keep conditional and reference fields attached to the parent selected in
    // the same configuration. A stale paper type under “题型专项”, or a
    // platform template ID/version left behind after its name was cleared,
    // must never survive normalization into the save payload.
    const paperCategory = typeof systemInputDisplayValue === 'function'
        ? systemInputDisplayValue(next.paperCategory ?? next.paper_category).trim()
        : String(next.paperCategory ?? next.paper_category ?? '').trim();
    if (paperCategory) next.paperCategory = paperCategory;
    else delete next.paperCategory;
    delete next.paper_category;
    if (!systemInputCascadeAvailable('paper', 'paperType', { paperCategory })) {
        delete next.paperType;
        delete next.paper_type;
    }
    const platformTemplateName = typeof systemInputDisplayValue === 'function'
        ? systemInputDisplayValue(next.platformTemplateName ?? next.platform_template_name).trim()
        : String(next.platformTemplateName ?? next.platform_template_name ?? '').trim();
    if (platformTemplateName) next.platformTemplateName = platformTemplateName;
    else delete next.platformTemplateName;
    delete next.platform_template_name;
    if (!systemInputCascadeValuePresent(platformTemplateName)) {
        delete next.platformTemplateId;
        delete next.platform_template_id;
        delete next.platformTemplateVersion;
        delete next.platform_template_version;
    }
    const rawName = typeof systemInputDisplayValue === 'function'
        ? systemInputDisplayValue(next.paperName ?? next.paper_name).trim()
        : String(next.paperName ?? next.paper_name ?? '').trim();
    const paperName = systemInputPaperNameForUnit(rawName, unit, index, total, units);
    if (paperName) next.paperName = paperName;
    else {
        delete next.paperName;
        delete next.paper_name;
    }
    delete next.paper_name;
    return next;
}

function systemInputUnitStatusPresentation(unit, configuration, index, total) {
    const inputType = String(
        $('system-input-type')?.value
        || configuration?.input_type
        || configuration?.inputType
        || unit?.input_type
        || systemInputInteractionWorkspace()?.system_input?.input_type
        || 'paper',
    ).trim();
    const capability = systemInputTypeCapability(
        inputType,
        systemInputInteractionWorkspace(),
    );
    const missing = systemInputUnitMissingFields(configuration, inputType);
    const reserved = capability?.external_supported !== true;
    const statusMissing = reserved ? ['外部录入能力待接入'] : missing;
    const label = systemInputDisplayValue(
        unit?.label,
        `第${index + 1}${inputType === 'paper' ? '套' : '个录入单元'}`,
    );
    return {
        unitId: String(unit?.unit_id || ''),
        label,
        complete: !reserved && missing.length === 0,
        missing: statusMissing,
        status: reserved
            ? '待接入外部能力'
            : missing.length ? `待补齐 ${missing.length} 项` : '已补齐',
    };
}

function renderSystemInputUnitOverview(systemInput) {
    const overview = $('system-input-unit-overview');
    const progress = $('system-input-unit-progress');
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const visible = units.length > 1;
    if (overview) overview.hidden = !visible;
    if (progress) progress.hidden = !visible;
    if ($('system-input-drawer')?.classList.contains('is-workspace')) {
        refreshSystemInputTargetEditor();
        return;
    }
    if (!visible) {
        overview?.replaceChildren();
        return;
    }

    const presentations = units.map((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        const configuration = systemInputUnitDrafts.get(unitId)
            || systemInputUnitConfiguration(unit, systemInput);
        return systemInputUnitStatusPresentation(
            unit,
            systemInputNormalizeUnitConfiguration(
                configuration,
                unit,
                index,
                units.length,
                units,
                $('system-input-type')?.value || systemInput?.input_type || 'paper',
            ),
            index,
            units.length,
        );
    });
    const completeCount = presentations.filter(item => item.complete).length;
    if (progress) progress.textContent = `${completeCount} / ${units.length} 已补齐`;
    if (!overview) return;
    const focusedUnit = overview.contains(document.activeElement) ? document.activeElement?.dataset?.unitId : '';
    overview.replaceChildren();
    presentations.forEach(presentation => {
        if (!presentation.unitId) return;
        const row = document.createElement('div');
        row.className = 'system-input-unit-overview-item';
        row.setAttribute('role', 'listitem');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `system-input-unit-overview-button${presentation.complete ? ' is-complete' : ''}`;
        button.dataset.unitId = presentation.unitId;
        const selected = presentation.unitId === String(systemInputSelectedUnitId || '');
        button.setAttribute('aria-current', selected ? 'true' : 'false');
        button.title = selected ? `正在编辑${presentation.label}` : `切换到${presentation.label}`;
        const copy = document.createElement('span');
        copy.className = 'system-input-unit-overview-copy';
        const title = document.createElement('strong');
        title.textContent = presentation.label;
        const detail = document.createElement('small');
        detail.textContent = presentation.complete
            ? '必填已补齐'
            : `还缺：${presentation.missing.slice(0, 3).join('、')}${presentation.missing.length > 3 ? '…' : ''}`;
        copy.append(title, detail);
        const status = document.createElement('span');
        status.className = 'system-input-unit-overview-status';
        status.textContent = presentation.status;
        button.append(copy, status);
        button.addEventListener('click', () => {
            const field = $('system-input-unit-select');
            if (!field || field.value === presentation.unitId) return;
            field.value = presentation.unitId;
            field.dispatchEvent(new Event('change', { bubbles: true }));
        });
        row.appendChild(button);
        overview.appendChild(row);
    });
    if (focusedUnit) overview.querySelector(`[data-unit-id="${CSS.escape(focusedUnit)}"]`)?.focus({ preventScroll: true });
}

function syncSystemInputUnitDraftFromForm(event) {
    const fieldId = event?.target?.id;
    // Unit selection is navigation, not a form edit. Its direct change
    // handler saves the unit being left before rendering the next unit. Do
    // not let the bubbling change event save the newly rendered form again.
    if (!fieldId || fieldId === 'system-input-unit-select'
        || fieldId === 'system-input-app-template') return;
    const picker = typeof systemInputPickerRegistry !== 'undefined'
        ? systemInputPickerRegistry.get(fieldId)
        : null;
    // Shared catalogue pickers use their input as a transient search box.
    // Until a real option is chosen, neither the search text nor the native
    // change event emitted on blur is a valid configuration value. Free-form
    // textbook fields remain writable when the catalogue is unavailable.
    const isFixedPicker = Boolean(
        picker
        && (!picker.allowCustomValue || picker.selectionOnly || picker.input?.readOnly)
    );
    if (isFixedPicker && !picker.selectedValue) return;
    if (fieldId === 'system-input-app-template-scope') {
        updateSystemInputAppTemplateAction(systemInputInteractionWorkspace()?.system_input);
        return;
    }
    if (fieldId === 'system-input-answer-time') systemInputMarkAnswerTimeAsManual();
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    if (Array.isArray(systemInput?.units) && systemInput.units.length) {
        saveSystemInputCurrentUnitDraft(systemInput);
    }
}

function systemInputAppTemplateApplyScopeValue(systemInput = currentWorkspace?.system_input) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    if (units.length <= 1) return 'current';
    const scope = $('system-input-app-template-scope')?.value;
    return ['all', 'selected'].includes(scope) ? scope : '';
}

function systemInputSelectedAppTemplateUnitIds(units) {
    const entries = Array.isArray(units) ? units : [];
    const exposedIds = typeof root.systemInputTargetSelectedUnitIds === 'function'
        ? root.systemInputTargetSelectedUnitIds()
        : [];
    const ids = new Set(
        (Array.isArray(exposedIds) && exposedIds.length ? exposedIds : [...document.querySelectorAll('#target-rows input[type="checkbox"][data-unit-id]:checked')]
            .map(input => String(input.dataset.unitId || '').trim()))
            .filter(Boolean),
    );
    return ids.size ? ids : new Set(entries.filter(unit => unit?.selected === true).map(unit => String(unit.unit_id || '')));
}

function systemInputAppTemplateTargetUnits(units, selectedUnitId, scope = 'current') {
    const entries = Array.isArray(units) ? units.filter(Boolean) : [];
    if (scope === 'all') return entries;
    if (scope === 'selected') {
        const ids = systemInputSelectedAppTemplateUnitIds(entries);
        return entries.filter(unit => ids.has(String(unit?.unit_id || '').trim()));
    }
    // Keep the legacy value available to non-workspace callers that do not
    // render the target checklist. The workspace UI exposes only “勾选条目”
    // and “全部单元”, so this branch is never an implicit UI choice.
    const selected = entries.find(unit => String(unit?.unit_id || '') === String(selectedUnitId || ''))
        || entries[0]
        || null;
    return selected ? [selected] : [];
}

function updateSystemInputAppTemplateScope(systemInput = currentWorkspace?.system_input, { resetScope = false } = {}) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const multi = units.length > 1;
    const field = $('system-input-app-template-scope');
    const scope = $('system-input-app-template-scope-field');
    if (field && (resetScope || !multi)) field.value = multi ? '' : 'current';
    if (scope) scope.hidden = !multi;
    // Older persisted markup may still contain the removed “当前单元” option.
    const currentOption = document.querySelector('[data-system-input-choice-group="system-input-app-template-scope"] [data-value="current"]');
    currentOption?.remove();
    const selectedNote = document.querySelector('[data-system-input-choice-group="system-input-app-template-scope"] [data-value="selected"] small');
    const allNote = $('system-input-app-template-scope-all-note');
    const selectedCount = systemInputSelectedAppTemplateUnitIds(units).size;
    if (selectedNote) selectedNote.textContent = selectedCount ? `仅修改清单中已勾选的 ${selectedCount} 条` : '请先在清单中勾选条目';
    if (allNote) allNote.textContent = multi
        ? `带入全部 ${units.length} 套单元；名称和文档内容保留`
        : '将模板带入当前录入目标';
    if (field) renderSystemInputChoiceGroup(field.id);
    return systemInputAppTemplateApplyScopeValue(systemInput);
}

function updateSystemInputAppTemplateAction(systemInput = currentWorkspace?.system_input, options = {}) {
    const button = $('system-input-apply-app-template-btn');
    const unitCount = Array.isArray(systemInput?.units) ? systemInput.units.length : 0;
    const multi = unitCount > 1;
    const scope = updateSystemInputAppTemplateScope(systemInput, options);
    if (!button) return scope;
    // The target table can be filtered. Read the authoritative checklist
    // projection instead of counting only rows currently mounted in the DOM.
    const selectedCount = systemInputSelectedAppTemplateUnitIds(systemInput?.units || []).size;
    const invalidScope = multi && (!['all', 'selected'].includes(scope)
        || (scope === 'selected' && selectedCount === 0));
    if ($('system-input-drawer')?.classList.contains('is-workspace')) {
        button.textContent = '预览并应用方案';
        button.disabled = Boolean(invalidScope);
        button.title = multi && scope === 'all'
            ? '先预览，再带入全部录入单元'
            : '先预览，再带入清单中已勾选的条目';
        return scope;
    }
    button.disabled = Boolean(invalidScope);
    button.textContent = multi && scope === 'all' ? '应用到全部单元' : multi && scope === 'selected' ? '应用到勾选条目' : '应用模板';
    button.title = multi && scope === 'all'
        ? '将所选模板带入全部录入单元'
        : multi
            ? '将所选模板带入清单中已勾选的录入单元'
            : '将所选模板带入当前录入目标';
    return scope;
}

function renderSystemInputUnitPicker(systemInput) {
    const section = $('system-input-unit-section');
    const field = $('system-input-unit-field');
    const select = $('system-input-unit-select');
    const applyAll = $('system-input-apply-all-units-btn');
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const visible = units.length > 1 && !$('system-input-drawer')?.classList.contains('is-workspace');
    if (section) section.hidden = !visible;
    if (field) field.hidden = !visible;
    if (applyAll) applyAll.hidden = !visible;
    updateSystemInputAppTemplateAction(systemInput);
    const currentId = String(systemInputSelectedUnitId || '');
    const selectedId = units.some(unit => String(unit?.unit_id || '') === currentId)
        ? currentId
        : String(units[0]?.unit_id || '');
    if (selectedId) systemInputSelectedUnitId = selectedId;
    if (select) select.value = selectedId;
    renderSystemInputUnitOverview(systemInput);
}

const SYSTEM_INPUT_TEXTBOOK_FIELDS = [
    ['textbookNameZh', 'system-input-textbook-name-zh'],
    ['textbookNameEn', 'system-input-textbook-name-en'],
    ['textbookForm', 'system-input-textbook-form'],
    ['textbookVersion', 'system-input-textbook-version'],
    ['textbookStage', 'system-input-textbook-stage'],
    ['textbookGrade', 'system-input-textbook-grade'],
    ['textbookVolume', 'system-input-textbook-volume'],
    ['textbookUnit', 'system-input-textbook-unit'],
    ['textbookLesson', 'system-input-textbook-lesson'],
];

function systemInputTextbookSuggestedValues(systemInput) {
    const suggested = systemInput?.suggested_configuration?.textbook;
    return suggested && typeof suggested === 'object' ? suggested : {};
}

function populateSystemInputUnitForm(systemInput) {
    const configuration = systemInputConfigForForm(systemInput);
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const selectedId = String(systemInputSelectedUnitId || units[0]?.unit_id || '');
    const selectedIndex = units.findIndex(unit => String(unit?.unit_id || '') === selectedId);
    const selectedUnit = selectedIndex >= 0 ? units[selectedIndex] : units[0];
    // The type selector is a drawer-level choice.  Keep it when switching
    // between units after a manual override; the server projection can still
    // contain the originally detected type until the user saves.
    const inputType = String(
        $('system-input-type')?.value
        || systemInput?.input_type
        || configuration.input_type
        || 'paper',
    ).trim() || 'paper';
    const valueFor = (...keys) => keys
        .map(key => configuration[key])
        .find(value => value !== undefined && value !== null);
    const appTemplate = systemInputTemplates.find(template => String(template.app_template_id || '') === String(configuration.app_template_id || ''));
    setSystemInputField('system-input-delivery-mode', systemInput?.delivery_mode || configuration.delivery_mode || 'audio_only');
    setSystemInputField('system-input-type', inputType);
    renderSystemInputChoiceGroup('system-input-type');
    setSystemInputAppTemplateSelection(appTemplate || (configuration.app_template_id
        ? { app_template_id: configuration.app_template_id }
        : null));
    const paperName = systemInputPaperNameForUnit(
        valueFor('paperName', 'paper_name') || '',
        selectedUnit,
        selectedIndex >= 0 ? selectedIndex : 0,
        units.length,
        units,
    );
    setSystemInputField('system-input-paper-name', paperName);
    const paperCategory = valueFor('paperCategory', 'paper_category')
        || systemInput?.paper_category
        || '题型专项';
    setSystemInputField('system-input-paper-category', paperCategory);
    renderSystemInputChoiceGroup('system-input-paper-category');
    setSystemInputField('system-input-province', systemInputDisplayValue(valueFor('provinceId', 'province_id')));
    setSystemInputField('system-input-city', systemInputDisplayValue(valueFor('cityId', 'city_id')));
    setSystemInputField('system-input-stage', systemInputDisplayValue(valueFor('stageId', 'stage_id')));
    setSystemInputField('system-input-grade', systemInputDisplayValue(valueFor('gradeId', 'grade_id')));
    const configuredYear = valueFor('year');
    const year = configuredYear === undefined || configuredYear === null || String(configuredYear).trim() === ''
        ? SYSTEM_INPUT_DEFAULT_YEAR
        : configuredYear;
    const configuredAnswerTime = systemInputAnswerTimeValue(configuration);
    const canReuseAutoDefault = configuredAnswerTime !== undefined
        && systemInputAnswerTimeIsAutoDefault(configuredAnswerTime, selectedId);
    const autoDefault = configuredAnswerTime === undefined || canReuseAutoDefault;
    const answerTime = autoDefault
        ? systemInputDefaultAnswerTimeForCategory(paperCategory)
        : configuredAnswerTime;
    setSystemInputField('system-input-year', year);
    setSystemInputAnswerTimeField(answerTime, {
        category: paperCategory,
        autoDefault,
        unitId: selectedId,
    });
    systemInputPlatformTemplatePendingSelection = systemInputPlatformTemplateReference({
        platform_template_id: valueFor('platformTemplateId', 'platform_template_id'),
        name: valueFor('platformTemplateName', 'platform_template_name'),
        platform_template_version: valueFor('platformTemplateVersion', 'platform_template_version'),
    });
    renderSystemInputPlatformTemplateOptions();
    const paperType = systemInputDisplayValue(valueFor('paperType', 'paper_type'));
    setSystemInputField('system-input-paper-type', paperType);
    syncSystemInputPickerInput('system-input-paper-type-search', paperType, paperType);
    systemInputLastProvinceValue = $('system-input-province')?.value || '';
    systemInputLastCityValue = $('system-input-city')?.value || '';
    // 课文字段：已保存值优先；为空时使用文档自动识别的建议值。
    const textbookSuggestions = systemInputTextbookSuggestedValues(systemInput);
    SYSTEM_INPUT_TEXTBOOK_FIELDS.forEach(([key, fieldId]) => {
        const snakeKey = key.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
        const existing = String(valueFor(key, snakeKey) || '').trim();
        const suggested = String(textbookSuggestions[key]?.value || '').trim();
        // Seed suggestions once; a draft deliberately cleared by the user stays empty.
        setSystemInputField(fieldId, systemInputUnitDrafts.has(selectedId) ? existing : (existing || suggested));
    });
    renderSystemInputTextbookOptions();
    const districtValue = valueFor('districtIds', 'district_ids');
    const districts = Array.isArray(districtValue) ? districtValue : [];
    systemInputDistrictSelections = districts.map(value => systemInputDistrictChoice(value) || systemInputChoice(value)).filter(Boolean);
    renderSystemInputDistrictChips();
    renderSystemInputRegionOptions();
    updateSystemInputFormVisibility();
    renderSystemInputUnitOverview(systemInput);
}

function setSystemInputField(id, value) {
    const field = $(id);
    if (field) field.value = value == null ? '' : String(value);
    if (field?.tagName === 'SELECT') window.WordTTSUI?.syncSelect(field);
    syncSystemInputPicker(id);
    renderSystemInputChoiceGroup(id);
}

function systemInputDistrictChoice(value) {
    const choice = systemInputChoice(value);
    if (!choice) return null;
    const province = systemInputFindRegion($('system-input-province')?.value || '', 'provinces');
    if (!province) return null;
    const city = systemInputFindRegion($('system-input-city')?.value || '', 'cities', { parentId: province.id });
    if (!city) return null;
    const match = systemInputFindRegion(choice.name, 'districts', { parentId: city.id });
    return match ? { id: match.id, name: match.name } : null;
}

function systemInputRefreshDistrictSelectionIdentities() {
    if (!Array.isArray(systemInputDistrictSelections) || !systemInputDistrictSelections.length) return false;
    const previous = systemInputDistrictSelections;
    const next = previous
        .map(value => systemInputDistrictChoice(value) || systemInputChoice(value))
        .filter(Boolean);
    const choiceKey = value => `${String(value?.id ?? '')}\u0000${systemInputNormalizeLabel(value?.name ?? value)}`;
    const changed = next.length !== previous.length
        || next.some((value, index) => choiceKey(value) !== choiceKey(previous[index]));
    if (!changed) return false;
    systemInputDistrictSelections = next;
    renderSystemInputDistrictChips();
    return true;
}

function renderSystemInputDistrictChips() {
    const container = $('system-input-district-chips');
    if (!container) return;
    container.replaceChildren();
    systemInputDistrictSelections.forEach((choice, index) => {
        const chip = document.createElement('span');
        chip.className = 'system-input-chip';
        chip.appendChild(document.createTextNode(systemInputDisplayValue(choice)));
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.setAttribute('aria-label', `移除${systemInputDisplayValue(choice)}`);
        remove.textContent = '×';
        remove.addEventListener('click', () => {
            systemInputDistrictSelections.splice(index, 1);
            renderSystemInputDistrictChips();
        });
        chip.appendChild(remove);
        container.appendChild(chip);
    });
    renderSystemInputDisclosureSummaries(systemInputInteractionWorkspace()?.system_input);
}

function addSystemInputDistrictFromInput() {
    const input = $('system-input-districts');
    if (!input) return;
    const values = String(input.value || '').split(/[，,]/).map(value => value.trim()).filter(Boolean);
    if (!values.length) return;
    values.forEach(value => addSystemInputDistrictChoice(value));
    input.value = '';
    syncSystemInputPickerInput('system-input-districts', '', '');
}

function systemInputFindFixedChoice(value, kind) {
    const choices = kind === 'stages'
        ? SYSTEM_INPUT_STAGES
        : kind === 'grades'
            ? SYSTEM_INPUT_GRADES
            : kind === 'paperTypes'
                ? SYSTEM_INPUT_PAPER_TYPES
                : [];
    const wanted = systemInputNormalizeLabel(value);
    if (!wanted) return null;
    return choices.find(choice => [choice.name, choice.id].some(candidate => systemInputNormalizeLabel(candidate) === wanted)) || null;
}

function systemInputChoiceFromText(value, kind) {
    const text = String(value || '').trim();
    if (!text) return null;
    const fixed = systemInputFindFixedChoice(text, kind);
    if (fixed) return { id: fixed.id, name: fixed.name };
    let parentId;
    if (kind === 'cities') {
        const province = systemInputFindRegion($('system-input-province')?.value || '', 'provinces');
        if (!province) return null;
        parentId = province.id;
    } else if (kind === 'districts') {
        const province = systemInputFindRegion($('system-input-province')?.value || '', 'provinces');
        if (!province) return null;
        const city = systemInputFindRegion($('system-input-city')?.value || '', 'cities', { parentId: province.id });
        if (!city) return null;
        parentId = city.id;
    }
    const match = parentId === undefined
        ? systemInputFindRegion(text, kind)
        : systemInputFindRegion(text, kind, { parentId });
    return match ? { id: match.id, name: match.name } : null;
}

function systemInputChoiceFromField(fieldId, kind) {
    const picker = systemInputPickerRegistry.get(fieldId);
    const isTransientSearch = Boolean(
        picker
        && picker.searchActive
        && (!picker.allowCustomValue || picker.selectionOnly || picker.input?.readOnly)
    );
    const value = String(
        isTransientSearch ? picker.committedLabel : $(fieldId)?.value,
    ).trim();
    if (!value) return null;
    const direct = systemInputChoiceFromText(value, kind);
    if (direct) return direct;
    const selectedValue = isTransientSearch ? picker.committedValue : picker?.selectedValue;
    const selected = picker?.options?.find(option => String(option.value) === String(selectedValue)
        && systemInputNormalizeLabel(option.label) === systemInputNormalizeLabel(value));
    return selected ? systemInputChoice(selected.raw || selected) : null;
}

function systemInputDrawerWorkflowKey(workspace = currentWorkspace) {
    return String(workspace?.snapshot?.workflow_id || currentSession?.session_id || '');
}

function updateHistoryResultWorkspace(workspace, context = activeResultContext) {
    const workflowId = historyResultWorkflowId(context);
    const workspaceId = String(workspace?.snapshot?.workflow_id || '').trim();
    if (!workflowId || !workspaceId || workflowId !== workspaceId) return null;
    context.workspace = workspace;
    context.delivery = workspace.delivery || {};
    context.artifacts = Array.isArray(workspace.artifacts) ? workspace.artifacts : context.artifacts;
    context.zipAvailable = context.delivery.zip_available === true;
    context.zipArtifactId = context.delivery.zip_artifact_id || null;
    context.stateVersion = Number(workspace.snapshot?.state_version || context.stateVersion || 0);
    if (isHistoryResultView(context)) renderSystemInputSurface(workspace);
    return workspace;
}

async function refreshHistoryResultWorkspace(context = activeResultContext, { silent = true } = {}) {
    const workflowId = historyResultWorkflowId(context);
    if (!isHistoryResultView(context) || !workflowApi?.getWorkspace || !workflowId) return null;
    try {
        const workspace = await workflowApi.getWorkspace(workflowId);
        if (activeResultContext !== context || !isHistoryResultView(context)) return null;
        return updateHistoryResultWorkspace(workspace, context);
    } catch (error) {
        if (!silent) showToast(workflowAdapter.issueMessage?.(error)?.message || '历史任务工作区暂时无法同步', 'error');
        return null;
    }
}

function systemInputCloneUnitDraftEntries() {
    return [...systemInputUnitDrafts.entries()].map(([unitId, configuration]) => [
        String(unitId),
        systemInputCopy(configuration),
    ]);
}

function captureSystemInputDrawerDraft(workspace = currentWorkspace) {
    const drawer = $('system-input-drawer');
    const systemInput = workspace?.system_input;
    if (!drawer || drawer.hidden || !systemInput) return null;
    saveSystemInputCurrentUnitDraft(systemInput);
    const answerTimeField = $('system-input-answer-time');
    const answerTimeUnitId = systemInputAnswerTimeDefaultStateKey(systemInputSelectedUnitId);
    const answerTimeState = systemInputAnswerTimeDefaultStates.get(answerTimeUnitId);
    systemInputDrawerDraftWorkflowId = systemInputDrawerWorkflowKey(workspace);
    systemInputDrawerDraft = {
        identityKey: systemInputDraftStorageKey(workspace),
        editorState: targetEditorState(),
        selectedUnitId: String(systemInputSelectedUnitId || ''),
        unitDraftEntries: systemInputCloneUnitDraftEntries(),
        deliveryMode: $('system-input-delivery-mode')?.value || '',
        inputType: $('system-input-type')?.value || 'paper',
        appTemplate: $('system-input-app-template')?.value || '',
        appTemplateId: String(systemInputPickerRegistry.get('system-input-app-template')?.selectedValue || '').trim(),
        paperName: $('system-input-paper-name')?.value || '',
        paperCategory: $('system-input-paper-category')?.value || '',
        province: $('system-input-province')?.value || '',
        city: $('system-input-city')?.value || '',
        stage: $('system-input-stage')?.value || '',
        grade: $('system-input-grade')?.value || '',
        year: $('system-input-year')?.value || '',
        answerTime: $('system-input-answer-time')?.value || '',
        answerTimeAutoDefault: Boolean(
            answerTimeState
            && String(answerTimeState.value) === String(answerTimeField?.value || ''),
        ),
        answerTimeDefaultEntries: [...systemInputAnswerTimeDefaultStates.entries()].map(([unitId, state]) => [
            unitId,
            { category: state.category, value: state.value },
        ]),
        paperType: $('system-input-paper-type')?.value || '',
        paperTypeSearch: $('system-input-paper-type-search')?.value || '',
        platformTemplateSearch: $('system-input-platform-template-search')?.value || '',
        platformTemplateSelection: systemInputPlatformTemplateReference(
            systemInputPlatformTemplatePendingSelection || systemInputSelectedPlatformTemplate(),
        ),
        districtSelections: systemInputCopy(systemInputDistrictSelections),
    };
    return systemInputDrawerDraft;
}

function clearSystemInputDrawerDraft() {
    systemInputDrawerDraft = null;
    systemInputDrawerDraftWorkflowId = '';
}

function restoreSystemInputDrawerDraft(systemInput, draft) {
    if (!draft || typeof draft !== 'object') {
        systemInputPopulateForm(systemInput);
        return;
    }
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const inputType = String(
        draft.inputType
        || $('system-input-type')?.value
        || systemInput?.input_type
        || 'paper',
    ).trim() || 'paper';
    setSystemInputField('system-input-type', inputType);
    const unitIds = new Set(
        units.map(unit => String(unit?.unit_id || '')).filter(Boolean),
    );
    systemInputAnswerTimeDefaultStates.clear();
    (Array.isArray(draft.answerTimeDefaultEntries) ? draft.answerTimeDefaultEntries : [])
        .filter(entry => Array.isArray(entry) && entry.length === 2 && entry[0] && entry[1])
        .filter(([unitId]) => unitIds.has(String(unitId)))
        .forEach(([unitId, state]) => {
            if (state.category === undefined || state.value === undefined) return;
            systemInputAnswerTimeDefaultStates.set(
                systemInputAnswerTimeDefaultStateKey(unitId),
                { category: String(state.category || '').trim(), value: String(state.value) },
            );
        });
    const restoredDrafts = new Map(
        (Array.isArray(draft.unitDraftEntries) ? draft.unitDraftEntries : [])
            .filter(entry => Array.isArray(entry) && entry.length === 2 && entry[0])
            .map(([unitId, configuration]) => {
                const normalizedId = String(unitId);
                const index = units.findIndex(unit => String(unit?.unit_id || '') === normalizedId);
                if (index < 0) return null;
                const unit = units[index];
                return [
                    normalizedId,
                    systemInputNormalizeUnitConfiguration(
                        configuration,
                        unit,
                        index,
                        units.length,
                        units,
                        inputType,
                    ),
                ];
            })
            .filter(Boolean),
    );
    // A boundary confirmation may add a unit while the drawer draft is still
    // open. Seed that new unit from the latest projection instead of falling
    // back to the currently visible form when the drawer is saved.
    units.forEach((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        if (!unitId || restoredDrafts.has(unitId)) return;
        restoredDrafts.set(
            unitId,
            systemInputNormalizeUnitConfiguration(
                systemInputUnitConfiguration(unit, systemInput),
                unit,
                index,
                units.length,
                units,
                inputType,
            ),
        );
        const configuration = systemInputUnitConfiguration(unit, systemInput);
        if (systemInputAnswerTimeValue(configuration) === undefined) {
            const category = configuration.paperCategory
                || configuration.paper_category
                || systemInput?.paper_category
                || '题型专项';
            systemInputAnswerTimeDefaultStates.set(
                systemInputAnswerTimeDefaultStateKey(unitId),
                {
                    category: String(category).trim(),
                    value: String(systemInputDefaultAnswerTimeForCategory(category)),
                },
            );
        }
    });
    systemInputUnitDrafts = restoredDrafts;
    const requestedSelectedUnitId = String(draft.selectedUnitId || '');
    systemInputSelectedUnitId = requestedSelectedUnitId;
    renderSystemInputUnitPicker(systemInput);
    const restoredSelectedUnitId = String(systemInputSelectedUnitId || '');
    const canRestoreSelectedUnitDraft = Boolean(
        requestedSelectedUnitId
        && restoredSelectedUnitId === requestedSelectedUnitId
        && unitIds.has(restoredSelectedUnitId),
    );
    populateSystemInputUnitForm(systemInput);
    updateSystemInputAppTemplateAction(systemInput, { resetScope: true });
    setSystemInputField('system-input-delivery-mode', draft.deliveryMode || systemInput?.delivery_mode || 'audio_only');
    setSystemInputField('system-input-type', inputType);
    if (canRestoreSelectedUnitDraft) {
        const restoredAppTemplate = systemInputAppTemplateForValue(draft.appTemplateId || draft.appTemplate);
        setSystemInputAppTemplateSelection(restoredAppTemplate || (draft.appTemplateId
            ? { app_template_id: draft.appTemplateId }
            : null));
        setSystemInputField('system-input-paper-name', draft.paperName || '');
        setSystemInputField('system-input-paper-category', draft.paperCategory || '题型专项');
        setSystemInputField('system-input-province', draft.province || '');
        setSystemInputField('system-input-city', draft.city || '');
        setSystemInputField('system-input-stage', draft.stage || '');
        setSystemInputField('system-input-grade', draft.grade || '');
        setSystemInputField('system-input-year', draft.year === undefined ? SYSTEM_INPUT_DEFAULT_YEAR : draft.year);
        const draftCategory = draft.paperCategory || '题型专项';
        const hasDraftAnswerTime = draft.answerTime !== undefined
            && draft.answerTime !== null
            && String(draft.answerTime).trim() !== '';
        setSystemInputAnswerTimeField(
            hasDraftAnswerTime ? draft.answerTime : systemInputDefaultAnswerTimeForCategory(draftCategory),
            {
                category: draftCategory,
                autoDefault: draft.answerTimeAutoDefault === true || !hasDraftAnswerTime,
                unitId: systemInputSelectedUnitId,
            },
        );
        setSystemInputField('system-input-paper-type', draft.paperType || '');
        syncSystemInputPickerInput('system-input-paper-type-search', draft.paperTypeSearch || '', draft.paperType || '');
        systemInputPlatformTemplatePendingSelection = systemInputPlatformTemplateReference(draft.platformTemplateSelection);
        renderSystemInputPlatformTemplateOptions(systemInputPlatformTemplatePendingSelection);
        const platformSearch = $('system-input-platform-template-search');
        if (platformSearch && draft.platformTemplateSearch !== undefined) {
            platformSearch.value = String(draft.platformTemplateSearch || '');
            syncSystemInputPicker('system-input-platform-template-search', {
                selectedValue: $('system-input-platform-template')?.value || '',
            });
            updateSystemInputPlatformTemplateActions();
        }
    }
    systemInputLastProvinceValue = $('system-input-province')?.value || '';
    systemInputLastCityValue = $('system-input-city')?.value || '';
    if (canRestoreSelectedUnitDraft && Array.isArray(draft.districtSelections)) {
        systemInputDistrictSelections = draft.districtSelections
            .map(value => systemInputDistrictChoice(value) || systemInputChoice(value))
            .filter(Boolean);
    }
    renderSystemInputDistrictChips();
    renderSystemInputRegionOptions();
    updateSystemInputFormVisibility();

    // 平台模板已取消“常用模板”编辑器，草图恢复不再需要重放编辑器的可见状态。
    clearSystemInputValidationErrors();
}

function systemInputPopulateForm(systemInput) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const inputType = String(systemInput?.input_type || units[0]?.input_type || 'paper').trim() || 'paper';
    // A fresh population starts from the server projection.  Subsequent unit
    // switches preserve the current hidden field through
    // populateSystemInputUnitForm instead of re-reading the stale projection.
    setSystemInputField('system-input-type', inputType);
    systemInputSeedAnswerTimeDefaultStates(systemInput, units);
    systemInputUnitDrafts = new Map(
        units.map((unit, index) => {
            const unitId = String(unit?.unit_id || '');
            return [
                unitId,
                systemInputNormalizeUnitConfiguration(
                    systemInputUnitConfiguration(unit, systemInput),
                    unit,
                    index,
                    units.length,
                    units,
                    inputType,
                ),
            ];
        }).filter(([unitId]) => unitId),
    );
    // Suggest the imported document name once so the common single-unit task
    // does not start from an empty required title. Multi-unit tasks stay
    // empty on purpose: same-named papers would collide on the platform.
    if (units.length === 1 && typeof systemInputSuggestedPaperName === 'function') {
        const onlyUnitId = String(units[0]?.unit_id || '');
        const draft = systemInputUnitDrafts.get(onlyUnitId);
        const existingName = String(draft?.paperName ?? draft?.paper_name ?? '').trim();
        if (draft && !existingName) {
            const suggested = systemInputSuggestedPaperName();
            if (suggested) systemInputUnitDrafts.set(onlyUnitId, { ...draft, paperName: suggested });
        }
    }
    // 课文字段的文档识别建议同样落入每个单元草稿：抽屉一次只展开一个
    // 单元的表单，未展开的单元仍要能带着建议值参与保存与校验。
    const textbookSuggestions = systemInputTextbookSuggestedValues(systemInput);
    if (Object.keys(textbookSuggestions).length) {
        systemInputUnitDrafts.forEach((draft, unitId) => {
            let changed = false;
            SYSTEM_INPUT_TEXTBOOK_FIELDS.forEach(([key]) => {
                if (!String(draft[key] ?? '').trim() && String(textbookSuggestions[key]?.value || '').trim()) {
                    draft[key] = String(textbookSuggestions[key].value).trim();
                    changed = true;
                }
            });
            if (changed) systemInputUnitDrafts.set(unitId, { ...draft });
        });
    }
    const currentId = String(systemInputSelectedUnitId || '');
    systemInputSelectedUnitId = units.some(unit => String(unit?.unit_id || '') === currentId)
        ? currentId
        : String(units[0]?.unit_id || '');
    renderSystemInputUnitPicker(systemInput);
    populateSystemInputUnitForm(systemInput);
    updateSystemInputAppTemplateAction(systemInput, { resetScope: true });
}

function collectSystemInputUnitFromForm(systemInput, unit, index, total) {
    const inputType = $('system-input-type')?.value || systemInput?.input_type || 'paper';
    const appTemplate = systemInputSelectedAppTemplate($('system-input-app-template')?.value || '');
    if (inputType === 'textbook') {
        const value = {
            unit_id: unit?.unit_id,
            app_template_id: appTemplate?.app_template_id || undefined,
            textbookNameZh: $('system-input-textbook-name-zh')?.value.trim() || '',
            textbookNameEn: $('system-input-textbook-name-en')?.value.trim() || '',
            textbookForm: $('system-input-textbook-form')?.value.trim() || '',
            textbookVersion: $('system-input-textbook-version')?.value.trim() || '',
            textbookStage: $('system-input-textbook-stage')?.value.trim() || '',
            textbookGrade: $('system-input-textbook-grade')?.value.trim() || '',
            textbookVolume: $('system-input-textbook-volume')?.value.trim() || '',
            textbookUnit: $('system-input-textbook-unit')?.value.trim() || '',
            textbookLesson: $('system-input-textbook-lesson')?.value.trim() || '',
        };
        return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined && item !== null && item !== ''));
    }
    if (inputType !== 'paper') return { unit_id: unit?.unit_id };
    const category = $('system-input-paper-category')?.value || '题型专项';
    const baseName = $('system-input-paper-name')?.value.trim() || '';
    const platformTemplate = systemInputPlatformTemplateReference(systemInputSelectedPlatformTemplate());
    const platformTemplateName = systemInputPlatformTemplateNameForForm();
    const value = {
        unit_id: unit?.unit_id,
        app_template_id: appTemplate?.app_template_id || undefined,
        paperName: systemInputPaperNameForUnit(
            baseName,
            unit,
            index,
            total,
            Array.isArray(systemInput?.units) ? systemInput.units : [],
        ),
        paperCategory: category,
        provinceId: systemInputChoiceFromField('system-input-province', 'provinces'),
        cityId: systemInputChoiceFromField('system-input-city', 'cities'),
        stageId: systemInputChoiceFromField('system-input-stage', 'stages'),
        gradeId: systemInputChoiceFromField('system-input-grade', 'grades'),
        year: systemInputOptionalNumber($('system-input-year')?.value),
        answerTimeMinutes: systemInputOptionalNumber($('system-input-answer-time')?.value),
        platformTemplateId: platformTemplate?.platform_template_id || undefined,
        platformTemplateName: platformTemplateName || undefined,
        platformTemplateVersion: platformTemplate?.platform_template_version || undefined,
        districtIds: systemInputDistrictSelections.map(item => systemInputChoice(item)).filter(Boolean),
    };
    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        value.paperType = systemInputChoiceFromText($('system-input-paper-type')?.value, 'paperTypes');
    }
    return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined && item !== null && item !== ''));
}

function handleSystemInputPaperCategoryChange() {
    const field = $('system-input-answer-time');
    const category = $('system-input-paper-category')?.value || '题型专项';
    // 考试类型只属于“听说考试”。切回“题型专项”时必须同时清掉
    // 隐藏值和共享选择器的可见值，避免再次切回时带出上一条旧选择。
    if (!systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        setSystemInputField('system-input-paper-type', '');
        syncSystemInputPickerInput('system-input-paper-type-search', '', '');
    }
    refreshSystemInputPlatformTemplatesForScope({ clear: true });
    const unitId = systemInputAnswerTimeDefaultStateKey(systemInputSelectedUnitId);
    const currentValue = String(field?.value || '').trim();
    const autoDefault = !currentValue || systemInputAnswerTimeIsAutoDefault(currentValue, unitId);
    if (field) {
        setSystemInputAnswerTimeField(
            autoDefault ? systemInputDefaultAnswerTimeForCategory(category) : currentValue,
            { category, autoDefault, unitId },
        );
    }
    updateSystemInputFormVisibility();
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    if (Array.isArray(systemInput?.units) && systemInput.units.length) {
        saveSystemInputCurrentUnitDraft(systemInput);
    }
}

function resetSystemInputPaperTypeCascade() {
    const category = $('system-input-paper-category')?.value || '题型专项';
    const values = {
        paperCategory: category,
        paperType: $('system-input-paper-type')?.value || '',
    };
    const cleared = systemInputCascadeResetDescendants('paper', values, 'paperCategory', {
        removeEmpty: true,
    });
    if (!cleared.includes('paperType')) return false;
    setSystemInputField('system-input-paper-type', '');
    syncSystemInputPickerInput('system-input-paper-type-search', '', '');
    return true;
}

function saveSystemInputCurrentUnitDraft(systemInput = currentWorkspace?.system_input) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const selectedId = String(systemInputSelectedUnitId || units[0]?.unit_id || '');
    const selectedIndex = units.findIndex(unit => String(unit?.unit_id || '') === selectedId);
    if (selectedIndex < 0 || !selectedId) return null;
    const configuration = collectSystemInputUnitFromForm(
        systemInput,
        units[selectedIndex],
        selectedIndex,
        units.length,
    );
    systemInputUnitDrafts.set(selectedId, configuration);
    targetEditorRecordManual(configuration);
    renderSystemInputUnitOverview(systemInput);
    renderSystemInputDisclosureSummaries(systemInput);
    return configuration;
}

function handleSystemInputUnitChange(event) {
    const select = event.target.closest('#system-input-unit-select');
    if (!select) return;
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    saveSystemInputCurrentUnitDraft(systemInput);
    closeSystemInputPicker();
    clearSystemInputValidationErrors();
    const nextId = String(select.value || '');
    if (!nextId) return;
    const inputType = $('system-input-type')?.value;
    const deliveryMode = $('system-input-delivery-mode')?.value;
    systemInputSelectedUnitId = nextId;
    populateSystemInputUnitForm(systemInput);
    if (inputType) setSystemInputField('system-input-type', inputType);
    if (deliveryMode) setSystemInputField('system-input-delivery-mode', deliveryMode);
    updateSystemInputFormVisibility();
    updateSystemInputAppTemplateAction(systemInput, { resetScope: true });
    // Refresh the card state after a click so the newly selected card is
    // marked as active alongside the newly rendered form.
    renderSystemInputUnitPicker(systemInput);
    renderSystemInputUnitOverview(systemInput);
    resetSystemInputDisclosureState(systemInput);
    renderSystemInputDisclosureSummaries(systemInput);
    refreshSystemInputTargetEditor();
}

async function applySystemInputCurrentUnitToAll() {
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    if (units.length < 2) return false;
    const inputType = String(
        $('system-input-type')?.value || systemInput?.input_type || 'paper',
    ).trim() || 'paper';
    const current = saveSystemInputCurrentUnitDraft(systemInput);
    if (!current) return false;
    const selectedUnit = units.find(unit => String(unit?.unit_id || '') === String(systemInputSelectedUnitId || '')) || units[0];
    const selectedLabel = systemInputDisplayValue(selectedUnit?.label, '当前录入单元');
    const targets = units.filter(unit => String(unit?.unit_id || '') !== String(selectedUnit?.unit_id || ''));
    const targetLabels = targets.map(unit => {
        const index = units.indexOf(unit);
        return systemInputDisplayValue(unit?.label, `第${index >= 0 ? index + 1 : 1}套`);
    });
    const confirmed = await showConfirmDialog({
        kicker: '多套录入单元',
        title: '同步当前配置到全部单元？',
        message: `将把「${selectedLabel}」的当前表单值同步到 ${targets.length} 个其他录入单元。`,
        detail: `受影响：${targetLabels.join('、')}。各套试卷名称保持独立，不会被同步改写。其他单元当前的配置值会被覆盖。`,
        confirmLabel: '同步到全部单元',
    });
    if (!confirmed) return false;
    units.forEach((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        if (!unitId) return;
        const copied = systemInputCopy(current);
        copied.unit_id = unitId;
        if (inputType === 'paper') {
            const existing = systemInputUnitDrafts.get(unitId)
                || systemInputUnitConfiguration(unit, systemInput);
            const existingName = String(existing.paperName || existing.paper_name || '').trim();
            // Synchronization is explicit, but paper names remain unit-local.
            // Never copy the selected unit's name into another card, and do
            // not invent one for a card that is still blank.
            if (existingName) copied.paperName = existingName;
            else {
                delete copied.paperName;
                delete copied.paper_name;
            }
        }
        systemInputUnitDrafts.set(
            unitId,
            systemInputNormalizeUnitConfiguration(
                copied,
                unit,
                index,
                units.length,
                units,
                inputType,
            ),
        );
    });
    renderSystemInputUnitOverview(systemInput);
    renderSystemInputDisclosureSummaries(systemInput);
    showToast(`已将「${selectedLabel}」的配置同步到 ${targets.length} 个其他录入单元，请检查后保存`, 'success');
    return true;
}

function updateSystemInputFormVisibility() {
    const workspace = systemInputInteractionWorkspace();
    const systemInput = workspace?.system_input;
    const inputType = $('system-input-type')?.value || 'paper';
    const category = $('system-input-paper-category')?.value || '题型专项';
    const capability = systemInputTypeCapability(inputType, workspace);
    const isPaper = inputType === 'paper';
    const isTextbook = inputType === 'textbook';
    ['system-input-paper-fields', 'system-input-range-fields']
        .forEach(id => { const section = $(id); if (section) section.hidden = !isPaper; });
    const optionalSection = $('system-input-optional-fields');
    // In the workspace the optional form is detached and controlled as the
    // shared “常用录入方案” child dialog. Its hidden state must not be
    // overwritten by the legacy form visibility pass when the input type
    // changes underneath the unified target list.
    if (optionalSection && !optionalSection.classList.contains('target-modal')) {
        optionalSection.hidden = !['paper', 'textbook', 'vocabulary'].includes(inputType);
    }
    const textbookSection = $('system-input-textbook-fields');
    if (textbookSection) textbookSection.hidden = !isTextbook;
    if (isTextbook) renderSystemInputTextbookOptions();
    // 自动识别呈现：识别结果干净时锁定选择并只展示结论；冲突或未知时
    // 保留手动切换入口，让用户处理对不上的情况。
    const typeLocked = systemInputTypeSelectionLocked(systemInput);
    const typeGroup = document.querySelector('[data-system-input-choice-group="system-input-type"]');
    if (typeGroup) {
        typeGroup.hidden = false;
        typeGroup.classList.toggle('is-readonly', typeLocked);
        typeGroup.querySelectorAll('[data-value]').forEach(button => { button.disabled = typeLocked; });
    }
    const typeDetected = $('system-input-type-detected');
    if (typeDetected) {
        const detectedStatus = String(systemInput?.suggested_configuration?.input_type_status || '').trim();
        if (typeLocked) {
            typeDetected.hidden = false;
            typeDetected.textContent = detectedStatus === 'user_override'
                ? `已确认录入类型：${capability?.label || inputType}（来自已保存的配置）`
                : `已根据文档自动识别录入类型：${capability?.label || inputType}`;
        } else {
            typeDetected.hidden = true;
            typeDetected.textContent = '';
        }
    }
    const categoryLocked = isPaper && systemInputPaperCategoryLocked(systemInput);
    const categoryGroup = document.querySelector('[data-system-input-choice-group="system-input-paper-category"]');
    if (categoryGroup) {
        categoryGroup.hidden = false;
        categoryGroup.classList.toggle('is-readonly', categoryLocked);
        categoryGroup.querySelectorAll('[data-value]').forEach(button => { button.disabled = categoryLocked; });
    }
    const categoryDetected = $('system-input-paper-category-detected');
    if (categoryDetected) {
        if (categoryLocked) {
            categoryDetected.hidden = false;
            categoryDetected.textContent = `已根据文档自动识别试卷分类：${category}`;
        } else {
            categoryDetected.hidden = true;
            categoryDetected.textContent = '';
        }
    }
    const paperTypeSection = $('system-input-paper-type-section');
    const paperTypeVisible = isPaper
        && systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category });
    if (!paperTypeVisible) resetSystemInputPaperTypeCascade();
    if (typeof setSystemInputPickerEnabled === 'function') {
        setSystemInputPickerEnabled('system-input-paper-type-search', paperTypeVisible, {
            enabledPlaceholder: '搜索考试类型',
            disabledPlaceholder: '仅“听说考试”可填写考试类型',
        });
    }
    if (paperTypeSection) {
        paperTypeSection.hidden = !paperTypeVisible;
        if (paperTypeVisible && !String($('system-input-paper-type')?.value || '').trim()) {
            paperTypeSection.open = true;
        }
    }
    const typeHint = $('system-input-type-status');
    if (typeHint) typeHint.textContent = capability?.external_supported === true
        ? (typeLocked ? '已根据文档自动识别；此类型支持系统录入' : '此类型支持系统录入')
        : `${capability?.label || '当前类型'}暂不能执行录入，可以先保存配置。`;
    renderSystemInputDisclosureSummaries(systemInput);
}


registerRendererModule("systemInput.units", {
    systemInputConfigForForm,
    systemInputCopy,
    systemInputUnitConfiguration,
    systemInputNormalizeUnitConfiguration,
    systemInputDefaultAnswerTimeForCategory,
    systemInputUnitStatusPresentation,
    renderSystemInputUnitOverview,
    syncSystemInputUnitDraftFromForm,
    updateSystemInputAppTemplateAction,
    systemInputAppTemplateApplyScopeValue,
    systemInputAppTemplateTargetUnits,
    updateSystemInputAppTemplateScope,
    renderSystemInputUnitPicker,
    populateSystemInputUnitForm,
    setSystemInputField,
    systemInputDistrictChoice,
    systemInputRefreshDistrictSelectionIdentities,
    renderSystemInputDistrictChips,
    addSystemInputDistrictFromInput,
    systemInputFindFixedChoice,
    systemInputChoiceFromText,
    systemInputChoiceFromField,
    systemInputDrawerWorkflowKey,
    updateHistoryResultWorkspace,
    refreshHistoryResultWorkspace,
    systemInputCloneUnitDraftEntries,
    captureSystemInputDrawerDraft,
    clearSystemInputDrawerDraft,
    restoreSystemInputDrawerDraft,
    systemInputPopulateForm,
    collectSystemInputUnitFromForm,
    saveSystemInputCurrentUnitDraft,
    handleSystemInputUnitChange,
    handleSystemInputPaperCategoryChange,
    applySystemInputCurrentUnitToAll,
    updateSystemInputFormVisibility,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
